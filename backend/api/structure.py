"""结构边界检测接口：给成品播放器的播放头提供吸附候选。

拼接时算出的 `segment_start_sec` 是 clip 级毫秒真值，不需要检测；这个接口补的是
「单个 clip 内部的结构」—— 那是段边界信息完全看不到的部分。

两条路互补：`POST /structure/boundaries` 用谱形状检测器（毫秒级定位、无语义），
`GET /analyze_structure` 用深度学习结构模型（带 intro/verse/chorus 标签，但边界不能当裁切真值：
2026-11-09 冷跑对全部 9 条真接缝取最近边界，误差中位数 1.624 s、最差 10.667 s，见 README §3.4）。

后者是分钟级的活儿，所以同一份分析还提供任务式入口（`POST /structure/jobs` +
`GET /structure/jobs/{job_id}`）：提交拿 job_id，轮询到终态，前端不必挂着一条长连接等。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api.stitch import normalize_source
from utils import analyze_jobs, clip_index
from utils.audio_processor import AudioSourceError, read_audio
from utils.structure_analyzer import (
    analyze_music_structure,
    backend_info,
    fallback_segments,
    model_name,
    wav_duration_sec,
)
from utils.structure_detector import DEFAULT_FLOOR, DEFAULT_GAP_SEC, boundary_candidates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["structure"])


class BoundaryRequest(BaseModel):
    audio_url: str = Field(max_length=2048)
    # 固定绝对新颖度下限，量纲见 structure_detector 模块文档；调低 → 候选更密
    floor: float = Field(default=DEFAULT_FLOOR, gt=0.0, lt=1.0)
    gap_ms: int = Field(default=int(DEFAULT_GAP_SEC * 1000), ge=100, le=30_000)


class BoundaryResponse(BaseModel):
    audio_url: str
    duration: float
    sample_rate: int
    floor: float
    gap_ms: int
    candidates_sec: List[float]
    # 与 candidates_sec 一一对应的新颖度，前端用它区分「强边界」和「勉强过线」
    candidate_strengths: List[float]
    compute_ms: int


@router.post("/structure/boundaries", response_model=BoundaryResponse)
def structure_boundaries(payload: BoundaryRequest) -> BoundaryResponse:
    started = time.perf_counter()
    try:
        samples, frame_rate = read_audio(normalize_source(payload.audio_url))
    except AudioSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"音频不存在: {exc}") from exc
    except Exception as exc:  # pragma: no cover - 兜底日志
        logger.exception("structure analysis failed")
        raise HTTPException(status_code=500, detail="结构分析失败，请查看后端日志") from exc

    candidates = boundary_candidates(
        samples, frame_rate, floor=payload.floor, gap_sec=payload.gap_ms / 1000.0
    )
    compute_ms = int((time.perf_counter() - started) * 1000)
    duration = samples.shape[0] / float(frame_rate)
    logger.info(
        "structure_boundaries: %s dur=%.3fs candidates=%d floor=%.3f gap_ms=%d compute_ms=%d",
        payload.audio_url,
        duration,
        len(candidates),
        payload.floor,
        payload.gap_ms,
        compute_ms,
    )

    return BoundaryResponse(
        audio_url=payload.audio_url,
        duration=round(duration, 3),
        sample_rate=frame_rate,
        floor=payload.floor,
        gap_ms=payload.gap_ms,
        candidates_sec=[round(moment, 3) for moment, _ in candidates],
        candidate_strengths=[round(strength, 4) for _, strength in candidates],
        compute_ms=compute_ms,
    )


@router.get("/analyze_structure")
async def analyze_structure(
    clip_id: str = Query(..., min_length=1, max_length=64),
) -> JSONResponse:
    """深度学习音乐结构分段，返回 `[{"label","start","end"}]`（秒，毫秒精度）。

    推理是分钟级 CPU 活儿，放线程池跑，事件循环只负责等；分析服务挂了就退化成
    单段兜底 + HTTP 500，让前端拿到合法区间而不是崩在空数据上。
    """
    started = time.perf_counter()
    try:
        path = clip_index.resolve(clip_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AudioSourceError as exc:
        raise HTTPException(status_code=400, detail=f"资源路径不合法: {exc}") from exc

    duration = 0.0
    try:
        duration = wav_duration_sec(path)
        segments = await run_in_threadpool(analyze_music_structure, str(path))
        degraded = False
        detail: Optional[str] = None
    except Exception as exc:
        logger.exception("analyze_structure 失败: clip=%s file=%s", clip_id, path.name)
        segments = fallback_segments(duration)
        degraded = True
        detail = f"{type(exc).__name__}: {exc}"[:300]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "analyze_structure: clip=%s file=%s dur=%.3fs segments=%d degraded=%s analyze_ms=%d",
        clip_id,
        path.name,
        duration,
        len(segments),
        degraded,
        elapsed_ms,
    )

    body = {
        "clip_id": clip_id,
        "filename": path.name,
        "duration_sec": round(duration, 3),
        "model": model_name(),
        "degraded": degraded,
        "segment_count": len(segments),
        "segments": segments,
        "analyze_ms": elapsed_ms,
        "backend": backend_info(duration),
        **clip_index.origin_of(clip_id),
    }
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=500 if degraded else 200, content=body)


class AnalyzeJobRequest(BaseModel):
    clip_id: str = Field(min_length=1, max_length=64)


def _job_payload(job: "analyze_jobs.AnalyzeJob", reused: bool = False) -> Dict[str, Any]:
    return {**job.public(), "reused": reused, "model": model_name(), **clip_index.origin_of(job.clip_id)}


@router.post("/structure/jobs", status_code=202)
async def create_analyze_job(payload: AnalyzeJobRequest) -> JSONResponse:
    """提交一次深度学习结构分析，立刻返回 job_id（不等推理）。

    同一 clip 重复提交回同一个任务：分轨是 1 倍实时级别的开销，点两下波形不该跑两遍。
    """
    try:
        job, reused = await analyze_jobs.jobs().submit(payload.clip_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AudioSourceError as exc:
        raise HTTPException(status_code=400, detail=f"资源路径不合法: {exc}") from exc

    logger.info(
        "analyze job 提交: clip=%s job=%s status=%s reused=%s dur=%.3fs",
        payload.clip_id,
        job.job_id,
        job.status,
        reused,
        job.duration_sec,
    )
    return JSONResponse(status_code=200 if reused else 202, content=_job_payload(job, reused))


@router.get("/structure/jobs/{job_id}")
async def get_analyze_job(job_id: str) -> Dict[str, Any]:
    """轮询任务状态；终态是 done / failed，failed 也带着兜底段，前端不会拿到空结构。

    任务表在进程内存里，`--reload` 触发的重启会丢掉进行中的任务 → 这里回 404，
    由前端重新提交，而不是让页面卡在「分析中」。
    """
    job = analyze_jobs.jobs().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"没有这个分析任务（后端可能已重启）：{job_id}")
    return _job_payload(job)

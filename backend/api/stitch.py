"""多段音频拼接接口。"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from utils.audio_processor import (
    STATIC_DIR,
    AudioSourceError,
    stitch_sequence,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audio"])

SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class StitchSegment(BaseModel):
    id: str = Field(max_length=64)
    audio_url: str = Field(max_length=2048)
    # 该段与下一段之间的交叉淡化时长；最后一段的该值会被忽略
    crossfade_ms: int = Field(default=200, ge=0, le=5000)
    # 该段在成品里最多保留的秒数。Suno 不吃时长参数，纯器乐段会写满一整首，
    # 由前端把段落的规划时长传进来，后端在静音裁切之后按这段上限裁掉多余尾部
    max_duration_sec: Optional[float] = Field(default=None, gt=0, le=600)
    # 上限没兑现（候选还是整首/整段续写）时允许往后吸到第一个结构边界的秒数；
    # 0 = 只往更短一侧并硬停在 max_duration_sec。见 `_truncate_to_length` 的 beyond 档
    beyond_sec: Optional[float] = Field(default=None, ge=0, le=600)


class StitchRequest(BaseModel):
    segments: List[StitchSegment] = Field(min_length=2, max_length=16)
    output_name: Optional[str] = Field(default=None, max_length=48)


class StitchResponse(BaseModel):
    audio_url: str
    filename: str
    duration: float
    peak_dbfs: float
    sample_rate: int
    crossfade_ms: List[int]
    segment_order: List[str]
    # 每段在成品时间轴上的起点（秒），用于把整曲时间点映射回源片段
    segment_start_sec: List[float]
    # 每段拼接时物理剔除的头部静音（秒）；换算回源片段时间轴需要加回这段偏移
    head_trim_sec: List[float]
    # 每段拼接时物理剔除的尾部静音（秒）；Suno 成品自带 0.06~1.96s 尾部数字静音，
    # 不剔除就会留在接缝前面形成听感真空
    tail_trim_sec: List[float]
    # 每段按 max_duration_sec 裁切后实际保留的秒数；未超限或不传时为 null
    truncate_to_sec: List[Optional[float]]
    # 该段切点是否吸附到了 ±0.5s 内的结构边界候选；false 表示直接停在目标秒数上
    truncate_snapped: List[bool]


def normalize_source(url: str) -> str:
    """同一后端的绝对 /static 地址直接映射为本地路径，避免服务端请求自身。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.path.startswith("/static/"):
        return parsed.path
    return url


@router.post("/stitch", response_model=StitchResponse)
def stitch_timeline(payload: StitchRequest, request: Request) -> StitchResponse:
    name = payload.output_name or "mix"
    if not SAFE_NAME.match(name):
        raise HTTPException(status_code=422, detail="output_name 仅允许字母、数字、下划线和中划线")

    filename = f"{name}_{int(time.time())}_{Path(payload.segments[0].id).name[:8]}.wav"
    sources = [(normalize_source(seg.audio_url), seg.crossfade_ms) for seg in payload.segments]

    try:
        (
            output_path,
            duration,
            peak_dbfs,
            segment_starts,
            head_trim,
            tail_trim,
            truncate_to,
            truncate_snapped,
        ) = stitch_sequence(
            sources,
            output_path=filename,
            max_durations=[seg.max_duration_sec for seg in payload.segments],
            beyond_durations=[seg.beyond_sec or 0.0 for seg in payload.segments],
        )
    except AudioSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"音频不存在: {exc}") from exc
    except Exception as exc:  # pragma: no cover - 兜底日志
        logger.exception("stitch failed")
        raise HTTPException(status_code=500, detail="音频拼接失败，请查看后端日志") from exc

    relative = Path(output_path).resolve().relative_to(STATIC_DIR.resolve())
    base = str(request.base_url).rstrip("/")

    return StitchResponse(
        audio_url=f"{base}/static/{relative.as_posix()}",
        filename=Path(output_path).name,
        duration=round(duration, 3),
        peak_dbfs=round(peak_dbfs, 2),
        sample_rate=44_100,
        crossfade_ms=[seg.crossfade_ms for seg in payload.segments],
        segment_order=[seg.id for seg in payload.segments],
        segment_start_sec=[round(start, 3) for start in segment_starts],
        head_trim_sec=[round(sec, 3) for sec in head_trim],
        tail_trim_sec=[round(sec, 3) for sec in tail_trim],
        truncate_to_sec=[None if sec is None else round(sec, 3) for sec in truncate_to],
        truncate_snapped=truncate_snapped,
    )

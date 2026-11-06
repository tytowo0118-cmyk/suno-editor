"""结构分析服务（analyzer 容器的入口，只在里面跑）。

为什么单独一个进程：一首 492 s 的成品在 8 核 CPU 上要跑 htdemucs 分轨 + 谱特征 + 模型推理，
是分钟级的 CPU 密集任务；跟主后端同进程会把拼接 DSP 和 Suno 轮询一起拖死。
这个服务只读静态目录里的 WAV，写自己的临时工作目录，主后端通过 HTTP 调它并在失败时降级。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from utils import structure_weights
from utils.structure_analyzer import (
    StructureAnalysisError,
    analyze_music_structure,
    backend_info,
    model_name,
    queue_budget_sec,
    wav_duration_sec,
)

logger = logging.getLogger(__name__)

# 分析服务自己必须走进程内推理：留着这个变量会让它 HTTP 调自己，绕成死循环
os.environ.pop("STRUCTURE_ANALYZER_URL", None)

AUDIO_ROOT = Path(os.getenv("ANALYZER_AUDIO_ROOT", "/app/static")).resolve()
# 一次推理能吃满整机核，所以只允许一条流水线；后来的请求排队，等不到就 429 让调用方降级。
# 排队预算用 structure_analyzer.queue_budget_sec —— 和调用方的 HTTP 超时同一个公式，
# 别在这边写一份、那边写一份，两边不一致就会出现「服务端还在合法排队，调用方先掐断」。
_inference_lock = threading.Lock()


app = FastAPI(title="MuseBlocks Structure Analyzer", version="0.1.0")


class AnalyzeRequest(BaseModel):
    # 只认相对 AUDIO_ROOT 的路径：绝对路径属于调用方的挂载点，传过来没有意义
    path: str = Field(max_length=512, description="相对 AUDIO_ROOT 的 WAV 路径，如 output/suno_x.wav")


def _resolve(path_str: str) -> Path:
    candidate = (AUDIO_ROOT / path_str.lstrip("/")).resolve()
    if AUDIO_ROOT not in candidate.parents:
        raise StructureAnalysisError(f"路径越出音频根目录: {path_str}")
    if candidate.suffix.lower() != ".wav":
        raise StructureAnalysisError(f"只接受 WAV: {path_str}")
    if not candidate.is_file():
        raise StructureAnalysisError(f"音频不存在: {path_str}")
    return candidate


def _guarded_analyze(path: Path) -> Dict[str, Any]:
    started = time.perf_counter()
    budget = queue_budget_sec(wav_duration_sec(path))
    if not _inference_lock.acquire(timeout=budget):
        raise TimeoutError(f"分析服务繁忙，等待 {budget:.0f}s 仍无空闲工作线程")
    try:
        segments = analyze_music_structure(str(path))
    finally:
        _inference_lock.release()
    return {
        "path": str(path.relative_to(AUDIO_ROOT)),
        "duration_sec": round(wav_duration_sec(path), 3),
        "model": model_name(),
        "segments": segments,
        "analyze_ms": int((time.perf_counter() - started) * 1000),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    payload: Dict[str, Any] = {"status": "ok", "audio_root": str(AUDIO_ROOT), **backend_info()}
    try:
        manifest = structure_weights.ensure_weights(model_name(), check_only=True)
        payload["weights"] = {
            "files": len(manifest["files"]),
            "total_mb": round(manifest["total_bytes"] / 1048576, 1),
        }
    except Exception as exc:
        payload["status"] = "degraded"
        payload["weights_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return payload


@app.post("/analyze")
async def analyze(payload: AnalyzeRequest) -> JSONResponse:
    try:
        path = _resolve(payload.path)
        result = await run_in_threadpool(_guarded_analyze, path)
    except TimeoutError as exc:
        return JSONResponse(status_code=429, content={"detail": str(exc)})
    except StructureAnalysisError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except Exception as exc:
        logger.exception("analyze failed: %s", payload.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"[:300]},
        )
    return JSONResponse(status_code=200, content=result)

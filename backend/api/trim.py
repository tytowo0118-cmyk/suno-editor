"""单段音频裁短接口：把器乐候选在生成后立即裁成「只有本段」。

Suno 的 `make_instrumental` 不吃时长参数，空歌词的前奏/尾奏会写满一整首
（实测 205.560 / 189.504 s）。这条路由只做本地 DSP：读已落盘的 `/static` WAV，
按规划秒数裁短并写回 `static/output`，不再触发任何扣费请求。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.stitch import normalize_source
from utils.audio_processor import (
    STATIC_DIR,
    AudioSourceError,
    trim_clip_to_seconds,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audio"])


class TrimRequest(BaseModel):
    audio_url: str = Field(max_length=2048)
    # 本段在成品/时间轴上规划的秒数，与拼接侧 max_duration_sec 同一口径
    max_duration_sec: float = Field(gt=0, le=600)


class TrimResponse(BaseModel):
    audio_url: str
    filename: str
    duration: float
    source_duration: float
    # False = 原片本来就没超限，没有写新文件
    trimmed: bool
    # 切点是否吸附到了 ±0.5s 内的结构候选
    snapped: bool


@router.post("/trim", response_model=TrimResponse)
def trim_clip(payload: TrimRequest, request: Request) -> TrimResponse:
    source = normalize_source(payload.audio_url)
    # 只对后端自己的落盘文件动手：不下载外链，也不接受任意本地路径
    if not source.startswith("/static/"):
        raise HTTPException(status_code=400, detail="只接受后端 /static 落盘地址")

    try:
        output_path, source_sec, duration, snapped = trim_clip_to_seconds(
            source, payload.max_duration_sec
        )
    except AudioSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"音频不存在: {exc}") from exc
    except Exception as exc:  # pragma: no cover - 兜底日志
        logger.exception("clip trim failed")
        raise HTTPException(status_code=500, detail="音频裁短失败，请查看后端日志") from exc

    # 未超限时返回的是源文件路径，同样落在 STATIC_DIR 内
    relative = Path(output_path).resolve().relative_to(STATIC_DIR.resolve())
    base = str(request.base_url).rstrip("/")
    return TrimResponse(
        audio_url=f"{base}/static/{relative.as_posix()}",
        filename=Path(output_path).name,
        duration=round(duration, 3),
        source_duration=round(source_sec, 3),
        trimmed=duration < source_sec - 1e-6,
        snapped=snapped,
    )

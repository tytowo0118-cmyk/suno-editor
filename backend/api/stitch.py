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


def _normalize_source(url: str) -> str:
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
    sources = [(_normalize_source(seg.audio_url), seg.crossfade_ms) for seg in payload.segments]

    try:
        output_path, duration, peak_dbfs, segment_starts, head_trim, tail_trim = stitch_sequence(
            sources, output_path=filename
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
    )

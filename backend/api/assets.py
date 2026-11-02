"""生成音频落盘接口。

生成接口返回的音频以 Blob 形式只存在于浏览器内存；
锁定 Take 时先把 WAV 上传到这里，换取 /static 地址，供 /api/stitch 读取。
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from utils.audio_processor import STATIC_DIR

router = APIRouter(prefix="/api", tags=["assets"])

GENERATED_DIR = STATIC_DIR / "generated"
MAX_ASSET_BYTES = 32 * 1024 * 1024


class AssetResponse(BaseModel):
    audio_url: str
    filename: str
    bytes: int


@router.post("/assets", response_model=AssetResponse)
async def upload_asset(request: Request) -> AssetResponse:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="音频超过 32 MB 上限")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="请求体为空")
    if len(body) > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="音频超过 32 MB 上限")
    if len(body) < 12 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        raise HTTPException(status_code=400, detail="仅支持 WAV 音频")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"take_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    (GENERATED_DIR / filename).write_bytes(body)

    base = str(request.base_url).rstrip("/")
    return AssetResponse(
        audio_url=f"{base}/static/generated/{filename}",
        filename=filename,
        bytes=len(body),
    )

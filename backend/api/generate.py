"""Suno 代理生成接口。

浏览器只提交 `prompt`（风格提示 + [Section] 标签 + 本段歌词），后端负责：
1. 把 prompt 拆成 Suno 的 tags 与歌词，提交 `/suno/submit/music`；
2. 每 5 秒轮询 `/suno/fetch/{task_id}`，直到任务完成；
3. 下载远端音频，用 ffmpeg 统一转成 44.1 kHz 立体声 WAV 后返回二进制流。

附带 `continue_clip_id` / `continue_at` 时走续写：上游把该 clip 在 `continue_at` 秒之前的音频
当作创作上下文，返回一段以此为基础**重新生成**的续写音频（实测并不包含原开头，因此它是一版
新候选而不是延长版）；响应头 `X-Suno-Clip-Id` 回传本次音频的 clip id，可继续作为下一次续写的源。

上游代理地址与密钥通过环境变量配置：SUNO_API_BASE / SUNO_API_KEY。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
import wave
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import FileResponse

from utils.audio_processor import STATIC_DIR

logger = logging.getLogger(__name__)

# SUNO_VERBOSE_LOG=1 时把上游请求体/响应原文打到日志，用于联调扣费类接口时留痕
VERBOSE_LOG = os.environ.get("SUNO_VERBOSE_LOG", "").strip().lower() in {"1", "true", "yes"}
UPSTREAM_LOG_LIMIT = 3000


def _log_upstream(label: str, response: httpx.Response) -> None:
    if not VERBOSE_LOG:
        return
    logger.info(
        "suno upstream %s: HTTP %s body=%s",
        label,
        response.status_code,
        response.text[:UPSTREAM_LOG_LIMIT],
    )

router = APIRouter(prefix="/api", tags=["generation"])

OUTPUT_DIR = STATIC_DIR / "output"
MAX_DOWNLOAD_BYTES = 48 * 1024 * 1024

SECTION_TAG = re.compile(
    r"^\[(?:intro|verse|pre-chorus|prechorus|chorus|bridge|outro)[^\]]*\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
ANY_TAG_LINE = re.compile(r"^\[[^\]]+\]\s*$", re.MULTILINE)
COMPLETE_STATES = {
    "COMPLETE",
    "COMPLETED",
    "SUCCESS",
    "SUCCESSFUL",
    "SUCCEED",
    "SUCCEEDED",
    "DONE",
    "END",
}
FAILED_STATES = {
    "FAILED",
    "ERROR",
    "CANCELED",
    "CANCELLED",
    "REJECTED",
    "TIMEOUT",
    "TIMED_OUT",
}
# 片段级别尚未产出可下载音频的中间态
PENDING_STATES = {"PENDING", "QUEUED", "STREAMING", "IN_PROGRESS", "RECEIVED", "SUBMITTED"}
TASK_ID_KEYS = ("task_id", "taskId", "clip_id", "clipId", "id")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _upstream() -> tuple[str, dict[str, str]]:
    key = os.environ.get("SUNO_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=500, detail="未配置 SUNO_API_KEY，請檢查後端環境變數。")
    base = os.environ.get("SUNO_API_BASE", "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=500, detail="未配置 SUNO_API_BASE，請檢查後端環境變數。")
    return base, {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _split_prompt(prompt: str) -> tuple[str, str, bool]:
    """把「风格提示 + [Section] 歌词」拆成 Suno 的 tags / prompt / instrumental。"""
    text = prompt.strip()
    match = SECTION_TAG.search(text)
    if not match:
        # 没有结构标签时整段当歌词，风格留空由代理使用默认值
        return "", text, False

    tags = re.sub(r"\s+", " ", text[: match.start()]).strip(" ,;，；")
    lyrics = text[match.start() :].strip()
    lyric_text = ANY_TAG_LINE.sub("", lyrics).strip()
    return tags, lyrics, not lyric_text


def _pick_str(data: Any, keys: tuple[str, ...]) -> Optional[str]:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _upstream_message(text: str) -> str:
    """从上游错误响应里提取可读信息，避免把整段 JSON 塞给前端。"""
    raw = text.strip()
    if not raw:
        return ""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:200]
    if isinstance(body, dict):
        message = _pick_str(body, ("message", "msg", "detail"))
        if not message and isinstance(body.get("error"), dict):
            message = _pick_str(body["error"], ("message", "msg"))
        if message:
            return message[:200]
        code = body.get("code")
        if code not in (None, ""):
            return f"code={code}"
    return raw[:200]


def _task_id(payload: Any, depth: int = 0) -> Optional[str]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        for key in ("clips", "records"):
            items = data.get(key)
            if isinstance(items, list) and items and depth < 3:
                nested = _task_id(items[0], depth + 1)
                if nested:
                    return nested
        found = _pick_str(data, TASK_ID_KEYS)
        if found:
            return found
        if depth < 3:
            return _task_id(data.get("data"), depth + 1)
    return None


def _is_success(body: Any) -> bool:
    """网关的业务码写法不统一：200 / "200" / 0 / "success" 都算通过。"""
    if not isinstance(body, dict) or "code" not in body:
        return True
    return str(body.get("code")).strip().lower() in {"", "0", "200", "success", "ok"}


def _fetch_state(payload: Any) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return "", None, None, None

    status = str(data.get("status", "")).strip().upper()
    audio_url = _pick_str(data, ("audio_url", "audioUrl", "url"))
    message = _pick_str(data, ("fail_reason", "error_message", "message", "msg", "error"))
    if isinstance(message, str) and not message.strip():
        message = None
    clip_id = _pick_str(data, ("clip_id", "clipId"))

    clip: Optional[dict[str, Any]] = None
    for key in ("clips", "sunoData", "records", "clip", "data"):
        nested = data.get(key)
        if isinstance(nested, list):
            nested = nested[0] if nested else None
        if isinstance(nested, dict):
            clip = nested
            break

    clip_status = ""
    if clip is not None:
        if not audio_url:
            audio_url = _pick_str(clip, ("audio_url", "audioUrl"))
        if not clip_id:
            clip_id = _pick_str(clip, ("clip_id", "clipId", "id"))
        clip_status = str(clip.get("status") or clip.get("state") or "").strip().upper()
        if not status:
            status = clip_status
        if not message:
            message = _pick_str(clip, ("fail_reason", "error_message", "message", "msg", "error"))

    # streaming 阶段的 audio_url 是流地址而非成品，必须等片段状态落定
    if clip_status in PENDING_STATES:
        audio_url = None
    if status in FAILED_STATES or clip_status in FAILED_STATES:
        audio_url = None
        status = status if status in FAILED_STATES else clip_status

    return status, audio_url, message, clip_id


async def _submit(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    prompt: str,
    continue_clip_id: Optional[str] = None,
    continue_at: Optional[float] = None,
) -> str:
    tags, lyrics, instrumental = _split_prompt(prompt)
    payload: dict[str, Any] = {
        "prompt": lyrics,
        "tags": tags or "pop, modern",
        "make_instrumental": instrumental,
        "wait_audio": False,
    }
    model = os.environ.get("SUNO_MODEL", "").strip()
    if model:
        payload["model"] = model
    if continue_clip_id:
        # 续写：上游以 clip 在 continue_at 之前的音频为上下文，生成其后的新内容
        payload["continue_clip_id"] = continue_clip_id
        if continue_at is not None:
            payload["continue_at"] = round(continue_at, 3)

    if VERBOSE_LOG:
        logger.info(
            "suno submit payload: %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    try:
        response = await client.post(f"{base}/suno/submit/music", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接 Suno 代理：{exc}") from exc

    _log_upstream("submit", response)

    if response.status_code >= 400:
        detail = _upstream_message(response.text) or f"HTTP {response.status_code}"
        raise HTTPException(status_code=502, detail=f"Suno 提交失败（HTTP {response.status_code}）：{detail}")

    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Suno 提交返回的不是 JSON") from exc

    if isinstance(body, dict) and not _is_success(body):
        detail = _upstream_message(response.text)
        raise HTTPException(status_code=502, detail=f"Suno 提交被拒绝：{detail or '未知错误'}")

    task_id = _task_id(body)
    if not task_id:
        raise HTTPException(
            status_code=502,
            detail=f"Suno 提交成功但未返回 task_id：{_upstream_message(response.text)}",
        )
    return task_id


async def _wait_for_audio(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], task_id: str
) -> tuple[str, Optional[str]]:
    interval = max(0.05, _env_float("SUNO_POLL_INTERVAL", 5.0))
    timeout = max(10, _env_int("SUNO_TIMEOUT", 300))
    deadline = time.monotonic() + timeout
    url = f"{base}/suno/fetch/{urllib.parse.quote(task_id, safe='')}"

    while True:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"轮询 Suno 失败：{exc}") from exc

        _log_upstream(f"fetch {task_id}", response)

        if response.status_code >= 400:
            detail = _upstream_message(response.text) or f"HTTP {response.status_code}"
            raise HTTPException(
                status_code=502,
                detail=f"Suno 查询失败（HTTP {response.status_code}）：{detail}",
            )

        try:
            body = response.json()
        except ValueError:
            body = None

        status, audio_url, message, clip_id = _fetch_state(body)
        if status in FAILED_STATES:
            raise HTTPException(status_code=502, detail=f"Suno 生成失败：{message or status}")
        if audio_url and status not in FAILED_STATES:
            return audio_url, clip_id
        if status in COMPLETE_STATES and not audio_url:
            raise HTTPException(status_code=502, detail="Suno 任务已完成但没有返回音频地址")

        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=504,
                detail=f"Suno 生成超时（{timeout}s，task_id={task_id}）",
            )
        await asyncio.sleep(interval)


async def _download(client: httpx.AsyncClient, audio_url: str, destination: Path) -> None:
    total = 0
    try:
        async with client.stream("GET", audio_url, follow_redirects=True) as response:
            if response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"下载 Suno 音频失败（HTTP {response.status_code}）",
                )
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(status_code=502, detail="Suno 音频超过 48 MB 上限")
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"下载 Suno 音频失败：{exc}") from exc


def _ffmpeg_binary() -> str:
    """优先用显式配置；否则使用 imageio-ffmpeg 自带的静态二进制。"""
    override = os.environ.get("FFMPEG_BINARY", "").strip()
    if override:
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - 依赖缺失时回退到系统 PATH
        logger.warning("imageio-ffmpeg unavailable, falling back to system ffmpeg")
        return "ffmpeg"


async def _to_wav(source: Path, target: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        _ffmpeg_binary(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0 or not target.exists():
        detail = stderr.decode("utf-8", "ignore").strip()[-300:]
        raise HTTPException(status_code=500, detail=f"音频转码失败：{detail or '请检查 ffmpeg'}")


CLIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_CONTINUE_AT = 600.0


@router.post("/generate")
async def generate_music(
    prompt: str = Form(..., max_length=4000),
    duration: Optional[float] = Form(default=None),
    continue_clip_id: str = Form(default=""),
    continue_at: Optional[float] = Form(default=None),
):
    """同步代理 Suno：提交任务 → 轮询 → 下载 → 转 WAV → 返回音频流。

    `duration` 仅为兼容前端占位字段，实际时长由 Suno 根据歌词决定。
    带 `continue_clip_id` 时改为续写：上游把该 clip 在 `continue_at` 秒之前的音频当作创作上下文，
    返回一段在其后**新生成**的音频（不是原音频的延长版，适合当作该段的额外候选试听）。
    """
    del duration
    clip_id = continue_clip_id.strip()
    if clip_id and not CLIP_ID_RE.match(clip_id):
        raise HTTPException(status_code=422, detail="continue_clip_id 仅允许字母、数字、下划线和中划线")
    if continue_at is not None and not clip_id:
        raise HTTPException(status_code=422, detail="continue_at 需要配合 continue_clip_id 使用")
    if continue_at is not None and not 0 <= continue_at <= MAX_CONTINUE_AT:
        raise HTTPException(status_code=422, detail=f"continue_at 需要在 0–{int(MAX_CONTINUE_AT)} 秒之间")

    base, headers = _upstream()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        task_id = await _submit(client, base, headers, prompt, clip_id or None, continue_at)
        if clip_id:
            logger.info("suno continue: clip=%s at=%s", clip_id, continue_at)
        logger.info("suno task submitted: %s", task_id)

        started = time.perf_counter()
        audio_url, source_clip_id = await _wait_for_audio(client, base, headers, task_id)
        logger.info(
            "suno task ready: %s (%.1fs) clip=%s -> %s",
            task_id,
            time.perf_counter() - started,
            source_clip_id,
            audio_url,
        )

        stem = f"suno_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        suffix = Path(urllib.parse.urlparse(audio_url).path).suffix.lower()
        if suffix not in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
            suffix = ".mp3"
        source = OUTPUT_DIR / f"{stem}{suffix}"
        target = OUTPUT_DIR / f"{stem}.wav"

        try:
            await _download(client, audio_url, source)
            await _to_wav(source, target)
        finally:
            source.unlink(missing_ok=True)

        with wave.open(str(target), "rb") as handle:
            frames, frame_rate, channels, sample_width = (
                handle.getnframes(),
                handle.getframerate(),
                handle.getnchannels(),
                handle.getsampwidth(),
            )
        logger.info(
            "suno wav written: %s start=0.000s duration=%.3fs frames=%d rate=%d ch=%d bits=%d",
            target.name,
            frames / float(frame_rate),
            frames,
            frame_rate,
            channels,
            sample_width * 8,
        )

    return FileResponse(
        target,
        media_type="audio/wav",
        filename=target.name,
        headers={"X-Suno-Task-Id": task_id, "X-Suno-Clip-Id": source_clip_id or ""},
    )

"""Suno 代理生成接口。

浏览器只提交 `prompt`（风格提示 + [Section] 标签 + 本段歌词），后端负责：
1. 把 prompt 拆成 Suno 的 tags 与歌词，并做人声保底预处理（tags 去掉 `lo-fi` 并追加人声权重词，
   歌词开头补 `[Verse 1]`、把 `[Intro]` / `[Instrumental]` 换掉），提交 `/suno/submit/music`；
2. 每 5 秒轮询 `/suno/fetch/{task_id}`，直到任务完成；
3. 下载远端音频，用 ffmpeg 统一转成 44.1 kHz 立体声 WAV 后返回二进制流。

附带 `continue_clip_id` / `continue_at` 时走续写：上游把该 clip 在 `continue_at` 秒之前的音频
当作创作上下文，返回一段以此为基础**重新生成**的续写音频（实测并不包含原开头，因此它是一版
新候选而不是延长版）；响应头 `X-Suno-Clip-Id` 回传本次音频的 clip id，可继续作为下一次续写的源。

一次任务上游固定产出 2 个片段（双生），`max_clips=2` 时第 2 个也会下载转码，通过
`X-Suno-Extra-Clips`（base64 编码的 JSON 数组）回传，供前端挂载为同段落的另一个候选。

上游代理地址与密钥通过环境变量配置：SUNO_API_BASE / SUNO_API_KEY。
"""

from __future__ import annotations

import asyncio
import base64
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
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse

from utils.audio_processor import STATIC_DIR

logger = logging.getLogger(__name__)

# SUNO_VERBOSE_LOG=1 时把上游请求体/响应原文打到日志，用于联调扣费类接口时留痕
VERBOSE_LOG = os.environ.get("SUNO_VERBOSE_LOG", "").strip().lower() in {"1", "true", "yes"}
UPSTREAM_LOG_LIMIT = 3000

# 提交是扣费操作，只有「请求还没离开本机」的失败才允许重试：建单前的连接/连接池超时重复提交不会多扣费，
# 而读写超时时上游可能已经建单，重试就是再花一次钱，所以这些一律直接失败。
PRE_SEND_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)
SUBMIT_RETRY_DELAYS = (1.0, 3.0)

# 轮询和下载都是幂等 GET：既不建单也不扣费，所以传输层失败和上游 5xx / 429 能安全重试。
# 一次网络抖动就作废整条已经扣费的任务，损失远大于多等几秒。
POLL_RETRY_DELAYS = (1.0, 2.0, 4.0)
DOWNLOAD_RETRY_DELAYS = (2.0, 5.0)
RETRYABLE_UPSTREAM_STATUS = {429, 500, 502, 503, 504}


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

# 标签行只有一个定义：整行就是一对中括号里的内容。同一个模式既用来找「风格 / 歌词」的切分点，
# 也用来从歌词里剔除标签以判断是否纯器乐。此前切分点只认 intro/verse/… 六个固定词，
# [Hook] / [Instrumental] / [808 bass] 找不到切分点，会把风格文本整段当歌词提交、
# tags 落成默认值，而剔除用的 ANY 模式却把这些行当标签，两处判定不一致才产生纯器乐误判。
# 仅认 ASCII 方括号：前端 sectionTag() 会把中英标签归一成 [Verse 1] / [Chorus] 等英文形态。
TAG_LINE = re.compile(r"^\[[^\]\n]+\]\s*$", re.MULTILINE)
# 上游对 lo-fi 这类词很敏感，容易被带进纯器乐编曲
LO_FI_WORD = re.compile(r"\blo-?fi\b", re.IGNORECASE)
LEADING_TAG = re.compile(r"^\[[^\]]*\]")
# 开头这些标记会让模型先铺一段无人声的前奏，续写/生成时统一换掉
LEADING_EMPTY_HEAD_TAG = re.compile(r"^\[(?:intro|instrumental)\b[^\]]*\]", re.IGNORECASE)
VOCAL_TAG_SUFFIX = "clear vocal, lead vocal, vocal-driven"
VOCAL_HEAD_TAG = "[Verse 1]"
DEFAULT_TAGS = "pop, modern"
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
    match = TAG_LINE.search(text)
    if not match:
        # 没有结构标签时整段当歌词，风格留空由代理使用默认值
        return "", text, False

    tags = re.sub(r"\s+", " ", text[: match.start()]).strip(" ,;，；")
    lyrics = text[match.start() :].strip()
    lyric_text = TAG_LINE.sub("", lyrics).strip()
    return tags, lyrics, not lyric_text


def _prepare_vocal_payload(tags: str, lyrics: str) -> tuple[str, str]:
    """人声保底预处理：曲风标签去掉 lo-fi 并追加人声权重词，歌词补齐可演唱的结构标签。

    Suno 在被 `lo-fi` 或 `[Intro]` / `[Instrumental]` 开头带偏时会直接忽略歌词出纯音乐，
    这里在提交前把载荷掰回来；纯器乐（歌词只剩标签）由调用方挡掉，不走这条逻辑。
    """
    cleaned_tags = ", ".join(
        re.sub(r"\s+", " ", part).strip(" 、")
        for part in re.split(r"[,，;；]", LO_FI_WORD.sub(" ", tags))
        if re.sub(r"\s+", " ", part).strip(" 、")
    )
    prepared_tags = ", ".join(filter(None, [cleaned_tags or DEFAULT_TAGS, VOCAL_TAG_SUFFIX]))

    body = lyrics.strip()
    head = LEADING_TAG.match(body)
    if head is None:
        # 完全没有段落标记时补一个，避免上游把整段文本当成描述而不是歌词
        body = f"{VOCAL_HEAD_TAG}\n{body}"
    elif LEADING_EMPTY_HEAD_TAG.match(head.group(0)):
        body = VOCAL_HEAD_TAG + body[head.end() :]

    return prepared_tags, body


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


CLIP_LIST_KEYS = ("clips", "sunoData", "records", "data", "clip")


def _clip_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """取出任务负载里的片段数组；不同网关版本键名不统一。"""
    for key in CLIP_LIST_KEYS:
        nested = data.get(key)
        if isinstance(nested, dict):
            nested = [nested]
        if isinstance(nested, list):
            items = [item for item in nested if isinstance(item, dict)]
            if items:
                return items
    return []


def _clip_state(clip: dict[str, Any]) -> str:
    return str(clip.get("status") or clip.get("state") or "").strip().upper()


def _clip_record(clip: dict[str, Any]) -> Optional[dict[str, Any]]:
    """片段落定且有成品地址时返回 {clip_id, audio_url}，否则 None。"""
    state = _clip_state(clip)
    if state in FAILED_STATES or state in PENDING_STATES:
        return None
    audio_url = _pick_str(clip, ("audio_url", "audioUrl"))
    if not audio_url:
        return None
    return {"clip_id": _pick_str(clip, ("clip_id", "clipId", "id")), "audio_url": audio_url}


def _fetch_state(payload: Any) -> tuple[str, list[dict[str, Any]], Optional[str]]:
    """解析轮询负载，返回 (任务状态, 已可下载的片段列表, 失败信息)。

    一次 Suno 任务固定产出 2 个片段（双生），这里按上游顺序返回全部已落定的片段，
    由调用方决定取几个，避免只读 data[0] 浪费掉一半已扣费的结果。
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        clip_items = [item for item in data if isinstance(item, dict)]
        data = clip_items[0] if clip_items else None
    if not isinstance(data, dict):
        return "", [], None

    status = str(data.get("status", "")).strip().upper()
    message = _pick_str(data, ("fail_reason", "error_message", "message", "msg", "error"))
    if isinstance(message, str) and not message.strip():
        message = None

    clips: list[dict[str, Any]] = []
    nested = _clip_list(data)
    if nested:
        for clip in nested:
            record = _clip_record(clip)
            if record is not None:
                clips.append(record)
        if not status:
            status = _clip_state(nested[0])
        if not clips and any(_clip_state(c) in FAILED_STATES for c in nested):
            status = "FAILED"
            failed = next(c for c in nested if _clip_state(c) in FAILED_STATES)
            message = _pick_str(failed, ("fail_reason", "error_message", "message", "msg", "error"))
    else:
        # 少数网关把音频直接挂在任务层，合成一条伪片段记录
        task_audio = _pick_str(data, ("audio_url", "audioUrl", "url"))
        if status in FAILED_STATES:
            task_audio = None
        elif status in PENDING_STATES:
            # streaming 阶段的地址是流地址而非成品，必须等状态落定
            task_audio = None
        if task_audio:
            clips.append({"clip_id": _pick_str(data, ("clip_id", "clipId")), "audio_url": task_audio})

    if status in FAILED_STATES:
        clips = []

    return status, clips, message


async def _post_submit(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    """提交建单，只对建单前的连接失败退避重试。

    一段 3 候选是并发提交的，上游网关忙时接入队列会排队，握手在 connect 超时（10s）内没完成
    就抛 ConnectTimeout；这类请求根本没发出去，重试是安全的。读写超时时字节可能已经到达上游，
    重试等于再扣一次费，因此不重试，直接把异常类名和耗时摊到错误信息里。
    """
    total_started = time.perf_counter()
    for attempt in range(len(SUBMIT_RETRY_DELAYS) + 1):
        attempt_started = time.perf_counter()
        try:
            return await client.post(f"{base}/suno/submit/music", headers=headers, json=payload)
        except PRE_SEND_ERRORS as exc:
            elapsed = time.perf_counter() - attempt_started
            if attempt >= len(SUBMIT_RETRY_DELAYS):
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"无法连接 Suno 代理：{type(exc).__name__}"
                        f"（尝试 {attempt + 1} 次 / {time.perf_counter() - total_started:.1f}s）"
                    ),
                ) from exc
            delay = SUBMIT_RETRY_DELAYS[attempt]
            logger.warning(
                "suno submit pre-send failure: %s after %.2fs, retry %d/%d in %.1fs",
                type(exc).__name__,
                elapsed,
                attempt + 1,
                len(SUBMIT_RETRY_DELAYS),
                delay,
            )
            await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"无法连接 Suno 代理：{type(exc).__name__}"
                    f"（{time.perf_counter() - attempt_started:.1f}s，已发出所以不重试）"
                ),
            ) from exc

    raise HTTPException(status_code=502, detail="无法连接 Suno 代理：提交未产生响应")


async def _submit(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    prompt: str,
    continue_clip_id: Optional[str] = None,
    continue_at: Optional[float] = None,
) -> str:
    tags, lyrics, instrumental = _split_prompt(prompt)
    if not instrumental:
        # 全曲生成与截断续写都走这里，续写同样容易被上游判成纯器乐
        tags, lyrics = _prepare_vocal_payload(tags, lyrics)
    payload: dict[str, Any] = {
        "prompt": lyrics,
        "tags": tags or DEFAULT_TAGS,
        "make_instrumental": instrumental,
        "wait_audio": False,
    }
    logger.info(
        "suno payload prepared: instrumental=%s continue=%s tags=%r prompt_head=%r",
        instrumental,
        continue_clip_id or "-",
        payload["tags"],
        payload["prompt"][:32],
    )
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

    response = await _post_submit(client, base, headers, payload)

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


async def _poll_once(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    task_id: str,
) -> httpx.Response:
    """带退避地取一次任务状态；重试耗尽后把 task_id 摊进错误信息，方便人工接着查。"""
    total_started = time.perf_counter()
    for attempt in range(len(POLL_RETRY_DELAYS) + 1):
        attempt_started = time.perf_counter()
        exhausted = attempt >= len(POLL_RETRY_DELAYS)
        prefix = f"{attempt + 1}/{len(POLL_RETRY_DELAYS) + 1}"
        delay = 0.0 if exhausted else POLL_RETRY_DELAYS[attempt]

        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            if exhausted:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"轮询 Suno 失败：{type(exc).__name__}"
                        f"（task_id={task_id}，尝试 {prefix} / {time.perf_counter() - total_started:.1f}s）"
                    ),
                ) from exc
            logger.warning(
                "suno fetch %s transient failure: %s after %.2fs, retry %s in %.1fs",
                task_id,
                type(exc).__name__,
                time.perf_counter() - attempt_started,
                prefix,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        _log_upstream(f"fetch {task_id}", response)

        if response.status_code in RETRYABLE_UPSTREAM_STATUS:
            if exhausted:
                detail = _upstream_message(response.text) or f"HTTP {response.status_code}"
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Suno 查询失败（HTTP {response.status_code}，task_id={task_id}，"
                        f"尝试 {prefix} / {time.perf_counter() - total_started:.1f}s）：{detail}"
                    ),
                )
            logger.warning(
                "suno fetch %s upstream HTTP %d, retry %s in %.1fs",
                task_id,
                response.status_code,
                prefix,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        return response

    raise HTTPException(status_code=502, detail=f"Suno 查询未产生响应：task_id={task_id}")


async def _wait_for_audio(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    task_id: str,
    max_clips: int = 1,
) -> list[dict[str, Any]]:
    """轮询直到至少有一个片段落定，返回最多 max_clips 条 {clip_id, audio_url}。

    单次查询交给 `_poll_once` 做退避重试：任务已经建单扣费，不能被一次连接抖动或上游瞬时 5xx 作废。
    """
    interval = max(0.05, _env_float("SUNO_POLL_INTERVAL", 5.0))
    timeout = max(10, _env_int("SUNO_TIMEOUT", 300))
    deadline = time.monotonic() + timeout
    url = f"{base}/suno/fetch/{urllib.parse.quote(task_id, safe='')}"

    while True:
        response = await _poll_once(client, url, headers, task_id)

        # 落到这里说明是 2xx 或不可重试的 4xx（401 / 404 之类，重试也救不回来）
        if response.status_code >= 400:
            detail = _upstream_message(response.text) or f"HTTP {response.status_code}"
            raise HTTPException(
                status_code=502,
                detail=f"Suno 查询失败（HTTP {response.status_code}，task_id={task_id}）：{detail}",
            )

        try:
            body = response.json()
        except ValueError:
            body = None

        status, clips, message = _fetch_state(body)
        if status in FAILED_STATES:
            raise HTTPException(
                status_code=502,
                detail=f"Suno 生成失败（task_id={task_id}）：{message or status}",
            )
        if clips:
            return clips[:max_clips]
        if status in COMPLETE_STATES:
            raise HTTPException(
                status_code=502,
                detail=f"Suno 任务已完成但没有返回音频地址：task_id={task_id}",
            )

        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=504,
                detail=f"Suno 生成超时（{timeout}s，task_id={task_id}）",
            )
        await asyncio.sleep(interval)


async def _download(client: httpx.AsyncClient, audio_url: str, destination: Path) -> None:
    """把远端音频写到 destination，传输层失败与上游 5xx / 429 按 2s、5s 退避重试。

    下载同样是幂等 GET、不再扣费，而这条音频所在的任务已经付过钱，一次断流就整单作废太亏。
    每次重试都重新以 "wb" 打开文件并把字节数归零——半截音频必须从头重下，
    否则残留的开头会拼进新数据，得到一个长度对但内容错位的 WAV。
    """
    started = time.perf_counter()
    for attempt in range(len(DOWNLOAD_RETRY_DELAYS) + 1):
        total = 0
        exhausted = attempt >= len(DOWNLOAD_RETRY_DELAYS)
        prefix = f"{attempt + 1}/{len(DOWNLOAD_RETRY_DELAYS) + 1}"
        delay = 0.0 if exhausted else DOWNLOAD_RETRY_DELAYS[attempt]

        try:
            async with client.stream("GET", audio_url, follow_redirects=True) as response:
                if response.status_code in RETRYABLE_UPSTREAM_STATUS:
                    if exhausted:
                        raise HTTPException(
                            status_code=502,
                            detail=(
                                f"下载 Suno 音频失败（HTTP {response.status_code}，"
                                f"尝试 {prefix} / {time.perf_counter() - started:.1f}s）"
                            ),
                        )
                    logger.warning(
                        "suno download %s upstream HTTP %d, retry %s in %.1fs",
                        destination.name,
                        response.status_code,
                        prefix,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

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
            if exhausted:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"下载 Suno 音频失败：{type(exc).__name__}"
                        f"（{time.perf_counter() - started:.1f}s / 已落盘 {total} 字节 / 尝试 {prefix}）"
                    ),
                ) from exc
            logger.warning(
                "suno download %s transient failure: %s after %.1fs (%d 字节), retry %s in %.1fs",
                destination.name,
                type(exc).__name__,
                time.perf_counter() - started,
                total,
                prefix,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        return

    raise HTTPException(status_code=502, detail="下载 Suno 音频失败：未产生响应")


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
# 一次 Suno 任务固定产出 2 个片段，超出部分不下载
MAX_CLIPS_PER_TASK = 2
DOWNLOAD_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}


async def _materialize(
    client: httpx.AsyncClient, record: dict[str, Any], stem: str, index: int
) -> tuple[Path, float]:
    """下载并转码单个片段，返回 (WAV 路径, 时长秒)。index 0 用主文件名。"""
    audio_url = record["audio_url"]
    suffix = Path(urllib.parse.urlparse(audio_url).path).suffix.lower()
    if suffix not in DOWNLOAD_SUFFIXES:
        suffix = ".mp3"
    # 临时下载名带 _src 后缀：上游直接给 .wav 时，若与 target 同名 ffmpeg 会报
    # 「same as Input」，且 finally 里的 unlink 会连成品一起删掉
    source = OUTPUT_DIR / f"{stem}_{index}_src{suffix}"
    target = OUTPUT_DIR / (f"{stem}.wav" if index == 0 else f"{stem}_{index}.wav")
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
    duration = frames / float(frame_rate)
    logger.info(
        "suno wav written [%d/%d]: %s start=0.000s duration=%.3fs frames=%d rate=%d ch=%d bits=%d",
        index + 1,
        MAX_CLIPS_PER_TASK,
        target.name,
        duration,
        frames,
        frame_rate,
        channels,
        sample_width * 8,
    )
    return target, duration


@router.post("/generate")
async def generate_music(
    request: Request,
    prompt: str = Form(..., max_length=4000),
    duration: Optional[float] = Form(default=None),
    continue_clip_id: str = Form(default=""),
    continue_at: Optional[float] = Form(default=None),
    max_clips: int = Form(default=1),
):
    """同步代理 Suno：提交任务 → 轮询 → 下载 → 转 WAV → 返回音频流。

    `duration` 仅为兼容前端占位字段，实际时长由 Suno 根据歌词决定。
    带 `continue_clip_id` 时改为续写：上游把该 clip 在 `continue_at` 秒之前的音频当作创作上下文，
    返回一段在其后**新生成**的音频（不是原音频的延长版，适合当作该段的额外候选试听）。
    `max_clips=2` 时把同一任务的第 2 个片段也下载转码，以 base64(JSON) 放在
    `X-Suno-Extra-Clips` 响应头里返回，避免已扣费的双生结果被丢弃。
    """
    del duration
    clip_id = continue_clip_id.strip()
    if clip_id and not CLIP_ID_RE.match(clip_id):
        raise HTTPException(status_code=422, detail="continue_clip_id 仅允许字母、数字、下划线和中划线")
    if continue_at is not None and not clip_id:
        raise HTTPException(status_code=422, detail="continue_at 需要配合 continue_clip_id 使用")
    if continue_at is not None and not 0 <= continue_at <= MAX_CONTINUE_AT:
        raise HTTPException(status_code=422, detail=f"continue_at 需要在 0–{int(MAX_CONTINUE_AT)} 秒之间")
    wanted_clips = min(max(1, max_clips), MAX_CLIPS_PER_TASK)

    base, headers = _upstream()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        task_id = await _submit(client, base, headers, prompt, clip_id or None, continue_at)
        if clip_id:
            logger.info("suno continue: clip=%s at=%s", clip_id, continue_at)
        logger.info("suno task submitted: %s", task_id)

        started = time.perf_counter()
        records = await _wait_for_audio(client, base, headers, task_id, wanted_clips)
        logger.info(
            "suno task ready: %s (%.1fs) clips=%d ids=%s",
            task_id,
            time.perf_counter() - started,
            len(records),
            [record["clip_id"] for record in records],
        )

        stem = f"suno_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        materialized: list[tuple[Path, float]] = []
        for index, record in enumerate(records):
            try:
                materialized.append(await _materialize(client, record, stem, index))
            except HTTPException:
                for path, _duration in materialized:
                    path.unlink(missing_ok=True)
                raise

        base_url = str(request.base_url).rstrip("/")
        extras = [
            {
                "clipId": records[index]["clip_id"],
                "url": f"{base_url}/static/output/{materialized[index][0].name}",
                "duration": round(materialized[index][1], 3),
            }
            for index in range(1, len(materialized))
        ]
        logger.info(
            "suno dual mount: task=%s kept=%d/%d extras=%d",
            task_id,
            len(materialized),
            wanted_clips,
            len(extras),
        )

    target, _ = materialized[0]
    extra_header = (
        base64.b64encode(json.dumps(extras, ensure_ascii=True).encode("utf-8")).decode("ascii")
        if extras
        else ""
    )

    return FileResponse(
        target,
        media_type="audio/wav",
        filename=target.name,
        headers={
            "X-Suno-Task-Id": task_id,
            "X-Suno-Clip-Id": records[0]["clip_id"] or "",
            "X-Suno-Extra-Clips": extra_header,
        },
    )

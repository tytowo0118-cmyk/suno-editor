"""Suno 代理生成接口。

浏览器只提交 `prompt`（风格提示 + [Section] 标签 + 本段歌词），后端负责：
1. 把 prompt 拆成 Suno 的 tags 与歌词，并做人声保底预处理（tags 去掉 `lo-fi` 并追加人声权重词，
   歌词开头补 `[Verse 1]`、把 `[Intro]` / `[Instrumental]` 换掉），提交 `/suno/submit/music`；
2. 每 5 秒轮询 `/suno/fetch/{task_id}`，直到任务完成；
3. 下载远端音频，用 ffmpeg 统一转成 44.1 kHz 立体声 WAV 后返回二进制流。

**提交是扣费操作，所以主链路 `run_generation` 一拿到 task_id 就把它按载荷指纹写进落盘账本**
（`utils/generate_cache.py`）。同指纹的后续请求 —— 重试、刷新、后端重启 —— 先免费重查那张票据，
而不是再建一单；只有上游明确判死才允许再次付费。同步端点与任务端点共用这一个函数。

两条入口：`POST /api/generate` 一条请求挂到底（兼容旧调用），
`POST /api/generate/jobs` + `GET /api/generate/jobs/{job_id}` 提交加轮询、带阶段进度
（submitting / generating / downloading / transcoding），见 `api/generate_jobs.py`。

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse

from utils import clip_index, generate_cache
from utils.audio_processor import STATIC_DIR
from utils.generate_cache import ClipRef

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


class PollTickLost(Exception):
    """一次轮询 tick 的退避预算耗尽，没能问到任务状态。

    实测上游代理会连续几十秒随机掐 TLS（2026-11-06 那轮 84 s 窗口里旁路 5 s 一轮只落到 4 个负载），
    9.9 s 的阶梯必被击穿。任务已经建单扣费，所以「问不到」只该让这一次 tick 作废，
    由 `_wait_for_audio` 睡到下一个轮询间隔继续问，直到 `SUNO_TIMEOUT` 总预算才算超时。
    """


class UpstreamTaskFailed(HTTPException):
    """上游把这条已扣费的任务判成终态且不可能再出音频。

    只有这一类失败才允许把账本写成 `failed`（= 允许再次付费提交）：任务 FAILED，
    或者任务 COMPLETE 却一个音频地址都没给。本地超时、下载失败、进程重启都不属于此列，
    那些票据留在 `submitted`，下次同指纹的请求免费重查。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=502, detail=detail)


# 进度回调只收「增量字段」，由任务表合并进 job.progress；同步端点不传，行为与改造前一致。
ProgressHook = Callable[[dict[str, Any]], None]


def _noop_progress(_update: dict[str, Any]) -> None:
    return None


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


def build_payload(
    prompt: str,
    continue_clip_id: Optional[str] = None,
    continue_at: Optional[float] = None,
) -> dict[str, Any]:
    """把浏览器的 prompt 掰成上游实际载荷。**纯函数、不建单、不花钱**。

    单独抽出来是为了让扣费指纹算在「上游真正收到的东西」上：`_prepare_vocal_payload` 会重写
    tags 与歌词头，两个不同的 raw prompt 可能掰成同一份载荷，那本来就是同一次创作，
    应该命中同一条账本；反过来 `mv` 或续写字段变了必须是新键。
    """
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
    mv = os.environ.get("SUNO_MODEL", "").strip()
    if mv:
        payload["mv"] = mv
    if continue_clip_id:
        # 续写：上游以 clip 在 continue_at 之前的音频为上下文，生成其后的新内容
        payload["continue_clip_id"] = continue_clip_id
        if continue_at is not None:
            payload["continue_at"] = round(continue_at, 3)

    logger.info(
        "suno payload prepared: instrumental=%s continue=%s tags=%r prompt_head=%r",
        instrumental,
        continue_clip_id or "-",
        payload["tags"],
        payload["prompt"][:32],
    )
    if VERBOSE_LOG:
        logger.info(
            "suno submit payload: %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    return payload


async def _submit(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> str:
    """建单（扣费）。载荷由 `build_payload` 先算好，指纹才能在提交前就确定。"""
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
    deadline: Optional[float] = None,
) -> httpx.Response:
    """带退避地取一次任务状态；退避预算耗尽只抛 `PollTickLost`，不判死已经扣费的任务。

    `deadline` 是 `time.monotonic()` 时刻：退避一睡就要越过它时，这个 tick 当场作废，
    否则阶梯会把总预算顶穿（实测 10s 预算跑到 14.4s）。
    """
    total_started = time.perf_counter()
    for attempt in range(len(POLL_RETRY_DELAYS) + 1):
        attempt_started = time.perf_counter()
        exhausted = attempt >= len(POLL_RETRY_DELAYS)
        prefix = f"{attempt + 1}/{len(POLL_RETRY_DELAYS) + 1}"
        delay = 0.0 if exhausted else POLL_RETRY_DELAYS[attempt]
        over_budget = not exhausted and deadline is not None and time.monotonic() + delay >= deadline

        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            if exhausted or over_budget:
                raise PollTickLost(
                    f"{type(exc).__name__}（尝试 {prefix} / "
                    f"{time.perf_counter() - total_started:.1f}s"
                    f"{'，总预算用完' if over_budget else ''}）"
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
            if exhausted or over_budget:
                detail = _upstream_message(response.text) or f"HTTP {response.status_code}"
                raise PollTickLost(
                    f"HTTP {response.status_code}（尝试 {prefix} / "
                    f"{time.perf_counter() - total_started:.1f}s"
                    f"{'，总预算用完' if over_budget else ''}）：{detail}"
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

    raise PollTickLost("未产生任何上游响应")


async def _wait_for_audio(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    task_id: str,
    max_clips: int = 1,
    on_tick: Optional[ProgressHook] = None,
) -> list[dict[str, Any]]:
    """轮询直到至少有一个片段落定，返回最多 max_clips 条 {clip_id, audio_url}。

    单次查询交给 `_poll_once` 做退避重试；重试预算耗尽只作废当前 tick，继续按
    `SUNO_POLL_INTERVAL` 问到 `SUNO_TIMEOUT` 为止 —— 任务已经建单扣费，
    不能因为代理掐了十几秒 TLS 就判死（实测见 README §2.3）。

    `on_tick` 每拍到一次状态就回调（含作废的 tick），让任务表能显示
    「上游还在 TEXT / 已问到第几拍 / 丢了几拍」，而不是只有一个转圈。
    """
    interval = max(0.05, _env_float("SUNO_POLL_INTERVAL", 5.0))
    timeout = max(10, _env_int("SUNO_TIMEOUT", 300))
    deadline = time.monotonic() + timeout
    url = f"{base}/suno/fetch/{urllib.parse.quote(task_id, safe='')}"
    lost_ticks = 0
    poll_ticks = 0
    emit = on_tick or _noop_progress

    while True:
        try:
            response = await _poll_once(client, url, headers, task_id, deadline)
        except PollTickLost as lost:
            lost_ticks += 1
            emit({"poll_ticks": poll_ticks, "lost_ticks": lost_ticks, "last_tick_lost": str(lost)[:160]})
            if time.monotonic() >= deadline:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        f"Suno 轮询在 {timeout}s 内问不到状态（task_id={task_id}，"
                        f"作废 {lost_ticks} 次）：{lost}；任务已扣费且已记账，重试会免费重查这张票据"
                    ),
                ) from lost
            logger.warning(
                "suno fetch %s tick lost #%d: %s，%.1fs 后接着问（剩 %.0fs 预算）",
                task_id,
                lost_ticks,
                lost,
                interval,
                deadline - time.monotonic(),
            )
            await asyncio.sleep(interval)
            continue

        poll_ticks += 1
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
        emit(
            {
                "upstream_status": status,
                "poll_ticks": poll_ticks,
                "lost_ticks": lost_ticks,
                "ready_clips": len(clips),
                "last_tick_lost": None,
            }
        )
        if status in FAILED_STATES:
            raise UpstreamTaskFailed(f"Suno 生成失败（task_id={task_id}）：{message or status}")
        if clips:
            return clips[:max_clips]
        if status in COMPLETE_STATES:
            raise UpstreamTaskFailed(f"Suno 任务已完成但没有返回音频地址：task_id={task_id}")

        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Suno 生成超时（{timeout}s，task_id={task_id}）；"
                    "任务已扣费且已记账，重试会免费重查这张票据"
                ),
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
    client: httpx.AsyncClient,
    record: dict[str, Any],
    stem: str,
    index: int,
    on_stage: Optional[Callable[[str], None]] = None,
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
    stage = on_stage or (lambda _name: None)
    try:
        stage("downloading")
        await _download(client, audio_url, source)
        stage("transcoding")
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


SOURCE_SUBMIT = "submit"
SOURCE_REFETCH = "refetch"
SOURCE_CACHE = "cache"


@dataclass
class GenerationResult:
    """一次生成的结果 + 这次到底有没有花钱。

    `source` 是给前端看的计费语义，三种都不含糊：
    - `submit`：本次真的建了单（扣费）；
    - `refetch`：账本里有已扣费票据，这次只做了免费的 fetch + 下载；
    - `cache`：WAV 已在盘上，连网络请求都没发。
    """

    fingerprint: str
    task_id: str
    clips: list[ClipRef]
    source: str
    elapsed_sec: float

    @property
    def reused(self) -> bool:
        return self.source != SOURCE_SUBMIT

    def public(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "task_id": self.task_id,
            "source": self.source,
            "reused": self.reused,
            "clips": [clip.public() for clip in self.clips],
            "elapsed_sec": round(self.elapsed_sec, 1),
        }


def validate_continue(continue_clip_id: str, continue_at: Optional[float]) -> str:
    """续写参数校验，同步端点与任务端点共用，返回清洗后的 clip_id。"""
    clip_id = continue_clip_id.strip()
    if clip_id and not CLIP_ID_RE.match(clip_id):
        raise HTTPException(status_code=422, detail="continue_clip_id 仅允许字母、数字、下划线和中划线")
    if continue_at is not None and not clip_id:
        raise HTTPException(status_code=422, detail="continue_at 需要配合 continue_clip_id 使用")
    if continue_at is not None and not 0 <= continue_at <= MAX_CONTINUE_AT:
        raise HTTPException(status_code=422, detail=f"continue_at 需要在 0–{int(MAX_CONTINUE_AT)} 秒之间")
    return clip_id


async def _land_clips(
    client: httpx.AsyncClient,
    fp: str,
    task_id: str,
    records: list[dict[str, Any]],
    known: list[ClipRef],
    emit: ProgressHook,
) -> list[ClipRef]:
    """把上游片段落盘成 WAV，已在账本里的直接复用，只下载差集。

    改造前第 2 条下载失败会把第 1 条已落盘的 WAV 一起删掉，下次重试两条重下。
    现在已落地的先 `mark_ready` 兑现进账本，重试只补缺的那条 —— 两条都已经付过钱，
    重下不花钱但白等一次转码，删掉更是把免费恢复的机会扔了。
    """
    by_clip_id = {clip.clip_id: clip for clip in known if clip.clip_id}
    result: list[ClipRef] = []
    newly: list[ClipRef] = []
    total = len(records)

    for index, record in enumerate(records):
        existing = by_clip_id.get(record.get("clip_id") or "")
        if existing is not None:
            result.append(existing)
            emit({"phase": "downloading", "clip_index": index, "clip_total": total, "clip_reused": True})
            logger.info(
                "suno clip already on disk: task=%s clip=%s file=%s",
                task_id,
                existing.clip_id,
                existing.filename,
            )
            continue

        def stage(name: str, _index: int = index) -> None:
            emit({"phase": name, "clip_index": _index, "clip_total": total, "clip_reused": False})

        stem = f"suno_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        try:
            path, duration = await _materialize(client, record, stem, index, on_stage=stage)
        except HTTPException:
            if newly:
                generate_cache.mark_ready(fp, newly)
            raise
        # 落盘名里那 6 位是本地的 uuid4，不是 Suno 的 clip_id；不记这一笔，
        # 结构分析接口就只能靠文件名找人
        clip_index.record(record.get("clip_id"), path.name, task_id)
        ref = ClipRef(clip_id=record.get("clip_id"), filename=path.name, duration=duration)
        result.append(ref)
        newly.append(ref)

    return result


async def run_generation(
    prompt: str,
    *,
    continue_clip_id: Optional[str] = None,
    continue_at: Optional[float] = None,
    max_clips: int = 1,
    on_progress: Optional[ProgressHook] = None,
) -> GenerationResult:
    """生成主链路：查账本 →（必要时）建单记账 → 轮询 → 落盘兑现。

    同步端点 `/api/generate` 与任务端点 `/api/generate/jobs` 共用这一个函数，
    所以「防重复计费」在两条入口上是同一套行为，不会一边省钱一边花钱。

    记账的时机是契约的一部分：`_submit` 一返回 task_id 就 `mark_submitted`，
    中间不插任何可失败的 await —— 否则一次轮询超时或 `--reload` 重启就会把票据丢掉，
    下一次同样的请求重新建单、重新扣费。
    """
    started = time.perf_counter()
    emit = on_progress or _noop_progress
    wanted = min(max(1, max_clips), MAX_CLIPS_PER_TASK)
    payload = build_payload(prompt, continue_clip_id, continue_at)
    fp = generate_cache.fingerprint(payload)
    base, headers = _upstream()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        entry = generate_cache.lookup(fp)
        known = entry.live_clips() if entry else []

        if entry is not None and entry.state == generate_cache.STATE_READY and len(known) >= wanted:
            emit({"phase": "done", "source": SOURCE_CACHE, "task_id": entry.task_id})
            logger.info(
                "suno cache hit: fp=%s task=%s clips=%d（未建单、未请求上游）",
                fp,
                entry.task_id,
                len(known),
            )
            return GenerationResult(
                fingerprint=fp,
                task_id=entry.task_id,
                clips=known[:wanted],
                source=SOURCE_CACHE,
                elapsed_sec=time.perf_counter() - started,
            )

        if entry is not None:
            # 票据在但 WAV 没落地（超时 / 下载失败 / 文件被清）：重查是幂等 GET，免费
            task_id = entry.task_id
            source = SOURCE_REFETCH
            emit({"phase": "generating", "source": source, "task_id": task_id})
            logger.info(
                "suno ledger refetch: fp=%s task=%s state=%s live=%d/%d（不建单）",
                fp,
                task_id,
                entry.state,
                len(known),
                wanted,
            )
        else:
            emit({"phase": "submitting", "source": SOURCE_SUBMIT})
            task_id = await _submit(client, base, headers, payload)
            generate_cache.mark_submitted(fp, task_id, payload)
            source = SOURCE_SUBMIT
            emit({"phase": "generating", "source": source, "task_id": task_id})
            if continue_clip_id:
                logger.info("suno continue: clip=%s at=%s", continue_clip_id, continue_at)
            logger.info("suno task submitted: %s fp=%s", task_id, fp)

        wait_started = time.perf_counter()
        try:
            records = await _wait_for_audio(
                client, base, headers, task_id, wanted, on_tick=emit
            )
        except UpstreamTaskFailed as exc:
            # 只有上游判死才允许再次付费；本地超时保持 submitted
            generate_cache.mark_failed(fp, str(exc.detail))
            raise
        logger.info(
            "suno task ready: %s (%.1fs) clips=%d ids=%s",
            task_id,
            time.perf_counter() - wait_started,
            len(records),
            [record["clip_id"] for record in records],
        )

        clips = await _land_clips(client, fp, task_id, records, known, emit)
        generate_cache.mark_ready(fp, clips)
        logger.info(
            "suno dual mount: task=%s kept=%d/%d source=%s",
            task_id,
            len(clips),
            wanted,
            source,
        )

    emit({"phase": "done", "source": source, "task_id": task_id})
    return GenerationResult(
        fingerprint=fp,
        task_id=task_id,
        clips=clips,
        source=source,
        elapsed_sec=time.perf_counter() - started,
    )


@router.post("/generate")
async def generate_music(
    request: Request,
    prompt: str = Form(..., max_length=4000),
    duration: Optional[float] = Form(default=None),
    continue_clip_id: str = Form(default=""),
    continue_at: Optional[float] = Form(default=None),
    max_clips: int = Form(default=1),
):
    """同步代理 Suno：查账本 →（必要时）提交 → 轮询 → 下载 → 转 WAV → 返回音频流。

    保留这条同步入口是为了兼容既有调用与注入验证；需要中间进度时用
    `POST /api/generate/jobs` + `GET /api/generate/jobs/{job_id}`，两者共用 `run_generation`。

    `duration` 仅为兼容前端占位字段，实际时长由 Suno 根据歌词决定。
    带 `continue_clip_id` 时改为续写：上游把该 clip 在 `continue_at` 秒之前的音频当作创作上下文，
    返回一段在其后**新生成**的音频（不是原音频的延长版，适合当作该段的额外候选试听）。
    `max_clips=2` 时把同一任务的第 2 个片段也下载转码，以 base64(JSON) 放在
    `X-Suno-Extra-Clips` 响应头里返回，避免已扣费的双生结果被丢弃。
    `X-Suno-Source` 回传本次是 submit / refetch / cache，让调用方能确认有没有再花钱。
    """
    del duration
    clip_id = validate_continue(continue_clip_id, continue_at)
    result = await run_generation(
        prompt,
        continue_clip_id=clip_id or None,
        continue_at=continue_at,
        max_clips=max_clips,
    )
    if not result.clips:
        raise HTTPException(status_code=502, detail=f"Suno 任务没有落盘任何音频：task_id={result.task_id}")

    base_url = str(request.base_url).rstrip("/")
    extras = [
        {
            "clipId": clip.clip_id,
            "url": f"{base_url}/static/output/{clip.filename}",
            "duration": round(clip.duration, 3),
        }
        for clip in result.clips[1:]
    ]
    extra_header = (
        base64.b64encode(json.dumps(extras, ensure_ascii=True).encode("utf-8")).decode("ascii")
        if extras
        else ""
    )
    target = OUTPUT_DIR / result.clips[0].filename

    return FileResponse(
        target,
        media_type="audio/wav",
        filename=target.name,
        headers={
            "X-Suno-Task-Id": result.task_id,
            "X-Suno-Clip-Id": result.clips[0].clip_id or "",
            "X-Suno-Extra-Clips": extra_header,
            "X-Suno-Source": result.source,
        },
    )

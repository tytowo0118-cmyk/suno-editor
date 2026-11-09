"""已扣费生成任务的落盘账本：载荷指纹 → task_id / 已落盘片段。

**为什么必须落盘而不是只放内存**：提交是扣费操作，而 `api/generate.py` 的轮询超时文案早就写着
「任务已扣费，可用该 task_id 免费重查」—— 在补这个模块之前，没有任何地方记住那个 task_id。
于是一次轮询超时、一次 `--reload` 重启、一次用户取消等待，都会让下一次同样的请求重新建单、
重新扣一次钱。账本把「钱已经花了」变成可恢复的事实：`_submit` 一拿到 task_id 就记
`state=submitted`，之后同指纹的请求先**免费重查**那条任务，片段落盘后再补 `state=ready`。

**指纹取上游实际载荷，不取浏览器发来的 prompt**：提交前 `_prepare_vocal_payload` 会重写 tags
（去 `lo-fi`、追加人声权重词）和歌词头（补 `[Verse 1]`、换掉 `[Intro]`）。两个不同的 raw prompt
可能掰成同一份载荷，按 prompt 做键就漏掉这种复用；反过来载荷里的 `mv`（模型）或续写字段变了
就必须是新键，否则会把不同创作意图的结果错当成同一个。传输开关 `wait_audio` 不进指纹。

三种状态各自的花钱含义：

- `submitted`：已建单扣费、还没拿到落盘 WAV。重查是幂等 GET，免费；永远不该被当成「没生成过」。
- `ready`：片段已落盘，命中直接回 `/static` 地址。**文件被删掉也不重新扣费** —— task_id 还在，
  重新 fetch + 下载同样是免费 GET，所以这里只把丢了的片段从清单里摘掉，保留票据。
- `failed`：上游明确判死（任务 FAILED）。只有这种才允许再次付费提交。
  本地超时、下载失败、进程重启一律**不写** failed，那些都还是 submitted。

淘汰只动 `ready` / `failed`，按 `updated_at` 从旧到新；`submitted` 永不淘汰 —— 淘汰它等于把
一张已经付过钱的票据扔掉。写盘用 tempfile + `os.replace`（与 `clip_index` 同一套原子写法），
读失败按空账本处理：账本坏了最坏结果是多花一次钱，不该让生成主链路挂掉。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.audio_processor import STATIC_DIR

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
LEDGER_PATH = BASE_DIR / "data" / "generate_cache.json"
OUTPUT_DIR = STATIC_DIR / "output"

LEDGER_VERSION = 1
STATE_SUBMITTED = "submitted"
STATE_READY = "ready"
STATE_FAILED = "failed"

# 账本条目上限。一条只有几百字节，512 条足够覆盖很长的工作历史；
# 真正占磁盘的是 WAV 本身，那由 static/output 的清理策略管，不在这里重复计费。
_MAX_ENTRIES = 512

# 进指纹的载荷字段之外的东西一律不算创作输入：wait_audio 是传输开关，
# 改它不该让同一份歌词+曲风变成「新的一次生成」。
_TRANSPORT_KEYS = frozenset({"wait_audio"})

_lock = threading.Lock()


def fingerprint(payload: Dict[str, Any]) -> str:
    """上游载荷的稳定指纹：键排序 + 紧凑 JSON + sha256，取前 32 位十六进制。

    32 位（128 bit）对「本机几百条生成历史」这个量级碰撞概率可以忽略，
    而短键让日志和 data/generate_cache.json 都能直接肉眼比对。
    """
    creative = {key: value for key, value in payload.items() if key not in _TRANSPORT_KEYS}
    canonical = json.dumps(creative, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass
class ClipRef:
    clip_id: Optional[str]
    filename: str
    duration: float

    def public(self) -> Dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "filename": self.filename,
            "duration": round(self.duration, 3),
            "audio_url": f"/static/output/{self.filename}",
        }


@dataclass
class Entry:
    fingerprint: str
    task_id: str
    state: str = STATE_SUBMITTED
    tags: str = ""
    instrumental: bool = False
    mv: str = ""
    detail: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    clips: List[ClipRef] = field(default_factory=list)

    def live_clips(self) -> List[ClipRef]:
        """只返回 WAV 真的还在盘上的片段；账本可能比磁盘旧（用户清过 static/output）。"""
        return [clip for clip in self.clips if _wav_path(clip.filename) is not None]

    def public(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "task_id": self.task_id,
            "state": self.state,
            "tags": self.tags,
            "instrumental": self.instrumental,
            "mv": self.mv,
            "clips": [clip.public() for clip in self.clips],
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            **({"detail": self.detail} if self.detail else {}),
        }


def _wav_path(filename: str) -> Optional[Path]:
    """账本里的文件名必须是个纯名字且落在 output 目录内，越界或不存在都当没有。"""
    name = str(filename or "")
    if not name or name != Path(name).name or not name.endswith(".wav"):
        return None
    path = (OUTPUT_DIR / name).resolve()
    if OUTPUT_DIR.resolve() not in path.parents or not path.is_file():
        return None
    return path


def _read() -> Dict[str, Any]:
    if not LEDGER_PATH.is_file():
        return {}
    try:
        with LEDGER_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("generate_cache 读取失败，按空账本处理: %s", exc)
        return {}
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _write(entries: Dict[str, Any]) -> None:
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=str(LEDGER_PATH.parent), suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            json.dump(
                {"version": LEDGER_VERSION, "entries": entries},
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        finally:
            handle.close()
        os.replace(handle.name, LEDGER_PATH)
    except Exception as exc:  # pragma: no cover - 账本是省钱设施，不该打断生成
        logger.warning("generate_cache 写入失败: %s", exc)


def _entry_from_raw(fp: str, raw: Any) -> Optional[Entry]:
    if not isinstance(raw, dict):
        return None
    task_id = str(raw.get("task_id") or "").strip()
    state = str(raw.get("state") or "")
    if not task_id or state not in (STATE_SUBMITTED, STATE_READY, STATE_FAILED):
        return None
    clips: List[ClipRef] = []
    for item in raw.get("clips") or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "")
        if not filename:
            continue
        try:
            duration = float(item.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        clip_id = item.get("clip_id")
        clips.append(
            ClipRef(
                clip_id=str(clip_id) if isinstance(clip_id, str) and clip_id else None,
                filename=filename,
                duration=duration,
            )
        )
    return Entry(
        fingerprint=fp,
        task_id=task_id,
        state=state,
        tags=str(raw.get("tags") or ""),
        instrumental=bool(raw.get("instrumental")),
        mv=str(raw.get("mv") or ""),
        detail=str(raw["detail"])[:300] if raw.get("detail") else None,
        created_at=float(raw.get("created_at") or 0.0),
        updated_at=float(raw.get("updated_at") or 0.0),
        clips=clips,
    )


def _raw_from_entry(entry: Entry) -> Dict[str, Any]:
    return {
        "task_id": entry.task_id,
        "state": entry.state,
        "tags": entry.tags,
        "instrumental": entry.instrumental,
        "mv": entry.mv,
        "detail": entry.detail,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "clips": [
            {"clip_id": clip.clip_id, "filename": clip.filename, "duration": round(clip.duration, 3)}
            for clip in entry.clips
        ],
    }


def _prune(entries: Dict[str, Any]) -> None:
    if len(entries) <= _MAX_ENTRIES:
        return
    # submitted 是已付过钱还没兑现的票据，不参与淘汰
    evictable = [
        (key, value)
        for key, value in entries.items()
        if isinstance(value, dict) and value.get("state") in (STATE_READY, STATE_FAILED)
    ]
    evictable.sort(key=lambda item: float(item[1].get("updated_at") or 0.0))
    for key, _ in evictable[: len(entries) - _MAX_ENTRIES]:
        entries.pop(key, None)


def lookup(fp: str) -> Optional[Entry]:
    """查账本。failed 一律当没有（允许再次付费提交）；ready 但文件丢了会摘掉死片段。

    摘掉之后如果一条都不剩，状态退回 submitted —— 票据还在，重新 fetch 是免费的，
    比「当作没生成过」再扣一次钱正确得多。
    """
    with _lock:
        entries = _read()
        entry = _entry_from_raw(fp, entries.get(fp))
        if entry is None or entry.state == STATE_FAILED:
            return None
        if entry.state == STATE_READY:
            live = entry.live_clips()
            if len(live) != len(entry.clips):
                logger.info(
                    "generate_cache 命中但文件已不在: fp=%s task=%s clips=%d→%d（保留票据，重查免费）",
                    fp,
                    entry.task_id,
                    len(entry.clips),
                    len(live),
                )
                entry.clips = live
                if not live:
                    entry.state = STATE_SUBMITTED
                entry.updated_at = time.time()
                entries[fp] = _raw_from_entry(entry)
                _write(entries)
        return entry


def mark_submitted(fp: str, task_id: str, payload: Dict[str, Any]) -> None:
    """建单成功立刻记账。这一步与「开始轮询」之间不能有任何可失败的等待。"""
    now = time.time()
    with _lock:
        entries = _read()
        previous = _entry_from_raw(fp, entries.get(fp))
        entries[fp] = _raw_from_entry(
            Entry(
                fingerprint=fp,
                task_id=task_id,
                state=STATE_SUBMITTED,
                tags=str(payload.get("tags") or ""),
                instrumental=bool(payload.get("make_instrumental")),
                mv=str(payload.get("mv") or ""),
                created_at=previous.created_at if previous else now,
                updated_at=now,
                clips=previous.clips if previous else [],
            )
        )
        _prune(entries)
        _write(entries)
    logger.info("generate_cache 记账 submitted: fp=%s task=%s", fp, task_id)


def mark_ready(fp: str, clips: List[ClipRef]) -> None:
    """片段落盘后兑现票据。只补清单，不覆盖已有的其它片段（双生可能分两次下载）。"""
    with _lock:
        entries = _read()
        entry = _entry_from_raw(fp, entries.get(fp))
        if entry is None:
            logger.warning("generate_cache 兑现时找不到票据: fp=%s", fp)
            return
        known = {clip.filename for clip in entry.clips}
        entry.clips.extend(clip for clip in clips if clip.filename not in known)
        entry.state = STATE_READY
        entry.detail = None
        entry.updated_at = time.time()
        entries[fp] = _raw_from_entry(entry)
        _prune(entries)
        _write(entries)
    logger.info("generate_cache 记账 ready: fp=%s task=%s clips=%d", fp, entry.task_id, len(entry.clips))


def mark_failed(fp: str, detail: str) -> None:
    """只有上游明确判死才写 failed；本地超时/下载失败不要调用这个函数。"""
    with _lock:
        entries = _read()
        entry = _entry_from_raw(fp, entries.get(fp))
        if entry is None:
            return
        entry.state = STATE_FAILED
        entry.detail = detail[:300]
        entry.updated_at = time.time()
        entries[fp] = _raw_from_entry(entry)
        _write(entries)
    logger.info("generate_cache 记账 failed: fp=%s task=%s detail=%s", fp, entry.task_id, detail[:120])


def summary() -> Dict[str, int]:
    """给 /api/health 与验证脚本看账本规模，不含任何歌词内容。"""
    with _lock:
        entries = _read()
    counts = {STATE_SUBMITTED: 0, STATE_READY: 0, STATE_FAILED: 0}
    for raw in entries.values():
        state = str(raw.get("state") or "") if isinstance(raw, dict) else ""
        if state in counts:
            counts[state] += 1
    return {"entries": len(entries), **counts}
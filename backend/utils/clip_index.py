"""clip_id → 本地 WAV 的索引与解析。

在补这个模块之前，「按 clip_id 找文件」其实是做不到的：落盘名 `suno_{时间戳}_{6位}` 里那
6 位取自 `uuid4().hex`（见 api/generate.py 的 stem 生成），是本地随机数，不是 Suno 的 clip_id；
clip_id 只出现在响应头 `X-Suno-Clip-Id` 里，下载完就丢了。
所以这里在片段落盘时把映射写进 data/clip_index.json，历史成品没有索引，只能按文件名或 stem 取。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from utils.audio_processor import STATIC_DIR, _resolve_local_path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_PATH = BASE_DIR / "data" / "clip_index.json"
ASSET_DIRS = ("output", "generated")


def record(clip_id: Optional[str], filename: str, task_id: Optional[str] = None) -> None:
    """记下 clip_id → 文件名。缺 clip_id 或写失败都不该打断生成主流程，所以只留日志。"""
    if not clip_id:
        return
    try:
        index = _read()
        index[clip_id] = {
            "filename": filename,
            "task_id": task_id or "",
            "recorded_at": int(time.time()),
        }
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=str(INDEX_PATH.parent), suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            json.dump(index, handle, ensure_ascii=False, indent=2, sort_keys=True)
        finally:
            handle.close()
        os.replace(handle.name, INDEX_PATH)
    except Exception as exc:  # pragma: no cover - 索引是便利设施，不是主链路
        logger.warning("clip_index 写入失败: clip=%s file=%s err=%s", clip_id, filename, exc)


def _read() -> dict:
    if not INDEX_PATH.is_file():
        return {}
    try:
        with INDEX_PATH.open(encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("clip_index 读取失败，按空索引处理: %s", exc)
        return {}


def _within_static(path: Path) -> Path:
    resolved = path.resolve()
    if STATIC_DIR.resolve() not in resolved.parents:
        raise LookupError(f"资源路径越出静态目录：{path}")
    return resolved


def resolve(clip_id: str) -> Path:
    """把 clip_id 解析成静态目录内的真实 WAV 路径。

    三种写法都接受：真 clip_id（走索引）、文件名或去后缀的 stem、
    以及 `/static/output/...` 这样的本站资源路径。任何一条都不允许走出 STATIC_DIR。
    """
    lookup = clip_id.strip()
    if not lookup:
        raise LookupError("clip_id 为空")

    entry = _read().get(lookup)
    if entry:
        name = Path(str(entry.get("filename", ""))).name
        for directory in ASSET_DIRS:
            candidate = _within_static(STATIC_DIR / directory / name)
            if candidate.is_file():
                return candidate
        raise LookupError(f"索引指向的文件已不在静态目录：{name}")

    if lookup.startswith("/static/"):
        candidate = _within_static(_resolve_local_path(lookup))
        if candidate.is_file():
            return candidate
    else:
        name = lookup if "." in lookup else f"{lookup}.wav"
        for directory in ASSET_DIRS:
            candidate = _within_static(STATIC_DIR / directory / name)
            if candidate.is_file():
                return candidate

    raise LookupError(f"找不到 clip_id={lookup} 对应的本地音频（历史成品请按文件名或 stem 访问）")


def origin_of(clip_id: str) -> dict:
    """给接口回显：这条 id 命中索引还是直接命中文件名。"""
    entry = _read().get(clip_id.strip())
    return {
        "matched": "clip_index" if entry else "filename",
        "task_id": (entry or {}).get("task_id", ""),
        "recorded_at": (entry or {}).get("recorded_at"),
    }

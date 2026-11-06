"""结构分析任务表：把分钟级的整曲推理从「一条挂着的 HTTP 长连接」改成「提交 + 轮询」。

为什么必须有这一层：8 核 CPU 上 demucs 分轨就要 1.0 倍实时（实测 127.887 s 的歌分轨 129.0 s），
整条流水线是它的若干倍，前端不可能拿一个 fetch 等几分钟 —— 连接被代理或浏览器掐断就得重头再来。
任务表让等待变得可恢复：提交拿到 job_id，轮询到 done/failed 为止，页面重挂不影响后台继续算。

三条约束：
- 同一 clip 只算一次：`by_clip` 做去重，重复提交回同一个 job（点两下波形不该跑两遍分轨）；
  只有 failed 的 job 会被重跑，否则一次临时故障就把这条成品永久钉死在兜底段上；
- 同时只跑一个：`_slot` 限流，因为分析服务内部本来就是一条流水线（`_inference_lock`），
  在主后端这边并发只会把请求堆进对端的排队预算里，等着 429 然后降级；
- 状态是有界的：只留最近 `_MAX_JOBS` 条，超了按完成时间淘汰，不随会话无限长。

失败不抛给调用方：与 `GET /api/analyze_structure` 的降级契约一致，failed 也带着
一段兜底区间和 `detail`，前端拿到的一定是合法结构。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi.concurrency import run_in_threadpool

from utils.clip_index import resolve as resolve_clip
from utils.structure_analyzer import (
    analyze_music_structure,
    fallback_segments,
    wav_duration_sec,
)

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# 终态：不再变化，可以从状态里直接读出结果
FINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)

_MAX_JOBS = 64
# 一次整曲推理的最长兜底等待：比任何「按时长给的 HTTP 超时」都宽，
# 任务不该因为本地限流器等待而变成 failed
_SLOT_TIMEOUT_SEC = 3600.0


@dataclass
class AnalyzeJob:
    job_id: str
    clip_id: str
    filename: str
    path: Path
    duration_sec: float
    status: str = STATUS_QUEUED
    segments: List[Dict[str, Any]] = field(default_factory=list)
    detail: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def public(self) -> Dict[str, Any]:
        # 从提交到此刻的墙钟：排队 + 推理都算在内，命名上不含糊
        elapsed = (self.finished_at or time.monotonic()) - self.created_at
        payload: Dict[str, Any] = {
            "job_id": self.job_id,
            "clip_id": self.clip_id,
            "filename": self.filename,
            "duration_sec": round(self.duration_sec, 3),
            "status": self.status,
            "segments": self.segments,
            "segment_count": len(self.segments),
            "degraded": self.status == STATUS_FAILED,
            "elapsed_sec": round(elapsed, 1),
            "final": self.status in FINAL_STATUSES,
        }
        if self.started_at is not None and self.finished_at is not None:
            payload["analyze_ms"] = int((self.finished_at - self.started_at) * 1000)
        if self.detail:
            payload["detail"] = self.detail
        return payload


class JobTable:
    def __init__(self) -> None:
        self._jobs: Dict[str, AnalyzeJob] = {}
        self._by_clip: Dict[str, str] = {}
        self._slot = asyncio.Semaphore(1)
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, clip_id: str) -> Tuple[AnalyzeJob, bool]:
        """解析音频路径并排队，返回 (job, 是否复用既有任务)。

        路径不合法在这里就抛（LookupError / AudioSourceError），
        让提交请求当场给出 404/400，而不是让一个永远不会完成的 job 挂在表里。
        """
        existing = self._jobs.get(self._by_clip.get(clip_id, ""))
        # failed 不进复用表：降级是一次分析服务的临时故障（对端重启、队列超时），
        # 复用只会让前端永远读到同一段兜底区间，再也没有重试的机会。
        if existing and existing.status != STATUS_FAILED:
            return existing, True

        # resolve() 已经保证路径在静态目录内且真实存在，这里只补时长与落盘名
        path = resolve_clip(clip_id)
        job = AnalyzeJob(
            job_id=uuid.uuid4().hex[:12],
            clip_id=clip_id,
            filename=path.name,
            path=path,
            duration_sec=wav_duration_sec(path),
        )
        self._jobs[job.job_id] = job
        self._by_clip[clip_id] = job.job_id
        self._prune()

        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job, False

    def get(self, job_id: str) -> Optional[AnalyzeJob]:
        return self._jobs.get(job_id)

    async def _run(self, job: AnalyzeJob) -> None:
        try:
            await asyncio.wait_for(self._slot.acquire(), timeout=_SLOT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            job.status = STATUS_FAILED
            job.detail = f"本地分析槽位等待 {int(_SLOT_TIMEOUT_SEC)}s 仍被占用"
            job.segments = fallback_segments(job.duration_sec)
            job.finished_at = time.monotonic()
            logger.warning("analyze job 槽位超时: job=%s clip=%s", job.job_id, job.clip_id)
            return

        job.status = STATUS_RUNNING
        job.started_at = time.monotonic()
        try:
            job.segments = await run_in_threadpool(analyze_music_structure, str(job.path))
            job.status = STATUS_DONE
        except Exception as exc:  # noqa: BLE001 —— 契约要求把失败收敛成兜底段，不外抛
            logger.exception("analyze job 失败: job=%s clip=%s", job.job_id, job.clip_id)
            job.status = STATUS_FAILED
            job.detail = f"{type(exc).__name__}: {exc}"[:300]
            job.segments = fallback_segments(job.duration_sec)
        finally:
            job.finished_at = time.monotonic()
            self._slot.release()
            logger.info(
                "analyze job 完成: job=%s clip=%s status=%s segments=%d analyze_ms=%d",
                job.job_id,
                job.clip_id,
                job.status,
                len(job.segments),
                int((job.finished_at - (job.started_at or job.finished_at)) * 1000),
            )

    def _prune(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        finished = [j for j in self._jobs.values() if j.status in FINAL_STATUSES]
        finished.sort(key=lambda j: j.finished_at or 0.0)
        for job in finished[: len(self._jobs) - _MAX_JOBS]:
            self._jobs.pop(job.job_id, None)
            if self._by_clip.get(job.clip_id) == job.job_id:
                self._by_clip.pop(job.clip_id, None)


_table: Optional[JobTable] = None


def jobs() -> JobTable:
    global _table
    if _table is None:
        _table = JobTable()
    return _table

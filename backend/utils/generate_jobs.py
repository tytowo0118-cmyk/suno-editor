"""生成任务表：把「一条挂几分钟的扣费请求」改成「提交 + 轮询」，并挡住重复计费。

与 `utils/analyze_jobs.py` 同一套契约（提交拿 job_id、轮询到终态、有界表、failed 不复用），
但这里的去重键不是 clip_id 而是**上游载荷指纹**，因为去重的目的不只是省算力，更是省钱：
同一段歌词 + 曲风连点两下、刷新后重挂、后端 `--reload` 重启，都不该变成第二张账单。

三层防线各管一件事：

- 任务表 `by_fp`：同一指纹在**本进程内**只跑一个 job，并发提交回同一个 job_id；
  终态不复用，done 之后重新提交会走账本命中拿到 `source=cache`，如实说明这次没花钱；
- 落盘账本 `utils/generate_cache.py`：跨进程记住「已经扣过费的 task_id」，
  重启后同指纹先免费重查，而不是重新建单；
- `run_generation` 的记账时机：`_submit` 一返回就落盘，中间不插可失败的 await。

进度是增量合并的：`run_generation` 每到一个阶段就回调一个 dict（phase / upstream_status /
poll_ticks / lost_ticks / clip_index …），这里并进 `job.progress`，前端按字段取用即可。
轮询作废的 tick 也会上报 —— 「上游十几秒问不到」和「上游真的失败了」必须让用户分辨得出来。

本地限流 `_MAX_CONCURRENT=3`：实测网关忙时并发建单会在接入队列里排队、握手 10 s 超时
（README §2.3），放开并发只会把失败率抬上去。槽位等待不算失败，等不到才 failed。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from api.generate import MAX_CLIPS_PER_TASK, GenerationResult, build_payload, run_generation
from utils import generate_cache
from utils.generate_cache import ClipRef

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
FINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)

PHASE_QUEUED = "queued"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

_MAX_JOBS = 64
_MAX_CONCURRENT = 3
# 槽位等待预算：比单条任务的 SUNO_TIMEOUT（默认 300 s）宽得多，
# 排队本身不该把一条还没花钱的请求判死
_SLOT_TIMEOUT_SEC = 900.0


@dataclass
class GenerateJob:
    job_id: str
    fingerprint: str
    prompt_chars: int
    wanted_clips: int
    status: str = STATUS_QUEUED
    phase: str = PHASE_QUEUED
    task_id: Optional[str] = None
    source: Optional[str] = None
    clips: List[ClipRef] = field(default_factory=list)
    progress: Dict[str, Any] = field(default_factory=dict)
    detail: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def public(self) -> Dict[str, Any]:
        elapsed = (self.finished_at or time.monotonic()) - self.created_at
        payload: Dict[str, Any] = {
            "job_id": self.job_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "phase": self.phase,
            "final": self.status in FINAL_STATUSES,
            "elapsed_sec": round(elapsed, 1),
            "prompt_chars": self.prompt_chars,
            "wanted_clips": self.wanted_clips,
            "clips": [clip.public() for clip in self.clips],
            "clip_count": len(self.clips),
            "progress": dict(self.progress),
            # 没建单（命中账本或已落盘）时为 True，前端据此说明「未再次计费」
            "reused": bool(self.source and self.source != "submit"),
        }
        if self.task_id:
            payload["task_id"] = self.task_id
        if self.source:
            payload["source"] = self.source
        if self.started_at is not None and self.finished_at is not None:
            payload["generate_ms"] = int((self.finished_at - self.started_at) * 1000)
        if self.detail:
            payload["detail"] = self.detail
        return payload


class GenerateJobTable:
    def __init__(self) -> None:
        self._jobs: Dict[str, GenerateJob] = {}
        self._by_fp: Dict[str, str] = {}
        self._slot = asyncio.Semaphore(_MAX_CONCURRENT)
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(
        self,
        prompt: str,
        *,
        continue_clip_id: Optional[str] = None,
        continue_at: Optional[float] = None,
        max_clips: int = 1,
    ) -> Tuple[GenerateJob, bool]:
        """按载荷指纹排队，返回 (job, 是否复用既有任务)。

        指纹在这里就算出来（`build_payload` 是纯函数、不建单），所以「点两下只花一次钱」
        在提交请求这一层就成立，不必等到后台真去建单。
        """
        payload = build_payload(prompt, continue_clip_id, continue_at)
        fp = generate_cache.fingerprint(payload)
        wanted = min(max(1, max_clips), MAX_CLIPS_PER_TASK)

        existing = self._jobs.get(self._by_fp.get(fp, ""))
        # 只复用在跑的任务：并发点两下必须落到同一个 job，否则就是两张账单。
        # 终态不复用 —— done 重新提交会走账本命中拿到 source=cache（如实说明「这次没花钱」），
        # 直接回旧 job 会让前端读到当初那次的 source=submit，把免费复用误报成又扣了一次。
        # failed 也不复用：可能是上游判死（账本同为 failed，允许再付费），
        # 也可能是本地超时（账本仍是 submitted，重跑免费重查），两种都该给重试机会。
        if existing and existing.status in (STATUS_QUEUED, STATUS_RUNNING):
            return existing, True

        job = GenerateJob(
            job_id=uuid.uuid4().hex[:12],
            fingerprint=fp,
            prompt_chars=len(prompt),
            wanted_clips=wanted,
        )
        self._jobs[job.job_id] = job
        self._by_fp[fp] = job.job_id
        self._prune()

        task = asyncio.create_task(
            self._run(job, prompt, continue_clip_id, continue_at, wanted)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job, False

    def get(self, job_id: str) -> Optional[GenerateJob]:
        return self._jobs.get(job_id)

    def _on_progress(self, job: GenerateJob, update: Dict[str, Any]) -> None:
        # phase / source / task_id 提升成 job 的一级字段，其余原样合并；
        # None 也要合并进去 —— `last_tick_lost=None` 正是「这一拍问到了，清掉上一条丢拍说明」
        job.progress.update(
            {key: value for key, value in update.items() if key not in ("phase", "source", "task_id")}
        )
        phase = update.get("phase")
        if isinstance(phase, str):
            job.phase = phase
        task_id = update.get("task_id")
        if isinstance(task_id, str) and task_id:
            job.task_id = task_id
        source = update.get("source")
        if isinstance(source, str) and source:
            job.source = source

    async def _run(
        self,
        job: GenerateJob,
        prompt: str,
        continue_clip_id: Optional[str],
        continue_at: Optional[float],
        wanted: int,
    ) -> None:
        try:
            await asyncio.wait_for(self._slot.acquire(), timeout=_SLOT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            job.status = STATUS_FAILED
            job.phase = PHASE_FAILED
            job.detail = f"本地生成槽位等待 {int(_SLOT_TIMEOUT_SEC)}s 仍被占用（尚未建单，未扣费）"
            job.finished_at = time.monotonic()
            logger.warning("generate job 槽位超时: job=%s fp=%s", job.job_id, job.fingerprint)
            return

        job.status = STATUS_RUNNING
        job.started_at = time.monotonic()
        try:
            result: GenerationResult = await run_generation(
                prompt,
                continue_clip_id=continue_clip_id,
                continue_at=continue_at,
                max_clips=wanted,
                on_progress=lambda update: self._on_progress(job, update),
            )
            job.clips = result.clips
            job.task_id = result.task_id
            job.source = result.source
            job.phase = PHASE_DONE
            job.status = STATUS_DONE if result.clips else STATUS_FAILED
            if not result.clips:
                job.detail = f"任务完成但没有落盘任何音频：task_id={result.task_id}"
        except HTTPException as exc:
            job.status = STATUS_FAILED
            job.phase = PHASE_FAILED
            job.detail = str(exc.detail)[:300]
            logger.warning(
                "generate job 失败: job=%s fp=%s task=%s detail=%s",
                job.job_id,
                job.fingerprint,
                job.task_id or "-",
                job.detail,
            )
        except Exception as exc:  # noqa: BLE001 —— 任务表不外抛，失败收敛成终态供轮询读取
            logger.exception("generate job 异常: job=%s fp=%s", job.job_id, job.fingerprint)
            job.status = STATUS_FAILED
            job.phase = PHASE_FAILED
            job.detail = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            job.finished_at = time.monotonic()
            self._slot.release()
            logger.info(
                "generate job 完成: job=%s fp=%s status=%s source=%s clips=%d poll_ticks=%s "
                "lost_ticks=%s generate_ms=%d",
                job.job_id,
                job.fingerprint,
                job.status,
                job.source or "-",
                len(job.clips),
                job.progress.get("poll_ticks", 0),
                job.progress.get("lost_ticks", 0),
                int((job.finished_at - (job.started_at or job.finished_at)) * 1000),
            )

    def _prune(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        finished = [job for job in self._jobs.values() if job.status in FINAL_STATUSES]
        finished.sort(key=lambda job: job.finished_at or 0.0)
        for job in finished[: len(self._jobs) - _MAX_JOBS]:
            self._jobs.pop(job.job_id, None)
            if self._by_fp.get(job.fingerprint) == job.job_id:
                self._by_fp.pop(job.fingerprint, None)


_table: Optional[GenerateJobTable] = None


def jobs() -> GenerateJobTable:
    global _table
    if _table is None:
        _table = GenerateJobTable()
    return _table
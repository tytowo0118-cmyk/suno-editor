"""生成任务式入口：提交拿 job_id，轮询读阶段进度，直到 done / failed。

为什么不用一条挂着的长请求：一次 Suno 生成实测是几十秒到几分钟，期间代理还会随机掐 TLS；
长连接被掐断时任务已经扣费，前端却只能重头再来 —— 改造前那就是再花一次钱。
任务式入口把「等待」变成可恢复的状态读取，配合落盘账本（`utils/generate_cache.py`）
让刷新、重启、重试都不再重复计费。

契约与 `POST /api/structure/jobs` 一致，前端可以复用同一套轮询代码：

- `POST /api/generate/jobs` → 202（新建）或 200（同指纹复用既有 job），body 是 job 状态；
- `GET /api/generate/jobs/{job_id}` → 200 状态，404 表示任务表随后端重启丢了
  （账本没丢：重新提交同指纹会免费重查已扣费的那张票据，不会再建单）。

`source` 字段是计费语义的唯一权威：`submit` 本次建单扣费，`refetch` 只做了免费重查，
`cache` 连上游都没请求。前端不要靠耗时去猜有没有花钱。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.generate import MAX_CLIPS_PER_TASK, validate_continue
from utils import generate_jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/generate", tags=["generation"])


class GenerateJobRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    continue_clip_id: str = Field(default="", max_length=64)
    continue_at: Optional[float] = None
    max_clips: int = Field(default=1, ge=1, le=MAX_CLIPS_PER_TASK)


@router.post("/jobs")
async def submit_generate_job(payload: GenerateJobRequest) -> JSONResponse:
    """排队一次生成。同指纹的在跑任务直接复用，不会并发建两张单。"""
    clip_id = validate_continue(payload.continue_clip_id, payload.continue_at)
    job, reused = await generate_jobs.jobs().submit(
        payload.prompt,
        continue_clip_id=clip_id or None,
        continue_at=payload.continue_at,
        max_clips=payload.max_clips,
    )
    logger.info(
        "generate job submitted: job=%s fp=%s reused=%s chars=%d max_clips=%d",
        job.job_id,
        job.fingerprint,
        reused,
        job.prompt_chars,
        job.wanted_clips,
    )
    return JSONResponse(status_code=200 if reused else 202, content=job.public())


@router.get("/jobs/{job_id}")
def read_generate_job(job_id: str) -> Dict[str, Any]:
    job = generate_jobs.jobs().get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="没有这个生成任务（后端可能已重启）；重新提交同样内容会免费重查已扣费的任务",
        )
    return job.public()
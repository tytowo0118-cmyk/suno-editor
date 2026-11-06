"""FastAPI 入口。

启动：cd backend && uvicorn main:app --reload --port 8000
文档：http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.assets import router as assets_router
from api.generate import router as generate_router
from api.lyrics import router as lyrics_router
from api.stitch import router as stitch_router
from api.structure import router as structure_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "output").mkdir(exist_ok=True)


app = FastAPI(
    title="MuseBlocks API",
    description="Web 端模块化 AI 音乐生成系统 — 后端服务（Suno 代理）",
    version="0.2.0",
)

# MVP 阶段允许所有来源；生产环境应收紧到前端域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # 不声明的话浏览器读不到这三个头，续写与双生片段挂载会静默失效
    expose_headers=["X-Suno-Task-Id", "X-Suno-Clip-Id", "X-Suno-Extra-Clips"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(assets_router)
app.include_router(generate_router)
app.include_router(lyrics_router)
app.include_router(stitch_router)
app.include_router(structure_router)


@app.get("/")
def index() -> dict:
    return {
        "service": "MuseBlocks API",
        "version": "0.3.0",
        "docs": "/docs",
        "provider": "suno-proxy",
    }


@app.get("/api/health")
def health() -> dict:
    llm_base = os.environ.get("LLM_API_BASE", "").strip()
    llm_key = (
        os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("SUNO_API_KEY", "").strip()
    )
    return {
        "status": "ok",
        "provider": "suno-proxy",
        "suno_configured": bool(os.environ.get("SUNO_API_KEY", "").strip()),
        "llm_configured": bool(llm_base and llm_key),
        "llm_model": os.environ.get("LLM_MODEL", "").strip() or "gpt-4o-mini",
        "sample_rate": 44_100,
    }


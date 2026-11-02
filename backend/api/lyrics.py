"""歌词生成接口。

默认调用 OpenAI 兼容的 `/chat/completions`（`LLM_API_BASE` + `LLM_API_KEY`），
要求模型输出带 [Section] 标签的歌词；未配置 LLM 或返回格式不可用时回退到本地模板，
保证没有密钥也能演示。标签与 `frontend/src/lib/lyrics.ts` 的解析器约定一致。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["lyrics"])

MAX_LYRICS_CHARS = 6000
DEFAULT_LLM_MODEL = "gpt-4o-mini"
ALLOWED_TAG = re.compile(
    r"^\[(?:intro|verse|pre-chorus|prechorus|chorus|bridge|outro)(?:\s*\d+)?\]\s*$",
    re.IGNORECASE,
)
TAG_LINE = re.compile(r"^\[[^\]\n]{1,24}\]\s*$", re.MULTILINE)

SYSTEM_PROMPT = """你是资深华语流行歌曲作词人。根据用户给出的歌曲提示词创作一首完整歌词。
要求：
1. 只输出歌词本身：不要解释、不要标题、不要 Markdown 代码块、不要编号。
2. 结构标签必须独占一行，且只能使用这些标签：[Intro]、[Verse 1]、[Chorus]、[Verse 2]、[Chorus]、[Bridge]、[Outro]。
3. [Intro] 与 [Outro] 可以留空；其余每段写 4 行，行与行之间不要空行。
4. 语言与提示词一致：中文提示词写中文，英文提示词写英文；中文每行 8–14 字。
5. 副歌承担记忆点，可复用；整体情绪要有起伏，意象具体，避免空洞口号。"""


class LyricsRequest(BaseModel):
    prompt: str = Field(default="", max_length=500)
    seed: Optional[int] = Field(default=None, ge=0, le=2**31 - 1)


class LyricsResponse(BaseModel):
    lyrics: str
    provider: str
    seed: int
    note: str


@dataclass(frozen=True)
class Theme:
    verses: tuple[str, ...]
    choruses: tuple[str, ...]
    bridges: tuple[str, ...]


THEMES: dict[str, Theme] = {
    "night": Theme(
        verses=(
            "街灯把影子拉得很长\n风带走指缝里的慌张\n我把没说完的那句话\n折进旧外套的口袋中央",
            "末班车驶过空的站台\n玻璃上呵出一小片白\n城市在耳边慢慢安静\n只剩心跳替我们告白",
            "把耳机调到最轻的音量\n让旋律替我记住方向\n走过的路不必都发光\n只要有一盏为我点亮",
        ),
        choruses=(
            "如果还有一束光\n就让它落在肩上\n我们在人海里奔忙\n也值得被好好收藏",
            "让我再唱一遍\n唱到眼泪变甜\n就算世界很吵很倦\n我们也要走到天亮之前",
        ),
        bridges=(
            "慢一点 再慢一点\n把此刻留在和弦里面\n慢一点 再慢一点\n让心跳落回原点",
        ),
    ),
    "sea": Theme(
        verses=(
            "海风从很远的岛屿吹来\n带着咸味和没说完的对白\n我把名字写进湿的沙滩\n等一次潮汐把它更改",
            "灯塔把夜色切成两半\n一半是归途 一半是远帆\n我们坐在旧船的甲板\n听浪把遗憾轻轻拍散",
            "把脚印留在退潮的线\n让月光替我保管从前\n如果明天就要说再见\n也请记得今晚的航线",
        ),
        choruses=(
            "向海的那一边\n把心事都交给浪尖\n无论走得多远\n总有一片蓝为我们搁浅",
            "让风吹过耳边\n带走所有不安的预言\n等天亮以前\n我们会找到回岸的航线",
        ),
        bridges=(
            "潮起又潮落\n谁在岸边轻轻和\n把昨夜的沉默\n唱成明天的辽阔",
        ),
    ),
    "default": Theme(
        verses=(
            "从前的我们说好不回头\n后来才懂得偶尔停留\n把所有没寄出的问候\n写进这一夜的风里头",
            "行囊里装着几封旧信\n每一封都写着别担心\n列车穿过沉睡的城\n我在窗边练习安静",
            "路灯一盏盏向后退去\n像我们来不及的相遇\n别怕前方依然有雾\n脚印会替我指出路",
        ),
        choruses=(
            "愿我们都被温柔以待\n愿所有等待不算空白\n当夜风穿过窗台\n新的故事刚刚打开",
            "向着光的方向走\n把昨天的影子留在身后\n就算世界偶尔失手\n我们还有歌唱的自由",
        ),
        bridges=(
            "停一停 别急着重来\n听心跳把答案念出来\n停一停 别急着离开\n今晚的星还亮着等待",
        ),
    ),
}

ENGLISH_THEMES: dict[str, Theme] = {
    "night": Theme(
        verses=(
            "Streetlights stretch our shadows long\nThe wind steals the nervous from my song\nI fold the words I never said\nAnd tuck them in this old coat instead",
            "The last train pulls through empty halls\nA little white breath on the glass walls\nThe city lowers down its voice\nOnly our heartbeats make the choice",
            "I turn the volume down so low\nLet the melody show me where to go\nNot every road has to glow\nJust one small light to call it home",
        ),
        choruses=(
            "If there's a light left anywhere\nLet it rest upon your shoulder\nWe run through the crowd and the cold night air\nAnd we are worth being held closer",
            "Let me sing it one more time\nTill the tears start tasting sweet\nEven when the world is tired and loud\nWe will dance until the morning comes around",
        ),
        bridges=(
            "Slow down, a little slower now\nLeave this moment in the sound\nSlow down, a little slower now\nLet our heartbeat come back around",
        ),
    ),
    "sea": Theme(
        verses=(
            "The sea wind travels from far away\nWith salt and words we could not say\nI write my name on the wet sand\nAnd let the tide take it from my hand",
            "The lighthouse cuts the night in two\nHalf is home and half is you\nWe sit upon the old boat's deck\nAnd let the waves undo the wreck",
            "Leave our footprints on the fading line\nLet the moonlight keep what we left behind\nIf tomorrow means goodbye\nRemember how we learned to fly",
        ),
        choruses=(
            "Out where the ocean ends\nGive your heart to the waves, my friend\nNo matter how far we go\nThere's a blue that lets us start again",
            "Let the wind pass by your ear\nTake away the things you fear\nBefore the morning light is near\nWe'll find the shoreline clear",
        ),
        bridges=(
            "The tide comes and goes\nSomeone hums where the water flows\nTurn the silence of last night\nInto a morning wide and bright",
        ),
    ),
    "default": Theme(
        verses=(
            "We promised not to look behind\nNow we know we need to pause sometimes\nEvery letter left unsent\nI write into the wind instead",
            "Old letters folded in my bag\nEach one says that I should not be sad\nThe train moves through the sleeping town\nI practice being calm and sound",
            "Streetlights slide away in line\nLike the chances we ran out of time\nDon't fear the fog ahead\nThe footprints know the way we tread",
        ),
        choruses=(
            "May we all be treated kind\nMay no waiting be left behind\nWhen the night wind finds the sill\nA brand new story starts to fill",
            "Walk into the light\nLeave yesterday behind tonight\nEven if the world slips up\nWe still own the freedom of our song",
        ),
        bridges=(
            "Pause, don't hurry to restart\nListen to the answer in your heart\nPause, don't rush to leave\nThe stars are still awake for us",
        ),
    ),
}

THEME_KEYWORDS = {
    "night": ("夜", "城市", "街", "灯", "霓虹", "night", "city", "street", "neon", "midnight"),
    "sea": ("海", "浪", "潮汐", "岛屿", "沙滩", "sea", "ocean", "wave", "tide", "shore", "sail"),
}


def _detect_language(prompt: str) -> str:
    cjk = sum(1 for char in prompt if "\u4e00" <= char <= "\u9fff")
    latin = sum(1 for char in prompt if char.isascii() and char.isalpha())
    # 中文提示词常混入少量英文风格词，以汉字占比判断更稳妥
    return "zh" if cjk >= max(1, latin // 2) else "en"


def _detect_theme(prompt: str) -> str:
    lowered = prompt.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return theme
    return "default"


def _compose_lyrics(prompt: str, seed: int) -> str:
    rng = random.Random(seed)
    language = _detect_language(prompt)
    pool = THEMES if language == "zh" else ENGLISH_THEMES
    theme = pool[_detect_theme(prompt)]

    verses = rng.sample(theme.verses, k=min(2, len(theme.verses)))
    chorus = rng.choice(theme.choruses)
    bridge = rng.choice(theme.bridges)

    blocks = [
        "[Intro]",
        f"[Verse 1]\n{verses[0]}",
        f"[Chorus]\n{chorus}",
        f"[Verse 2]\n{verses[1]}",
        f"[Chorus]\n{chorus}",
        f"[Bridge]\n{bridge}",
        "[Outro]",
    ]
    lyrics = "\n\n".join(blocks)
    return lyrics[:MAX_LYRICS_CHARS]


def _llm_config() -> Optional[tuple[str, str, str]]:
    base = os.environ.get("LLM_API_BASE", "").strip().rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("SUNO_API_KEY", "").strip()
    if not base or not key:
        return None
    model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL
    return base, key, model


def _upstream_detail(text: str, status: int) -> str:
    raw = text.strip()
    if not raw:
        return f"HTTP {status}"
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:200]
    if isinstance(body, dict):
        message = body.get("message") or body.get("msg") or body.get("detail")
        if not message and isinstance(body.get("error"), dict):
            message = body["error"].get("message")
        if message:
            return str(message)[:200]
    return raw[:200]


def _clean_llm_output(text: str) -> str:
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(
        r"^\[(?:intro|verse|pre-chorus|prechorus|chorus|bridge|outro)",
        cleaned,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        cleaned = cleaned[match.start() :]
    return cleaned.strip()[:MAX_LYRICS_CHARS]


def _lyrics_valid(text: str) -> bool:
    tags = [line.strip() for line in text.splitlines() if TAG_LINE.match(line)]
    if len(tags) < 2 or any(not ALLOWED_TAG.match(tag) for tag in tags):
        return False
    if not TAG_LINE.sub("", text).strip():
        return False
    return bool(
        re.search(r"^\[verse", text, re.IGNORECASE | re.MULTILINE)
        and re.search(r"^\[chorus", text, re.IGNORECASE | re.MULTILINE)
    )


async def _request_llm(prompt: str, seed: int) -> tuple[str, Optional[str]]:
    config = _llm_config()
    if config is None:
        return "", None
    base, key, model = config

    song_prompt = prompt.strip() or "写一首关于城市夜晚与重新出发的中文流行歌曲"
    if _detect_language(song_prompt) == "en":
        language = "英文（只输出英文歌词，不要出现中文）"
    else:
        language = "中文（只输出简体中文歌词）"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"歌曲提示词：{song_prompt}\n歌词语言：{language}\n创作种子：{seed}",
            },
        ],
        "temperature": 0.9,
        "max_tokens": 1200,
    }
    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接 LLM 服务：{exc}") from exc

    if response.status_code >= 400:
        detail = _upstream_detail(response.text, response.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"LLM 调用失败（HTTP {response.status_code}）：{detail}",
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="LLM 返回的不是 JSON") from exc

    choices = body.get("choices") if isinstance(body, dict) else None
    content = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            content = message["content"]

    lyrics = _clean_llm_output(content)
    if not lyrics:
        raise HTTPException(status_code=502, detail="LLM 返回了空歌词")
    if not _lyrics_valid(lyrics):
        raise HTTPException(
            status_code=502,
            detail="LLM 返回的歌词缺少可识别的 [Verse] / [Chorus] 标签",
        )
    return lyrics, model


@router.post("/lyrics", response_model=LyricsResponse)
async def generate_lyrics(payload: LyricsRequest) -> LyricsResponse:
    seed = payload.seed if payload.seed is not None else random.SystemRandom().randrange(2**31)

    lyrics, model = await _request_llm(payload.prompt, seed)
    if model:
        logger.info("lyrics generated by %s", model)
        return LyricsResponse(
            lyrics=lyrics,
            provider=f"llm:{model}",
            seed=seed,
            note=f"由 {model} 通过 /chat/completions 实时生成；结构标签与前端解析器一致。",
        )

    logger.info("lyrics generated by local-template (LLM not configured)")
    return LyricsResponse(
        lyrics=_compose_lyrics(payload.prompt, seed),
        provider="local-template",
        seed=seed,
        note=(
            "未配置 LLM_API_BASE / LLM_API_KEY，已回退本地模板；"
            "配置后自动改为真实 LLM 写词。"
        ),
    )

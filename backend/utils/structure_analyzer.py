"""深度学习音乐结构分析（All-In-One / allin1）。

精度契约（先读这段再用它的数据）：模型在 100 FPS 的帧网格上工作
（config.py：sample_rate=44100、hop_size=441 → 0.01 s 一格），输出的 start/end 能报到毫秒，
但那是**量化分辨率**，不是**定位精度** —— 边界定位误差是 ±0.1~0.5 s 量级。
拼接时算出的 `segment_start_sec` 才是 clip 级毫秒真值，两者不能互换。
它不可替代的价值是语义标签（intro/verse/chorus/...），那是谱形状检测器原理上给不出的。

两种角色共用这一个模块：
- analyzer 容器内（无 STRUCTURE_ANALYZER_URL）→ `_analyze_local` 直接跑 allin1；
- 主后端容器内（有 STRUCTURE_ANALYZER_URL）→ `_analyze_remote` 走 HTTP，主容器不需要 torch。
"""

from __future__ import annotations

import json
import logging
import os
import time
import wave
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)

# allin1.HARMONIX_LABELS 里的 inst 读起来别扭，映射成项目用语
LABEL_MAP = {
    "intro": "intro",
    "outro": "outro",
    "break": "break",
    "bridge": "bridge",
    "inst": "instrumental",
    "solo": "solo",
    "verse": "verse",
    "chorus": "chorus",
}
DEFAULT_MODEL = "harmonix-fold0"
# 短于这个长度的段对「物理裁切」没有操作意义，并回前一段
MIN_SEGMENT_SEC = 2.0


class StructureAnalysisError(RuntimeError):
    """模型不可用 / 推理失败 —— 由路由层降级成单段兜底，绝不静默返回假结果当真的。"""


def model_name() -> str:
    return os.getenv("AIO_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _analyzer_url() -> str:
    return os.getenv("STRUCTURE_ANALYZER_URL", "").strip().rstrip("/")


def _sec_per_audio_sec() -> float:
    return float(os.getenv("STRUCTURE_ANALYZER_SEC_PER_SEC", "3"))


# 分析容器的排队预算倍率：排在后面的人付的是「前面那条歌的时长」，
# 实测整曲 1.05~1.10× 实时，1.5× 够排掉一条同长度的分析。
QUEUE_SEC_PER_AUDIO_SEC = 1.5


def queue_budget_sec(duration_sec: float) -> float:
    """分析容器愿意让一次请求排队多久（服务端与调用方共用这一个公式，别各写一份）。"""
    floor = float(os.getenv("AIO_QUEUE_TIMEOUT", "900"))
    return max(floor, QUEUE_SEC_PER_AUDIO_SEC * max(duration_sec, 0.0))


def _analyzer_timeout(duration_sec: float = 0.0) -> float:
    """HTTP 超时按音频时长给，不钉死一个数。

    整曲推理的墙钟近似与时长成正比，钉死一个数的后果是「五分钟的歌一定降级」，
    而这恰好是最需要分段的那种素材。倍率由 `scripts/bench_structure_threads.py` 量出来：
    8 核容器、`OMP_NUM_THREADS=4` + BLAS/MKL=1 时，127.887 s 音频的整条流水线跑 134.6 s，
    即 **1.05× 实时**（其中 demucs 分轨 126.6 s，谱特征 + DiNAT 推理 + 后处理只占 8.0 s；
    3.4 另有 491.883 / 157.843 / 587.165 s 三条冷跑的 1.09× / 1.10× / 1.02×）。
    分析倍率给 3 而不是 1.05：余量付模型加载、结果卷冷启动和邻居抢核，这几项不随时长缩放。

    上限还要加上**排队**那一段：`STRUCTURE_ANALYZER_TIMEOUT` 只管「我自己跑完」，
    对端那条 `_inference_lock` 排队和自己的时长无关。两者相加才是调用方等得起的总账 ——
    只给 3× 会在对端还合法排队时先掐断（实测 127.887 s 排在 587.165 s 后面，
    383.7 s 报 `ReadTimeout`，前面跑完的分轨白烧）。
    """
    own = max(
        float(os.getenv("STRUCTURE_ANALYZER_TIMEOUT", "180")),
        _sec_per_audio_sec() * max(duration_sec, 0.0),
    )
    return queue_budget_sec(duration_sec) + own


def _work_dir() -> Path:
    # allin1 的结果缓存 / demucs 4 轨 / 谱特征都写在这里（分析容器里挂成卷）
    return Path(os.getenv("STRUCTURE_WORK_DIR", "/tmp/museblocks-structure")).expanduser()


def wav_duration_sec(path: Path) -> float:
    """时长以 WAV 头为准，不信模型或上游回传的数字。"""
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def _iter_bounds(segments: Iterable[Any]) -> List[Tuple[float, float, str]]:
    parsed: List[Tuple[float, float, str]] = []
    for seg in segments:
        # allin1 给的是 Segment dataclass，分析服务的 JSON 给的是 dict。
        # 别写成 getattr(seg, "start", seg["start"])：默认值参数会先求值，
        # dataclass 没有 __getitem__，真实推理结果会在这里抛 TypeError。
        if isinstance(seg, dict):
            start, end, raw_label = float(seg["start"]), float(seg["end"]), str(seg.get("label", ""))
        else:
            start, end, raw_label = float(seg.start), float(seg.end), str(seg.label)
        parsed.append((start, end, LABEL_MAP.get(raw_label.lower(), "section")))
    parsed.sort(key=lambda item: item[0])
    return parsed


def normalize_segments(segments: Iterable[Any], duration: float) -> List[Dict[str, Any]]:
    """模型段 → 可物理裁切的连续段：毫秒取整、无缝无洞、短段并回前一段。

    边界只在「切点」上取整一次，相邻两段共用同一个数，所以
    Σ(end − start) == duration 且 start/end 严格单调，前端不需要再做缝合。
    """
    parsed = _iter_bounds(segments)
    if duration <= 0:
        return []

    kept: List[List[Any]] = []
    for start, end, label in parsed:
        start = min(max(start, 0.0), duration)
        end = min(max(end, start), duration)
        if end - start <= 0:
            continue
        if kept and end - start < MIN_SEGMENT_SEC:
            kept[-1][1] = end  # 并进前一段
            continue
        kept.append([start, end, label])

    # 首段过短没法向前合并，就丢掉它的切点，让第二段从 0 开始覆盖它
    if len(kept) > 1 and kept[0][1] - kept[0][0] < MIN_SEGMENT_SEC:
        kept.pop(0)

    marks = [0.0]
    for _, end, _label in kept:
        if end > marks[-1] + 1e-6:
            marks.append(end)
    if duration > marks[-1] + 1e-6:
        marks.append(duration)

    # 切点取整之后必须仍然严格递增：模型给的最后一个 end 和 WAV 头算出的 duration
    # 只差 0.4 ms 时（127.887 s 的探测混合实测如此，末段 end=127.8866…），
    # 两个切点会 round 成同一个数，塌出一段 start == end 的零长度区间 ——
    # 它物理上裁不出来，还会被前端当成一个「文件末尾的内部边界」。
    rounded: List[float] = []
    for mark in marks:
        value = round(mark, 3)
        if not rounded or value > rounded[-1]:
            rounded.append(value)
    total = round(duration, 3)
    if rounded[-1] < total:
        rounded[-1] = total

    out: List[Dict[str, Any]] = []
    for index, end_mark in enumerate(rounded[1:]):
        label = kept[index][2] if index < len(kept) else "section"
        out.append({"label": label, "start": rounded[index], "end": end_mark})
    return out


def fallback_segments(duration: float) -> List[Dict[str, Any]]:
    """单段兜底：只保证前端 DAG 有合法区间可渲染，绝不冒充分析结果。"""
    return [{"label": "unknown", "start": 0.000, "end": round(max(duration, 0.0), 3)}]


def analyze_music_structure(audio_path: str) -> List[Dict[str, Any]]:
    """分析一个本地 WAV，返回 [{"label","start","end"}]，时间单位秒、精度到毫秒。

    阻塞且 CPU 密集（8 核 CPU 上整曲可达分钟级），调用方请放线程池。
    """
    started = time.perf_counter()
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        raise StructureAnalysisError(f"音频不存在: {audio_path}")
    duration = wav_duration_sec(path)

    segments = _analyze_remote(path, duration) if _analyzer_url() else _analyze_local(path)
    normalized = normalize_segments(segments, duration)
    if not normalized:
        raise StructureAnalysisError("模型没有给出任何可用分段")

    logger.info(
        "structure: %s dur=%.3fs segments=%d elapsed=%.1fs",
        path.name,
        duration,
        len(normalized),
        time.perf_counter() - started,
    )
    return normalized


def _static_relative(path: Path) -> str:
    """两个容器共享 ./backend/static，所以只传相对路径，绝对路径留在各自挂载点里。"""
    from utils.audio_processor import STATIC_DIR

    try:
        return str(path.resolve().relative_to(STATIC_DIR.resolve()))
    except ValueError as exc:
        raise StructureAnalysisError(f"音频不在静态目录内，分析服务读不到: {path}") from exc


def _analyze_local(path: Path) -> List[Any]:
    work = _work_dir()
    results_dir = work / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    cached = results_dir / f"{path.stem}.json"
    overwrite = os.getenv("STRUCTURE_OVERWRITE", "0") == "1"

    # 命中缓存就自己读 JSON，不要把「已有结果」交给 allin1.analyze 的加载分支：
    # 1.1.0 里 `overwrite=False` 且结果已存在时 todo_paths 为空，`if todo_paths:` 整块被跳过，
    # 于是块内才赋值的 demix_paths / spec_paths 根本不存在，收尾的 keep_byproducts 清理
    # 直接抛 `UnboundLocalError: demix_paths`（实测第二次分析同一首曲子必炸，
    # 第一次成功、之后永远失败）。自己读不但绕开这条分支，还省掉 torch 加载。
    if cached.is_file() and not overwrite:
        try:
            with cached.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            segments = list(payload.get("segments", []))
            logger.info("structure 缓存命中: %s segments=%d", cached.name, len(segments))
            return segments
        except Exception as exc:
            logger.warning("结果缓存不可读，改为重算: %s（%s）", cached, exc)
            cached.unlink(missing_ok=True)

    threads = int(os.getenv("AIO_THREADS", "4"))
    try:
        import allin1  # 只有分析容器装得上
        import torch

        torch.set_num_threads(threads)
    except Exception as exc:
        raise StructureAnalysisError(f"分析依赖不可用: {type(exc).__name__}: {exc}") from exc

    if overwrite:
        # 重算前先删掉旧结果：传 overwrite=True 给 allin1 只是让它自己走「全部重算」分支，
        # 但那样 keep_byproducts 与它互斥的坑就回来了；删文件让「缓存未命中」成为唯一路径。
        cached.unlink(missing_ok=True)

    try:
        result = allin1.analyze(
            str(path),
            out_dir=results_dir,
            model=model_name(),
            device=os.getenv("AIO_DEVICE", "cpu"),
            demix_dir=work / "demix",
            spec_dir=work / "spec",
            # 4 轨 float WAV 一首曲子就上百 MB，只留 JSON 结果这一份便宜缓存
            keep_byproducts=False,
            # 缓存的读与删都由上面那段自己做，这里永远给 False
            overwrite=False,
            # 默认 True 会 fork 进程池，容器里跑会失控
            multiprocess=False,
            visualize=False,
            sonify=False,
        )
    except Exception as exc:
        raise StructureAnalysisError(f"{type(exc).__name__}: {exc}") from exc

    if isinstance(result, list):
        result = result[0] if result else None
    if result is None:
        raise StructureAnalysisError("allin1 未返回结果")
    return list(result.segments)


def _analyze_remote(path: Path, duration_sec: float) -> List[Any]:
    import httpx

    # 不传 model：用哪个模型由分析容器自己的 AIO_MODEL 决定，主后端只报它配的名字
    payload = {"path": _static_relative(path)}
    timeout = _analyzer_timeout(duration_sec)
    try:
        # trust_env=False：容器里可能配了出网代理，内部服务地址不能再被代理转一道
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.post(f"{_analyzer_url()}/analyze", json=payload)
    except Exception as exc:
        raise StructureAnalysisError(
            f"分析服务不可达({timeout:.0f}s 超时上限): {type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code != 200:
        raise StructureAnalysisError(
            f"分析服务返回 {response.status_code}: {response.text[:200]}"
        )
    try:
        return list(response.json().get("segments", []))
    except Exception as exc:
        raise StructureAnalysisError(f"分析服务响应无法解析: {exc}") from exc


def backend_info(duration_sec: float = 0.0) -> Dict[str, Any]:
    """给健康检查与日志用，说明现在走的是哪条路径。

    `timeout_sec` 是「这条时长为 duration_sec 的请求实际拿到的上限」，
    没给时长时回的是下限，所以别把它当成全局超时读。
    """
    url = _analyzer_url()
    return {
        "route": "analyzer" if url else "local",
        "analyzer_url": url or None,
        "model": model_name(),
        "timeout_sec": round(_analyzer_timeout(duration_sec), 1),
        "queue_budget_sec": round(queue_budget_sec(duration_sec), 1),
        "sec_per_audio_sec": _sec_per_audio_sec(),
        "work_dir": str(_work_dir()),
    }

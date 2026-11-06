"use client";

import { useEffect, useRef, useState } from "react";
import type WaveSurferType from "wavesurfer.js";
import { formatClock, SNAP_WINDOW_SEC, snapToCandidates } from "@/lib/sections";
import type { StructureCandidate } from "@/lib/types";

/** 同一时刻只保留一个正在播放的实例 */
let activeInstance: WaveSurferType | null = null;

export function pauseAllAudio() {
  activeInstance?.pause();
  activeInstance = null;
}

/**
 * 吸附要等落点停下来再做：dragToSeek 期间 wavesurfer 会连发 interaction，
 * 边拖边 seekTo 会跟用户抢播放头。
 */
const SNAP_SETTLE_MS = 90;

/** 波形横向缩放档位，1 表示不缩放、按容器宽度铺满 */
const ZOOM_FACTORS = [1, 2, 4, 8];

/** 画布宽度会被 wavesurfer 向下取整到 barWidth+barGap = 4px 的整数倍，先把容器对齐到这个网格 */
const BAR_GRID_PX = 4;

interface WaveformPlayerProps {
  src: string;
  playerId: string;
  accent: string;
  waveColor: string;
  progressColor: string;
  durationSec: number;
  /** 用户点击/拖拽波形 seek 后回调，参数为整条音频内的秒数（吸附之后） */
  onPoint?: (timeSec: number) => void;
  /** 播放进度回调；wavesurfer 每帧都发 timeupdate，这里限流到约 20Hz 以免拖垮父组件 */
  onTimeUpdate?: (timeSec: number) => void;
  /** 在波形上标记一个秒数位置（如续写点） */
  markerSec?: number | null;
  /** 结构检测出的吸附候选，传了就在波形上画刻度并把落点吸到最近的候选上 */
  snapPoints?: StructureCandidate[];
  /** 吸附窗口（秒） */
  snapWindowSec?: number;
  /** 实际发生吸附时回调（原始落点, 吸附后落点） */
  onSnap?: (fromSec: number, toSec: number) => void;
  /** 允许横向缩放：波形宽度按档位超出容器并可滚动，播放头自动跟走 */
  zoomable?: boolean;
}

type Status = "loading" | "ready" | "error";

export default function WaveformPlayer({
  src,
  playerId,
  accent,
  waveColor,
  progressColor,
  durationSec,
  onPoint,
  onTimeUpdate,
  markerSec,
  snapPoints = [],
  snapWindowSec = SNAP_WINDOW_SEC,
  onSnap,
  zoomable = false,
}: WaveformPlayerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const waveRef = useRef<WaveSurferType | null>(null);
  const onPointRef = useRef(onPoint);
  const onTimeUpdateRef = useRef(onTimeUpdate);
  const snapPointsRef = useRef(snapPoints);
  const snapWindowRef = useRef(snapWindowSec);
  const onSnapRef = useRef(onSnap);
  const zoomFactorRef = useRef(1);
  const [status, setStatus] = useState<Status>("loading");
  const [currentTime, setCurrentTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [zoomFactor, setZoomFactor] = useState(1);
  const [viewportPx, setViewportPx] = useState(0);

  useEffect(() => {
    onPointRef.current = onPoint;
  }, [onPoint]);

  useEffect(() => {
    onTimeUpdateRef.current = onTimeUpdate;
  }, [onTimeUpdate]);

  // 候选点是异步拿到的，用 ref 传递，避免候选一到就重建整个 wavesurfer 实例
  useEffect(() => {
    snapPointsRef.current = snapPoints;
  }, [snapPoints]);

  useEffect(() => {
    snapWindowRef.current = snapWindowSec;
  }, [snapWindowSec]);

  useEffect(() => {
    onSnapRef.current = onSnap;
  }, [onSnap]);

  useEffect(() => {
    zoomFactorRef.current = zoomFactor;
  }, [zoomFactor]);

  // 缩放档位是「容器宽度的倍数」，所以要先量到可视宽度。ResizeObserver 在 observe 时
  // 会立刻回调一次，不需要再同步读一次 clientWidth（那会踩 set-state-in-effect）。
  useEffect(() => {
    const node = scrollRef.current;
    if (!node || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[entries.length - 1]?.contentRect.width ?? 0;
      setViewportPx(Math.round(width));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let disposed = false;
    let instance: WaveSurferType | null = null;
    let lastReportSec = -1;
    let snapTimer: number | undefined;

    // 父组件的时间轴播放指针只需要约 20Hz，太密会让整棵 Studio 树跟着重渲染
    const report = (time: number) => {
      if (disposed || Math.abs(time - lastReportSec) < 0.05) return;
      lastReportSec = time;
      onTimeUpdateRef.current?.(time);
    };

    // 滚动条归 React 这边，wavesurfer 的 autoScroll/scrollIntoView 只在自己认为
    // 画布可滚动（minPxPerSec 撑破父容器）时才跟走，缩放时它那边是关不掉的，得自己拉。
    const followCursor = (time: number) => {
      const player = waveRef.current;
      const node = scrollRef.current;
      if (!player || !node || zoomFactorRef.current <= 1) return;
      const duration = player.getDuration();
      const widthPx = player.getWidth();
      const viewPx = node.clientWidth;
      if (duration <= 0 || widthPx <= viewPx) return;
      const cursorPx = (time / duration) * widthPx;
      if (cursorPx < node.scrollLeft + 8) node.scrollLeft = Math.max(0, cursorPx - viewPx * 0.2);
      else if (cursorPx > node.scrollLeft + viewPx - 8) node.scrollLeft = cursorPx - viewPx * 0.8;
    };

    void (async () => {
      const { default: WaveSurfer } = await import("wavesurfer.js");
      if (disposed || !containerRef.current) return;

      instance = WaveSurfer.create({
        container: containerRef.current,
        url: src,
        height: 56,
        waveColor,
        progressColor,
        cursorWidth: 0,
        barWidth: 2,
        barGap: 2,
        barRadius: 2,
        normalize: true,
        interact: true,
        dragToSeek: true,
        hideScrollbar: true,
      });
      waveRef.current = instance;

      instance.on("ready", () => {
        if (!disposed) setStatus("ready");
      });
      instance.on("play", () => {
        if (activeInstance && activeInstance !== instance) activeInstance.pause();
        activeInstance = instance;
        if (!disposed) setIsPlaying(true);
      });
      instance.on("timeupdate", (time) => {
        if (!disposed) setCurrentTime(time);
        followCursor(time);
        report(time);
      });
      instance.on("pause", () => {
        if (activeInstance === instance) activeInstance = null;
        if (!disposed) setIsPlaying(false);
        report(instance?.getCurrentTime() ?? 0);
      });
      instance.on("finish", () => {
        instance?.seekTo(0);
        if (activeInstance === instance) activeInstance = null;
        if (!disposed) {
          setCurrentTime(0);
          setIsPlaying(false);
        }
        lastReportSec = 0;
        onTimeUpdateRef.current?.(0);
      });
      instance.on("error", (error) => {
        console.error("WaveSurfer load failed:", error);
        if (!disposed) setStatus("error");
      });
      instance.on("interaction", (time) => {
        if (disposed) return;
        const points = snapPointsRef.current;
        const radius = snapWindowRef.current;
        if (snapTimer !== undefined) window.clearTimeout(snapTimer);
        if (!points.length || radius <= 0) {
          onPointRef.current?.(time);
          return;
        }
        snapTimer = window.setTimeout(() => {
          if (disposed) return;
          const target = snapToCandidates(time, points, radius);
          if (target.snapped && instance) {
            // interaction 只在 ready 之后发，getDuration 一定已经可用
            instance.seekTo(target.sec / instance.getDuration());
            onSnapRef.current?.(time, target.sec);
          }
          onPointRef.current?.(target.sec);
        }, SNAP_SETTLE_MS);
      });
    })();

    return () => {
      disposed = true;
      if (snapTimer !== undefined) window.clearTimeout(snapTimer);
      if (activeInstance === instance) activeInstance = null;
      instance?.destroy();
      if (waveRef.current === instance) waveRef.current = null;
    };
  }, [src, playerId, waveColor, progressColor]);

  const togglePlay = () => {
    const instance = waveRef.current;
    if (!instance || status === "error") return;
    if (instance.isPlaying()) {
      instance.pause();
      return;
    }
    void instance.play().catch((error) => console.error("WaveSurfer play failed:", error));
  };

  // 缩放档位表达成「可视宽度的倍数」，这样窗口变窄时缩放比例不变；宽度对齐到条形网格，
  // 免得 wavesurfer 把画布向下取整后，右边差出几像素让刻度线和播放头错位。
  const contentPx =
    zoomable && zoomFactor > 1 && viewportPx > 0
      ? Math.ceil((viewportPx * zoomFactor) / BAR_GRID_PX) * BAR_GRID_PX
      : 0;
  const pxPerSec = durationSec > 0 ? Math.max(contentPx, viewportPx) / durationSec : 0;

  return (
    <div className="space-y-2.5">
      <div className="relative rounded-xl bg-black/30">
        <div
          ref={scrollRef}
          className="overflow-x-auto overflow-y-hidden rounded-xl [scrollbar-color:rgba(255,255,255,0.16)_transparent] [scrollbar-width:thin]"
        >
          <div
            className="relative min-h-[56px]"
            style={contentPx > 0 ? { width: `${contentPx}px` } : undefined}
          >
            <div ref={containerRef} className="w-full" />
            {status === "ready" &&
              durationSec > 0 &&
              snapPoints.map((candidate) => (
                <div
                  key={`${candidate.source ?? "detector"}-${candidate.sec}`}
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-y-0 w-px"
                  style={{
                    left: `${Math.min(100, (candidate.sec / durationSec) * 100)}%`,
                    // 检测候选按新颖度调透明度；模型边界没有强度这个量，给它自己的颜色，
                    // 两种证据的可靠性不是一个量纲，不能混在同一根灰阶上
                    backgroundColor:
                      candidate.source === "model"
                        ? "rgba(125, 211, 252, 0.72)"
                        : `rgba(255,255,255,${(0.18 + Math.min(0.42, candidate.strength)).toFixed(
                            2,
                          )})`,
                  }}
                />
              ))}
            {markerSec != null && durationSec > 0 && status === "ready" && (
              <div
                aria-hidden="true"
                className="pointer-events-none absolute inset-y-0 w-0.5 bg-white/80 shadow-[0_0_8px_rgba(255,255,255,0.6)]"
                style={{ left: `${Math.min(100, (markerSec / durationSec) * 100)}%` }}
              />
            )}
          </div>
        </div>
        {status === "loading" && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 rounded-xl bg-black/20 text-[11px] text-zinc-500">
            <span className="h-3 w-3 animate-spin rounded-full border border-zinc-600 border-t-zinc-300" />
            加载波形
          </div>
        )}
        {status === "error" && (
          <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-black/40 text-[11px] text-rose-300">
            音频加载失败
          </div>
        )}
      </div>

      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={togglePlay}
            disabled={status !== "ready"}
            aria-label={isPlaying ? "暂停试听" : "播放试听"}
            className="inline-flex h-8 w-8 items-center justify-center rounded-full text-[#08080d] shadow-lg transition enabled:hover:scale-105 disabled:cursor-not-allowed disabled:opacity-40"
            style={{ backgroundColor: accent }}
          >
            {isPlaying ? (
              <svg width="11" height="11" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
                <rect x="2" y="1.5" width="2.6" height="9" rx="0.8" />
                <rect x="7.4" y="1.5" width="2.6" height="9" rx="0.8" />
              </svg>
            ) : (
              <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
                <path d="M3 1.6v8.8c0 .5.55.8.98.54l6.3-4.4a.65.65 0 0 0 0-1.08L3.98 1.06A.65.65 0 0 0 3 1.6Z" />
              </svg>
            )}
          </button>
          {zoomable && viewportPx > 0 && (
            <div className="flex items-center gap-1" role="group" aria-label="波形横向缩放">
              {ZOOM_FACTORS.map((factor) => (
                <button
                  key={factor}
                  type="button"
                  onClick={() => setZoomFactor(factor)}
                  aria-pressed={zoomFactor === factor}
                  className={`h-6 rounded-md px-1.5 font-mono text-[10px] transition ${
                    zoomFactor === factor
                      ? "bg-white/[0.12] text-zinc-200"
                      : "text-zinc-600 hover:bg-white/[0.06] hover:text-zinc-400"
                  }`}
                >
                  {factor === 1 ? "适配" : `×${factor}`}
                </button>
              ))}
            </div>
          )}
        </div>
        <span className="font-mono text-[11px] tabular-nums text-zinc-500">
          {formatClock(currentTime)} / {formatClock(durationSec)}
        </span>
      </div>

      {zoomable && status === "ready" && pxPerSec > 0 && (
        <p className="font-mono text-[10px] leading-relaxed text-zinc-600">
          1px = {(1 / pxPerSec).toFixed(3)}s · ±{snapWindowSec}s 吸附窗 ≈{" "}
          {(2 * snapWindowSec * pxPerSec).toFixed(1)}px
          {zoomFactor > 1 ? " · 波形可横向滚动" : ""}
        </p>
      )}
    </div>
  );
}

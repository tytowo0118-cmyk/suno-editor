"use client";

import { useEffect, useRef, useState } from "react";
import type WaveSurferType from "wavesurfer.js";
import { formatClock } from "@/lib/sections";

/** 同一时刻只保留一个正在播放的实例 */
let activeInstance: WaveSurferType | null = null;

export function pauseAllAudio() {
  activeInstance?.pause();
  activeInstance = null;
}

interface WaveformPlayerProps {
  src: string;
  playerId: string;
  accent: string;
  waveColor: string;
  progressColor: string;
  durationSec: number;
  /** 用户点击/拖拽波形 seek 后回调，参数为整条音频内的秒数 */
  onPoint?: (timeSec: number) => void;
  /** 在波形上标记一个秒数位置（如续写点） */
  markerSec?: number | null;
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
  markerSec,
}: WaveformPlayerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const waveRef = useRef<WaveSurferType | null>(null);
  const onPointRef = useRef(onPoint);
  const [status, setStatus] = useState<Status>("loading");
  const [currentTime, setCurrentTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);

  useEffect(() => {
    onPointRef.current = onPoint;
  }, [onPoint]);

  useEffect(() => {
    let disposed = false;
    let instance: WaveSurferType | null = null;

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
      });
      instance.on("pause", () => {
        if (activeInstance === instance) activeInstance = null;
        if (!disposed) setIsPlaying(false);
      });
      instance.on("finish", () => {
        instance?.seekTo(0);
        if (activeInstance === instance) activeInstance = null;
        if (!disposed) {
          setCurrentTime(0);
          setIsPlaying(false);
        }
      });
      instance.on("error", (error) => {
        console.error("WaveSurfer load failed:", error);
        if (!disposed) setStatus("error");
      });
      instance.on("interaction", (time) => {
        if (!disposed) onPointRef.current?.(time);
      });
    })();

    return () => {
      disposed = true;
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

  return (
    <div className="space-y-2.5">
      <div className="relative min-h-[56px] overflow-hidden rounded-xl bg-black/30">
        <div ref={containerRef} className="w-full" />
        {markerSec != null && durationSec > 0 && status === "ready" && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-y-0 w-0.5 bg-white/80 shadow-[0_0_8px_rgba(255,255,255,0.6)]"
            style={{ left: `${Math.min(100, (markerSec / durationSec) * 100)}%` }}
          />
        )}
        {status === "loading" && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 bg-black/20 text-[11px] text-zinc-500">
            <span className="h-3 w-3 animate-spin rounded-full border border-zinc-600 border-t-zinc-300" />
            加载波形
          </div>
        )}
        {status === "error" && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-[11px] text-rose-300">
            音频加载失败
          </div>
        )}
      </div>

      <div className="flex items-center justify-between gap-3">
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
        <span className="font-mono text-[11px] tabular-nums text-zinc-500">
          {formatClock(currentTime)} / {formatClock(durationSec)}
        </span>
      </div>
    </div>
  );
}

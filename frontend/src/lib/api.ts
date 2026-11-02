import type { StitchResult } from "./types";

const rawBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
export const API_BASE = rawBase.replace(/\/+$/, "");

interface RawAsset {
  audio_url: string;
  filename: string;
}

interface RawStitch {
  audio_url: string;
  filename: string;
  duration: number;
  peak_dbfs: number;
  segment_order: string[];
  segment_start_sec: number[];
  head_trim_sec?: number[];
}

function extractError(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (typeof item === "object" && item && "msg" in item ? String(item.msg) : String(item)))
        .join("；");
    }
  }
  return `请求失败（HTTP ${status}）`;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(extractError(body, response.status));
  }

  return (await response.json()) as T;
}

/**
 * 把浏览器里的 Blob 音频上传到后端落盘，换取可被 /api/stitch 引用的静态地址。
 * 生成的音频在浏览器里只是 Blob，拼接前必须先持久化。
 */
export async function persistAudio(blobUrl: string): Promise<string> {
  const blob = await (await fetch(blobUrl)).blob();
  const response = await fetch(`${API_BASE}/api/assets`, {
    method: "POST",
    headers: { "Content-Type": blob.type || "audio/wav" },
    body: blob,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(extractError(body, response.status));
  }

  const asset = (await response.json()) as RawAsset;
  return asset.audio_url;
}

export interface LyricsDraft {
  lyrics: string;
  provider: string;
  seed: number;
  note: string;
}

/** 按歌曲提示词生成带 [Section] 标签的歌词草稿 */
export async function requestLyrics(prompt: string): Promise<LyricsDraft> {
  return requestJson<LyricsDraft>("/api/lyrics", {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function stitchTimeline(segments: Array<{
  id: string;
  audioUrl: string;
  crossfadeMs: number;
}>): Promise<StitchResult> {
  const data = await requestJson<RawStitch>("/api/stitch", {
    method: "POST",
    body: JSON.stringify({
      output_name: "museblocks-mix",
      segments: segments.map((segment) => ({
        id: segment.id,
        audio_url: segment.audioUrl,
        crossfade_ms: segment.crossfadeMs,
      })),
    }),
  });

  return {
    audioUrl: data.audio_url,
    filename: data.filename,
    duration: data.duration,
    peakDbfs: data.peak_dbfs,
    segmentOrder: data.segment_order,
    segmentStartSec: data.segment_start_sec ?? [],
    headTrimSec: data.head_trim_sec ?? [],
  };
}

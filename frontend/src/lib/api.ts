import type {
  StitchResult,
  StructureCandidate,
  StructureJob,
  StructureJobStatus,
} from "./types";

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
  truncate_to_sec?: (number | null)[];
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
  /** 本段在成品里最多保留的秒数；不传就整段用完。Suno 不吃时长参数，超长只能在这儿兑现 */
  maxDurationSec?: number;
}>): Promise<StitchResult> {
  const data = await requestJson<RawStitch>("/api/stitch", {
    method: "POST",
    body: JSON.stringify({
      output_name: "museblocks-mix",
      segments: segments.map((segment) => ({
        id: segment.id,
        audio_url: segment.audioUrl,
        crossfade_ms: segment.crossfadeMs,
        ...(segment.maxDurationSec ? { max_duration_sec: segment.maxDurationSec } : {}),
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
    truncateToSec: data.truncate_to_sec ?? [],
  };
}

interface RawBoundaries {
  candidates_sec: number[];
  candidate_strengths: number[];
  duration: number;
  compute_ms: number;
}

export interface StructureBoundaries {
  candidates: StructureCandidate[];
  duration: number;
  computeMs: number;
}

/**
 * 分析一条成品音频的内部结构边界，供播放头吸附使用。
 * 后端是本地 numpy，490s 的成品约 1.2s，不需要缓存到全局状态里。
 */
export async function fetchStructureBoundaries(
  audioUrl: string,
): Promise<StructureBoundaries> {
  const data = await requestJson<RawBoundaries>("/api/structure/boundaries", {
    method: "POST",
    body: JSON.stringify({ audio_url: audioUrl }),
  });

  return {
    candidates: data.candidates_sec.map((sec, index) => ({
      sec,
      strength: data.candidate_strengths[index] ?? 0,
    })),
    duration: data.duration,
    computeMs: data.compute_ms,
  };
}

interface RawStructureJob {
  job_id: string;
  clip_id: string;
  filename: string;
  status: StructureJobStatus;
  final: boolean;
  duration_sec: number;
  segments: { label: string; start: number; end: number }[];
  elapsed_sec: number;
  analyze_ms?: number;
  detail?: string;
  reused: boolean;
  model: string;
}

function toJob(data: RawStructureJob): StructureJob {
  return {
    jobId: data.job_id,
    clipId: data.clip_id,
    filename: data.filename,
    status: data.status,
    final: data.final,
    durationSec: data.duration_sec,
    segments: data.segments,
    elapsedSec: data.elapsed_sec,
    analyzeMs: data.analyze_ms,
    detail: data.detail,
    reused: data.reused,
    model: data.model,
  };
}

/**
 * 成品音频的地址 → 后端认得的 clip_id。
 * 落盘名是 `dlp-A_1793845120_suno_179.wav`，去掉目录与 .wav 后就是 clip_index 认识的 stem；
 * 真实 Suno clip id 走的是同一张接口的索引分支，两者在这里不需要区分。
 */
export function clipIdFromAudioUrl(audioUrl: string): string {
  const name = audioUrl.split("/").pop() ?? audioUrl;
  return name.replace(/\.wav$/i, "");
}

async function fetchJob(jobId: string): Promise<StructureJob | null> {
  const response = await fetch(`${API_BASE}/api/structure/jobs/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
  });
  // 404 = 后端重启把内存任务表清了，交给调用方重新提交，不算分析失败
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(extractError(await response.json().catch(() => null), response.status));
  return toJob((await response.json()) as RawStructureJob);
}

export interface RunStructureJobOptions {
  /** 每次状态变化都回调，让 UI 能显示「排队中 / 分析中 / 已出 5 段」 */
  onJob?: (job: StructureJob) => void;
  intervalMs?: number;
  isCancelled?: () => boolean;
}

/**
 * 提交并轮询一次深度学习结构分析，直到终态（done / failed）。
 *
 * 整曲推理在 8 核 CPU 上是分钟级，所以不是一条挂着的请求，而是提交 + 轮询：
 * 期间任何一次轮询失败只当作「这一拍没问到」，由下一次轮询补上；
 * 只有后端明确 404（任务表随重启丢失）才重新提交，且最多补提交一次。
 */
export async function runStructureJob(
  clipId: string,
  options: RunStructureJobOptions = {},
): Promise<StructureJob | null> {
  const intervalMs = options.intervalMs ?? 3000;
  const cancelled = options.isCancelled ?? (() => false);
  let job = toJob(await requestJson<RawStructureJob>("/api/structure/jobs", {
    method: "POST",
    body: JSON.stringify({ clip_id: clipId }),
  }));
  options.onJob?.(job);
  let resubmitted = false;

  while (!job.final && !cancelled()) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    if (cancelled()) break;
    let next: StructureJob | null;
    try {
      next = await fetchJob(job.jobId);
    } catch (error) {
      // 轮询这一拍失败（代理抖一下）不致命，下一拍接着问。
      console.error("结构分析轮询失败，下一拍重试:", error);
      continue;
    }
    if (next) {
      job = next;
    } else if (!resubmitted) {
      resubmitted = true;
      job = toJob(await requestJson<RawStructureJob>("/api/structure/jobs", {
        method: "POST",
        body: JSON.stringify({ clip_id: clipId }),
      }));
    } else {
      // 一次 404 当作后端重启，重提一次就够；反复 404 说明在跟重启互相追，别把轮询挂成永续。
      throw new Error(`分析任务反复丢失（后端可能在反复重启）：${job.jobId}`);
    }
    options.onJob?.(job);
  }

  return cancelled() ? null : job;
}

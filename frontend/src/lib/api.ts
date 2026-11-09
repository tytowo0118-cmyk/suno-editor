import type {
  GenerateJob,
  GenerateJobStatus,
  GenerateSource,
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

interface PollableJob {
  jobId: string;
  final: boolean;
}

interface PollOptions<T extends PollableJob> {
  intervalMs: number;
  isCancelled: () => boolean;
  onJob?: (job: T) => void;
  fetchJob: (jobId: string) => Promise<T | null>;
  resubmit: () => Promise<T>;
  lostError: (jobId: string) => string;
  logLabel: string;
}

/**
 * 任务式接口的共用轮询循环（结构分析与生成各有一套任务表，契约相同）。
 *
 * 期间任何一次轮询失败只当作「这一拍没问到」，由下一次轮询补上；
 * 只有后端明确 404（任务表随重启丢失）才重新提交，且最多补提交一次 ——
 * 反复 404 说明在跟重启互相追，别把轮询挂成永续。
 *
 * 生成任务重提交是安全的：后端账本记着已扣费的 task_id，同指纹只会免费重查，不会再建单。
 */
async function pollUntilFinal<T extends PollableJob>(
  first: T,
  options: PollOptions<T>,
): Promise<T | null> {
  let job = first;
  options.onJob?.(job);
  let resubmitted = false;

  while (!job.final && !options.isCancelled()) {
    await new Promise((resolve) => setTimeout(resolve, options.intervalMs));
    if (options.isCancelled()) break;
    let next: T | null;
    try {
      next = await options.fetchJob(job.jobId);
    } catch (error) {
      // 轮询这一拍失败（代理抖一下）不致命，下一拍接着问。
      console.error(`${options.logLabel}轮询失败，下一拍重试:`, error);
      continue;
    }
    if (next) {
      job = next;
    } else if (!resubmitted) {
      resubmitted = true;
      job = await options.resubmit();
    } else {
      throw new Error(options.lostError(job.jobId));
    }
    options.onJob?.(job);
  }

  return options.isCancelled() ? null : job;
}

/**
 * 提交并轮询一次深度学习结构分析，直到终态（done / failed）。
 * 整曲推理在 8 核 CPU 上是分钟级，所以不是一条挂着的请求，而是提交 + 轮询。
 */
export async function runStructureJob(
  clipId: string,
  options: RunStructureJobOptions = {},
): Promise<StructureJob | null> {
  const submit = () =>
    requestJson<RawStructureJob>("/api/structure/jobs", {
      method: "POST",
      body: JSON.stringify({ clip_id: clipId }),
    }).then(toJob);

  return pollUntilFinal(await submit(), {
    intervalMs: options.intervalMs ?? 3000,
    isCancelled: options.isCancelled ?? (() => false),
    onJob: options.onJob,
    fetchJob,
    resubmit: submit,
    lostError: (jobId) => `分析任务反复丢失（后端可能在反复重启）：${jobId}`,
    logLabel: "结构分析",
  });
}

interface RawGenerateJobClip {
  clip_id: string | null;
  filename: string;
  duration: number;
  audio_url: string;
}

interface RawGenerateJob {
  job_id: string;
  fingerprint: string;
  status: GenerateJobStatus;
  phase: string;
  final: boolean;
  elapsed_sec: number;
  prompt_chars: number;
  wanted_clips: number;
  clips: RawGenerateJobClip[];
  clip_count: number;
  progress: {
    upstream_status?: string;
    poll_ticks?: number;
    lost_ticks?: number;
    ready_clips?: number;
    clip_index?: number;
    clip_total?: number;
    clip_reused?: boolean;
    last_tick_lost?: string | null;
  };
  reused: boolean;
  task_id?: string;
  source?: GenerateSource;
  generate_ms?: number;
  detail?: string;
}

function toGenerateJob(data: RawGenerateJob): GenerateJob {
  return {
    jobId: data.job_id,
    fingerprint: data.fingerprint,
    status: data.status,
    phase: data.phase,
    final: data.final,
    elapsedSec: data.elapsed_sec,
    promptChars: data.prompt_chars,
    wantedClips: data.wanted_clips,
    clips: (data.clips ?? []).map((clip) => ({
      clipId: clip.clip_id,
      filename: clip.filename,
      duration: clip.duration,
      audioUrl: clip.audio_url,
    })),
    clipCount: data.clip_count,
    progress: {
      upstreamStatus: data.progress?.upstream_status,
      pollTicks: data.progress?.poll_ticks,
      lostTicks: data.progress?.lost_ticks,
      readyClips: data.progress?.ready_clips,
      clipIndex: data.progress?.clip_index,
      clipTotal: data.progress?.clip_total,
      clipReused: data.progress?.clip_reused,
      lastTickLost: data.progress?.last_tick_lost ?? null,
    },
    reused: data.reused,
    taskId: data.task_id,
    source: data.source,
    generateMs: data.generate_ms,
    detail: data.detail,
  };
}

async function fetchGenerateJob(jobId: string): Promise<GenerateJob | null> {
  const response = await fetch(
    `${API_BASE}/api/generate/jobs/${encodeURIComponent(jobId)}`,
    { cache: "no-store" },
  );
  // 404 = 后端重启把内存任务表清了；账本没丢，重新提交同指纹会免费重查，不会再扣费
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(extractError(await response.json().catch(() => null), response.status));
  }
  return toGenerateJob((await response.json()) as RawGenerateJob);
}

export interface RunGenerateJobOptions {
  /** 每次状态变化都回调，UI 据此显示阶段、上游状态与已耗时 */
  onJob?: (job: GenerateJob) => void;
  intervalMs?: number;
  /** 只停止等待，不取消服务端任务：钱可能已经花了，结果会留在账本里下次直接复用 */
  isCancelled?: () => boolean;
}

/**
 * 提交并轮询一次 Suno 生成，直到终态（done / failed）。
 *
 * 与结构分析同一套契约，但这里轮询的是**已经扣费**的任务：
 * 单次轮询失败不判死，404 重提交也不会再花钱（后端按载荷指纹免费重查）。
 * 取消只是不再等，服务端会把片段落盘并记账。
 */
export async function runGenerateJob(
  input: {
    prompt: string;
    continueClipId?: string;
    continueAt?: number;
    maxClips?: number;
  },
  options: RunGenerateJobOptions = {},
): Promise<GenerateJob | null> {
  const body = JSON.stringify({
    prompt: input.prompt,
    ...(input.continueClipId
      ? {
          continue_clip_id: input.continueClipId,
          continue_at: Math.max(0, input.continueAt ?? 0),
        }
      : {}),
    ...(input.maxClips ? { max_clips: input.maxClips } : {}),
  });
  const submit = () =>
    requestJson<RawGenerateJob>("/api/generate/jobs", { method: "POST", body }).then(toGenerateJob);

  return pollUntilFinal(await submit(), {
    intervalMs: options.intervalMs ?? 2000,
    isCancelled: options.isCancelled ?? (() => false),
    onJob: options.onJob,
    fetchJob: fetchGenerateJob,
    resubmit: submit,
    lostError: (jobId) => `生成任务反复丢失（后端可能在反复重启）：${jobId}`,
    logLabel: "生成任务",
  });
}

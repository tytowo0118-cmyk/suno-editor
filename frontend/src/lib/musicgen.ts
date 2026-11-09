import { API_BASE, runGenerateJob } from "./api";
import type { GenerateJob, GenerateSource, Take } from "./types";

/** FastAPI 后端的生成任务端点：提交拿 job_id，轮询读阶段进度（旧的同步 /api/generate 仍保留） */
export const GENERATE_ENDPOINT = `${API_BASE}/api/generate/jobs`;

/** 后端 `MAX_CLIPS_PER_TASK = 2`：一次任务上游固定产 2 条双生，同价，不取回才是白扔 */
export const CLIPS_PER_TASK = 2;

/** 每段发几次任务。2 次 × 双生 = 4 个候选，比原来 3 次任务省一次扣费 */
export const TASKS_PER_SECTION = 2;

/** 一次「生成候选」预期的候选数，只用于界面文案与骨架屏 */
export const CANDIDATES_PER_SECTION = TASKS_PER_SECTION * CLIPS_PER_TASK;

/** 同一任务里第 2 个已扣费片段（双生），后端已下载转码为 /static 上的 WAV */
interface ExtraClip {
  clipId: string | null;
  url: string;
  duration: number;
}

interface GeneratedAudio {
  url: string;
  duration: number;
  /** 本次音频对应的 Suno clip id，续写时需要回传给后端 */
  clipId: string | null;
  extras: ExtraClip[];
  /** 计费来源：submit 真扣费 / refetch 免费重查 / cache 直接复用 */
  source: GenerateSource;
}

interface GenerateRequest {
  prompt: string;
  /** 占位：Suno 不吃时长参数，实际演唱时长由歌词决定，这里不再发给后端 */
  durationSec: number;
  signal?: AbortSignal;
  continueClipId?: string;
  continueAt?: number;
  /** 一次任务上游固定产出 2 个片段，>1 时把多出来的那些一并取回 */
  maxClips?: number;
  onProgress?: (job: GenerateJob) => void;
}

/** 后端回的是 /static/output/... 相对地址，补上 API_BASE 才是浏览器能播的绝对地址 */
function absoluteAudio(url: string): string {
  return url.startsWith("http") ? url : `${API_BASE}${url}`;
}

/**
 * 提交一次生成任务并轮询到终态。
 *
 * 与改造前最大的不同：候选不再落成浏览器 Blob，而是直接引用后端已落盘的 WAV。
 * 于是刷新后候选还在（配合会话快照），双生第二条也不必再下载一遍，
 * 而「这次有没有花钱」由 job.source 如实给出，不靠耗时去猜。
 */
async function generateOne(input: GenerateRequest): Promise<GeneratedAudio> {
  const job = await runGenerateJob(
    {
      prompt: input.prompt,
      continueClipId: input.continueClipId,
      continueAt: input.continueAt,
      maxClips: input.maxClips ?? CLIPS_PER_TASK,
    },
    {
      onJob: input.onProgress,
      // 取消只是不再等：服务端任务可能已经扣费，它会继续跑完并把结果记进账本
      isCancelled: () => Boolean(input.signal?.aborted),
    },
  );

  if (!job) {
    throw new Error("已停止等待；任务仍在服务端跑，结果会留在账本里，下次同样内容直接复用");
  }
  if (job.status === "failed" || job.clips.length === 0) {
    const detail = job.detail ?? "生成失败";
    // 有 task_id 就说明建单成功、钱已经花了；账本记着它，重试是免费重查。
    // 但后端超时的 detail 自带「任务已扣费且已记账」那句，再补一遍就是同一句安慰说两遍
    // （实测浮层读作「…免费重查这张票据；已扣费并记账，重试不会再次计费」），
    // 所以只在 detail 没提记账时补 —— 下载失败之类的 detail 只讲传输，不讲钱。
    const needsBillingNote = Boolean(job.taskId) && !detail.includes("记账");
    throw new Error(needsBillingNote ? `${detail}；已扣费并记账，重试不会再次计费` : detail);
  }

  const [first, ...rest] = job.clips;
  return {
    url: absoluteAudio(first.audioUrl),
    duration: first.duration,
    clipId: first.clipId,
    extras: rest.map((clip) => ({
      clipId: clip.clipId,
      url: absoluteAudio(clip.audioUrl),
      duration: clip.duration,
    })),
    source: job.source ?? "submit",
  };
}

/** 本次生成有没有再花钱，用于候选挂载后的提示；措辞与后端 source 一一对应 */
export function billingNote(sources: GenerateSource[]): string | null {
  if (sources.length === 0) return null;
  if (sources.every((source) => source === "cache")) {
    return "复用已落盘结果，未再次计费";
  }
  if (sources.every((source) => source !== "submit")) {
    return "重查已扣费任务取回结果，未再次计费";
  }
  const charged = sources.filter((source) => source === "submit").length;
  const free = sources.length - charged;
  return free > 0
    ? `本次建单 ${charged} 次，另 ${free} 次复用已扣费结果`
    : `本次建单 ${charged} 次（已扣费）`;
}

const PHASE_LABEL: Record<string, string> = {
  queued: "排队中",
  submitting: "提交建单",
  generating: "上游生成中",
  downloading: "下载片段",
  transcoding: "转码 WAV",
  done: "已落盘",
  failed: "失败",
};

/** 把任务状态翻成一行可读进度，计费语义单独给出，避免「等得久＝又花钱了」的误解 */
export function describeGenerateJob(job: GenerateJob): {
  label: string;
  billing: string | null;
  failed: boolean;
} {
  const phase = PHASE_LABEL[job.phase] ?? job.phase;
  const parts = [phase];
  if (job.phase === "generating") {
    if (job.progress.upstreamStatus) parts.push(`上游 ${job.progress.upstreamStatus}`);
    if (job.progress.pollTicks) parts.push(`第 ${job.progress.pollTicks} 拍`);
  }
  if ((job.phase === "downloading" || job.phase === "transcoding") && job.progress.clipTotal) {
    parts.push(`${(job.progress.clipIndex ?? 0) + 1}/${job.progress.clipTotal}`);
  }
  if (!job.final) parts.push(`${job.elapsedSec.toFixed(1)}s`);
  if (job.progress.lostTicks) {
    parts.push(`丢 ${job.progress.lostTicks} 拍（代理抖动，任务未判死）`);
  }
  const billing =
    job.source === "cache"
      ? "复用已落盘结果，未再次计费"
      : job.source === "refetch"
        ? "重查已扣费任务，未再次计费"
        : job.source === "submit" && job.final && job.status === "done"
          ? "本次建单已扣费"
          : null;
  return { label: parts.join(" · "), billing, failed: job.status === "failed" };
}

/**
 * 生成一组候选：并发提交 `count` 个任务，每个任务上游固定产 2 条双生片段，
 * 主片段与 extras 全部挂载成候选 —— 第 2 条同样已扣费，扔掉它等于白付一半钱。
 * 片段地址都是后端 /static 上的 WAV，不需要再下载或建 Blob。
 * 任一任务失败则抛错；已扣费的结果留在后端账本里，重试同指纹不会再次计费。
 */
export async function generateTakes(input: {
  sectionId: string;
  prompt: string;
  durationSec: number;
  count?: number;
  signal?: AbortSignal;
  onProgress?: (job: GenerateJob, taskIndex: number) => void;
}): Promise<{ takes: Take[]; sources: GenerateSource[] }> {
  const count = input.count ?? TASKS_PER_SECTION;
  const durationSec = Math.max(1, Math.round(input.durationSec));
  const settled = await Promise.allSettled(
    Array.from({ length: count }, (_, taskIndex) =>
      generateOne({
        prompt: input.prompt,
        durationSec,
        signal: input.signal,
        maxClips: CLIPS_PER_TASK,
        onProgress: input.onProgress
          ? (job) => input.onProgress?.(job, taskIndex)
          : undefined,
      }),
    ),
  );

  const generated = settled.flatMap((item) =>
    item.status === "fulfilled" ? [item.value] : [],
  );
  const failure = settled.find(
    (item): item is PromiseRejectedResult => item.status === "rejected",
  );
  if (failure) {
    throw failure.reason instanceof Error ? failure.reason : new Error("生成失败");
  }

  const stamp = Date.now().toString(36);
  const takes: Take[] = [];
  // id 一律按候选序号取字母：双生曾经固定占 `-twin-` 这一格，同一次生成的两条双生 id 相同，
  // 而 key={take.id} 与 lockedTakeId 都按 id 判同，锁一条双生会把两条一起标成已锁定。
  const pushTake = (audioUrl: string, duration: number, clipId: string | null): void => {
    const letter = String.fromCharCode(65 + takes.length);
    takes.push({
      id: `${input.sectionId}-${letter.toLowerCase()}-${stamp}`,
      label: `Take ${letter}`,
      audioUrl,
      duration,
      clipId,
    });
  };

  for (const audio of generated) {
    pushTake(audio.url, audio.duration, audio.clipId);
    for (const extra of audio.extras) {
      pushTake(extra.url, extra.duration, extra.clipId);
    }
  }

  return { takes, sources: generated.map((audio) => audio.source) };
}

/**
 * 从某个 clip 的指定秒数续写：上游把 continueAt 之前的音频当作创作上下文，
 * 返回在其后新生成的音频（实测不含原开头），因此结果适合作为该段的额外候选试听。
 * 一次任务上游固定产出 2 个片段（双生），这里全部挂载为独立候选，免得已扣费的另一半被丢掉。
 */
export async function continueTakes(input: {
  sectionId: string;
  clipId: string;
  continueAt: number;
  prompt: string;
  durationSec: number;
  signal?: AbortSignal;
  onProgress?: (job: GenerateJob) => void;
}): Promise<{ takes: Take[]; source: GenerateSource }> {
  const audio = await generateOne({
    prompt: input.prompt,
    durationSec: input.durationSec,
    signal: input.signal,
    continueClipId: input.clipId,
    continueAt: input.continueAt,
    maxClips: CLIPS_PER_TASK,
    onProgress: input.onProgress,
  });

  const stamp = Date.now().toString(36);
  const label = (index: number) => `续写分支 ${String.fromCharCode(65 + index)}`;
  const takes: Take[] = [
    {
      id: `${input.sectionId}-continue-a-${stamp}`,
      label: label(0),
      audioUrl: audio.url,
      duration: audio.duration,
      clipId: audio.clipId ?? input.clipId,
    },
  ];

  audio.extras.forEach((extra) => {
    takes.push({
      id: `${input.sectionId}-continue-${stamp}-${takes.length}`,
      label: label(takes.length),
      audioUrl: extra.url,
      duration: extra.duration,
      clipId: extra.clipId,
    });
  });

  return { takes, source: audio.source };
}

export function revokeTakeAudio(takes: Take[] | undefined): void {
  takes?.forEach((take) => {
    if (take.audioUrl.startsWith("blob:")) {
      URL.revokeObjectURL(take.audioUrl);
    }
  });
}
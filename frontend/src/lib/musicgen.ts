import { API_BASE, runGenerateJob, trimAudio } from "./api";
import { plannedTruncateSec, truncateBeyondSec } from "./lyrics";
import type { GenerateJob, GenerateSource, Take, TimelineSection } from "./types";

/** FastAPI 后端的生成任务端点：提交拿 job_id，轮询读阶段进度（旧的同步 /api/generate 仍保留） */
export const GENERATE_ENDPOINT = `${API_BASE}/api/generate/jobs`;

/** 后端 `MAX_CLIPS_PER_TASK = 2`：一次任务上游固定产 2 条双生，同价，不取回才是白扔 */
export const CLIPS_PER_TASK = 2;

/**
 * 每段发几次任务。1 次 × 双生 = 2 个候选。
 *
 * 曾经发 2 次想把候选凑到 4 个，但任务化之后并发提交的两个请求载荷完全相同，
 * 第二个会被指纹账本并到同一个 task 上（实测 2026-11-09：只建单 1 次、只落 2 个 WAV，
 * 4 张卡里 Take C/D 与 A/B 同 URL）—— 多发的任务不会再多一次建单，只会把同一对双生
 * 画两遍，平白让用户以为有 4 个选择。两个真候选就发一次。
 */
export const TASKS_PER_SECTION = 1;

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
 * 上一段「已锁定候选」换算成本段续写的种子：`continue_clip_id` + `continue_at`。
 *
 * 三个不显然的口径：
 * 1. `/api/trim` 只往 `/static` 写新 WAV，**不产 clip_id**（响应里只有 audio_url /
 *    duration / source_duration / snapped），所以续写只能回指上游原片的 clip_id。
 *    裁短保留的正是原片 `[0, cut]`，`take.duration` 就是原片时间轴上的续写点，
 *    两处同源，不会出现「本地裁到 8s、上游按 205s 给上下文」的错位。
 *    续写产物同理：它自己也是一个 clip，下一段拿它的 `clip_id` + 本段截点继续往下接，
 *    所以链是「逐级延长」而不是一级一回首。
 * 2. **每一段都截**（`plannedTruncateSec` 现在对有词段也返回秒数），所以续写点默认取
 *    `take.duration` —— 那是截短实际保留的秒数，含 beyond 档往后吸到的边界。
 * 3. 截短没成（`/api/trim` 挂了、或恢复的历史现场里躺着整条未裁素材）时不能照抄
 *    `take.duration`：主歌候选 44.232s 里已经唱完副歌，在它末尾续写等于跳过本段。
 *    判据用 `pendingTruncateSec`：候选还长过「规划 + 往后吸护栏」就认定没截成，退回规划秒数。
 *
 * 上一段没锁定、候选没有 clip_id（历史现场的 blob 地址）都返回 undefined，
 * 调用方据此退回独立整首生成 —— 必须让调用方知道，续写链断了是听得出来的。
 */
export function continuationSeed(
  prevSection: TimelineSection | undefined,
  prevTake: Take | undefined,
): { clipId: string; continueAt: number } | undefined {
  if (!prevSection || !prevTake) return undefined;
  const clipId = prevTake.clipId?.trim();
  if (!clipId) return undefined;
  const keepSec =
    pendingTruncateSec(prevSection, prevTake) ?? (prevTake.duration || undefined);
  if (!Number.isFinite(keepSec) || !keepSec || keepSec <= 0) return undefined;
  // 上游 continue_at 会 round(_, 3)，这里先量化到毫秒，避免浮点尾巴进载荷和指纹
  return { clipId, continueAt: Math.round(keepSec * 1000) / 1000 };
}

/**
 * 本段还需不需要在拼接侧再截一次：候选长度超过「规划 + 往后吸护栏」= 生成后那次截短没成
 * （接口挂了 / 恢复的历史现场里躺着整条素材）。
 *
 * 已经截过的必须返回 undefined —— 拿规划秒数去二次裁会把刚吸到的边界又砍掉一截
 * （主歌吸到 19.313s，再按 12s 裁就唱到半句就断）。
 */
export function pendingTruncateSec(
  section: TimelineSection,
  take: Take,
): number | undefined {
  const planned = plannedTruncateSec(section);
  if (!planned) return undefined;
  return take.duration > planned + truncateBeyondSec(section) + 0.05 ? planned : undefined;
}

/**
 * 生成一组候选：并发提交 `count` 个任务，每个任务上游固定产 2 条双生片段，
 * 主片段与 extras 全部挂载成候选 —— 第 2 条同样已扣费，扔掉它等于白付一半钱。
 * 片段地址都是后端 /static 上的 WAV，不需要再下载或建 Blob。
 * 带 `seed` 时走 Suno 续写：上游拿上一段在 `continueAt` 之前的音频当上下文，本段接在后面，
 * 接缝处的调性/配器/速度由模型延续，不再是本地 8 ms 硬切缝出来的「换了一首歌」。
 * 任一任务失败则抛错；已扣费的结果留在后端账本里，重试同指纹不会再次计费。
 */
export async function generateTakes(input: {
  sectionId: string;
  prompt: string;
  durationSec: number;
  count?: number;
  /** 上一段锁定候选换算出的续写种子；缺省即独立整首生成 */
  seed?: { clipId: string; continueAt: number };
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
        continueClipId: input.seed?.clipId,
        continueAt: input.seed?.continueAt,
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
 * 候选的「只留本段」：上游不吃任何时长参数 —— 空歌词段 `make_instrumental` 一条前奏写满整首
 * （实测 205.560 / 189.504 s），`extend` 续写回来的也是本段之后的一切（实测 2 行主歌 44.232 s）。
 * 生成后立刻按规划秒数交给后端截短，候选卡、锁定、时间轴、下一段的续写点与成品才是同一个段长；
 * 不截的话时间轴上躺着一块 205.6 s 的「前奏」或 44.2 s 的「主歌」，而拼接时才被砍到规划值 —— 两处口径对不上。
 * `beyondSec` 让后端在短侧窗没候选时往后吸到第一个结构边界，而不是硬停在估算秒数上切进唱词中间。
 *
 * 裁短失败保留原片（锁定后拼接仍会按 max_duration_sec 裁），把失败数交给调用方提示，
 * 不把「没裁成」伪装成裁好了。
 */
export async function trimTakesToPlan(
  takes: Take[],
  maxDurationSec: number,
  beyondSec = 0,
): Promise<{ takes: Take[]; failed: number }> {
  let failed = 0;
  const trimmed = await Promise.all(
    takes.map(async (take) => {
      try {
        const result = await trimAudio({ audioUrl: take.audioUrl, maxDurationSec, beyondSec });
        return { ...take, audioUrl: result.audioUrl, duration: result.duration };
      } catch {
        failed += 1;
        return take;
      }
    }),
  );
  return { takes: trimmed, failed };
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
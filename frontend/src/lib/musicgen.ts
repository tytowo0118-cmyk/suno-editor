import { API_BASE } from "./api";
import type { Take } from "./types";

/** FastAPI 后端的 Suno 代理端点：只接收 prompt，返回整段 WAV */
export const GENERATE_ENDPOINT = `${API_BASE}/api/generate`;

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
}

interface GenerateRequest {
  prompt: string;
  durationSec: number;
  signal?: AbortSignal;
  continueClipId?: string;
  continueAt?: number;
  /** 一次任务上游固定产出 2 个片段，>1 时把多出来的那些一并取回 */
  maxClips?: number;
}

function readDuration(blobUrl: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const audio = new Audio();
    audio.preload = "metadata";
    audio.src = blobUrl;
    audio.onloadedmetadata = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        resolve(audio.duration);
      } else {
        reject(new Error("无法解析生成音频的时长"));
      }
    };
    audio.onerror = () => reject(new Error("生成的音频无法解码，请检查返回格式"));
  });
}

function isWav(bytes: Uint8Array): boolean {
  if (bytes.length < 12) return false;
  const tag = (start: number) => String.fromCharCode(...bytes.slice(start, start + 4));
  return tag(0) === "RIFF" && tag(8) === "WAVE";
}

function extractDetail(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      return String((parsed as { detail: unknown }).detail).slice(0, 160);
    }
  } catch {
    // 非 JSON 错误体直接截断展示
  }
  return trimmed.slice(0, 160);
}

/** 后端把双生片段列表 base64 放在 X-Suno-Extra-Clips 里（纯 ASCII JSON，可直接 atob） */
function readExtraClips(response: Response): ExtraClip[] {
  const raw = response.headers.get("x-suno-extra-clips")?.trim();
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(atob(raw));
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const record = item as { clipId?: unknown; url?: unknown; duration?: unknown };
      if (typeof record.url !== "string" || !record.url) return [];
      return [
        {
          clipId: typeof record.clipId === "string" ? record.clipId : null,
          url: record.url,
          duration: typeof record.duration === "number" ? record.duration : 0,
        },
      ];
    });
  } catch {
    return [];
  }
}

async function fetchExtraAudio(extra: ExtraClip, signal?: AbortSignal): Promise<GeneratedAudio> {
  const response = await fetch(extra.url, { signal, cache: "no-store" });
  if (!response.ok) {
    throw new Error(`双生片段下载失败（HTTP ${response.status}）`);
  }
  const blob = await response.blob();
  const header = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  if (!isWav(header)) {
    throw new Error("双生片段不是 WAV 音频");
  }
  const url = URL.createObjectURL(blob);
  const duration =
    Number.isFinite(extra.duration) && extra.duration > 0 ? extra.duration : await readDuration(url);
  return { url, duration, clipId: extra.clipId, extras: [] };
}

async function generateOne(input: GenerateRequest): Promise<GeneratedAudio> {
  const form = new FormData();
  // 结构标签与歌词已在调用方拼进 prompt
  form.append("prompt", input.prompt);
  // 占位字段：后端按歌词自行决定演唱时长，不再强依赖该值
  form.append("duration", String(Math.max(1, Math.round(input.durationSec))));
  if (input.continueClipId) {
    form.append("continue_clip_id", input.continueClipId);
    form.append("continue_at", Math.max(0, input.continueAt ?? 0).toFixed(3));
  }
  if (input.maxClips && input.maxClips > 1) {
    form.append("max_clips", String(input.maxClips));
  }

  let response: Response;
  try {
    response = await fetch(GENERATE_ENDPOINT, {
      method: "POST",
      body: form,
      signal: input.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (input.signal?.aborted) throw error;
    throw new Error(
      `无法连接生成服务（${GENERATE_ENDPOINT}）：请确认 FastAPI 后端与 Suno 代理已启动`,
    );
  }

  if (!response.ok) {
    const detail = extractDetail(await response.text().catch(() => ""));
    throw new Error(`生成失败（HTTP ${response.status}）${detail ? `：${detail}` : ""}`);
  }

  const contentType = response.headers.get("content-type") ?? "未知";
  const clipId = response.headers.get("x-suno-clip-id")?.trim() || null;
  const extras = readExtraClips(response);
  const blob = await response.blob();
  const header = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  if (!isWav(header)) {
    throw new Error(`生成服务返回的不是 WAV 音频（Content-Type: ${contentType}）`);
  }

  const url = URL.createObjectURL(blob);
  try {
    const duration = await readDuration(url);
    return { url, duration, clipId, extras };
  } catch (error) {
    URL.revokeObjectURL(url);
    throw error;
  }
}

/**
 * 生成一组候选：同一段 prompt 并发请求 count 次，每次返回一段完整 WAV。
 * 任一失败则回收已生成的 Blob URL 并抛错，避免内存泄漏。
 */
export async function generateTakes(input: {
  sectionId: string;
  prompt: string;
  durationSec: number;
  count?: number;
  signal?: AbortSignal;
}): Promise<Take[]> {
  const count = input.count ?? 3;
  const labels = ["A", "B", "C"];
  const durationSec = Math.max(1, Math.round(input.durationSec));
  const settled = await Promise.allSettled(
    Array.from({ length: count }, () =>
      generateOne({ prompt: input.prompt, durationSec, signal: input.signal }),
    ),
  );

  const generated = settled.flatMap((item) =>
    item.status === "fulfilled" ? [item.value] : [],
  );
  const failure = settled.find(
    (item): item is PromiseRejectedResult => item.status === "rejected",
  );
  if (failure) {
    generated.forEach((audio) => URL.revokeObjectURL(audio.url));
    throw failure.reason instanceof Error ? failure.reason : new Error("生成失败");
  }

  const stamp = Date.now().toString(36);
  return generated.map((audio, index) => ({
    id: `${input.sectionId}-${labels[index].toLowerCase()}-${stamp}`,
    label: `Take ${labels[index]}`,
    audioUrl: audio.url,
    duration: audio.duration,
    clipId: audio.clipId,
  }));
}

/**
 * 从某个 clip 的指定秒数续写：上游把 continueAt 之前的音频当作创作上下文，
 * 返回在其后新生成的音频（实测不含原开头），因此结果适合作为该段的额外候选试听。
 * 一次任务上游固定产出 2 个片段（双生），这里全部下载并挂载为独立候选，
 * 免得已扣费的另一半被丢掉。
 */
export async function continueTakes(input: {
  sectionId: string;
  clipId: string;
  continueAt: number;
  prompt: string;
  durationSec: number;
  signal?: AbortSignal;
}): Promise<Take[]> {
  const audio = await generateOne({
    prompt: input.prompt,
    durationSec: input.durationSec,
    signal: input.signal,
    continueClipId: input.clipId,
    continueAt: input.continueAt,
    maxClips: 2,
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

  for (const extra of audio.extras) {
    try {
      const downloaded = await fetchExtraAudio(extra, input.signal);
      takes.push({
        id: `${input.sectionId}-continue-${stamp}-${takes.length}`,
        label: label(takes.length),
        audioUrl: downloaded.url,
        duration: downloaded.duration,
        clipId: downloaded.clipId,
      });
    } catch (error) {
      if (input.signal?.aborted) {
        revokeTakeAudio(takes);
        throw error;
      }
      // 残值片段取不回来不该作废整次续写，保留已经挂载的候选
    }
  }

  return takes;
}

export function revokeTakeAudio(takes: Take[] | undefined): void {
  takes?.forEach((take) => {
    if (take.audioUrl.startsWith("blob:")) {
      URL.revokeObjectURL(take.audioUrl);
    }
  });
}

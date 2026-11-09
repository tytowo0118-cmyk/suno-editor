import { MAX_SECTIONS } from "./lyrics";
import { SECTION_META, SECTION_STYLES } from "./sections";
import type { SectionKind, StitchResult, Take, TimelineSection } from "./types";

/**
 * 会话快照：把「已花过钱的现场」留在本标签页的 sessionStorage 里。
 *
 * 只存**能安全还原**的东西：歌词、歌曲提示词、段落结构（含已被返回音频改写的自然时长）、
 * 提示词草稿、当前选中段、已锁定 Take、成品。已锁定 Take 与成品的音频地址都是后端 `/static`
 * 上的绝对地址，文件真在磁盘上，所以刷新后照样能播、能续拼；未锁定候选的地址是 `blob:`，
 * 只活在那一次页面进程里，存下来必然变死链 —— 这类地址一律不写进快照，读回来时也再校验一次。
 *
 * 磁盘上的文件不保证还在（清 `static/` 目录、换机器都会）：地址格式对 ≠ 文件在，所以恢复之后
 * 还要对每条 `/static` 地址探一次活（`HEAD`），404 的那条按「我们这边失败」处理 —— 从 `locked`
 * 里摘掉、成品直接作废，并以 error 级提示点名是哪几段，而不是留一个点开就报错的死链。
 */
export const SESSION_STORAGE_KEY = "museblocks.session.v1";
const SESSION_VERSION = 1;

/** 文本字段的安全上限：歌词 12 段用不到 2 万字，超了就是脏数据 */
const MAX_LYRICS_LEN = 20_000;
const MAX_PROMPT_LEN = 4_000;
const MAX_LABEL_LEN = 64;

export interface StoredSection {
  id: string;
  kind: SectionKind;
  title: string;
  english: string;
  durationSec: number;
  prompt: string;
  lyrics: string;
}

export interface StoredTake {
  id: string;
  label: string;
  audioUrl: string;
  duration: number;
  clipId: string | null;
}

/** `StitchResult` 的可持久形态；字段与后端回执同名，数组长度必须对齐 `segmentOrder` */
export interface StoredMix {
  audioUrl: string;
  filename: string;
  duration: number;
  peakDbfs: number;
  segmentOrder: string[];
  segmentStartSec: number[];
  headTrimSec: number[];
  truncateToSec: (number | null)[];
}

export interface SessionSnapshot {
  version: number;
  step: "brief" | "studio";
  structureReady: boolean;
  songPrompt: string;
  lyrics: string;
  activeId: string;
  sections: StoredSection[];
  /** 段 id → 已锁定 Take；只收 `/static` 地址 */
  locked: Record<string, StoredTake>;
  prompts: Record<string, string>;
  /** 上一次拼好的成品；地址同样是 `/static` 才写盘，缺文件时恢复阶段会被探活剔除 */
  mix: StoredMix | null;
  savedAtMs: number;
}

const SECTION_KINDS = Object.keys(SECTION_META) as SectionKind[];

function asString(value: unknown, maxLen: number): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > maxLen) return null;
  return trimmed;
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : Number.NaN;
}

/**
 * 只有后端 `/static` 上的绝对地址才值得写进快照：`blob:` 属于上一次页面进程，
 * 刷新后就是一个必然 404 的死链，比没有更糟（会被当成已锁定去拼接）。
 */
export function isPersistentAudioUrl(url: unknown): url is string {
  if (typeof url !== "string") return false;
  if (!/^https?:\/\//i.test(url)) return false;
  return url.includes("/static/");
}

function sanitizeSection(value: unknown): StoredSection | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const id = asString(raw.id, 64);
  const kind = asString(raw.kind, 24) as SectionKind | null;
  if (!id || !kind || !SECTION_KINDS.includes(kind)) return null;

  const durationSec = asNumber(raw.durationSec);
  return {
    id,
    kind,
    // 标题与英文名带序号（「主歌 2」），不能只由 kind 反推，原样保留但过一遍长度上限
    title: asString(raw.title, MAX_LABEL_LEN) ?? SECTION_META[kind].title,
    english: asString(raw.english, MAX_LABEL_LEN) ?? SECTION_META[kind].english,
    // 时长必须落在 0.1s–600s：越界（或被写成 NaN）直接判这条段落无效
    durationSec: Number.isFinite(durationSec) && durationSec > 0 && durationSec <= 600 ? durationSec : 8,
    prompt: asString(raw.prompt, MAX_PROMPT_LEN) ?? "",
    lyrics: typeof raw.lyrics === "string" ? raw.lyrics.slice(0, MAX_LYRICS_LEN) : "",
  };
}

function sanitizeTake(value: unknown): StoredTake | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const id = asString(raw.id, 96);
  const audioUrl = asString(raw.audioUrl, 512);
  if (!id || !isPersistentAudioUrl(audioUrl)) return null;

  const duration = asNumber(raw.duration);
  const clipId = asString(raw.clipId, 96);
  return {
    id,
    label: asString(raw.label, MAX_LABEL_LEN) ?? "Take",
    audioUrl,
    duration: Number.isFinite(duration) && duration > 0 && duration <= 600 ? duration : 0,
    clipId: clipId ?? null,
  };
}

function asNumberArray(value: unknown, maxLen: number): number[] | null {
  if (!Array.isArray(value)) return null;
  if (value.length > maxLen) return null;
  const numbers = value.map(asNumber);
  return numbers.every((n) => Number.isFinite(n) && n >= -1 && n <= 3600) ? numbers : null;
}

function asNullableNumberArray(value: unknown, maxLen: number): (number | null)[] | null {
  if (!Array.isArray(value) || value.length > maxLen) return null;
  return value.map((item) => {
    if (item === null) return null;
    const n = asNumber(item);
    return Number.isFinite(n) && n >= 0 && n <= 3600 ? n : null;
  });
}

/**
 * 审计数组（头部裁切 / 按段裁切）必须与 `segmentOrder` 等长：下游是按下标取值的
 * （第二条色带按 `segment_start_sec[i]` 反推占长，续写按 `head_trim_sec[i]` 换回源时间轴），
 * 长度差一位就会读到 `undefined`。对不上就整条换成默认填充，而不是保留半截。
 */
function alignNumbers(value: unknown, len: number, filler: number): number[] {
  const arr = asNumberArray(value, len);
  if (!arr || arr.length !== len) return new Array(len).fill(filler);
  return arr;
}

function alignNullableNumbers(value: unknown, len: number): (number | null)[] {
  const arr = asNullableNumberArray(value, len);
  if (!arr || arr.length !== len) return new Array(len).fill(null);
  return arr;
}

function sanitizeMix(value: unknown): StoredMix | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const audioUrl = asString(raw.audioUrl, 512);
  const filename = asString(raw.filename, 192);
  if (!filename || !isPersistentAudioUrl(audioUrl)) return null;

  const duration = asNumber(raw.duration);
  const peakDbfs = asNumber(raw.peakDbfs);
  const segmentOrder = Array.isArray(raw.segmentOrder)
    ? raw.segmentOrder.slice(0, MAX_SECTIONS + 4)
    : null;
  if (!segmentOrder || segmentOrder.some((id) => typeof id !== "string")) return null;
  const order = segmentOrder as string[];

  // 段起点是成品时间轴的唯一依据，缺一位就没法换算，整条成品作废
  const segmentStartSec = asNumberArray(raw.segmentStartSec, order.length);
  if (!segmentStartSec || segmentStartSec.length !== order.length) return null;
  const headTrimSec = alignNumbers(raw.headTrimSec, order.length, 0);
  const truncateToSec = alignNullableNumbers(raw.truncateToSec, order.length);

  return {
    audioUrl,
    filename,
    duration: Number.isFinite(duration) && duration > 0 && duration <= 3600 ? duration : 0,
    peakDbfs: Number.isFinite(peakDbfs) ? peakDbfs : 0,
    segmentOrder: order,
    segmentStartSec,
    headTrimSec,
    truncateToSec,
  };
}

/** 逐字段校验的外部输入；任何一处不成形就整体作废，不做「尽力而为」的修补 */
export function parseSession(raw: unknown): SessionSnapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (value.version !== SESSION_VERSION) return null;

  const sectionsValue = Array.isArray(value.sections) ? value.sections : null;
  if (!sectionsValue) return null;
  const sections = sectionsValue
    .slice(0, MAX_SECTIONS)
    .map(sanitizeSection)
    .filter((section): section is StoredSection => section !== null);
  if (sections.length === 0) return null;

  const lockedSource =
    value.locked && typeof value.locked === "object"
      ? (value.locked as Record<string, unknown>)
      : {};
  const sectionIds = new Set(sections.map((section) => section.id));
  const locked: Record<string, StoredTake> = {};
  Object.entries(lockedSource).forEach(([sectionId, take]) => {
    if (!sectionIds.has(sectionId)) return;
    const sanitized = sanitizeTake(take);
    if (sanitized) locked[sectionId] = sanitized;
  });

  const promptsSource =
    value.prompts && typeof value.prompts === "object"
      ? (value.prompts as Record<string, unknown>)
      : {};
  const prompts: Record<string, string> = {};
  sections.forEach((section) => {
    const prompt = asString(promptsSource[section.id], MAX_PROMPT_LEN);
    if (prompt) prompts[section.id] = prompt;
  });

  const lyrics = typeof value.lyrics === "string" ? value.lyrics.slice(0, MAX_LYRICS_LEN) : "";
  const savedAtMs = asNumber(value.savedAtMs);
  const activeIds = new Set(sections.map((section) => section.id));
  const storedActive = asString(value.activeId, 64);

  return {
    version: SESSION_VERSION,
    step: value.step === "studio" ? "studio" : "brief",
    // 段落结构在，就说明用户确认过结构；否则恢复后会重走第一页
    structureReady: value.structureReady === true && sections.length > 0,
    songPrompt: asString(value.songPrompt, MAX_PROMPT_LEN) ?? "",
    lyrics,
    activeId: storedActive && activeIds.has(storedActive) ? storedActive : sections[0].id,
    sections,
    locked,
    prompts,
    mix: sanitizeMix(value.mix),
    savedAtMs: Number.isFinite(savedAtMs) ? savedAtMs : 0,
  };
}

export interface CaptureInput {
  step: "brief" | "studio";
  structureReady: boolean;
  songPrompt: string;
  lyrics: string;
  activeId: string;
  sections: TimelineSection[];
  locked: Record<string, Take | undefined>;
  prompts: Record<string, string>;
  mix: StitchResult | null;
}

/** 当前状态 → 快照；样式字段不写盘，恢复时由 `kind` 重新推导 */
export function captureSession(input: CaptureInput): SessionSnapshot {
  const locked: Record<string, StoredTake> = {};
  // `blob:` 地址的锁定不写盘：那必然是死链，比缺失更糟（会被当成可拼接的段落）
  Object.entries(input.locked).forEach(([sectionId, take]) => {
    if (!take || !isPersistentAudioUrl(take.audioUrl)) return;
    locked[sectionId] = {
      id: take.id,
      label: take.label,
      audioUrl: take.audioUrl,
      duration: take.duration,
      clipId: take.clipId ?? null,
    };
  });

  const prompts: Record<string, string> = {};
  input.sections.forEach((section) => {
    const prompt = input.prompts[section.id];
    if (prompt) prompts[section.id] = prompt;
  });

  return {
    version: SESSION_VERSION,
    step: input.step,
    structureReady: input.structureReady,
    songPrompt: input.songPrompt,
    lyrics: input.lyrics,
    activeId: input.activeId,
    sections: input.sections.slice(0, MAX_SECTIONS).map((section) => ({
      id: section.id,
      kind: section.kind,
      title: section.title,
      english: section.english,
      durationSec: section.durationSec,
      prompt: section.prompt,
      lyrics: section.lyrics,
    })),
    locked,
    prompts,
    // 成品随时可以免费重拼，但既然它在磁盘上就值得留下：省一次等待，也保住续写落点
    mix:
      input.mix && isPersistentAudioUrl(input.mix.audioUrl)
        ? {
            audioUrl: input.mix.audioUrl,
            filename: input.mix.filename,
            duration: input.mix.duration,
            peakDbfs: input.mix.peakDbfs,
            segmentOrder: [...input.mix.segmentOrder],
            segmentStartSec: [...input.mix.segmentStartSec],
            headTrimSec: [...input.mix.headTrimSec],
            truncateToSec: [...input.mix.truncateToSec],
          }
        : null,
    savedAtMs: Date.now(),
  };
}

/**
 * 快照 → 时间轴段落。配色与波形颜色由 `SECTION_STYLES[kind]` 现推，
 * 磁盘上那份只当数据用；这样即便快照被改坏也只是变回默认配色，不会把任意 class 注进 DOM。
 */
export function sectionFromStored(stored: StoredSection): TimelineSection {
  return {
    ...stored,
    ...SECTION_STYLES[stored.kind],
  };
}

export function takeFromStored(stored: StoredTake): Take {
  return {
    id: stored.id,
    label: stored.label,
    audioUrl: stored.audioUrl,
    duration: stored.duration,
    clipId: stored.clipId,
  };
}

/** 快照里的成品 → 前端 `StitchResult`（字段同名，第二条色带与续写落点都靠这几组数组） */
export function mixFromStored(stored: StoredMix): StitchResult {
  return {
    audioUrl: stored.audioUrl,
    filename: stored.filename,
    duration: stored.duration,
    peakDbfs: stored.peakDbfs,
    segmentOrder: [...stored.segmentOrder],
    segmentStartSec: [...stored.segmentStartSec],
    headTrimSec: [...stored.headTrimSec],
    truncateToSec: [...stored.truncateToSec],
  };
}

export function saveSession(snapshot: SessionSnapshot): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(snapshot));
    return true;
  } catch {
    // 隐私模式 / 配额爆掉：静默降级成「不持久」，不能因此打断用户的正经操作
    return false;
  }
}

export function loadSession(): unknown {
  if (typeof window === "undefined") return null;
  try {
    const text = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!text) return null;
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function clearSession(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // 读不到就无所谓删
  }
}

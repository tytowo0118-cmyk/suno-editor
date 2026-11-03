import type { SectionKind, TimelineSection } from "./types";

export interface SectionStyle {
  accent: string;
  waveColor: string;
  progressColor: string;
  blockClass: string;
  chipClass: string;
  textClass: string;
  barClass: string;
}

export const DEFAULT_SONG_PROMPT =
  "夜晚城市 lo-fi，温暖克制的钢琴与柔和鼓点，空气感铺底，干净混音";

export const SECTION_META: Record<
  SectionKind,
  { title: string; english: string; hint: string }
> = {
  intro: {
    title: "前奏",
    english: "INTRO",
    hint: "从前奏进入，建立情绪基调，配器克制、留出空间",
  },
  verse: {
    title: "主歌",
    english: "VERSE",
    hint: "主歌推进叙事，加入稳定律动与低音支撑",
  },
  prechorus: {
    title: "预副歌",
    english: "PRE-CHORUS",
    hint: "预副歌逐步抬升张力，和声与配器逐渐加密",
  },
  chorus: {
    title: "副歌",
    english: "CHORUS",
    hint: "副歌释放情绪，旋律记忆点突出，配器最饱满",
  },
  bridge: {
    title: "桥段",
    english: "BRIDGE",
    hint: "桥段变化织体与和声，与前后形成对比和转折",
  },
  outro: {
    title: "尾奏",
    english: "OUTRO",
    hint: "尾奏收束情绪，逐渐减薄配器并自然淡出",
  },
};

export const SECTION_STYLES: Record<SectionKind, SectionStyle> = {
  intro: {
    accent: "#22d3ee",
    waveColor: "rgba(34, 211, 238, 0.28)",
    progressColor: "#a5f3fc",
    blockClass:
      "bg-gradient-to-br from-cyan-400/20 via-cyan-500/5 to-white/[0.02] border-cyan-300/30",
    chipClass: "border-cyan-300/20 bg-cyan-400/10 text-cyan-200",
    textClass: "text-cyan-200",
    barClass: "bg-cyan-300",
  },
  verse: {
    accent: "#a78bfa",
    waveColor: "rgba(167, 139, 250, 0.28)",
    progressColor: "#ddd6fe",
    blockClass:
      "bg-gradient-to-br from-violet-400/20 via-violet-500/5 to-white/[0.02] border-violet-300/30",
    chipClass: "border-violet-300/20 bg-violet-400/10 text-violet-200",
    textClass: "text-violet-200",
    barClass: "bg-violet-300",
  },
  prechorus: {
    accent: "#38bdf8",
    waveColor: "rgba(56, 189, 248, 0.28)",
    progressColor: "#bae6fd",
    blockClass:
      "bg-gradient-to-br from-sky-400/20 via-sky-500/5 to-white/[0.02] border-sky-300/30",
    chipClass: "border-sky-300/20 bg-sky-400/10 text-sky-200",
    textClass: "text-sky-200",
    barClass: "bg-sky-300",
  },
  chorus: {
    accent: "#fbbf24",
    waveColor: "rgba(251, 191, 36, 0.28)",
    progressColor: "#fde68a",
    blockClass:
      "bg-gradient-to-br from-amber-400/20 via-amber-500/5 to-white/[0.02] border-amber-300/30",
    chipClass: "border-amber-300/20 bg-amber-400/10 text-amber-200",
    textClass: "text-amber-200",
    barClass: "bg-amber-300",
  },
  bridge: {
    accent: "#34d399",
    waveColor: "rgba(52, 211, 153, 0.28)",
    progressColor: "#a7f3d0",
    blockClass:
      "bg-gradient-to-br from-emerald-400/20 via-emerald-500/5 to-white/[0.02] border-emerald-300/30",
    chipClass: "border-emerald-300/20 bg-emerald-400/10 text-emerald-200",
    textClass: "text-emerald-200",
    barClass: "bg-emerald-300",
  },
  outro: {
    accent: "#a1a1aa",
    waveColor: "rgba(161, 161, 170, 0.25)",
    progressColor: "#d4d4d8",
    blockClass:
      "bg-gradient-to-br from-zinc-400/15 via-zinc-500/5 to-white/[0.02] border-zinc-300/25",
    chipClass: "border-zinc-300/20 bg-zinc-400/10 text-zinc-300",
    textClass: "text-zinc-300",
    barClass: "bg-zinc-300",
  },
};

/**
 * 初始骨架。页面上方的「创作设定」会按歌词重新解析出完整段落，
 * 这里保留 3 段示例，避免空页面无从下手。
 */
export const INITIAL_SECTIONS: TimelineSection[] = [
  {
    id: "intro-1",
    kind: "intro",
    title: "前奏",
    english: "INTRO",
    durationSec: 10,
    prompt: "温暖克制的氛围前奏，钢琴与空气感铺底，为后续段落留出空间",
    lyrics: "",
    ...SECTION_STYLES.intro,
  },
  {
    id: "verse-1",
    kind: "verse",
    title: "主歌",
    english: "VERSE",
    durationSec: 20,
    prompt: "延续前奏情绪，加入舒缓鼓点与贝斯，保持 lo-fi 的松弛质感",
    lyrics: "",
    ...SECTION_STYLES.verse,
  },
  {
    id: "chorus-1",
    kind: "chorus",
    title: "副歌",
    english: "CHORUS",
    durationSec: 30,
    prompt: "情绪抬升，加入明亮旋律与更饱满的配器，形成可记忆的副歌动机",
    lyrics: "",
    ...SECTION_STYLES.chorus,
  },
];

// Suno 返回的是已混好的人声成品，只做 8ms 微淡入淡出消除接缝爆音，近似硬切，避免人声重影
export const CROSSFADE_MS = 8;

export function formatClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

export function buildTimings(sections: TimelineSection[]) {
  let cursor = 0;
  return sections.map((section) => {
    const startSec = cursor;
    cursor += section.durationSec;
    return { startSec, endSec: cursor };
  });
}

/**
 * 成品时间轴上的某一秒段 → 它落在哪个源片段、片段内偏移多少秒。
 * 起点由后端拼接时按实际重叠样本量算出，因此这里直接二分式地找最后一个不超过该点的段。
 * 拼接会物理剔除源片段头部静音，所以偏移要加回这段垫片，才能对回源片段自身的时间轴（续写用）。
 */
export function locateMixSegment(
  segmentOrder: string[],
  segmentStartSec: number[],
  timeSec: number,
  headTrimSec: number[] = [],
): { sectionId: string; offsetSec: number } | null {
  let found: { sectionId: string; offsetSec: number } | null = null;
  for (let index = 0; index < segmentOrder.length; index += 1) {
    const start = segmentStartSec[index];
    if (!Number.isFinite(start) || start > timeSec) break;
    found = {
      sectionId: segmentOrder[index],
      offsetSec: timeSec - start + (headTrimSec[index] ?? 0),
    };
  }
  return found;
}

export interface MixPlayhead {
  timeSec: number;
  segmentOrder: string[];
  segmentStartSec: number[];
  durationSec: number;
}

/**
 * 成品播放位置 → 结构时间轴色块轨道上的位置。
 * 色块宽度按 `durationSec` 分配，而成品里每段实际占用的长度是拼接后的长度（扣掉首部裁切与
 * 交叉淡化重叠），所以先定位当前落在哪一段，再按段内比例换算回该色块的宽度占比。
 * 未锁定的段落不参与成品，指针会直接跨过它们的色块。
 */
export function mixPlayheadPosition(
  sections: TimelineSection[],
  playhead: MixPlayhead,
): { sectionId: string; percent: number } | null {
  const total = sections.reduce((sum, section) => sum + section.durationSec, 0);
  if (total <= 0 || playhead.durationSec <= 0) return null;

  const located = locateMixSegment(
    playhead.segmentOrder,
    playhead.segmentStartSec,
    Math.min(Math.max(playhead.timeSec, 0), playhead.durationSec),
  );
  if (!located) return null;

  const index = sections.findIndex((section) => section.id === located.sectionId);
  if (index < 0) return null;

  const segmentIndex = playhead.segmentOrder.indexOf(located.sectionId);
  const segmentStart = playhead.segmentStartSec[segmentIndex] ?? 0;
  const segmentEnd =
    segmentIndex + 1 < playhead.segmentStartSec.length
      ? playhead.segmentStartSec[segmentIndex + 1]
      : playhead.durationSec;
  const segmentLength = Math.max(0.001, segmentEnd - segmentStart);
  const withinSec = Math.min(Math.max(playhead.timeSec - segmentStart, 0), segmentLength);

  const prefix = sections.slice(0, index).reduce((sum, section) => sum + section.durationSec, 0);
  const block = sections[index].durationSec;
  return {
    sectionId: located.sectionId,
    percent: ((prefix + (withinSec / segmentLength) * block) / total) * 100,
  };
}

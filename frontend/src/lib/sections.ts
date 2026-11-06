import type {
  SectionKind,
  StructureCandidate,
  StructureSegment,
  TimelineSection,
} from "./types";

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
 * 播放头吸附窗（秒）。
 *
 * 窗口不能按检测误差来定（候选自身 MAE 只有 0.062s，±0.25s 反而是亚像素、点不中），
 * 它是「误吸率」与「可点性」的折中：波形把整条时间轴压扁进容器宽度，落点只有像素那么粗。
 * 实测容器 477px：75s 成品 0.157s/px（窗口≈6px，好点）；492s 成品 1.031s/px（窗口≈1px，
 * 相邻像素一个吸一个不吸）。0.5s 把候选覆盖从 11.1% 压到 6.0%，代价是瞄准接缝±1像素的
 * 21 次点击只吸中 12 次（1.0s 时 19 次）。长成品点不中不该靠放大这个常数来救，
 * 而是用成品波形的横向缩放（×4 → 0.258s/px、窗口≈3.9px；×8 → 0.129s/px、窗口≈7.8px）。
 */
export const SNAP_WINDOW_SEC = 0.5;

/**
 * 把落点吸附到窗口内最近的候选边界。吸附窗口不区分来源（检测候选和模型边界一起吃），
 * 但**不**吸附 clip 级段边界：段边界在结构时间轴上就是色块分界，看得见；
 * 而且吸附到段起点会让续写点变成「该段第 0 秒」，正好撞上 continue_at 的 0.2s 下界被拒。
 */
export function snapToCandidates(
  timeSec: number,
  candidates: StructureCandidate[],
  windowSec: number = SNAP_WINDOW_SEC,
): { sec: number; snapped: boolean; fromSec: number } {
  let best: number | null = null;
  let bestDist = Infinity;
  for (const candidate of candidates) {
    const dist = Math.abs(candidate.sec - timeSec);
    if (dist <= windowSec && dist < bestDist) {
      best = candidate.sec;
      bestDist = dist;
    }
  }
  if (best == null) return { sec: timeSec, snapped: false, fromSec: timeSec };
  return { sec: best, snapped: true, fromSec: timeSec };
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

/** 成品时间轴上，某一段真实占用的长度。 */
export interface MixSegmentSpan {
  sectionId: string;
  /** 该段在成品时间轴上的起点（后端 `segment_start_sec` 原样） */
  startSec: number;
  /** 该段在成品里真正占用的秒数（含与下一段共享的那 8ms） */
  lengthSec: number;
  /** 占成品总长的百分比，直接当色带宽度用 */
  percent: number;
}

/**
 * 成品里每段**真实占用**的长度 —— 完全从后端回传的段起点反推，不猜、不重算 DSP。
 *
 * 拼接是 8ms 微淡入淡出，相邻两段共享这 8ms，所以第 i 段（i < n-1）占到
 * `start[i+1] - start[i] + crossfade`，最后一段占到成品结尾。
 * 自检恒等式 `Σ lengthSec - crossfade × (n-1) === durationSec`：实测三段
 * （起点 0 / 7.992018 / 185.144036，成品 193.144s）算出 8.000 / 177.160 / 8.000 s，
 * 相加 193.160 再减两条接缝的 16ms 正好回到 193.144。
 *
 * 这条函数存在的理由：规划色块按 `durationSec`（Suno 返回音频的自然长度）分配宽度，
 * 而无词段落进成品会被裁到规划上限，于是一条 182.6s 的前奏只占 8s —— 色带宽度
 * 和成品对不上。把真实占用摊成第二条色带，两处口径就能同屏对照。
 */
export function mixSegmentSpans(playhead: MixPlayhead): MixSegmentSpan[] {
  const { segmentOrder, segmentStartSec, durationSec } = playhead;
  if (!(durationSec > 0) || segmentOrder.length === 0) return [];

  const overlapSec = CROSSFADE_MS / 1000;
  return segmentOrder.map((sectionId, index) => {
    const startSec = segmentStartSec[index] ?? 0;
    const nextStart =
      index + 1 < segmentOrder.length ? (segmentStartSec[index + 1] ?? durationSec) : durationSec;
    // 最后一段占到结尾，其余段把与下一段重叠的那 8ms 也算进自己的占用
    const lengthSec = Math.max(
      0,
      nextStart - startSec + (index + 1 < segmentOrder.length ? overlapSec : 0),
    );
    return {
      sectionId,
      startSec,
      lengthSec,
      percent: (lengthSec / durationSec) * 100,
    };
  });
}

/** 模型边界与检测候选重合到这个精度以内就只留检测候选那条（它定位更准）。 */
const MERGE_DEDUPE_SEC = 0.05;

/** 模型段 → 内部边界秒数：首段起点恒为 0，那是文件头不是可裁切的内部接缝。 */
export function structureBoundarySec(segments: StructureSegment[]): number[] {
  return segments.slice(1).map((segment) => segment.start);
}

/**
 * 吸附目标 = 检测候选 ∪ 模型段边界。
 *
 * 曾经的做法是反过来用模型边界「收窄」检测候选，实测否决了：dlp-B（127.887 s，2 条毫秒真值接缝）
 * 上模型的第二个边界比检测器晚 4.42 s（96.99 vs 真值 92.567），±1 s 的收窄窗把检测器在那条
 * 接缝上唯一吸得上的候选（92.944，误差 0.377 s）连坐删掉 —— 收窄窗 0.5/1/2 s 三档的
 * 「真接缝存活率」都是 0/2，而检测器自己本来是 1/2。减候选数减掉的是真边界，那不是降噪是截肢。
 *
 * 并集才讲得通：这个模型的边界误差比检测器**不稳**（0.125 s 与 4.42 s 同时出现在一首歌里），
 * 但它是那条 31.375 s 接缝上唯一存在的证据（检测器最近候选差 2.53 s，模型给的 31.50 差 0.125 s）。
 * 两边各有各看不见的接缝，取并集后 dlp-B 的 ±0.5 s 可吸接缝从 1/2 变 2/2。
 * 语义标签另有一用：波形下方的分段色带读的是段，不是点。
 */
export function mergeSnapPoints(
  candidates: StructureCandidate[],
  segments: StructureSegment[],
): StructureCandidate[] {
  const boundaries = structureBoundarySec(segments).filter(
    (sec) => candidates.every((candidate) => Math.abs(candidate.sec - sec) > MERGE_DEDUPE_SEC),
  );
  return [
    ...candidates,
    ...boundaries.map((sec) => ({ sec, strength: 0, source: "model" as const })),
  ].sort((a, b) => a.sec - b.sec);
}

/** 语义标签的展示用语：模型的 harmonix 标签读起来是数据集名，不是音乐用语。 */
const SEGMENT_LABEL_CN: Record<string, string> = {
  intro: "前奏",
  verse: "主歌",
  chorus: "副歌",
  bridge: "桥段",
  instrumental: "器乐",
  outro: "尾奏",
  break: "间奏",
  solo: "独奏",
  section: "段落",
};

export function segmentLabelCn(label: string): string {
  return SEGMENT_LABEL_CN[label] ?? label;
}

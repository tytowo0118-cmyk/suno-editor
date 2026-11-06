export type SectionKind = "intro" | "verse" | "prechorus" | "chorus" | "bridge" | "outro";

export interface TimelineSection {
  id: string;
  kind: SectionKind;
  title: string;
  english: string;
  durationSec: number;
  prompt: string;
  lyrics: string;
  accent: string;
  waveColor: string;
  progressColor: string;
  blockClass: string;
  chipClass: string;
  textClass: string;
  barClass: string;
}

export interface Take {
  id: string;
  label: string;
  /** Blob URL（刚生成）或后端 /static 地址（已落盘） */
  audioUrl: string;
  duration: number;
  /** Suno 侧的 clip id，续写（continue_at）时作为源片段 */
  clipId?: string | null;
}

export interface StitchResult {
  audioUrl: string;
  filename: string;
  duration: number;
  peakDbfs: number;
  segmentOrder: string[];
  /** 每段在成品时间轴上的起点秒数，与 segmentOrder 一一对应 */
  segmentStartSec: number[];
  /** 拼接时物理剔除的每段头部静音秒数，换算回源片段自身时间轴要加回来 */
  headTrimSec: number[];
  /** 每段按 maxDurationSec 实际裁保留的秒数；null = 这段没裁（不超限或没给上限） */
  truncateToSec: (number | null)[];
}

/** 某一秒段相对整条时间轴的起止位置 */
export interface SectionTiming {
  startSec: number;
  endSec: number;
}

/** 结构候选：sec 是成品时间轴上的秒数。strength 是谱新颖度（仅检测器给），
 *  source 标出这条证据来自哪里 —— 检测器定位到毫秒但没有语义，模型带语义标签但只到 ±0.1~0.5 s。 */
export interface StructureCandidate {
  sec: number;
  strength: number;
  source?: "detector" | "model";
}

/** 深度学习结构模型的分段：带 intro/verse/chorus 语义标签，边界定位精度 ±0.1~0.5 s */
export interface StructureSegment {
  label: string;
  start: number;
  end: number;
}

export type StructureJobStatus = "queued" | "running" | "done" | "failed";

/** 结构分析任务：提交后轮询，终态是 done / failed（failed 也带一段兜底区间） */
export interface StructureJob {
  jobId: string;
  clipId: string;
  filename: string;
  status: StructureJobStatus;
  final: boolean;
  durationSec: number;
  segments: StructureSegment[];
  elapsedSec: number;
  analyzeMs?: number;
  detail?: string;
  reused: boolean;
  model: string;
}

/**
 * 底部提示的两种级别，判据是「谁没做到该做的事」。
 * info 是用户操作还没满足条件或纯反馈，3.2s 自动消失没关系；
 * error 是我们这边失败了 —— 网络、后端、扣费相关的丢失，一闪而过就等于把证据藏起来，
 * 所以常驻到用户手动关闭（413 拒掉已扣费音频那轮就是被自动消失坑的，见 README §2.4）。
 */
export type NoticeLevel = "info" | "error";

export interface Notice {
  text: string;
  level: NoticeLevel;
}

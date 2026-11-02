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
}

/** 某一秒段相对整条时间轴的起止位置 */
export interface SectionTiming {
  startSec: number;
  endSec: number;
}

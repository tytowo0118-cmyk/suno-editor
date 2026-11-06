import { SECTION_META, SECTION_STYLES } from "./sections";
import type { SectionKind, TimelineSection } from "./types";

/** 结构过长会让逐段生成与拼接成本失控，超过部分直接截断 */
export const MAX_SECTIONS = 12;
const MAX_PROMPT_CHARS = 500;
const MAX_LYRIC_LINES_PER_SECTION = 24;

export interface LyricBlock {
  kind: SectionKind;
  label: string;
  number?: number;
  lyrics: string;
}

const TAG_RE = /^\s*[\[【(（]\s*([^\]】)）]{1,24}?)\s*[\]】)）]\s*$/;

const TAG_ALIASES: Record<string, SectionKind> = {
  intro: "intro",
  前奏: "intro",
  引子: "intro",
  开场: "intro",
  verse: "verse",
  主歌: "verse",
  a段: "verse",
  prechorus: "prechorus",
  预副歌: "prechorus",
  导歌: "prechorus",
  chorus: "chorus",
  副歌: "chorus",
  b段: "chorus",
  hook: "chorus",
  高潮: "chorus",
  bridge: "bridge",
  桥段: "bridge",
  过桥: "bridge",
  间奏: "bridge",
  outro: "outro",
  尾奏: "outro",
  尾声: "outro",
  结束: "outro",
};

const CN_NUMBERS: Record<string, number> = {
  一: 1,
  二: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  七: 7,
  八: 8,
  九: 9,
  十: 10,
};

function classifyTag(raw: string): { kind: SectionKind; number?: number } | null {
  let text = raw.trim().toLowerCase();
  let number: number | undefined;

  const arabic = text.match(/(\d+)\s*$/);
  if (arabic?.index !== undefined) {
    number = Number(arabic[1]);
    text = text.slice(0, arabic.index).trim();
  } else {
    const chinese = text.match(/([一二三四五六七八九十])\s*$/);
    if (chinese?.index !== undefined) {
      number = CN_NUMBERS[chinese[1]];
      text = text.slice(0, chinese.index).trim();
    }
  }

  const key = text.replace(/[\s\-_·.]+/g, "");
  const kind = TAG_ALIASES[key];
  return kind ? { kind, number } : null;
}

function cleanLines(lines: string[]): string[] {
  const result = [...lines];
  while (result.length > 0 && !result[0].trim()) result.shift();
  while (result.length > 0 && !result[result.length - 1].trim()) result.pop();
  return result;
}

function chunkLines(lines: string[], size: number): string[][] {
  const chunks: string[][] = [];
  for (let index = 0; index < lines.length; index += size) {
    chunks.push(lines.slice(index, index + size));
  }
  return chunks;
}

/** 无标签时按空行分段；整段没有空行则每 4 行切一段 */
function parseParagraphs(text: string): LyricBlock[] {
  const paragraphs = text
    .split(/\n\s*\n/)
    .map((paragraph) =>
      paragraph
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    )
    .filter((paragraph) => paragraph.length > 0);

  let groups = paragraphs;
  if (groups.length === 1 && groups[0].length > 4) {
    groups = chunkLines(groups[0], 4);
  }
  if (groups.length === 0) return [];

  const total = groups.length;
  return groups.map((lines, index) => {
    let kind: SectionKind = index % 2 === 0 ? "verse" : "chorus";
    if (total >= 2 && index === total - 1) kind = "chorus";
    return {
      kind,
      label: kind === "verse" ? "Verse" : "Chorus",
      lyrics: lines.slice(0, MAX_LYRIC_LINES_PER_SECTION).join("\n"),
    };
  });
}

/**
 * 解析歌词。识别 [Intro] / [Verse 1] / [Pre-Chorus] / [Chorus] / [Bridge] /
 * [Outro] 以及【主歌】【副歌】等中文标签；没有标签时按段落启发式切分。
 */
export function parseLyricBlocks(text: string): LyricBlock[] {
  const normalized = text.replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");
  const hasTag = lines.some((line) => {
    const match = line.match(TAG_RE);
    return match ? classifyTag(match[1]) !== null : false;
  });
  if (!hasTag) return parseParagraphs(normalized);

  const blocks: LyricBlock[] = [];
  let kind: SectionKind | null = null;
  let number: number | undefined;
  let label = "";
  let buffer: string[] = [];

  const flush = () => {
    if (!kind) return;
    blocks.push({
      kind,
      label,
      number,
      lyrics: cleanLines(buffer).slice(0, MAX_LYRIC_LINES_PER_SECTION).join("\n"),
    });
    buffer = [];
  };

  for (const line of lines) {
    const match = line.match(TAG_RE);
    const tag = match ? classifyTag(match[1]) : null;

    if (tag) {
      flush();
      kind = tag.kind;
      number = tag.number;
      label = match![1].trim();
      continue;
    }

    if (!kind) {
      if (!line.trim()) continue;
      // 标签之前的正文按主歌处理，避免丢失手写内容
      kind = "verse";
      label = "Verse";
    }
    buffer.push(line.trimEnd());
  }
  flush();

  return blocks;
}

function countLyricLines(lyrics: string): number {
  return lyrics.split("\n").filter((line) => line.trim()).length;
}

function estimateDuration(kind: SectionKind, lyrics: string): number {
  const lines = countLyricLines(lyrics);
  if (lines === 0) return kind === "intro" || kind === "outro" ? 8 : 12;

  const seconds = lines * 4 + 4; // 每句约 4s，另留换气与器乐余量
  const max =
    kind === "verse" || kind === "chorus" ? 60 : kind === "intro" || kind === "outro" ? 30 : 40;
  return Math.round(Math.min(max, Math.max(8, seconds)));
}

/** 无词段落在拼接侧最多保留几秒；有歌词就返回 undefined，不裁。
 *  这类段落发给上游的是 `make_instrumental=true` + 空歌词，Suno 没有时长字段可吃，
 *  一律写满一整首（实测 177~193s），只能靠这个规划秒数在拼接时兑现。
 *  刻意不读 `section.durationSec`：生成后它会被 applyNaturalDuration 覆写成返回音频的实际长度，
 *  拿 182.6s 去裁 182.6s 的素材等于什么都没做。 */
export function plannedTruncateSec(section: TimelineSection): number | undefined {
  return section.lyrics.trim() ? undefined : estimateDuration(section.kind, section.lyrics);
}

function firstLyricLine(lyrics: string): string {
  return (
    lyrics
      .split("\n")
      .map((line) => line.trim())
      .find(Boolean) ?? ""
  );
}

/** 全局风格提示词 + 段落角色 + 首句意象，控制在 500 字以内 */
export function composeSectionPrompt(
  songPrompt: string,
  kind: SectionKind,
  lyrics: string,
): string {
  const parts = [songPrompt.trim(), SECTION_META[kind].hint];
  const cue = firstLyricLine(lyrics);
  if (cue) parts.push(`情绪线索：${cue.slice(0, 24)}`);
  return parts
    .filter(Boolean)
    .join("；")
    .slice(0, MAX_PROMPT_CHARS);
}

const SUNO_TAG_NAMES: Record<SectionKind, string> = {
  intro: "Intro",
  verse: "Verse",
  prechorus: "Pre-Chorus",
  chorus: "Chorus",
  bridge: "Bridge",
  outro: "Outro",
};

/** 由段落序号生成 Suno 风格标签：[Verse 1] / [Chorus 2] / [Bridge] */
export function sectionTag(section: TimelineSection): string {
  const ordinal = Number(section.id.slice(section.id.lastIndexOf("-") + 1));
  const number = Number.isFinite(ordinal) && ordinal > 0 ? ordinal : 1;
  const numbered =
    section.kind === "verse" ||
    section.kind === "chorus" ||
    section.kind === "prechorus" ||
    number > 1;
  const name = SUNO_TAG_NAMES[section.kind];
  return `[${numbered ? `${name} ${number}` : name}]`;
}

/** 发给 Suno 的纯文本：风格提示 + 结构标签 + 本段歌词 */
export function buildGenerationPrompt(section: TimelineSection, stylePrompt: string): string {
  const style = stylePrompt.trim();
  const lyrics = section.lyrics.trim();
  return [style, sectionTag(section), lyrics].filter(Boolean).join("\n\n");
}

function buildSections(blocks: LyricBlock[], songPrompt: string): TimelineSection[] {
  const totals = new Map<SectionKind, number>();
  blocks.forEach((block) => totals.set(block.kind, (totals.get(block.kind) ?? 0) + 1));

  const seen = new Map<SectionKind, number>();
  return blocks.map((block) => {
    const ordinal = (seen.get(block.kind) ?? 0) + 1;
    seen.set(block.kind, ordinal);

    const repeated = (totals.get(block.kind) ?? 0) > 1;
    const suffix = block.number ?? (repeated ? ordinal : undefined);
    const meta = SECTION_META[block.kind];

    return {
      id: `${block.kind}-${ordinal}`,
      kind: block.kind,
      title: suffix ? `${meta.title} ${suffix}` : meta.title,
      english: suffix ? `${meta.english} ${suffix}` : meta.english,
      durationSec: estimateDuration(block.kind, block.lyrics),
      prompt: composeSectionPrompt(songPrompt, block.kind, block.lyrics),
      lyrics: block.lyrics,
      ...SECTION_STYLES[block.kind],
    };
  });
}

/** 歌词 → 时间轴段落；顺序、时长与配色都由歌词标签推导 */
export function buildSectionsFromLyrics(
  lyrics: string,
  songPrompt: string,
): TimelineSection[] {
  const blocks = parseLyricBlocks(lyrics).slice(0, MAX_SECTIONS);
  return buildSections(blocks, songPrompt);
}

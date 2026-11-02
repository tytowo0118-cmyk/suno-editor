"use client";

import { MAX_SECTIONS } from "@/lib/lyrics";
import type { TimelineSection } from "@/lib/types";

interface SongBriefPanelProps {
  songPrompt: string;
  onSongPromptChange: (value: string) => void;
  lyrics: string;
  onLyricsChange: (value: string) => void;
  lyricsLoading: boolean;
  lyricsError: string | null;
  onGenerateLyrics: () => void;
  onConfirm: () => void;
  previewSections: TimelineSection[];
  previewBlockCount: number;
  previewError: string | null;
  hasWork: boolean;
}

function lyricLineCount(lyrics: string): number {
  return lyrics.split("\n").filter((line) => line.trim()).length;
}

/** 第一页：歌词与风格设定，确认后按歌词切分段落 */
export default function SongBriefPanel({
  songPrompt,
  onSongPromptChange,
  lyrics,
  onLyricsChange,
  lyricsLoading,
  lyricsError,
  onGenerateLyrics,
  onConfirm,
  previewSections,
  previewBlockCount,
  previewError,
  hasWork,
}: SongBriefPanelProps) {
  const totalDuration = previewSections.reduce((sum, section) => sum + section.durationSec, 0);
  const truncated = previewBlockCount > MAX_SECTIONS;
  const canConfirm = Boolean(lyrics.trim()) && !previewError && previewSections.length > 0;

  return (
    <div className="page-enter mx-auto max-w-5xl">
      <section className="rounded-3xl border border-white/10 bg-gradient-to-br from-white/[0.06] via-white/[0.02] to-transparent p-5 backdrop-blur-sm sm:p-7">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[10px] tracking-[0.3em] text-cyan-300/80">
              STEP 01 · SONG BRIEF
            </p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight text-white sm:text-3xl">
              创作设定
            </h2>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-zinc-400">
              写下歌词与风格提示词，歌词中的 [Verse] / [Chorus] 标签决定段落划分；
              每段实际时长以 Suno 返回音频为准。确认后进入逐段生成。
            </p>
          </div>
          {hasWork && (
            <span className="rounded-full border border-amber-400/25 bg-amber-400/[0.08] px-3 py-1.5 text-[11px] text-amber-200">
              已有生成结果 · 重新确认会重建结构
            </span>
          )}
        </div>

        <div className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <div className="flex flex-col">
            <label
              htmlFor="song-prompt"
              className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500"
            >
              歌曲提示词 / Song Prompt
            </label>
            <textarea
              id="song-prompt"
              value={songPrompt}
              onChange={(event) => onSongPromptChange(event.target.value)}
              maxLength={500}
              rows={9}
              placeholder="例如：夜晚城市 lo-fi，温暖克制的钢琴与柔和鼓点"
              className="mt-1.5 min-h-[220px] flex-1 resize-y rounded-xl border border-white/10 bg-black/25 px-3.5 py-3 text-xs leading-relaxed text-zinc-200 outline-none transition placeholder:text-zinc-600 focus:border-white/30 focus:bg-black/40"
            />
            <p className="mt-2 text-[10px] leading-relaxed text-zinc-600">
              作为每一段 Suno prompt 的风格前缀，留空则只使用段落角色描述。
            </p>
          </div>

          <div className="flex flex-col">
            <div className="flex items-center justify-between gap-2">
              <label
                htmlFor="lyrics"
                className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500"
              >
                歌词 / Lyrics
              </label>
              <span className="font-mono text-[10px] text-zinc-600">{lyrics.length} 字</span>
            </div>
            <textarea
              id="lyrics"
              value={lyrics}
              onChange={(event) => onLyricsChange(event.target.value)}
              rows={9}
              placeholder={
                "[Verse 1]\n写下你的第一段歌词\n\n[Chorus]\n副歌的记忆点\n\n也支持【主歌】【副歌】；没有标签时按空行分段"
              }
              className="mt-1.5 min-h-[220px] w-full flex-1 resize-y rounded-xl border border-white/10 bg-black/25 px-3.5 py-3 font-mono text-xs leading-relaxed text-zinc-200 outline-none transition placeholder:font-sans placeholder:text-zinc-600 focus:border-white/30 focus:bg-black/40"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={onGenerateLyrics}
                disabled={lyricsLoading}
                className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.05] px-3 text-xs text-zinc-300 transition hover:border-white/25 hover:bg-white/[0.09] disabled:cursor-wait disabled:opacity-50"
              >
                <SparkleIcon />
                {lyricsLoading ? "生成中" : "AI 生成歌词"}
              </button>
              <span className="font-mono text-[10px] text-zinc-600">
                INTRO / VERSE / PRE-CHORUS / CHORUS / BRIDGE / OUTRO
              </span>
            </div>
            {lyricsError && (
              <p className="mt-2 text-[11px] text-rose-300">歌词生成失败：{lyricsError}</p>
            )}
          </div>
        </div>

        <div className="mt-6 rounded-2xl border border-white/[0.08] bg-black/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="h-1.5 w-1.5 rounded-full bg-violet-400" />
              <h3 className="text-xs font-medium text-white">段落预览</h3>
              <span className="rounded-full border border-white/10 px-2 py-0.5 font-mono text-[9px] tracking-wider text-zinc-500">
                SECTION PREVIEW
              </span>
            </div>
            {previewSections.length > 0 && (
              <span className="font-mono text-[10px] text-zinc-500">
                {previewSections.length} 段 · 总长 {totalDuration}s
              </span>
            )}
          </div>

          {previewError ? (
            <p className="mt-3 text-[11px] text-rose-300">{previewError}</p>
          ) : previewSections.length === 0 ? (
            <p className="mt-3 rounded-xl border border-dashed border-white/10 px-4 py-6 text-center text-[11px] text-zinc-600">
              输入歌词后，这里会实时显示将要生成的段落与时长
            </p>
          ) : (
            <>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {previewSections.map((section, index) => (
                  <span
                    key={section.id}
                    className={`inline-flex items-center gap-2 rounded-xl border px-3 py-2 ${section.blockClass}`}
                  >
                    <span className="font-mono text-[9px] text-white/40">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span className="text-xs font-medium text-white">{section.title}</span>
                    <span className="font-mono text-[10px] text-white/50">
                      {section.durationSec}s
                    </span>
                    {section.lyrics && (
                      <span className="font-mono text-[9px] text-white/30">
                        {lyricLineCount(section.lyrics)} 行
                      </span>
                    )}
                  </span>
                ))}
              </div>
              {truncated && (
                <p className="mt-2 text-[10px] text-amber-300/80">
                  歌词共 {previewBlockCount} 段，仅保留前 {MAX_SECTIONS} 段。
                </p>
              )}
            </>
          )}
        </div>

        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[11px] leading-relaxed text-zinc-500">
            进入后点击时间轴可切换生成框；设定不变时随时可以回来，不会丢失已生成的候选。
          </p>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!canConfirm}
            className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-xl bg-white px-5 text-sm font-semibold text-zinc-900 transition hover:bg-zinc-200 disabled:cursor-not-allowed disabled:bg-white/10 disabled:text-zinc-500"
          >
            进入逐段生成
            <ArrowIcon />
          </button>
        </div>
      </section>
    </div>
  );
}

function SparkleIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M8 2.2c.5 2.5 1.3 3.3 3.8 3.8-2.5.5-3.3 1.3-3.8 3.8-.5-2.5-1.3-3.3-3.8-3.8C6.7 5.5 7.5 4.7 8 2.2ZM12.5 9.4c.3 1.4.8 1.9 2.2 2.2-1.4.3-1.9.8-2.2 2.2-.3-1.4-.8-1.9-2.2-2.2 1.4-.3 1.9-.8 2.2-2.2Z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 8h10M9.5 4.5 13 8l-3.5 3.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

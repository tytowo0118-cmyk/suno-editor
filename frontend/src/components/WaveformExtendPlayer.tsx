"use client";

import { useState } from "react";
import { sectionTag } from "@/lib/lyrics";
import { continueTakes } from "@/lib/musicgen";
import { formatClock, locateMixSegment } from "@/lib/sections";
import type { StitchResult, Take, TimelineSection } from "@/lib/types";
import WaveformPlayer, { pauseAllAudio } from "./WaveformPlayer";

interface WaveformExtendPlayerProps {
  mix: StitchResult;
  sections: TimelineSection[];
  locked: Record<string, Take>;
  prompts: Record<string, string>;
  /** 续写结果挂到该段落，参数为段落 id 与本次新增的候选（含双生片段） */
  onTakes: (sectionId: string, takes: Take[]) => void;
  /** 把续写点所在段落切到前台，便于试听新候选 */
  onActivate: (sectionId: string) => void;
  onNotice: (message: string) => void;
}

/**
 * 成品整曲的波形播放器：点击取截断点，把全曲时间换算回该段 Suno clip 的段内秒数
 * （含后端首部静音裁切的偏移补偿），以 continue_at 请求续写。
 */
export default function WaveformExtendPlayer({
  mix,
  sections,
  locked,
  prompts,
  onTakes,
  onActivate,
  onNotice,
}: WaveformExtendPlayerProps) {
  const [point, setPoint] = useState<number | null>(null);
  const [lyrics, setLyrics] = useState("");
  const [busy, setBusy] = useState(false);

  const target =
    point != null
      ? locateMixSegment(mix.segmentOrder, mix.segmentStartSec, point, mix.headTrimSec)
      : null;
  const targetSection = target
    ? sections.find((section) => section.id === target.sectionId)
    : undefined;
  const targetTake = target ? locked[target.sectionId] : undefined;
  const canExtend = Boolean(
    target &&
      targetTake?.clipId &&
      target.offsetSec >= 0.2 &&
      target.offsetSec < (targetTake?.duration ?? 0) - 0.5,
  );

  const handleExtend = async () => {
    if (!target || point == null) return;
    if (!targetSection || !targetTake) {
      onNotice("续写点所在的段落没有锁定音频，无法续写");
      return;
    }
    if (!targetTake.clipId) {
      onNotice("该段音频缺少 Suno clip id（后端未回传），无法续写，请重新生成这一段");
      return;
    }
    if (target.offsetSec >= targetTake.duration - 0.5) {
      onNotice("续写点太接近该段结尾，请点在段落中间位置");
      return;
    }

    // 留空时歌词为空 → 后端把该段标记为纯器乐，续写出器乐尾
    const style = prompts[targetSection.id] ?? targetSection.prompt;
    const prompt = [style, `${sectionTag(targetSection)}\n${lyrics.trim()}`]
      .filter(Boolean)
      .join("\n\n");

    pauseAllAudio();
    setBusy(true);
    try {
      const extended = await continueTakes({
        sectionId: targetSection.id,
        clipId: targetTake.clipId,
        continueAt: target.offsetSec,
        prompt,
        durationSec: targetTake.duration,
      });
      // 上游返回的是「以点击点为上下文新生成」的音频，不替换锁定，只作为该段额外候选
      onTakes(targetSection.id, extended);
      onActivate(targetSection.id);
      onNotice(
        `已按续写点生成「${targetSection.title}」的 ${extended.length} 个新候选（${extended
          .map((item) => `${item.duration.toFixed(1)}s`)
          .join(" / ")}），试听后再决定是否锁定`,
      );
    } catch (error) {
      onNotice(error instanceof Error ? `续写失败：${error.message}` : "续写失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="mt-4 max-w-3xl">
        <WaveformPlayer
          src={mix.audioUrl}
          playerId="final-mix"
          accent="#34d399"
          waveColor="rgba(52, 211, 153, 0.28)"
          progressColor="#a7f3d0"
          durationSec={mix.duration}
          markerSec={point}
          onPoint={setPoint}
        />
      </div>
      <p className="mt-2 font-mono text-[10px] text-zinc-500">
        {mix.duration.toFixed(2)}s · peak {mix.peakDbfs.toFixed(1)} dBFS ·{" "}
        {mix.segmentOrder.join(" → ")}
      </p>

      <div className="mt-4 rounded-2xl border border-white/[0.06] bg-black/20 px-4 py-3.5">
        <p className="text-[11px] leading-relaxed text-zinc-400">
          点击成品波形选择续写点，后端把全曲时间换算回该段 Suno clip 的段内秒数，以
          <span className="font-mono text-zinc-300"> continue_at </span>
          请求续写：该点之前的音频作为上下文，其后重新生成，结果作为该段的新候选追加在下方，
          试听满意再锁定。
        </p>
        {point != null && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-emerald-400/20 bg-emerald-400/[0.08] px-2.5 py-1 font-mono text-[10px] text-emerald-300">
              {formatClock(point)} → {targetSection?.title ?? target?.sectionId ?? "未知段落"} · 段内{" "}
              {(target?.offsetSec ?? 0).toFixed(1)}s
            </span>
            <input
              value={lyrics}
              onChange={(event) => setLyrics(event.target.value)}
              maxLength={500}
              placeholder="续写歌词（留空则接一段器乐）"
              className="h-9 min-w-0 flex-1 rounded-lg border border-white/10 bg-black/30 px-3 text-xs text-zinc-200 outline-none transition placeholder:text-zinc-600 focus:border-white/30"
            />
            <button
              type="button"
              onClick={() => void handleExtend()}
              disabled={!canExtend || busy}
              title={
                targetTake?.clipId
                  ? "消耗一个 Suno 任务额度"
                  : "该段音频缺少 Suno clip id，无法续写"
              }
              className="inline-flex h-9 shrink-0 items-center gap-2 rounded-lg border border-emerald-300/25 bg-emerald-400/[0.08] px-3 text-xs text-emerald-200 transition hover:bg-emerald-400/[0.15] disabled:cursor-not-allowed disabled:border-white/10 disabled:bg-white/[0.04] disabled:text-zinc-600"
            >
              {busy && (
                <span className="h-3 w-3 animate-spin rounded-full border border-emerald-200/30 border-t-emerald-200" />
              )}
              {busy ? "续写中" : "从此处续写"}
            </button>
          </div>
        )}
        {point != null && !canExtend && !busy && (
          <p className="mt-2 text-[10px] text-amber-300/80">
            {!targetTake?.clipId
              ? "该段音频缺少 Suno clip id，请重新生成并锁定后再续写"
              : "续写点需要落在段落中段（距首尾各 0.5s 以上）"}
          </p>
        )}
      </div>
    </>
  );
}

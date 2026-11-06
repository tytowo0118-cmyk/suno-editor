"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { clipIdFromAudioUrl, fetchStructureBoundaries, runStructureJob } from "@/lib/api";
import { sectionTag } from "@/lib/lyrics";
import { continueTakes } from "@/lib/musicgen";
import {
  formatClock,
  locateMixSegment,
  mergeSnapPoints,
  SNAP_WINDOW_SEC,
  segmentLabelCn,
} from "@/lib/sections";
import type {
  NoticeLevel,
  StitchResult,
  StructureCandidate,
  StructureJob,
  StructureSegment,
  Take,
  TimelineSection,
} from "@/lib/types";
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
  /** 底部提示；省略 level 即 info（3.2s 自动消失），"error" 常驻到用户手动关闭 */
  onNotice: (message: string, level?: NoticeLevel) => void;
  /** 成品播放进度（约 20Hz），用于让结构时间轴上的色块同步滑动指针 */
  onTimeUpdate?: (timeSec: number) => void;
}

/** 缓存命中的分析是几十毫秒，写成「0.0s」会让人以为没跑 */
function formatDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
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
  onTimeUpdate,
}: WaveformExtendPlayerProps) {
  const [point, setPoint] = useState<number | null>(null);
  const [lyrics, setLyrics] = useState("");
  const [busy, setBusy] = useState(false);
  const [candidates, setCandidates] = useState<StructureCandidate[]>([]);
  const [snapped, setSnapped] = useState<{ fromSec: number; toSec: number } | null>(null);
  const [dlSegments, setDlSegments] = useState<StructureSegment[]>([]);
  const [dlJob, setDlJob] = useState<StructureJob | null>(null);

  // 结构候选是成品自身的属性；Studio 用 mix.filename 作 key 重挂本组件，所以换成品会自然清空。
  // 分析失败只是没有吸附，不影响试听与续写。
  useEffect(() => {
    let disposed = false;
    void fetchStructureBoundaries(mix.audioUrl)
      .then((result) => {
        if (!disposed) setCandidates(result.candidates);
      })
      .catch((error) => console.error("结构边界分析失败:", error));
    return () => {
      disposed = true;
    };
  }, [mix.audioUrl]);

  // 深度学习分段是分钟级的活儿，所以提交 + 轮询；组件卸载就停止轮询，
  // 后台任务不取消（下一次打开同一成品会命中同一个 job，不重跑分轨）。
  // 换成品由 Studio 的 key={mix.filename} 重挂本组件，所以这里不需要清状态。
  const cancelledRef = useRef(false);
  useEffect(() => {
    cancelledRef.current = false;
    void runStructureJob(clipIdFromAudioUrl(mix.audioUrl), {
      onJob: (job) => {
        if (cancelledRef.current) return;
        setDlJob(job);
        if (job.final) setDlSegments(job.segments);
      },
      isCancelled: () => cancelledRef.current,
    }).catch((error: unknown) => {
      if (cancelledRef.current) return;
      // 任务反复丢失时要把状态落到 failed，否则状态行会永远停在「分析中」。
      setDlJob((prev) =>
        prev
          ? {
              ...prev,
              status: "failed",
              final: true,
              detail: String(error instanceof Error ? error.message : error).slice(0, 200),
            }
          : prev,
      );
      console.error("结构分析任务失败:", error);
    });
    return () => {
      cancelledRef.current = true;
    };
  }, [mix.audioUrl]);

  // 吸附目标 = 检测候选（毫秒定位）∪ 模型段边界（检测器漏掉的接缝往往只有它有）。
  // 模型没出来时就是原来的全量候选，界面不会因为分析中而少掉吸附能力。
  const snapPoints = useMemo(
    () => mergeSnapPoints(candidates, dlSegments),
    [candidates, dlSegments],
  );
  const modelAdded = snapPoints.length - candidates.length;

  // 模型任务是分钟级的，状态要说清楚在做什么、等了多久，否则界面看着像卡死。
  const dlStatusLine = (() => {
    if (!dlJob) return "模型分段 · 提交中";
    switch (dlJob.status) {
      case "queued":
        return `模型分段 · 排队中（分析容器一次只跑一首，已等 ${dlJob.elapsedSec.toFixed(0)}s）`;
      case "running":
        // 服务只有一条流水线，「已用」里可能全是排队，界面别把它说成一定在算。
        return `模型分段 · 分析中（已用 ${dlJob.elapsedSec.toFixed(0)}s，含在服务内排队；分轨占约 1 倍音频时长）`;
      case "done":
        // done 一定是真结果：任务表里任何异常都会收敛成 failed，degraded 只跟 failed 同时出现。
        return `模型分段 · ${dlJob.segments.length} 段 · ${formatDuration(
          dlJob.analyzeMs ?? 0,
        )}${dlJob.reused ? " · 复用已有结果" : ""}`;
      default:
        return `模型分段 · 失败${dlJob.detail ? `（${dlJob.detail}）` : ""}`;
    }
  })();

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
      onNotice("该段音频缺少 Suno clip id（后端未回传），无法续写，请重新生成这一段", "error");
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
      onNotice(error instanceof Error ? `续写失败：${error.message}` : "续写失败", "error");
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
          onTimeUpdate={onTimeUpdate}
          snapPoints={snapPoints}
          onSnap={(fromSec, toSec) => setSnapped({ fromSec, toSec })}
          zoomable
        />
      </div>
      <p className="mt-2 font-mono text-[10px] text-zinc-500">
        {mix.duration.toFixed(2)}s · peak {mix.peakDbfs.toFixed(1)} dBFS ·{" "}
        {mix.segmentOrder.join(" → ")}
      </p>
      <p className="mt-1 font-mono text-[10px] text-zinc-600">
        {candidates.length > 0
          ? `结构候选 ${candidates.length} 个 · 落点在 ±${SNAP_WINDOW_SEC}s 内自动吸附`
          : "结构候选 0 个 · 未检出可吸附的内部边界"}
        {modelAdded > 0
          ? ` · 模型边界 +${modelAdded} 个（蓝色刻度），吸附点共 ${snapPoints.length} 个`
          : ""}
        {snapped && point != null && Math.abs(point - snapped.toSec) < 0.001
          ? ` · 已吸附 ${snapped.fromSec.toFixed(2)}s → ${snapped.toSec.toFixed(2)}s`
          : ""}
      </p>
      <p className="mt-1 font-mono text-[10px] text-zinc-600">{dlStatusLine}</p>
      {dlSegments.length > 1 && (
        <div className="mt-2 max-w-3xl">
          <div className="flex h-6 w-full overflow-hidden rounded-lg border border-white/[0.06]">
            {dlSegments.map((segment) => (
              <div
                // 宽度按时长比例；min-width 归零后相邻段共用边界像素，位置读起来才是时间轴本身
                key={`${segment.start}-${segment.label}`}
                title={`${segmentLabelCn(segment.label)} ${formatClock(segment.start)}–${formatClock(segment.end)}`}
                style={{ width: `${((segment.end - segment.start) / mix.duration) * 100}%` }}
                className="flex min-w-0 items-center justify-center border-r border-black/40 bg-emerald-400/[0.07] px-1 last:border-r-0"
              >
                <span className="truncate font-mono text-[9px] text-emerald-200/70">
                  {segmentLabelCn(segment.label)}
                </span>
              </div>
            ))}
          </div>
          <p className="mt-1 font-mono text-[9px] text-zinc-600">
            {dlSegments
              .slice(1)
              .map((segment) => formatClock(segment.start))
              .join(" · ")}
          </p>
        </div>
      )}

      <div className="mt-4 rounded-2xl border border-white/[0.06] bg-black/20 px-4 py-3.5">
        <p className="text-[11px] leading-relaxed text-zinc-400">
          点击成品波形选择续写点，后端把全曲时间换算回该段 Suno clip 的段内秒数，以
          <span className="font-mono text-zinc-300"> continue_at </span>
          请求续写：该点之前的音频作为上下文，其后重新生成，结果作为该段的新候选追加在下方，
          试听满意再锁定。白色刻度是谱形状检测候选（毫秒定位），蓝色是模型段边界（±0.1~0.5 s，
          但能看到检测器漏掉的接缝）；色带是模型读出的段落语义，只帮判断，不参与时间换算。
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

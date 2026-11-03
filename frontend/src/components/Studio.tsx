"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { arrayMove } from "@dnd-kit/sortable";
import { API_BASE, persistAudio, requestLyrics, stitchTimeline } from "@/lib/api";
import {
  MAX_SECTIONS,
  buildGenerationPrompt,
  buildSectionsFromLyrics,
  parseLyricBlocks,
  sectionTag,
} from "@/lib/lyrics";
import { GENERATE_ENDPOINT, generateTakes, revokeTakeAudio } from "@/lib/musicgen";
import {
  CROSSFADE_MS,
  DEFAULT_SONG_PROMPT,
  INITIAL_SECTIONS,
} from "@/lib/sections";
import type { StitchResult, Take, TimelineSection } from "@/lib/types";
import SongBriefPanel from "./SongBriefPanel";
import TakeCard from "./TakeCard";
import Timeline from "./Timeline";
import WaveformExtendPlayer from "./WaveformExtendPlayer";
import { pauseAllAudio } from "./WaveformPlayer";

type StudioStep = "brief" | "studio";

function buildInitialPrompts(): Record<string, string> {
  return Object.fromEntries(INITIAL_SECTIONS.map((section) => [section.id, section.prompt]));
}

export default function Studio() {
  const [step, setStep] = useState<StudioStep>("brief");
  const [structureReady, setStructureReady] = useState(false);
  const [sections, setSections] = useState<TimelineSection[]>(INITIAL_SECTIONS);
  const [activeId, setActiveId] = useState<string>(INITIAL_SECTIONS[0].id);
  const [takes, setTakes] = useState<Record<string, Take[]>>({});
  const [locked, setLocked] = useState<Record<string, Take>>({});
  const [prompts, setPrompts] = useState<Record<string, string>>(buildInitialPrompts);
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [mix, setMix] = useState<StitchResult | null>(null);
  const [mixCurrentTime, setMixCurrentTime] = useState(0);
  const [stitching, setStitching] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [songPrompt, setSongPrompt] = useState(DEFAULT_SONG_PROMPT);
  const [lyrics, setLyrics] = useState("");
  const [lyricsLoading, setLyricsLoading] = useState(false);
  const [lyricsError, setLyricsError] = useState<string | null>(null);

  const sectionsRef = useRef(sections);
  const lockedRef = useRef(locked);
  const takesRef = useRef(takes);
  const promptsRef = useRef(prompts);
  const requestSeq = useRef<Record<string, number>>({});
  // 结构重建后旧请求的响应必须作废，epoch 递增即失效
  const epochRef = useRef(0);

  useEffect(() => {
    sectionsRef.current = sections;
  }, [sections]);

  useEffect(() => {
    lockedRef.current = locked;
  }, [locked]);

  useEffect(() => {
    takesRef.current = takes;
  }, [takes]);

  useEffect(() => {
    promptsRef.current = prompts;
  }, [prompts]);

  // 第一页的实时预览：与确认后实际构建的结构完全一致
  const briefPreview = useMemo(() => {
    const text = lyrics.trim();
    if (!text) {
      return { sections: [] as TimelineSection[], blockCount: 0, error: null as string | null };
    }
    const blocks = parseLyricBlocks(text);
    if (blocks.length === 0) {
      return {
        sections: [] as TimelineSection[],
        blockCount: 0,
        error: "没有解析出歌词段落，请检查文本格式",
      };
    }
    return {
      sections: buildSectionsFromLyrics(text, songPrompt),
      blockCount: blocks.length,
      error: null,
    };
  }, [lyrics, songPrompt]);

  const hasStudioWork = () =>
    Object.keys(lockedRef.current).length > 0 || Object.keys(takesRef.current).length > 0;

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // Suno 按歌词自行决定演唱时长，时间轴以实际返回音频为准，不再强制对齐预估秒数
  const applyNaturalDuration = useCallback((sectionId: string, duration: number) => {
    if (!Number.isFinite(duration) || duration <= 0) return;
    const nextDuration = Math.max(1, Math.round(duration * 10) / 10);
    const current = sectionsRef.current;
    const index = current.findIndex((section) => section.id === sectionId);
    if (index < 0 || Math.abs(current[index].durationSec - nextDuration) < 0.05) return;

    const next = current.map((section) =>
      section.id === sectionId ? { ...section, durationSec: nextDuration } : section,
    );
    sectionsRef.current = next;
    setSections(next);
    setMix(null);
  }, []);

  const loadTakes = useCallback(async (sectionId: string, promptOverride?: string) => {
    const currentSections = sectionsRef.current;
    const index = currentSections.findIndex((section) => section.id === sectionId);
    if (index < 0) return;
    const section = currentSections[index];
    const stylePrompt = promptOverride ?? promptsRef.current[sectionId] ?? section.prompt;
    // 发给 Suno 的纯文本：风格提示 + 结构标签 + 本段歌词
    const prompt = buildGenerationPrompt(section, stylePrompt);

    const seq = (requestSeq.current[sectionId] ?? 0) + 1;
    requestSeq.current[sectionId] = seq;
    const epoch = epochRef.current;

    pauseAllAudio();
    setLoading((prev) => ({ ...prev, [sectionId]: true }));
    setErrors((prev) => ({ ...prev, [sectionId]: null }));

    try {
      const generated = await generateTakes({
        sectionId,
        prompt,
        durationSec: section.durationSec,
        count: 3,
      });
      if (epochRef.current !== epoch || requestSeq.current[sectionId] !== seq) {
        revokeTakeAudio(generated);
        return;
      }
      setTakes((prev) => {
        revokeTakeAudio(prev[sectionId]);
        return { ...prev, [sectionId]: generated };
      });
      // 已锁定的段落以锁定音频为准，避免重新生成时改变成品布局
      if (!lockedRef.current[sectionId]) {
        applyNaturalDuration(sectionId, generated[0].duration);
      }
    } catch (error) {
      if (epochRef.current !== epoch || requestSeq.current[sectionId] !== seq) return;
      const message = error instanceof Error ? error.message : "生成候选失败";
      setErrors((prev) => ({ ...prev, [sectionId]: message }));
      setNotice(message);
    } finally {
      if (epochRef.current === epoch && requestSeq.current[sectionId] === seq) {
        setLoading((prev) => ({ ...prev, [sectionId]: false }));
      }
    }
  }, [applyNaturalDuration]);

  const applyStructure = (nextSections: TimelineSection[], nextLyrics: string, message: string) => {
    Object.values(takesRef.current).forEach(revokeTakeAudio);

    const nextPrompts = Object.fromEntries(
      nextSections.map((section) => [section.id, section.prompt]),
    );
    sectionsRef.current = nextSections;
    lockedRef.current = {};
    takesRef.current = {};
    promptsRef.current = nextPrompts;
    requestSeq.current = {};
    epochRef.current += 1;

    setSections(nextSections);
    setTakes({});
    setLocked({});
    setPrompts(nextPrompts);
    setLoading({});
    setErrors({});
    setMix(null);
    setActiveId(nextSections[0].id);
    setLyrics(nextLyrics);
    setLyricsError(null);
    pauseAllAudio();
    setNotice(message);
  };

  const handleConfirmBrief = () => {
    const text = lyrics.trim();
    if (!text) {
      setNotice("请先输入歌词，或点击「AI 生成歌词」");
      return;
    }
    if (briefPreview.error || briefPreview.sections.length === 0) {
      setNotice(briefPreview.error ?? "没有解析出歌词段落，请检查文本格式");
      return;
    }

    // 设定未变化时直接回到工作台，保留已有候选与锁定。
    // 时长由 Suno 返回音频自适应，不参与设定比较，否则天然长度会误判为设定变更。
    const unchanged =
      structureReady &&
      briefPreview.sections.length === sectionsRef.current.length &&
      briefPreview.sections.every((section, index) => {
        const current = sectionsRef.current[index];
        return (
          current.id === section.id &&
          current.title === section.title &&
          current.prompt === section.prompt &&
          current.lyrics === section.lyrics
        );
      });
    if (unchanged) {
      setStep("studio");
      setNotice("设定未变化，回到逐段生成");
      return;
    }

    if (
      hasStudioWork() &&
      !window.confirm("修改设定会按歌词重建结构，并清空已有候选与锁定，是否继续？")
    ) {
      return;
    }

    applyStructure(
      briefPreview.sections,
      text,
      briefPreview.blockCount > MAX_SECTIONS
        ? `歌词超过 ${MAX_SECTIONS} 段，已保留前 ${MAX_SECTIONS} 段并更新结构`
        : `已按歌词划分 ${briefPreview.sections.length} 段，可以逐段生成`,
    );
    setStructureReady(true);
    setStep("studio");
  };

  const handleGenerateLyrics = async () => {
    if (lyricsLoading) return;
    setLyricsLoading(true);
    setLyricsError(null);
    try {
      const draft = await requestLyrics(songPrompt);
      setLyrics(draft.lyrics);
      setNotice(
        hasStudioWork()
          ? "歌词已生成，重新确认会清空现有候选与锁定"
          : "歌词已生成，确认分段预览后进入逐段生成",
      );
    } catch (error) {
      setLyricsError(error instanceof Error ? error.message : "歌词生成失败");
    } finally {
      setLyricsLoading(false);
    }
  };

  const handleBackToBrief = () => {
    if (step !== "studio") return;
    pauseAllAudio();
    setStep("brief");
    setNotice("已返回创作设定；设定不变时可直接回到逐段生成");
  };

  const handleStepClick = (target: StudioStep) => {
    if (target === "brief") {
      handleBackToBrief();
      return;
    }
    if (step === "brief") handleConfirmBrief();
  };

  // 真机生成耗时且计费，进入页面不自动请求；由「生成候选」显式触发
  const handleSelectSection = (sectionId: string) => {
    if (sectionId === activeId) return;
    setActiveId(sectionId);
    pauseAllAudio();
  };

  const handleReorder = (activeCardId: string, overId: string) => {
    const current = sectionsRef.current;
    const from = current.findIndex((section) => section.id === activeCardId);
    const to = current.findIndex((section) => section.id === overId);
    if (from < 0 || to < 0) return;

    const next = arrayMove(current, from, to);
    sectionsRef.current = next;
    setSections(next);
    pauseAllAudio();
    setMix(null);
    setNotice("结构已调整，拼接顺序按新顺序生效");
  };

  const handleLock = async (sectionId: string, take: Take) => {
    const currentSections = sectionsRef.current;
    const index = currentSections.findIndex((section) => section.id === sectionId);
    if (index < 0) return;

    // 生成的音频是内存 Blob，先落盘换静态地址，拼接接口才能读到
    let stored = take;
    if (take.audioUrl.startsWith("blob:")) {
      try {
        stored = { ...take, audioUrl: await persistAudio(take.audioUrl) };
      } catch (error) {
        setNotice(error instanceof Error ? `保存音频失败：${error.message}` : "保存音频失败");
        return;
      }
    }

    const next = { ...lockedRef.current, [sectionId]: stored };
    lockedRef.current = next;
    setLocked(next);
    pauseAllAudio();
    setMix(null);
    // 时间轴该段按锁定音频的自然长度展开
    applyNaturalDuration(sectionId, stored.duration);

    const section = currentSections[index];
    const nextSection = currentSections[index + 1];
    if (nextSection) {
      // 正常流程：确定当前段后，生成框自动切到下一段
      setActiveId(nextSection.id);
      setNotice(
        `已锁定「${section.title} · ${stored.label}」，已切换到「${nextSection.title}」`,
      );
    } else {
      setNotice(
        `已锁定「${section.title} · ${stored.label}」，全部段落已锁定，可以生成成品了`,
      );
    }
  };

  const handlePromptChange = (sectionId: string, value: string) => {
    promptsRef.current = { ...promptsRef.current, [sectionId]: value };
    setPrompts((prev) => ({ ...prev, [sectionId]: value }));
  };

  const handleRegenerate = () => {
    void loadTakes(activeId, promptsRef.current[activeId]);
  };

  const handleStitch = async () => {
    const currentSections = sectionsRef.current;
    const sequence = currentSections.flatMap((section) => {
      const take = lockedRef.current[section.id];
      return take ? [{ section, take }] : [];
    });

    if (sequence.length < 2) {
      setNotice("至少锁定 2 个段落才能拼接");
      return;
    }

    setStitching(true);
    pauseAllAudio();
    try {
      const result = await stitchTimeline(
        sequence.map(({ section, take }) => ({
          id: section.id,
          audioUrl: take.audioUrl,
          crossfadeMs: CROSSFADE_MS,
        })),
      );
      setMix(result);
      setMixCurrentTime(0);
      setNotice(`成品已生成：${result.duration.toFixed(2)}s · 峰值 ${result.peakDbfs.toFixed(1)} dBFS`);
    } catch (error) {
      setNotice(error instanceof Error ? `拼接失败：${error.message}` : "拼接失败");
    } finally {
      setStitching(false);
    }
  };

  const activeIndex = sections.findIndex((section) => section.id === activeId);
  const activeSection = sections[activeIndex] ?? sections[0];
  const activeTakes = takes[activeSection.id] ?? [];
  const activeLoading = Boolean(loading[activeSection.id]);
  const activeError = errors[activeSection.id];
  const naturalDuration = locked[activeSection.id]?.duration ?? activeTakes[0]?.duration;
  const lockedCount = sections.filter((section) => locked[section.id]).length;
  const totalDuration = sections.reduce((sum, section) => sum + section.durationSec, 0);
  const allLocked = sections.length > 0 && lockedCount === sections.length;

  return (
    <div className="relative min-h-screen overflow-hidden bg-[#07070c] text-zinc-100">
      <div aria-hidden="true" className="pointer-events-none fixed inset-0">
        <div className="studio-grid absolute inset-0 opacity-50" />
        <div className="absolute -top-40 left-1/4 h-[420px] w-[420px] rounded-full bg-cyan-500/10 blur-[120px]" />
        <div className="absolute right-0 top-1/3 h-[360px] w-[360px] rounded-full bg-violet-500/10 blur-[120px]" />
        <div className="absolute bottom-0 left-1/3 h-[320px] w-[320px] rounded-full bg-amber-400/[0.06] blur-[120px]" />
      </div>

      <div className="relative mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-6 border-b border-white/[0.06] pb-7 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="flex items-center gap-3">
              <div className="grid h-10 w-10 place-items-center rounded-xl border border-white/10 bg-gradient-to-br from-cyan-400/25 via-violet-400/10 to-amber-300/20">
                <LogoMark />
              </div>
              <div>
                <p className="font-mono text-[10px] tracking-[0.3em] text-zinc-500">MUSEBLOCKS</p>
                <h1 className="text-xl font-semibold tracking-tight text-white">模块化 AI 音乐工作台</h1>
              </div>
            </div>
            <p className="mt-4 max-w-xl text-sm leading-relaxed text-zinc-400">
              先在创作设定里写歌词与风格提示词，确认后进入逐段生成：每段 3 个候选，
              锁定后自动切到下一段，最后由后端按时间轴硬切拼接成品。
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-2 rounded-full border border-emerald-400/20 bg-emerald-400/[0.08] px-3 py-1.5 text-[11px] text-emerald-300">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
              Suno API · 后端代理
            </span>
            <div className="flex items-center gap-1 rounded-full border border-white/10 bg-white/[0.03] p-1">
              <button
                type="button"
                onClick={() => handleStepClick("brief")}
                aria-current={step === "brief" ? "step" : undefined}
                className={`rounded-full px-3 py-1 text-[11px] transition ${
                  step === "brief"
                    ? "bg-white font-semibold text-zinc-900"
                    : "text-zinc-400 hover:text-white"
                }`}
              >
                01 创作设定
              </button>
              <span aria-hidden="true" className="text-[10px] text-zinc-600">
                →
              </span>
              <button
                type="button"
                onClick={() => handleStepClick("studio")}
                aria-current={step === "studio" ? "step" : undefined}
                className={`rounded-full px-3 py-1 text-[11px] transition ${
                  step === "studio"
                    ? "bg-white font-semibold text-zinc-900"
                    : "text-zinc-400 hover:text-white"
                }`}
              >
                02 逐段生成
              </button>
            </div>
            <a
              href={`${API_BASE}/docs`}
              target="_blank"
              rel="noreferrer"
              className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[11px] text-zinc-400 transition hover:border-white/20 hover:text-white"
            >
              API Docs ↗
            </a>
          </div>
        </header>

        <main className="mt-6 pb-10">
          {step === "brief" ? (
            <SongBriefPanel
              songPrompt={songPrompt}
              onSongPromptChange={setSongPrompt}
              lyrics={lyrics}
              onLyricsChange={setLyrics}
              lyricsLoading={lyricsLoading}
              lyricsError={lyricsError}
              onGenerateLyrics={() => void handleGenerateLyrics()}
              onConfirm={handleConfirmBrief}
              previewSections={briefPreview.sections}
              previewBlockCount={briefPreview.blockCount}
              previewError={briefPreview.error}
              hasWork={Object.keys(locked).length > 0 || Object.keys(takes).length > 0}
            />
          ) : (
            <div className="page-enter space-y-6">
              <section className="rounded-3xl border border-white/10 bg-white/[0.025] p-5 backdrop-blur-sm sm:p-6">
                <div className="flex flex-wrap items-end justify-between gap-3">
                  <div>
                    <p className="font-mono text-[10px] tracking-[0.3em] text-emerald-300/80">
                      STEP 02 · ARRANGEMENT
                    </p>
                    <h2 className="mt-2 text-2xl font-semibold tracking-tight text-white">
                      逐段生成
                    </h2>
                    <p className="mt-1.5 max-w-2xl text-[11px] leading-relaxed text-zinc-500">
                      点击时间轴切换生成框；锁定当前段后自动进入下一段，想改上一段随时点回去。
                    </p>
                    <p
                      className="mt-2 max-w-2xl truncate font-mono text-[10px] text-zinc-600"
                      title={songPrompt}
                    >
                      {songPrompt}
                    </p>
                    <p className="mt-1 font-mono text-[10px] text-zinc-600">
                      {sections.length} 段 · 总长 {totalDuration}s
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 font-mono text-[10px] text-zinc-300">
                      {lockedCount}/{sections.length} 已锁定
                    </span>
                    <button
                      type="button"
                      onClick={handleBackToBrief}
                      className="inline-flex h-9 items-center rounded-lg border border-white/10 bg-white/[0.04] px-3 text-xs text-zinc-300 transition hover:border-white/25 hover:bg-white/[0.08]"
                    >
                      ← 创作设定
                    </button>
                  </div>
                </div>
              </section>

              <Timeline
                sections={sections}
                activeId={activeSection.id}
                locked={locked}
                generating={loading}
                playhead={
                  mix
                    ? {
                        timeSec: mixCurrentTime,
                        segmentOrder: mix.segmentOrder,
                        segmentStartSec: mix.segmentStartSec,
                        durationSec: mix.duration,
                      }
                    : null
                }
                onSelect={handleSelectSection}
                onReorder={handleReorder}
              />

              <section className="rounded-3xl border border-white/10 bg-white/[0.025] p-5 backdrop-blur-sm sm:p-6">
                <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className={`h-2 w-2 rounded-full ${activeSection.barClass}`} />
                      <span className={`font-mono text-[10px] tracking-[0.2em] ${activeSection.textClass}`}>
                        SECTION {String(activeIndex + 1).padStart(2, "0")} / {activeSection.english}
                      </span>
                    </div>
                    <h2 className="mt-2 text-2xl font-semibold tracking-tight text-white">
                      {activeSection.title}
                      <span className="ml-2 text-base font-normal text-zinc-500">
                        {activeSection.durationSec}s
                      </span>
                    </h2>
                    <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
                      <span className="text-zinc-500">生成方式</span>
                      <span className="rounded-full border border-cyan-300/20 bg-cyan-400/[0.08] px-2.5 py-1 text-cyan-200">
                        Suno · 整段人声
                      </span>
                      <span className="rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1 text-zinc-400">
                        时长按返回音频自适应
                      </span>
                      {naturalDuration && (
                        <span className="rounded-full border border-emerald-400/25 bg-emerald-400/[0.08] px-2.5 py-1 font-mono text-emerald-300">
                          实际 {naturalDuration.toFixed(1)}s
                        </span>
                      )}
                    </div>
                    {activeSection.lyrics && (
                      <div className="mt-4 max-w-xl rounded-xl border border-white/[0.06] bg-black/20 px-3.5 py-2.5">
                        <p className="font-mono text-[9px] tracking-[0.18em] text-zinc-600">本段歌词</p>
                        <p className="mt-1 whitespace-pre-line text-xs leading-relaxed text-zinc-400">
                          {activeSection.lyrics}
                        </p>
                      </div>
                    )}
                  </div>

                  <div className="w-full lg:max-w-xl">
                    <label
                      htmlFor="prompt"
                      className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500"
                    >
                      文本提示词 / Prompt
                    </label>
                    <div className="mt-1.5 flex gap-2">
                      <input
                        id="prompt"
                        value={prompts[activeSection.id] ?? ""}
                        onChange={(event) => handlePromptChange(activeSection.id, event.target.value)}
                        maxLength={500}
                        placeholder="描述这一段想要的情绪、配器与律动，发送时自动附带结构标签与歌词"
                        className="h-10 min-w-0 flex-1 rounded-xl border border-white/10 bg-black/25 px-3 text-xs text-zinc-200 outline-none transition placeholder:text-zinc-600 focus:border-white/30 focus:bg-black/40"
                      />
                      <button
                        type="button"
                        onClick={handleRegenerate}
                        disabled={activeLoading}
                        className="inline-flex h-10 shrink-0 items-center gap-2 rounded-xl border border-white/10 bg-white/[0.05] px-3 text-xs text-zinc-300 transition hover:border-white/25 hover:bg-white/[0.09] disabled:cursor-wait disabled:opacity-50"
                      >
                        <RefreshIcon />
                        {activeLoading ? "生成中" : activeTakes.length > 0 ? "重新生成" : "生成候选"}
                      </button>
                    </div>
                    <p className="mt-1.5 truncate font-mono text-[9px] text-zinc-600">
                      发送格式：风格提示 + {sectionTag(activeSection)} + 本段歌词
                    </p>
                  </div>
                </div>

                {activeError && (
                  <div
                    role="alert"
                    className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-400/25 bg-rose-400/[0.08] px-4 py-3 text-xs text-rose-200"
                  >
                    <span>生成失败：{activeError}</span>
                    <button
                      type="button"
                      onClick={handleRegenerate}
                      className="rounded-lg border border-rose-300/30 px-2.5 py-1 text-rose-100 transition hover:bg-rose-400/10"
                    >
                      重试
                    </button>
                  </div>
                )}

                <div className="mt-6 flex items-center justify-between gap-3">
                  <h3 className="text-xs font-medium text-zinc-300">
                    生成结果 <span className="text-zinc-600">· 3 选 1</span>
                  </h3>
                  {activeLoading && activeTakes.length > 0 && (
                    <span className="flex items-center gap-2 text-[10px] text-zinc-500">
                      <span className="h-3 w-3 animate-spin rounded-full border border-zinc-600 border-t-zinc-300" />
                      正在重新生成候选
                    </span>
                  )}
                </div>

                <div className="mt-3 grid gap-4 md:grid-cols-3">
                  {activeLoading && activeTakes.length === 0 ? (
                    [0, 1, 2].map((index) => (
                      <div
                        key={index}
                        className="h-[248px] animate-pulse rounded-2xl border border-white/[0.06] bg-white/[0.03]"
                      />
                    ))
                  ) : activeTakes.length === 0 && !activeError ? (
                    <div className="col-span-full rounded-2xl border border-dashed border-white/10 px-4 py-10 text-center text-xs text-zinc-500">
                      暂无候选，点击「生成候选」请求 3 个 Take
                    </div>
                  ) : (
                    activeTakes.map((take, index) => (
                      <TakeCard
                        key={take.id}
                        take={take}
                        index={index}
                        section={activeSection}
                        lockedTakeId={locked[activeSection.id]?.id ?? null}
                        dimmed={activeLoading}
                        onSelect={(selected) => handleLock(activeSection.id, selected)}
                      />
                    ))
                  )}
                </div>
              </section>

              <section className="flex flex-col gap-5 rounded-3xl border border-white/10 bg-gradient-to-r from-white/[0.04] to-transparent p-5 sm:p-6 lg:flex-row lg:items-center lg:justify-between">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-sm font-medium text-white">成品拼接</h2>
                    <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 font-mono text-[10px] text-zinc-400">
                      {lockedCount}/{sections.length} 已锁定
                    </span>
                  </div>
                  <p className="mt-1.5 font-mono text-[10px] text-zinc-500">
                    硬切拼接 · {CROSSFADE_MS}ms 微淡入淡出 · 峰值上限 -1 dBFS
                  </p>
                  {!allLocked && lockedCount >= 2 && (
                    <p className="mt-1.5 text-[10px] text-amber-300/80">
                      还有 {sections.length - lockedCount} 段未锁定，将只拼接已锁定段落
                    </p>
                  )}
                  <div className="mt-3 flex flex-wrap items-center gap-1.5">
                    {sections.map((section, index) => (
                      <Fragment key={section.id}>
                        <span
                          className={`rounded-full border px-2.5 py-1 text-[10px] ${
                            locked[section.id]
                              ? "border-emerald-400/20 bg-emerald-400/[0.06] text-emerald-300"
                              : "border-white/10 bg-white/[0.02] text-zinc-500"
                          }`}
                        >
                          {section.title}
                          {locked[section.id] ? ` · ${locked[section.id].label}` : " · 未锁定"}
                        </span>
                        {index < sections.length - 1 && <span className="text-zinc-700">→</span>}
                      </Fragment>
                    ))}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={handleStitch}
                  disabled={lockedCount < 2 || stitching}
                  className={`inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-xl bg-emerald-400 px-5 text-sm font-semibold text-emerald-950 transition hover:bg-emerald-300 disabled:cursor-not-allowed disabled:bg-white/10 disabled:text-zinc-500 ${
                    allLocked ? "ring-2 ring-emerald-300/30" : ""
                  }`}
                >
                  {stitching ? (
                    <>
                      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-emerald-950/30 border-t-emerald-950" />
                      拼接中
                    </>
                  ) : (
                    <>
                      <MergeIcon />
                      生成成品
                    </>
                  )}
                </button>
              </section>

              {mix && (
                <section className="rounded-3xl border border-emerald-400/20 bg-emerald-400/[0.04] p-5 backdrop-blur-sm sm:p-6">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="font-mono text-[10px] tracking-[0.2em] text-emerald-300/80">FINAL MIX</p>
                      <h2 className="mt-1 text-lg font-semibold text-white">成品预览</h2>
                    </div>
                    <a
                      href={mix.audioUrl}
                      download={mix.filename}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex h-9 items-center gap-2 rounded-lg border border-emerald-300/25 bg-emerald-400/[0.08] px-3 text-xs text-emerald-200 transition hover:bg-emerald-400/[0.15]"
                    >
                      下载 WAV
                      <DownloadIcon />
                    </a>
                  </div>
                  <div className="mt-4 max-w-3xl">
                    <WaveformExtendPlayer
                      key={mix.filename}
                      mix={mix}
                      sections={sections}
                      locked={locked}
                      prompts={prompts}
                      onTakes={(sectionId, extended) =>
                        setTakes((prev) => ({
                          ...prev,
                          [sectionId]: [...(prev[sectionId] ?? []), ...extended],
                        }))
                      }
                      onActivate={setActiveId}
                      onNotice={setNotice}
                      onTimeUpdate={setMixCurrentTime}
                    />
                  </div>
                </section>
              )}
            </div>
          )}

          <footer className="mt-6 flex flex-wrap items-center justify-between gap-2 pb-2 font-mono text-[10px] text-zinc-600">
            <span>API {API_BASE}</span>
            <span>Generate {GENERATE_ENDPOINT}</span>
            <span>Next.js · FastAPI · pydub · Web Audio</span>
          </footer>
        </main>
      </div>

      {notice && (
        <div
          role="status"
          className="toast-enter fixed bottom-5 left-1/2 z-50 max-w-[90vw] -translate-x-1/2 rounded-2xl border border-white/15 bg-zinc-900/95 px-4 py-2 text-center text-xs text-zinc-200 shadow-2xl backdrop-blur"
        >
          {notice}
        </div>
      )}
    </div>
  );
}

function LogoMark() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M3 13V7M6.5 15V5M10 12V8M13.5 16V4M17 11.5v-3" stroke="white" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M13 8a5 5 0 1 1-1.6-3.7M13 3v2.2h-2.2"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function MergeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.5 3v2.2c0 1.4 1.1 2.5 2.5 2.5h6c1.4 0 2.5 1.1 2.5 2.5V13M11 10.8l2.8 2.2L11 15"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M8 2.5v7.2m0 0 2.6-2.6M8 9.7 5.4 7.1M3 12.5h10"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

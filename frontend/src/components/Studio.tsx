"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { arrayMove } from "@dnd-kit/sortable";
import { API_BASE, persistAudio, requestLyrics, stitchTimeline } from "@/lib/api";
import {
  MAX_SECTIONS,
  buildGenerationPrompt,
  buildSectionsFromLyrics,
  parseLyricBlocks,
  plannedTruncateSec,
  sectionTag,
} from "@/lib/lyrics";
import {
  CANDIDATES_PER_SECTION,
  GENERATE_ENDPOINT,
  TASKS_PER_SECTION,
  billingNote,
  describeGenerateJob,
  generateTakes,
  revokeTakeAudio,
  trimTakesToPlan,
} from "@/lib/musicgen";
import {
  CROSSFADE_MS,
  DEFAULT_SONG_PROMPT,
  INITIAL_SECTIONS,
} from "@/lib/sections";
import {
  captureSession,
  loadSession,
  mixFromStored,
  parseSession,
  saveSession,
  sectionFromStored,
  takeFromStored,
} from "@/lib/session";
import type { SessionSnapshot } from "@/lib/session";
import type {
  GenerateJob,
  Notice,
  NoticeLevel,
  StitchResult,
  Take,
  TimelineSection,
} from "@/lib/types";
import SongBriefPanel from "./SongBriefPanel";
import TakeCard from "./TakeCard";
import Timeline from "./Timeline";
import WaveformExtendPlayer from "./WaveformExtendPlayer";
import { pauseAllAudio } from "./WaveformPlayer";

type StudioStep = "brief" | "studio";

function buildInitialPrompts(): Record<string, string> {
  return Object.fromEntries(INITIAL_SECTIONS.map((section) => [section.id, section.prompt]));
}

/**
 * 会话快照只解析一次。用 useState 的惰性初始化而不是 ref：
 * `react-hooks/refs` 不允许在渲染期读 `ref.current`，而这份快照恰恰要当首帧各种 state 的初始值用。
 * 初始化函数只在挂载时执行一次，之后没人再调用 setter，所以它等价于「挂载时常量」。
 * 整棵 Studio 是客户端专属组件（`app/page.tsx` 里 `dynamic(..., { ssr: false })`），
 * 首帧直接拿它当初始值既没有 SSR 水合不一致，也不用「effect 里 setState」那种级联渲染。
 */
function useSessionSnapshot(): SessionSnapshot | null {
  const [session] = useState(() => parseSession(loadSession()));
  return session;
}

function restoreLockedTakes(session: SessionSnapshot | null): Record<string, Take> {
  if (!session) return {};
  return Object.fromEntries(
    Object.entries(session.locked).map(([sectionId, take]) => [sectionId, takeFromStored(take)]),
  );
}

/**
 * 候选列表回填。任务式生成之后候选也是 `/static` 落盘地址，刷新后照样能播，所以一并还原 ——
 * 否则时间轴写着「已锁定 Take A」而下方卡片是「暂无候选」，同一份现场自己跟自己矛盾。
 *
 * 旧快照（没有 `takes` 字段）只在**该段列表为空**时用锁定项兜出一条候选：
 * `loadTakes` 是整段替换候选但保留锁定的，所以「锁定项不在候选列表里」是合法状态，
 * 不能无条件把它塞回非空列表 —— 那会凭空多出一张上一次生成时已被替换掉的卡片。
 */
function restoreTakes(session: SessionSnapshot | null): Record<string, Take[]> {
  if (!session) return {};
  const restored: Record<string, Take[]> = {};
  session.sections.forEach((section) => {
    const stored = session.takes[section.id] ?? [];
    if (stored.length > 0) {
      restored[section.id] = stored.map(takeFromStored);
      return;
    }
    const locked = session.locked[section.id];
    if (locked) restored[section.id] = [takeFromStored(locked)];
  });
  return restored;
}

/**
 * 恢复结果直接做成首帧提示，而不是在 effect 里 setState：少一次渲染，也不会被时序坑。
 * 候选数按**实际回填的列表**算，不是按快照里的 `takes` 字段算 —— 旧快照没有那个字段，
 * 恢复侧会用锁定项兜出一条候选，照快照数就会报「0 条候选」而下方明明有一张卡片。
 */
function restoredNotice(
  session: SessionSnapshot | null,
  restoredTakes: Record<string, Take[]>,
): Notice | null {
  if (!session) return null;
  const lockedCount = Object.keys(session.locked).length;
  const candidateCount = Object.values(restoredTakes).reduce((sum, list) => sum + list.length, 0);
  const audioCount = candidateCount + lockedCount + (session.mix ? 1 : 0);
  return {
    text:
      `已恢复上次现场：${session.sections.length} 段结构、${lockedCount} 段已锁定、` +
      `${candidateCount} 条候选${session.mix ? "、成品 1 份" : ""}；` +
      // 探活是挂载后异步跑的，首帧只能说「正在核对」，不能把还没做的事说成做完了
      (audioCount > 0
        ? "音频都是后端落盘地址，正在核对文件是否还在（缺的会被点名剔除）"
        : "歌词与结构还在，可以直接逐段生成"),
    level: "info",
  };
}

/** 探活并发上限：候选最多 12 段 × 24 条，一次性放出去会把本地静态服务和本页连接数打满 */
const PROBE_CONCURRENCY = 8;

/** HEAD 幂等且不碰上游；返回拿不到文件的地址集合（网络错误与 404 同等对待） */
async function probeAudioUrls(urls: string[]): Promise<Set<string>> {
  const dead = new Set<string>();
  let cursor = 0;
  const worker = async () => {
    // 读下标与自增之间没有 await，JS 单线程下不会有两个 worker 抢到同一条
    while (cursor < urls.length) {
      const url = urls[cursor++];
      try {
        const response = await fetch(url, { method: "HEAD", cache: "no-store" });
        if (!response.ok) dead.add(url);
      } catch {
        dead.add(url);
      }
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(PROBE_CONCURRENCY, urls.length) }, () => worker()),
  );
  return dead;
}

/** 点名要有上限：24 条候选全丢时把 24 个名字铺满浮层，反而看不清「丢了几条」 */
function nameLosses(labels: string[], max = 6): string {
  const unique = [...new Set(labels)];
  if (unique.length <= max) return unique.join("、");
  return `${unique.slice(0, max).join("、")} 等 ${unique.length} 处`;
}

export default function Studio() {
  const session = useSessionSnapshot();
  const [step, setStep] = useState<StudioStep>(() => session?.step ?? "brief");
  const [structureReady, setStructureReady] = useState(() => Boolean(session?.structureReady));
  const [sections, setSections] = useState<TimelineSection[]>(() =>
    session ? session.sections.map(sectionFromStored) : INITIAL_SECTIONS,
  );
  const [activeId, setActiveId] = useState<string>(() => session?.activeId ?? INITIAL_SECTIONS[0].id);
  const [takes, setTakes] = useState<Record<string, Take[]>>(() => restoreTakes(session));
  const [locked, setLocked] = useState<Record<string, Take>>(() => restoreLockedTakes(session));
  const [prompts, setPrompts] = useState<Record<string, string>>(
    () => session?.prompts ?? buildInitialPrompts(),
  );
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  // 每段 TASKS_PER_SECTION 个并发任务各自的最新状态，用来显示阶段 / 上游状态 / 已耗时
  const [genJobs, setGenJobs] = useState<Record<string, (GenerateJob | null)[]>>({});
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [mix, setMix] = useState<StitchResult | null>(() =>
    session?.mix ? mixFromStored(session.mix) : null,
  );
  const [mixCurrentTime, setMixCurrentTime] = useState(0);
  const [stitching, setStitching] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(() => restoredNotice(session, takes));
  const [songPrompt, setSongPrompt] = useState(() => session?.songPrompt || DEFAULT_SONG_PROMPT);
  const [lyrics, setLyrics] = useState(() => session?.lyrics ?? "");
  const [lyricsLoading, setLyricsLoading] = useState(false);
  const [lyricsError, setLyricsError] = useState<string | null>(null);

  const sectionsRef = useRef(sections);
  const lockedRef = useRef(locked);
  const takesRef = useRef(takes);
  const promptsRef = useRef(prompts);
  // 探活要判断「现在这份成品还是不是恢复出来的那一份」，避免把探活期间新拼的成品误判为作废
  const mixRef = useRef(mix);
  const requestSeq = useRef<Record<string, number>>({});
  // 结构重建后旧请求的响应必须作废，epoch 递增即失效
  const epochRef = useRef(0);
  // 恢复出来的音频地址探一次活就够了（HEAD 幂等，但没必要每次重渲染都问一遍）
  const probedRef = useRef(false);

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

  useEffect(() => {
    mixRef.current = mix;
  }, [mix]);

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

  const notify = useCallback((text: string, level: NoticeLevel = "info") => {
    setNotice({ text, level });
  }, []);

  useEffect(() => {
    // 只有 info 走自动消失；error 常驻到用户手动关闭 —— 3.2s 抹掉的往往是唯一证据
    if (!notice || notice.level !== "info") return;
    const timer = window.setTimeout(() => setNotice(null), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  /**
   * 地址格式对 ≠ 文件还在：`static/` 被清过、换机器、换容器都会让快照里的 `/static` 地址变 404。
   * 恢复完就探一次活（HEAD，本地静态文件，不碰 Suno、不扣费，并发封顶 `PROBE_CONCURRENCY`），
   * 死链按「我们这边失败」处理 —— 锁定退回未锁定、候选从列表里剔除、成品作废，并以 error 级
   * 常驻提示点名到「段 + Take」，而不是留一个点开才报错的假锁定状态或一张播不出声的卡片。
   */
  const probeRestoredAudio = useCallback(
    async (snapshot: SessionSnapshot) => {
      const titleOf = (sectionId: string) =>
        snapshot.sections.find((section) => section.id === sectionId)?.title ?? sectionId;

      // 锁定 / 候选 / 成品分开记账：三者丢失的后果不同，提示不能混成一句「已退回未锁定」
      const lockedLabels = new Map<string, string[]>();
      Object.entries(snapshot.locked).forEach(([sectionId, take]) => {
        lockedLabels.set(take.audioUrl, [
          ...(lockedLabels.get(take.audioUrl) ?? []),
          titleOf(sectionId),
        ]);
      });
      // 候选按 id 记名字，并留一份「恢复出来的 id」白名单：探活期间用户可能已经点了「生成候选」，
      // 新列表的 id 不在白名单里，这次探活结果就不会误伤刚花钱拿到的新候选
      const candidateLabelById = new Map<string, string>();
      const candidateUrls = new Set<string>();
      Object.entries(snapshot.takes).forEach(([sectionId, list]) => {
        list.forEach((take) => {
          candidateLabelById.set(take.id, `${titleOf(sectionId)} ${take.label}`);
          candidateUrls.add(take.audioUrl);
        });
      });
      const mixUrl = snapshot.mix?.audioUrl ?? null;
      const urls = [
        ...new Set([...lockedLabels.keys(), ...candidateUrls, ...(mixUrl ? [mixUrl] : [])]),
      ];
      if (urls.length === 0) return;

      const dead = await probeAudioUrls(urls);
      if (dead.size === 0) return;

      // 剔除以**当前** state 为准（探活是异步的），名字取自快照；
      // 只统计真的被摘掉的条目，避免为一份早已被替换的列表报警
      const lostLocked: string[] = [];
      const nextLocked: Record<string, Take> = {};
      Object.entries(lockedRef.current).forEach(([sectionId, take]) => {
        if (take && dead.has(take.audioUrl)) {
          lostLocked.push(titleOf(sectionId));
          return;
        }
        if (take) nextLocked[sectionId] = take;
      });
      lockedRef.current = nextLocked;
      setLocked(nextLocked);

      const lostTakes: string[] = [];
      const nextTakes: Record<string, Take[]> = {};
      Object.entries(takesRef.current).forEach(([sectionId, list]) => {
        const kept = list.filter((take) => {
          if (!candidateLabelById.has(take.id) || !dead.has(take.audioUrl)) return true;
          lostTakes.push(candidateLabelById.get(take.id) ?? `${titleOf(sectionId)} ${take.label}`);
          return false;
        });
        if (kept.length > 0) nextTakes[sectionId] = kept;
      });
      takesRef.current = nextTakes;
      setTakes(nextTakes);

      const mixLost = Boolean(mixUrl && dead.has(mixUrl) && mixRef.current?.audioUrl === mixUrl);
      if (mixLost) {
        mixRef.current = null;
        setMix(null);
        setMixCurrentTime(0);
      }

      const consequences: string[] = [];
      if (lostLocked.length > 0) consequences.push(`${nameLosses(lostLocked)}（已退回未锁定）`);
      if (mixLost) consequences.push("成品已作废");
      if (lostTakes.length > 0) {
        consequences.push(`${lostTakes.length} 条候选已剔除（${nameLosses(lostTakes)}）`);
      }
      // 死链全属于已经被替换掉的旧列表：当前界面不再指向它们，没有可报告的损失
      if (consequences.length === 0) return;
      notify(
        `恢复的 ${dead.size} 个音频文件在后端磁盘上已经找不到了：${consequences.join("；")}。` +
          "结构和歌词还在，不用重头再来；后端账本若仍在，重新生成会免费重取已扣费的结果",
        "error",
      );
    },
    [notify],
  );

  // 恢复出来的地址探活；setState 全在 await 之后，不是 effect 体内的同步级联渲染
  useEffect(() => {
    if (!session || probedRef.current) return;
    probedRef.current = true;
    void probeRestoredAudio(session);
  }, [session, probeRestoredAudio]);

  // 现场一变就落盘：step / 歌词 / 段落 / 候选 / 锁定 / 提示词草稿 / 成品（机制与取舍见 lib/session.ts）
  useEffect(() => {
    saveSession(
      captureSession({
        step,
        structureReady,
        songPrompt,
        lyrics,
        activeId,
        sections,
        takes,
        locked,
        prompts,
        mix,
      }),
    );
  }, [step, structureReady, songPrompt, lyrics, activeId, sections, takes, locked, prompts, mix]);

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
    setGenJobs((prev) => ({
      ...prev,
      [sectionId]: Array.from({ length: TASKS_PER_SECTION }, () => null),
    }));

    try {
      const { takes: generated, sources } = await generateTakes({
        sectionId,
        prompt,
        durationSec: section.durationSec,
        count: TASKS_PER_SECTION,
        onProgress: (job, taskIndex) => {
          if (epochRef.current !== epoch || requestSeq.current[sectionId] !== seq) return;
          setGenJobs((prev) => {
            const slots = [...(prev[sectionId] ?? [])];
            slots[taskIndex] = job;
            return { ...prev, [sectionId]: slots };
          });
        },
      });
      if (epochRef.current !== epoch || requestSeq.current[sectionId] !== seq) {
        revokeTakeAudio(generated);
        return;
      }

      // 器乐段（无歌词）上游写满整首：生成后立刻按规划秒数裁成「只有本段」。
      // 裁剪走的是后端本地 DSP，不再花钱；失败就保留原片，拼接时仍会按 max_duration_sec 裁。
      const plannedSec = plannedTruncateSec(section);
      let candidates = generated;
      let trimFailed = 0;
      if (plannedSec && generated.length > 0) {
        const trimmed = await trimTakesToPlan(generated, plannedSec);
        candidates = trimmed.takes;
        trimFailed = trimmed.failed;
      }
      // 裁剪要读文件、写片段，期间可能已切换设定或重新点击；旧结果不能写回
      if (epochRef.current !== epoch || requestSeq.current[sectionId] !== seq) {
        revokeTakeAudio(candidates);
        return;
      }

      setTakes((prev) => {
        revokeTakeAudio(prev[sectionId]);
        return { ...prev, [sectionId]: candidates };
      });
      // 已锁定的段落以锁定音频为准，避免重新生成时改变成品布局
      if (!lockedRef.current[sectionId] && candidates[0]) {
        applyNaturalDuration(sectionId, candidates[0].duration);
      }
      if (trimFailed > 0 && plannedSec) {
        notify(
          `${trimFailed} 条候选未能裁到规划 ${plannedSec}s，暂时保留整首；锁定后拼接时仍会裁`,
        );
      }
      // 命中账本是「这次没花钱」的好消息，值得一条提示；正常建单不用刷屏，
      // 计费语义在进度条里常驻显示（info 提示 3.2s 就没了，进度条不会）
      if (sources.some((source) => source !== "submit")) {
        notify(billingNote(sources) ?? "复用已扣费结果");
      }
    } catch (error) {
      if (epochRef.current !== epoch || requestSeq.current[sectionId] !== seq) return;
      const message = error instanceof Error ? error.message : "生成候选失败";
      setErrors((prev) => ({ ...prev, [sectionId]: message }));
      notify(message, "error");
    } finally {
      if (epochRef.current === epoch && requestSeq.current[sectionId] === seq) {
        setLoading((prev) => ({ ...prev, [sectionId]: false }));
      }
    }
  }, [applyNaturalDuration, notify]);

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
    setGenJobs({});
    setErrors({});
    setMix(null);
    setActiveId(nextSections[0].id);
    setLyrics(nextLyrics);
    setLyricsError(null);
    pauseAllAudio();
    notify(message);
  };

  const handleConfirmBrief = () => {
    const text = lyrics.trim();
    if (!text) {
      notify("请先输入歌词，或点击「AI 生成歌词」");
      return;
    }
    if (briefPreview.error || briefPreview.sections.length === 0) {
      notify(briefPreview.error ?? "没有解析出歌词段落，请检查文本格式");
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
      notify("设定未变化，回到逐段生成");
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
      notify(
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
    notify("已返回创作设定；设定不变时可直接回到逐段生成");
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
    notify("结构已调整，拼接顺序按新顺序生效");
  };

  const handleLock = async (sectionId: string, take: Take) => {
    const currentSections = sectionsRef.current;
    const index = currentSections.findIndex((section) => section.id === sectionId);
    if (index < 0) return;

    // 候选现在生成即落盘、直接是 /static 地址；blob 分支只兜历史现场，遇到才上传换静态地址
    let stored = take;
    if (take.audioUrl.startsWith("blob:")) {
      try {
        stored = { ...take, audioUrl: await persistAudio(take.audioUrl) };
      } catch (error) {
        notify(error instanceof Error ? `保存音频失败：${error.message}` : "保存音频失败", "error");
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
      notify(
        `已锁定「${section.title} · ${stored.label}」，已切换到「${nextSection.title}」`,
      );
    } else {
      notify(
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
      notify("至少锁定 2 个段落才能拼接");
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
          // 无歌词段（make_instrumental=true）上游会写满一整首，按规划秒数裁到段长；
          // 有歌词的段落时长由唱词决定，裁在乐句中间比留长尾更糟，所以不裁
          maxDurationSec: plannedTruncateSec(section),
        })),
      );
      setMix(result);
      setMixCurrentTime(0);
      const cutCount = result.truncateToSec.filter((sec) => sec != null).length;
      notify(
        `成品已生成：${result.duration.toFixed(2)}s · 峰值 ${result.peakDbfs.toFixed(1)} dBFS` +
          (cutCount > 0 ? ` · ${cutCount} 个无词段落已裁到规划时长` : ""),
      );
    } catch (error) {
      notify(error instanceof Error ? `拼接失败：${error.message}` : "拼接失败", "error");
    } finally {
      setStitching(false);
    }
  };

  const activeIndex = sections.findIndex((section) => section.id === activeId);
  const activeSection = sections[activeIndex] ?? sections[0];
  const activeTakes = takes[activeSection.id] ?? [];
  const activeLoading = Boolean(loading[activeSection.id]);
  const activeGenJobs = genJobs[activeSection.id] ?? [];
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
              先在创作设定里写歌词与风格提示词，确认后进入逐段生成：每段 {CANDIDATES_PER_SECTION} 个候选，
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

                {activeGenJobs.some(Boolean) && (
                  <div className="mt-5 rounded-xl border border-white/[0.06] bg-black/20 px-4 py-3">
                    <div className="space-y-1">
                      {activeGenJobs.map((job, index) => {
                        const status = job ? describeGenerateJob(job) : null;
                        return (
                          <p
                            key={job?.jobId ?? `pending-${index}`}
                            className={`font-mono text-[10px] ${
                              status?.failed ? "text-rose-300/90" : "text-zinc-400"
                            }`}
                          >
                            任务 {index + 1}/{TASKS_PER_SECTION} ·{" "}
                            {status ? status.label : "提交中"}
                            {status?.billing && (
                              <span className="text-emerald-300/80"> · {status.billing}</span>
                            )}
                          </p>
                        );
                      })}
                    </div>
                    <p className="mt-2 text-[10px] leading-relaxed text-zinc-600">
                      刷新或关掉页面不会重复计费：建单成功即按内容指纹记进后端账本，
                      同样内容重试是免费重查那条已扣费的任务；片段已落盘时连上游都不请求。
                    </p>
                  </div>
                )}

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
                    生成结果{" "}
                    <span className="text-zinc-600">
                      · {activeTakes.length || CANDIDATES_PER_SECTION} 选 1
                    </span>
                  </h3>
                  {activeLoading && activeTakes.length > 0 && (
                    <span className="flex items-center gap-2 text-[10px] text-zinc-500">
                      <span className="h-3 w-3 animate-spin rounded-full border border-zinc-600 border-t-zinc-300" />
                      正在重新生成候选
                    </span>
                  )}
                </div>

                <div className="mt-3 grid gap-4 md:grid-cols-2">
                  {activeLoading && activeTakes.length === 0 ? (
                    Array.from({ length: CANDIDATES_PER_SECTION }, (_, index) => (
                      <div
                        key={index}
                        className="h-[248px] animate-pulse rounded-2xl border border-white/[0.06] bg-white/[0.03]"
                      />
                    ))
                  ) : activeTakes.length === 0 && !activeError ? (
                    <div className="col-span-full rounded-2xl border border-dashed border-white/10 px-4 py-10 text-center text-xs text-zinc-500">
                      暂无候选，点击「生成候选」发 {TASKS_PER_SECTION} 次任务取{" "}
                      {CANDIDATES_PER_SECTION} 个 Take
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
                      onNotice={notify}
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
          role={notice.level === "error" ? "alert" : "status"}
          className={`toast-enter fixed bottom-5 left-1/2 z-50 flex max-w-[90vw] -translate-x-1/2 items-center gap-3 rounded-2xl border px-4 py-2 text-xs shadow-2xl backdrop-blur ${
            notice.level === "error"
              ? "border-rose-400/40 bg-rose-400/10 text-rose-200"
              : "border-white/15 bg-zinc-900/95 text-zinc-200"
          }`}
        >
          <span className="text-left">{notice.text}</span>
          {notice.level === "error" && (
            <button
              type="button"
              onClick={() => setNotice(null)}
              className="shrink-0 rounded-lg border border-rose-300/30 px-2 py-0.5 text-[10px] text-rose-300 transition hover:border-rose-300/70 hover:text-rose-200"
            >
              关闭
            </button>
          )}
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

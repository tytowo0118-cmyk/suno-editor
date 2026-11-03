"use client";

import type { CSSProperties } from "react";
import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  horizontalListSortingStrategy,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { buildTimings, formatClock, mixPlayheadPosition, type MixPlayhead } from "@/lib/sections";
import type { SectionTiming, Take, TimelineSection } from "@/lib/types";

interface TimelineProps {
  sections: TimelineSection[];
  activeId: string;
  locked: Record<string, Take | undefined>;
  generating: Record<string, boolean | undefined>;
  /** 成品混音的播放位置，为空时时间轴不显示播放指针 */
  playhead: MixPlayhead | null;
  onSelect: (id: string) => void;
  onReorder: (activeId: string, overId: string) => void;
}

interface BlockProps {
  section: TimelineSection;
  index: number;
  timing: SectionTiming;
  isActive: boolean;
  /** 播放指针当前落在这一段 */
  isPlaying: boolean;
  lockedTake?: Take;
  isGenerating: boolean;
  onSelect: (id: string) => void;
}

function SortableBlock({
  section,
  index,
  timing,
  isActive,
  isPlaying,
  lockedTake,
  isGenerating,
  onSelect,
}: BlockProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: section.id,
  });

  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    flexGrow: section.durationSec,
    flexBasis: 0,
    zIndex: isDragging ? 30 : undefined,
    boxShadow: isActive
      ? `0 0 44px ${section.accent}22`
      : isPlaying
        ? `0 0 30px ${section.accent}2c`
        : undefined,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`group relative min-w-[132px] overflow-hidden rounded-2xl border transition-[box-shadow,opacity] ${section.blockClass} ${
        isActive ? "ring-1 ring-white/35" : isPlaying ? "ring-1 ring-white/15" : ""
      } ${isDragging ? "opacity-70" : ""}`}
    >
      <button
        type="button"
        onClick={() => onSelect(section.id)}
        aria-pressed={isActive}
        className="block h-full w-full p-4 pr-9 text-left"
      >
        <span className="font-mono text-[10px] tracking-[0.18em] text-white/40">
          {String(index + 1).padStart(2, "0")} · {section.english}
        </span>
        <h3 className="mt-5 text-[15px] font-semibold text-white">{section.title}</h3>
        <p className="mt-0.5 whitespace-nowrap font-mono text-[10px] text-white/45">
          {section.durationSec}s · {formatClock(timing.startSec)}–{formatClock(timing.endSec)}
        </p>
        <div className="mt-4 flex items-center gap-1.5 text-[10px]">
          {lockedTake ? (
            <>
              <CheckIcon />
              <span className="truncate text-emerald-300">已锁定 {lockedTake.label}</span>
            </>
          ) : isGenerating ? (
            <>
              <span className="h-2.5 w-2.5 animate-spin rounded-full border border-white/30 border-t-white/80" />
              <span className="text-white/50">生成中</span>
            </>
          ) : (
            <>
              <span className={`h-1.5 w-1.5 rounded-full opacity-60 ${section.barClass}`} />
              <span className="text-white/40">待生成</span>
            </>
          )}
        </div>
      </button>

      <button
        type="button"
        {...attributes}
        {...listeners}
        aria-label={`拖动排序：${section.title}`}
        className="absolute right-2 top-2 inline-flex h-6 w-6 cursor-grab touch-none items-center justify-center rounded-md text-white/30 transition hover:bg-white/10 hover:text-white/70 active:cursor-grabbing"
      >
        <GripIcon />
      </button>
    </div>
  );
}

export default function Timeline({
  sections,
  activeId,
  locked,
  generating,
  playhead,
  onSelect,
  onReorder,
}: TimelineProps) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const timings = buildTimings(sections);
  const totalDuration = sections.reduce((sum, section) => sum + section.durationSec, 0);
  const playheadPosition = playhead ? mixPlayheadPosition(sections, playhead) : null;

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    onReorder(String(active.id), String(over.id));
  };

  return (
    <section className="rounded-3xl border border-white/10 bg-white/[0.025] p-5 backdrop-blur-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
          <h2 className="text-sm font-medium text-white">结构时间轴</h2>
          <span className="rounded-full border border-white/10 px-2 py-0.5 font-mono text-[9px] tracking-wider text-zinc-500">
            DRAG TO REORDER
          </span>
        </div>
        <span className="font-mono text-[11px] text-zinc-500">
          {playhead ? (
            <>
              <span className="text-zinc-300">{formatClock(playhead.timeSec)}</span>
              <span className="mx-1 text-zinc-600">/</span>
              {`${playhead.durationSec.toFixed(2)}s`}
            </>
          ) : (
            `总长 ${totalDuration}s`
          )}
        </span>
      </div>

      <div className="mt-4 overflow-x-auto pb-1">
        <DndContext
          id="studio-timeline"
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={sections.map((section) => section.id)}
            strategy={horizontalListSortingStrategy}
          >
            <div className="flex min-w-[580px] gap-2.5">
              {sections.map((section, index) => (
                <SortableBlock
                  key={section.id}
                  section={section}
                  index={index}
                  timing={timings[index]}
                  isActive={section.id === activeId}
                  isPlaying={playheadPosition?.sectionId === section.id}
                  lockedTake={locked[section.id]}
                  isGenerating={Boolean(generating[section.id])}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      </div>

      <div className="relative mt-3">
        <div className="flex h-1 overflow-hidden rounded-full bg-white/5">
          {sections.map((section) => (
            <span
              key={section.id}
              className={`h-full opacity-60 ${section.barClass}`}
              style={{ flexGrow: section.durationSec, flexBasis: 0 }}
            />
          ))}
        </div>
        {playheadPosition && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -top-1 bottom-[-3px] w-0.5 -translate-x-1/2 rounded-full bg-white shadow-[0_0_10px_rgba(255,255,255,0.7)] transition-[left] duration-75 ease-linear"
            style={{ left: `${playheadPosition.percent}%` }}
          />
        )}
      </div>

      <p className="mt-3 text-[11px] leading-relaxed text-zinc-500">
        点击色块切换下方生成框 · 拖动右上角手柄调整结构 · 时长按 Suno 返回音频自适应展开
      </p>
    </section>
  );
}

function GripIcon() {
  return (
    <svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor" aria-hidden="true">
      <circle cx="2.5" cy="2.5" r="1.2" />
      <circle cx="7.5" cy="2.5" r="1.2" />
      <circle cx="2.5" cy="7" r="1.2" />
      <circle cx="7.5" cy="7" r="1.2" />
      <circle cx="2.5" cy="11.5" r="1.2" />
      <circle cx="7.5" cy="11.5" r="1.2" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="m3.5 8.3 3 3 6-6.6"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="text-emerald-300"
      />
    </svg>
  );
}

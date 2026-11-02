"use client";

import type { Take, TimelineSection } from "@/lib/types";
import WaveformPlayer from "./WaveformPlayer";

interface TakeCardProps {
  take: Take;
  index: number;
  section: TimelineSection;
  lockedTakeId: string | null;
  dimmed?: boolean;
  onSelect: (take: Take) => void;
}

export default function TakeCard({
  take,
  index,
  section,
  lockedTakeId,
  dimmed = false,
  onSelect,
}: TakeCardProps) {
  const isLocked = lockedTakeId === take.id;

  return (
    <article
      className={`relative flex flex-col gap-4 overflow-hidden rounded-2xl border p-4 transition ${
        isLocked
          ? "border-emerald-400/50 bg-emerald-400/[0.06] shadow-[0_0_40px_rgba(52,211,153,0.12)]"
          : "border-white/10 bg-white/[0.025] hover:border-white/20"
      } ${dimmed ? "pointer-events-none opacity-45" : ""}`}
    >
      <span className={`absolute inset-x-4 top-0 h-[2px] rounded-full opacity-80 ${section.barClass}`} />

      <header className="flex items-start justify-between gap-2">
        <div>
          <p className={`font-mono text-[10px] uppercase tracking-[0.22em] ${section.textClass}`}>
            候选项 {String(index + 1).padStart(2, "0")}
          </p>
          <h3 className="mt-1 text-lg font-semibold tracking-tight text-white">{take.label}</h3>
        </div>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] ${
            isLocked
              ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300"
              : "border-white/10 bg-white/[0.03] text-zinc-500"
          }`}
        >
          {isLocked ? "已锁定" : "待选择"}
        </span>
      </header>

      <WaveformPlayer
        key={`${take.id}:${take.audioUrl}`}
        src={take.audioUrl}
        playerId={take.id}
        accent={section.accent}
        waveColor={section.waveColor}
        progressColor={section.progressColor}
        durationSec={take.duration}
      />

      <div className="flex items-center justify-between font-mono text-[10px] text-zinc-500">
        <span>{take.duration.toFixed(1)}s</span>
        <span className="text-zinc-600">Suno</span>
      </div>

      <button
        type="button"
        onClick={() => onSelect(take)}
        aria-pressed={isLocked}
        className={`inline-flex h-10 w-full items-center justify-center gap-2 rounded-xl border text-xs font-medium transition ${
          isLocked
            ? "border-emerald-300/40 bg-emerald-400 text-emerald-950"
            : "border-white/10 bg-white/[0.04] text-zinc-200 hover:border-white/25 hover:bg-white/[0.08]"
        }`}
      >
        {isLocked ? <CheckIcon /> : <LockIcon />}
        {isLocked ? "已锁定 · 将参与拼接" : "Select · 锁定此段"}
      </button>
    </article>
  );
}

function LockIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="3.2" y="7" width="9.6" height="7" rx="1.8" stroke="currentColor" strokeWidth="1.3" />
      <path d="M5.4 7V5.2a2.6 2.6 0 0 1 5.2 0V7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="m3.5 8.3 3 3 6-6.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

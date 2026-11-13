/**
 * 零成本验证「逐段续写链」的载荷口径：前奏 → 主歌 → 副歌三段，每一段的建单请求里
 * `continue_clip_id` 必须 = 上一段候选的 clip_id（续写产物自己也是 clip，链是逐级延长），
 * `continue_at` 必须 = 上一段**截出来的**秒数。判错一次就是真金白银买回一段接错位置的音频，
 * 所以这里把口径钉成断言，不靠人肉读代码。
 *
 * 跑法（宿主机；容器里 /app 就是 frontend/）：
 *   docker exec -i museblocks-frontend sh -c 'cd /app \
 *     && rm -rf /tmp/muse-chain && npx tsc scripts/verify_extend_chain_payload.ts \
 *          --outDir /tmp/muse-chain --module commonjs --target es2020 \
 *          --moduleResolution node --esModuleInterop --skipLibCheck \
 *     && node /tmp/muse-chain/scripts/verify_extend_chain_payload.js'
 * 被测对象是 shipped 源码编译出的 `generateTakes` + `continuationSeed`，不是复刻；
 * `globalThis.fetch` 被假 client 整体接管，全程不发一个真实网络包（每个 URL 都断言落在桩里）。
 *
 * 素材数值取自 2026-11-11 真实扣费链：前奏裁到 8.000s；主歌 extend 产物 44.232s，
 * 按「估算 12s + 往后吸护栏 12s」重裁后吸到 19.313s —— 主歌续写点就该是 19.313，
 * 而不是整条 44.232（那样下一段会接在副歌唱完的地方）。
 */
import { continuationSeed, generateTakes } from "../src/lib/musicgen";
import type { TimelineSection } from "../src/lib/types";

let passed = 0;
const failures: string[] = [];

function check(name: string, got: unknown, want: unknown): void {
  const ok = JSON.stringify(got ?? null) === JSON.stringify(want ?? null);
  if (ok) {
    passed += 1;
    console.log(`PASS | ${name}`);
  } else {
    failures.push(name);
    console.log(
      `FAIL | ${name} | got=${JSON.stringify(got ?? null)} want=${JSON.stringify(want ?? null)}`,
    );
  }
}

function mkSection(kind: TimelineSection["kind"], lyrics: string): TimelineSection {
  return {
    id: `section-${kind}-1`,
    kind,
    title: kind === "intro" ? "前奏" : kind === "chorus" ? "副歌" : "主歌 1",
    english: kind === "intro" ? "Intro" : kind === "chorus" ? "Chorus" : "Verse 1",
    durationSec: 12,
    prompt: "夜晚城市 lo-fi",
    lyrics,
    accent: "#888",
    waveColor: "#666",
    progressColor: "#fff",
    blockClass: "",
    chipClass: "",
    textClass: "",
    barClass: "",
  };
}

const intro = mkSection("intro", "");
const verse = mkSection("verse", "探针用的占位歌词一行\n第二行也只用于装配载荷");

/** 假上游回执：每跳 2 条双生（上游一次任务固定产 2 条），第 1 条 clip_id 是本跳的续写产物 */
function fakeJob(jobId: string, clipId: string, url: string, duration: number): unknown {
  return {
    job_id: jobId,
    fingerprint: `fake-${jobId}`,
    status: "done",
    phase: "done",
    final: true,
    elapsed_sec: 1.0,
    prompt_chars: 4,
    wanted_clips: 2,
    clips: [
      { clip_id: clipId, filename: `${clipId}.wav`, duration, audio_url: url },
      { clip_id: `${clipId}-twin`, filename: `${clipId}-twin.wav`, duration, audio_url: `${url}.twin` },
    ],
    clip_count: 2,
    progress: {},
    reused: false,
    task_id: `task-${jobId}`,
    source: "submit",
  };
}

const captured: Array<{ url: string; body: Record<string, unknown> }> = [];
const servedUrls: string[] = [];

// 第 1 跳独立整首（前奏裁到 8.000）；第 2 跳 extend 产物（主歌裁后吸到 19.313）；
// 第 3 跳副歌的续写产物，本探针只关心它的 seed 载荷
const jobs: Record<string, { clipId: string; url: string; duration: number }> = {
  hop1: { clipId: "clip-intro", url: "/static/output/fake-intro.wav", duration: 8.0 },
  hop2: { clipId: "clip-verse-ext", url: "/static/output/fake-verse-ext.wav", duration: 19.313 },
  hop3: { clipId: "clip-chorus-ext", url: "/static/output/fake-chorus-ext.wav", duration: 20.5 },
};

async function main(): Promise<void> {
  let hop = 0;
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: RequestInit,
  ): Promise<Response> => {
    const url = String(input);
    const key = `hop${++hop}`;
    servedUrls.push(url);
    const fixture = jobs[key];
    if (!fixture) throw new Error(`桩收到计划外请求：${url}`);
    captured.push({ url, body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown> });
    return new Response(
      JSON.stringify(fakeJob(key, fixture.clipId, fixture.url, fixture.duration)),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };

  // 跳 1：前奏，无 seed —— 载荷里不能有续写字段
  const hop1 = await generateTakes({ sectionId: "intro", prompt: "P", durationSec: 12, count: 1 });
  const body1 = captured[0].body;
  check("前奏请求落在桩里", servedUrls[0], "http://127.0.0.1:8000/api/generate/jobs");
  check("前奏载荷无 continue_clip_id", "continue_clip_id" in body1, false);
  check("前奏载荷无 continue_at", "continue_at" in body1, false);
  check("前奏候选 clip_id", hop1.takes[0].clipId, "clip-intro");
  check("前奏候选秒数（已裁）", hop1.takes[0].duration, 8);

  // 跳 2：主歌 —— 种子必须是「前奏自己的 clip + 裁点 8.000」
  const seed1 = continuationSeed(intro, hop1.takes[0]);
  check("主歌种子 = 前奏 clip + 8.000", seed1, { clipId: "clip-intro", continueAt: 8 });
  const hop2 = await generateTakes({
    sectionId: "verse",
    prompt: "P",
    durationSec: 12,
    count: 1,
    seed: seed1,
  });
  const body2 = captured[1].body;
  check("主歌载荷 continue_clip_id = 前奏 clip", body2.continue_clip_id, "clip-intro");
  check("主歌载荷 continue_at = 前奏裁点", body2.continue_at, 8);
  check("主歌候选 clip_id = extend 产物自己", hop2.takes[0].clipId, "clip-verse-ext");

  // 跳 3：副歌 —— 种子必须指向**主歌的续写产物**（逐级延长），且用主歌截出来的 19.313，
  // 而不是整条 extend 的 44.232（那里副歌都唱完了）
  const seed2 = continuationSeed(verse, hop2.takes[0]);
  check("副歌种子 = 主歌续写产物 clip + 19.313", seed2, {
    clipId: "clip-verse-ext",
    continueAt: 19.313,
  });
  const hop3 = await generateTakes({
    sectionId: "chorus",
    prompt: "P",
    durationSec: 16,
    count: 1,
    seed: seed2,
  });
  const body3 = captured[2].body;
  check("副歌载荷 continue_clip_id = 主歌续写产物", body3.continue_clip_id, "clip-verse-ext");
  check("副歌载荷 continue_at = 主歌截点", body3.continue_at, 19.313);
  check(
    "链是逐级延长（不回头指前奏）",
    body3.continue_clip_id === "clip-intro",
    false,
  );
  check("副歌候选 clip_id = 副歌续写产物", hop3.takes[0].clipId, "clip-chorus-ext");

  check("桩共服务 3 个请求（无真实网络包）", servedUrls.length, 3);
  check("三跳载荷都落在 /api/generate/jobs", servedUrls.every((u) => u.endsWith("/api/generate/jobs")), true);

  console.log(`##### extend chain payload: ${passed} PASS / ${failures.length} FAIL #####`);
  if (failures.length > 0) {
    console.log(`未通过：${failures.join("、")}`);
    process.exit(1);
  }
}

void main();

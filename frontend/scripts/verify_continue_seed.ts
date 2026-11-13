/**
 * 零成本验证「逐段续写种子」的换算：`continuationSeed` 决定第 2 段起发给上游的
 * `continue_clip_id` / `continue_at`，判错一次就是真金白银买回一段接错位置的音频，
 * 所以这里把口径钉成断言，不靠人肉读代码。
 *
 * 跑法（宿主机；容器里 /app 就是 frontend/）：
 *   docker exec -i museblocks-frontend sh -c 'cd /app \
 *     && rm -rf /tmp/muse-seed && npx tsc scripts/verify_continue_seed.ts \
 *          --outDir /tmp/muse-seed --module commonjs --target es2020 \
 *          --moduleResolution node --esModuleInterop --skipLibCheck \
 *     && node /tmp/muse-seed/scripts/verify_continue_seed.js'
 * 被测对象是 shipped 源码编译出的函数，不是复刻。全程不碰后端、不建单、不扣费。
 *
 * 规划秒数出自 `plannedTruncateSec`：**每个段落都有**（无词 intro=8s、无词 verse/chorus=12s、
 * 有词按行数 4n+4 再夹进 [8, 60/40/30]）。`truncateBeyondSec` 决定「还没截成」的上限：
 * 无词段 0（前奏就该是 8s），有词段等于规划秒数（切点最多到规划的 2 倍）。
 */
import { continuationSeed } from "../src/lib/musicgen";
import type { Take, TimelineSection } from "../src/lib/types";

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

/** 段落只需要 kind / lyrics / durationSec 参与判定，其余是界面样式字段，占位即可 */
function mkSection(kind: TimelineSection["kind"], lyrics: string): TimelineSection {
  return {
    id: `section-${kind}-1`,
    kind,
    title: kind === "intro" ? "前奏" : "主歌 1",
    english: kind === "intro" ? "Intro" : "Verse 1",
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

function mkTake(duration: number, clipId: string | null, audioUrl = "/static/x.wav"): Take {
  return { id: "take-a", label: "候选 A", audioUrl, duration, clipId };
}

const intro = mkSection("intro", "");
const verse = mkSection("verse", "");
const vocal = mkSection("verse", "探针用的占位歌词一行\n第二行也只用于装配载荷");

// 1) 无词前奏已裁到 7.703s：裁短保留的正是原片 [0, cut]，候选秒数就是原片时间轴上的续写点
check(
  "器乐前奏已裁短 → 取裁后秒数",
  continuationSeed(intro, mkTake(7.703, "clip-aaa")),
  { clipId: "clip-aaa", continueAt: 7.703 },
);

// 2) 无词段裁短失败、候选仍是整首 205.560s：必须跟着规划 8s 走，
//    否则下一段接在整首末尾，而成品里这段只有 8s —— 接缝前后各差 197s
check(
  "器乐段没裁成（留整首）→ 夹到规划秒数",
  continuationSeed(intro, mkTake(205.56, "clip-bbb")),
  { clipId: "clip-bbb", continueAt: 8 },
);

// 3) 无词 verse 规划 12s、候选 11.384s（短于规划）：取更短的候选秒数，不能凭空推到 12
check(
  "器乐主歌候选短于规划 → 取候选秒数",
  continuationSeed(verse, mkTake(11.384, "clip-ccc")),
  { clipId: "clip-ccc", continueAt: 11.384 },
);

// 4) 有词段候选没截成（44.232s 的实测主歌里副歌都唱完了）：34.2 > 规划 12 + 护栏 12，
//    必须夹回规划秒数，不能在「整首歌写完的地方」续写下一段
check(
  "有词段候选仍是整条 → 夹回规划秒数",
  continuationSeed(vocal, mkTake(34.2, "clip-ddd")),
  { clipId: "clip-ddd", continueAt: 12 },
);

// 5) 上一段没锁定 / 段落不存在：退回独立整首生成，不能假装接上了
check("上一段未锁定 → undefined", continuationSeed(intro, undefined), undefined);
check("上一段不存在 → undefined", continuationSeed(undefined, mkTake(8, "clip-eee")), undefined);

// 6) 历史现场的 blob 候选没有 clip_id：/api/trim 也不产 clip_id，这类种子只能判无
check(
  "候选无 clip_id → undefined",
  continuationSeed(intro, mkTake(8, null, "blob:http://127.0.0.1:3000/abc")),
  undefined,
);

// 7) 脏 clip_id：trim 后才进载荷与指纹，空串视为没有
check("clip_id 带空格 → trim", continuationSeed(intro, mkTake(8, "  clip-fff  ")), {
  clipId: "clip-fff",
  continueAt: 8,
});
check("clip_id 全空格 → undefined", continuationSeed(intro, mkTake(8, "   ")), undefined);

// 8) 时长异常不给续写点（0 / 负 / NaN）
check("duration=0 → undefined", continuationSeed(intro, mkTake(0, "clip-hhh")), undefined);
check("duration=-3 → undefined", continuationSeed(intro, mkTake(-3, "clip-hhh")), undefined);
check(
  "duration=NaN → undefined",
  continuationSeed(intro, mkTake(Number.NaN, "clip-hhh")),
  undefined,
);

// 9) 毫秒量化：浮点尾巴进了载荷会让同一段两次生成的指纹不同，账本就会重复建单扣费
check(
  "浮点尾巴量化到毫秒",
  continuationSeed(verse, mkTake(11.383999999999999, "clip-iii")),
  { clipId: "clip-iii", continueAt: 11.384 },
);
check(
  "有词段浮点尾巴量化（已截到 19.313s 的边界）",
  continuationSeed(vocal, mkTake(19.312999999, "clip-jjj")),
  { clipId: "clip-jjj", continueAt: 19.313 },
);

// 10) 有词段已截到 beyond 档吸住的边界（19.313s，44.232s 主歌 extend 产物的实测截点）：
//     续写点取截点，绝不拿 12 再裁一次
check(
  "有词段已截到吸住的边界 → 取截点",
  continuationSeed(vocal, mkTake(19.313, "clip-kkk")),
  { clipId: "clip-kkk", continueAt: 19.313 },
);

// 11) 「没截成」的判据上限 = 规划 + 护栏 + 0.05（浮点噪声余量）：
//     压线 24.05 视为已截（保留原秒数），24.06 视为没截成（夹回规划）
check(
  "有词段压线 24.05 → 视为已截",
  continuationSeed(vocal, mkTake(24.05, "clip-lll")),
  { clipId: "clip-lll", continueAt: 24.05 },
);
check(
  "有词段 24.06 超上限 → 夹回规划秒数",
  continuationSeed(vocal, mkTake(24.06, "clip-mmm")),
  { clipId: "clip-mmm", continueAt: 12 },
);

console.log(`##### continuationSeed: ${passed} PASS / ${failures.length} FAIL #####`);
if (failures.length > 0) {
  console.log(`未通过：${failures.join("、")}`);
  process.exit(1);
}

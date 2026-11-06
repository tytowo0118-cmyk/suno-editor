# MuseBlocks — 基于歌词分段的 Web 端模块化 AI 音乐生成系统

输入歌曲提示词与歌词，系统按歌词标签自动划分前奏 / 主歌 / 预副歌 / 副歌 / 桥段 / 尾奏。
创作分为两步：第一页完成歌词与风格设定并实时预览分段，确认后进入第二页逐段生成；
每段 4 选 1 锁定后，生成框自动切到下一段（可点时间轴回跳），最后由后端把各段按
时间轴顺序硬切拼接成完整成品。

当前阶段：**生成侧已切换为 Suno API 代理**。浏览器以 `multipart/form-data` 调用
FastAPI 的 `POST /api/generate`（只传 `prompt`，由后端转发 Suno），拿到整段带人声的 WAV 后
转为 Blob 播放；每段音频按返回的自然时长在时间轴上自适应展开，不再截断或拉伸到预估秒数。
锁定 Take 时把音频上传到后端落盘，最终以 8 ms 微淡入淡出（近似硬切）拼接，避免人声重影。
成品波形可点选任意时间点，后端按 Suno 的 `continue_clip_id` / `continue_at` 从该点续写，
一次任务的两个双生片段都挂为该段的新候选，试听满意再替换锁定。
后端通过 `SUNO_API_BASE` / `SUNO_API_KEY` 调用真实 Suno 代理，未配置密钥时直接报错并提示；
歌词由 `/api/lyrics` 调用 OpenAI 兼容的 `/chat/completions` 实时生成（`LLM_MODEL` 默认
`gpt-4o-mini`），未配置 LLM 时回退本地模板，保证演示可用。

---

## 1. 架构总览

```
┌────────────────────────────── 浏览器 ──────────────────────────────┐
│  Next.js 16 (App Router, Turbopack) + React 19 + Tailwind CSS v4   │
│                                                                    │
│  Studio.tsx        两步流程：step=brief / studio；结构 / 锁定 / 候选 / 混音   │
│    ├─ SongBriefPanel.tsx 第一页：提示词 + 歌词 + AI 生成 + 分段实时预览       │
│    ├─ Timeline.tsx       第二页：点击色块切换下方唯一生成框；dnd-kit 拖拽重排 │
│    ├─ TakeCard.tsx       4 个候选卡片（试听 + 锁定，锁定后自动切下一段）      │
│    ├─ WaveformExtendPlayer.tsx 成品波形取点与 continue_at 续写提交            │
│    └─ WaveformPlayer.tsx wavesurfer 波形；单实例协调 + 吸附刻度线 + 横向缩放  │
│                                                                    │
│  lib/lyrics.ts     歌词标签解析 → 段落 / 时长 / 提示词组装          │
│  lib/musicgen.ts   POST /api/generate（FormData: prompt）→ 整段 WAV  │
│  lib/api.ts        歌词生成 + 落盘 + 拼接请求，统一错误提取            │
└───────────────┬────────────────────────────────────────────────────┘
                │ 全部走 FastAPI：生成 multipart / 其余 JSON
┌───────────────▼────────────────────────────────────────────────────┐
│ FastAPI (127.0.0.1:8000)                                           │
│ POST /api/generate  Suno 代理（提交 + 轮询 + 转 WAV）                │
│ POST /api/lyrics    LLM 写词（未配置时回退模板）                     │
│ POST /api/assets    候选落盘                                        │
│ POST /api/stitch    多段拼接（8ms 微淡入淡出）                       │
│ POST /api/structure/boundaries 播放头吸附候选（块余弦新颖度）         │
│ GET  /api/analyze_structure  深度学习结构分段（转发到 analyzer）      │
└───────────────┬───────────────────────────┬────────────────────────┘
                │ HTTPS / SDK               │ HTTP（compose 内网，不发布端口）
┌───────────────▼──────────────┐ ┌──────────▼────────────────────────┐
│ Suno API                     │ │ analyzer 容器（torch 只在这里）     │
│ 按歌词自动决定演唱时长，      │ │ allin1 + demucs + madmom：          │
│ 返回完整 audio/wav           │ │ 分轨 → 谱特征 → DiNAT → 结构段      │
└──────────────────────────────┘ └───────────────────────────────────┘
```

开发期三个服务分别运行在独立容器中；浏览器只访问 `127.0.0.1:8000`（CORS 全开），
Suno 由后端代理调用，浏览器不再直连模型服务，也无需处理模型侧 CORS。容器编排见第 5 节。

### 目录结构

```
.
├── README.md                     # 本文件（全局架构 / Repo Wiki）
├── docker-compose.yml            # 开发容器编排：frontend + backend + analyzer
├── backend/
│   ├── Dockerfile                # python:3.11-slim；ffmpeg 由 imageio-ffmpeg 提供
│   ├── Dockerfile.analyzer       # allin1 + torch(CPU) + demucs + madmom + natten，主镜像不含这些
│   ├── analyzer_server.py        # analyzer 容器入口：/health 权重自检、/analyze 单流水线
│   ├── requirements-ml.txt       # 仅分析容器：全部 pin 死，版本直接影响推理数值
│   ├── .dockerignore
│   ├── main.py                   # FastAPI 入口、CORS、静态目录
│   ├── requirements.txt
│   ├── api/
│   │   ├── assets.py             # POST /api/assets（候选 WAV 落盘）
│   │   ├── generate.py           # POST /api/generate（Suno 提交 + 轮询 + 转 WAV + clip_id 索引）
│   │   ├── lyrics.py             # POST /api/lyrics（LLM 写词 + 本地模板兜底）
│   │   ├── stitch.py             # POST /api/stitch
│   │   └── structure.py          # POST /api/structure/boundaries + GET /api/analyze_structure
│   ├── utils/
│   │   ├── audio_processor.py    # 采样统一、等功率交叉淡化、首尾静音裁切、防爆音、峰值限制、WAV 写出
│   │   ├── structure_detector.py # 块余弦谱形状新颖度检测（纯 numpy）
│   │   ├── structure_analyzer.py # allin1 分段 + 段归一化（毫秒、无缝）+ 本地/远程双角色
│   │   ├── structure_weights.py  # 预训练权重自动下载与校验（allin1 fold + htdemucs）
│   │   └── clip_index.py         # clip_id → 本地 WAV 路径索引（data/clip_index.json）
│   ├── scripts/
│   │   └── calibrate_structure.py # 零成本标定：本地重拼出毫秒真值，给两个检测器同一把尺子打分
│   ├── vendor/madmom/models/     # madmom@main 缺失的 models 包替身（见 3.4 依赖表）
│   ├── data/                     # 运行时状态：clip_id 索引（gitignore）+ 标定清单（提交）
│   └── static/                   # generated/ 候选、output/ 生成与拼接成品
└── frontend/
    ├── Dockerfile                # node:24-slim，npm ci + next dev
    ├── .dockerignore
    └── src/
        ├── app/                  # layout.tsx / page.tsx / globals.css
        ├── components/           # Studio / SongBriefPanel / Timeline / TakeCard / WaveformPlayer
        └── lib/                  # types.ts / sections.ts / lyrics.ts / api.ts / musicgen.ts
```

---

## 2. 数据结构（前后端通信契约）

### 2.1 前端内部模型 `frontend/src/lib/types.ts`

```ts
type SectionKind = "intro" | "verse" | "prechorus" | "chorus" | "bridge" | "outro";

interface TimelineSection {
  id: string;              // "intro-1" / "verse-1" / "chorus-2"，由歌词解析生成
  kind: SectionKind;
  title: string;           // 前奏 / 主歌 1 / 预副歌 / 副歌 2 / 桥段 / 尾奏
  english: string;         // INTRO / VERSE 1 / PRE-CHORUS / CHORUS 2 / BRIDGE / OUTRO
  durationSec: number;     // 初始按歌词行数估算（见 2.2）；生成后改为返回音频的自然时长
  prompt: string;          // 全局提示词 + 段落角色 + 首句意象
  lyrics: string;          // 本段歌词；前奏/尾奏等纯器乐段落为空字符串
  accent: string;          // 主题色（用于波形与卡片）
  waveColor: string;
  progressColor: string;
  blockClass: string;      // Tailwind 类名（色块）
  chipClass: string;
  textClass: string;
  barClass: string;        // 时间轴下方比例色条
}

interface Take {
  id: string;              // "verse-1-a-mf3k2x"
  label: string;           // "Take A"
  audioUrl: string;        // 生成后为 blob: URL；锁定落盘后为 /static/generated/*.wav
  duration: number;        // 由音频元数据读取的真实时长（秒）
}

interface StitchResult {
  audioUrl: string;
  filename: string;
  duration: number;        // 实际总时长（扣除交叉淡化重叠）
  peakDbfs: number;
  segmentOrder: string[];
}
```

六种段落的标题、角色提示与配色集中在 `sections.ts` 的 `SECTION_META` / `SECTION_STYLES`；
`SectionTiming`（`{ startSec, endSec }`）由 `buildTimings()` 按当前时间轴顺序累计计算，用于时间轴区间显示。

### 2.2 `POST /api/lyrics` — 歌词生成与结构解析

前端「创作设定」面板同时收集 **歌曲提示词** 与 **歌词**。歌词可手写或点击「AI 生成歌词」，
默认调用 OpenAI 兼容的 `/chat/completions`（`LLM_API_BASE` + `LLM_API_KEY`）：

```bash
curl -X POST http://127.0.0.1:8000/api/lyrics \
  -H "Content-Type: application/json" \
  -d '{"prompt":"夜晚城市 lo-fi，钢琴与柔和鼓点","seed":7}'
```

```json
{
  "lyrics": "[Intro]\n\n[Verse 1]\n...\n\n[Chorus]\n...\n\n[Outro]",
  "provider": "llm:gpt-4o-mini",
  "seed": 7,
  "note": "由 gpt-4o-mini 通过 /chat/completions 实时生成；结构标签与前端解析器一致。"
}
```

- **提示词约束**：System prompt 要求只输出歌词，标签只能使用
  `[Intro] / [Verse n] / [Chorus] / [Bridge] / [Outro]`，除前奏/尾奏外每段 4 行，
  语言跟随歌曲提示词；`seed` 作为创作种子写入消息，用于增加变化但不保证复现。
- **输出清洗与校验**：去掉 Markdown 代码块和首个标签之前的解释，校验标签白名单，
  并要求至少出现 `[Verse]` 与 `[Chorus]`；不通过时返回 502，由前端 Toast 提示重试。
- **回退**：未配置 `LLM_API_BASE` / `LLM_API_KEY` 时自动使用本地模板
  （`provider: local-template`），保证无密钥也能演示；上游 401/403/超时统一转 502
  并透出上游 `message`（例如余额不足）。
- **握手前的退避重试**（`_post_chat_completions`）：写词同样按 token 扣费，判据直接复用 Suno
  建单那条 `api.generate.PRE_SEND_ERRORS`（`ConnectTimeout / ConnectError / PoolTimeout`）——
  字节没离开本机才重试，按 1s、3s 退避、最多 3 次尝试；`ReadTimeout` 时上游可能已经算完，
  重试就是再买一次，一律不重试。要治的是**本机代理随机掐 TLS**：`key.34ku.com` 的 DNS 落到
  198.18.x 的 fake-IP，握手以 `ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]` 在 **0.62 s**
  内返回，而同一发请求重打能通 —— 用**假 key** 打真网关（上游回 401，零扣费）连打 4 发：
  3 发在 1.47~1.49 s 正常到达上游，第 4 发 0.59 s 被掐、重试后 3.08 s 拿到同样的 401。
  按这个单次 ~1/4 的抖动率，三次尝试后失败概率降到 ~1.6%。耗尽重试的 502 文案除了异常类名
  还带异常原文（截断 90 字符）、尝试次数与总耗时，能一眼分清是握手被掐还是 DNS 坏了。
  零成本验证：成功路径无额外延时（打本地假服务 0.047 s），连接被拒路径打满 3 次、4.02 s 返回。
- 提示词要求常用结构 `[Intro] → [Verse 1] → [Chorus] → [Verse 2] → [Chorus] → [Bridge] → [Outro]`；
  实际段落以 LLM 输出为准，前端按标签解析，不做强制改写。

| 环境变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_BASE` | 否 | — | OpenAI 兼容根地址（含 `/v1`），如 `https://key.34ku.com/v1` |
| `LLM_API_KEY` | 否 | 复用 `SUNO_API_KEY` | Bearer Token |
| `LLM_MODEL` | 否 | `gpt-4o-mini` | 写词模型 |

解析在浏览器端完成（`frontend/src/lib/lyrics.ts`）：第一页输入歌词时实时预览分段，
点击「进入逐段生成」时正式构建时间轴：

| 标签（中英文均可） | 段落 |
| --- | --- |
| `[Intro]` / 【前奏】【引子】 | 前奏 |
| `[Verse]` / `[Verse 1]` / 【主歌】【A 段】 | 主歌（自动编号） |
| `[Pre-Chorus]` / 【预副歌】【导歌】 | 预副歌 |
| `[Chorus]` / 【副歌】【B 段】 | 副歌（重复时自动编号） |
| `[Bridge]` / 【桥段】【间奏】 | 桥段 |
| `[Outro]` / 【尾奏】【尾声】 | 尾奏 |

- **无标签回退**：按空行分段；段落分类为主歌 / 副歌交替，最后一段优先作为副歌。
  整段没有空行时每 4 行切一段。无法识别的标签（如 `[laughs]`）按正文保留。
- **初始时长启发式**（`lib/lyrics.ts` 的 `estimateDuration`）：`4s × 歌词行数 + 4s`，
  主歌/副歌上限 60s、预副歌/桥段 40s、前奏/尾奏 30s，下限 8s；无歌词的前奏/尾奏固定 8s。
  实际时长以 Suno 返回音频为准，生成后时间轴自动改为自然长度。
  这条规则还多了一个兑现点：**无歌词段落**在拼接时按它给出的秒数物理裁短
  （`plannedTruncateSec()` → `/api/stitch` 的 `max_duration_sec`，见 §2.5）。
  之所以是独立函数而不是复用 `section.durationSec`，正是因为后者会被生成结果覆写 ——
  前奏那条色块现在标着 182.6s，拿它当上限等于不裁。有歌词的段落不裁：段长由唱词决定，
  硬停在乐句中间比留着长尾更糟。
- **段落提示词**：`全局歌曲提示词；段落角色提示；情绪线索：首句歌词（≤24 字）`，总长 ≤ 500 字。
- **上限**：最多 12 段，超出部分截断并提示；结构更新会清空现有候选与锁定（有内容时二次确认），
  同时递增 epoch 让在途生成请求作废。

### 2.3 `POST /api/generate` — Suno 代理（整段人声）

`POST ${NEXT_PUBLIC_API_BASE}/api/generate`，默认 `http://127.0.0.1:8000/api/generate`。
后端负责调用 Suno，浏览器不接触模型地址与密钥。

| 项 | 要求 |
| --- | --- |
| 方法 | `POST` |
| 请求体 | `multipart/form-data`（前端用 `FormData` 构建） |
| `prompt` | string，必填；风格提示 + `[Section]` 标签 + 本段歌词 |
| `duration` | 整数秒，**占位字段**；后端不再强依赖，实际时长由歌词决定 |
| `continue_clip_id` | string，选填；上游片段 ID，非空时本次请求走**续写**（`metadata.task: "extend"`） |
| `continue_at` | 浮点秒，选填（0–600）；续写起点，必须与 `continue_clip_id` 一起使用 |
| `max_clips` | 整数，选填（默认 1，上限 2）；一次任务上游固定产出 2 个片段，取 2 时把第 2 个一并下载回传 |
| 响应 | `audio/wav` 整段音频，无 JSON 包装；响应头 `X-Suno-Task-Id`、`X-Suno-Clip-Id` 回传本次音频的 ID，`X-Suno-Extra-Clips` 回传其余片段（base64 编码的 JSON 数组） |

- **初次生成不携带续写参数**：段落候选各自独立，段与段之间没有音频上下文；拼接只发生在最后一步。
- **双生片段**：上游每个任务实际返回 `data[0]` 与 `data[1]` 两条片段，只取其一等于浪费一半额度。
  `max_clips=2` 时后端把两条都下载转码，第一条作为响应体，其余以
  `X-Suno-Extra-Clips: base64([{"clipId","url","duration"}])` 回传（`url` 指向 `static/output/` 下的 WAV）；
  前端遍历数组，把它们挂为同一段落的独立子分支。三个自定义头都必须在 CORS
  `expose_headers` 里声明，否则跨端读取为空、续写与双生挂载会静默失效。
- **续写（可选）**：把某个已生成 clip 在 `continue_at` 秒之前的音频交给上游作为创作上下文，
  返回一段以此为基础**重新生成**的续写音频。实测结果**不包含**原开头（与源音频前段的波形相关系数
  接近 0），因此它是「这个点的另一版延续」而不是原片段的延长版，前端把它作为该段的**新候选**追加，
  试听满意再锁定。`X-Suno-Clip-Id` 让下一轮续写能引用这次的片段。
- **提示词组装**：`buildGenerationPrompt(section, stylePrompt)` 把三部分用空行拼接：
  `风格提示` + `[Section]` 标签 + `本段歌词`。标签由段落类型与序号生成
  （`[Intro]` / `[Verse 1]` / `[Chorus 2]` / `[Bridge]` …），编辑区下方的
  「发送格式」会实时显示当前标签。
- **自然时长**：`duration` 传当前 `durationSec` 取整仅作占位；收到音频后用 `Audio`
  元数据读取真实时长，回写 `sections[id].durationSec`（保留 0.1s），时间轴色块宽度与总长
  随之自适应，不做任何截断/拉伸。锁定候选时以该候选的真实时长再次校准。
- **4 选 1（2 次任务 × 双生）**：每段并发 `TASKS_PER_SECTION = 2` 次请求，每次带 `max_clips=2`，
  把同一任务的两条双生片段一并取回挂成候选，所以 **2 次任务出 4 个候选**
  （`CLIPS_PER_TASK` / `CANDIDATES_PER_SECTION` 与它同文件导出，页面文案、骨架屏张数都由此推导，
  不再有写死的「3」）。
  **为什么和早期版本相反**：这里原本钉的是「批量候选保持 `max_clips=1`，以免 3 选 1 被撑成 6 选 1」，
  代价是每次任务已扣费的第 2 条整条扔掉 —— 上游一次任务固定产 2 条、同价，那等于白付一半额度。
  改成 2 次任务后每段少一次建单（约 ¥0.78/任务，3 段一首歌省约 ¥2.34，≈33%），候选从 3 个变 4 个。
- **页面不自动生成**：进入某段不会自动发请求，必须点「生成候选」，避免误耗云端额度。
- **残值片段取不回来不作废整次生成**：任务本身失败仍然整体失败并回收已生成的 Blob URL；
  但某条 `X-Suno-Extra-Clips` 下载 404 / 解码失败时只少一个候选，已扣费的主片段照常可用 ——
  与续写路径同一个判据（`generateTakes` 与 `continueTakes` 共用这条容错口径）。
- **注入验证（零成本）**：浏览器里 monkeypatch `POST /api/generate`，用已扣费 WAV 作响应体、
  `X-Suno-Extra-Clips` 回带第 2 条：2 次任务、表单 `max_clips=2`、候选 `Take A/B/C/D`；主片段时长由
  `Audio` 解码得 182.6 s，双生用响应头的 192.624 s；锁定双生 `Take B` 走真实 `/api/assets` 返回 200
  （落盘 33,979,014 B，与 `output/suno_1793930559_dfe622.wav` 同 md5）。再把双生 URL 换成 404 跑一次：
  保留 2 个主片段候选、不报错，文案自动变「2 选 1」。
- **候选 id 只能有一个命名槽**：双生曾经固定命名成 `${sectionId}-twin-${stamp}`，而一次「生成候选」
  会并发 2 个任务、每个任务各回带 1 条双生 —— 这 2 条的 `stamp` 来自同一次 `Date.now()`，id 完全相同。
  `key={take.id}`（Studio.tsx:668）和 TakeCard 的 `lockedTakeId === take.id` 都按 id 判同，于是
  锁定 `Take B` 时界面同时把 `Take D` 标成已锁定，时间轴上的「已锁定 Take B」与实际参与拼接的候选
  不是同一个东西。改成 id 一律按候选序号取字母（`-a- -b- -c- -d-`，字母由 `takes.length` 推导），
  主片段与双生共用这一格。复跑同一个注入：锁 `Take B` → 卡片徽章只有 B 变「已锁定」，D 仍是
  「待选择 · Select · 锁定此段」，计数 `1/3 已锁定`；控制台 45 条消息全是 HMR/DevTools 提示，
  零 `two children with the same key`。**双生不能沿用主片段的命名槽**这件事就是这么来的。
- **真实任务确认上游普通提交产 2 条**（2026-11-06 跑了一次 `max_clips=2` 的普通提交，≈¥0.78，
  `task_e8a1MDm3Vp5N5Zk9dhle8rlTkP3D6kCD`）：落定的负载里 `data.data` 有 **2 条片段、两条都带 `audio_url`**，
  而生产解析器 `_fetch_state` 对同一个 body 复算得 `parsed_clips=2` —— 所以 `clips[:max_clips]` 这一刀
  在普通提交上是够的。两条是真的不同成品，不是同一条重复：id `8dd2ce7f…` / `8521e0c5…`，
  上游 `duration` 报 30 / 61 s，实际转码后 **30.840 s / 61.008 s**（误差 +0.840 / +0.008 s，
  与「`duration` 字段不可信、以音频为准」一致），mp3 原始字节 661,652 / 1,302,260 B。
  生成中（`IN_PROGRESS`）的 tick 里 `data.data` 只有 1 条且**不带 `audio_url`**，`_fetch_state` 对它返回
  `parsed_clips=0` —— 「片段列表非空即退出」不会被半成品骗到，双生因此不会漏。
- **但同一次真跑暴露了一个更值钱的缺口**：那 84 s 里代理在掐 TLS，旁路 5 s 一轮的免费 `GET /suno/fetch/`
  只落到 4 个负载（按 5 s 一轮约 17 次尝试算，≥13 次握手失败；相邻成功点最长隔 46 s）。
  后端 `_poll_once` 的重试预算是 1/2/4 s 共 4 次、约 9.9 s，
  于是第 4 次仍连不上就直接 502 —— **在 `SUNO_TIMEOUT=300` 总预算几乎没动用（才 52.5 s）的情况下，
  把一个已经扣费、且 5.6 s 后就要 `SUCCESS` 的任务判死了**（上游 `finish_time` = 07:56:11.0，
  后端 07:56:05.4 就放弃；`submit_time` → `finish_time` 全程 56 s）。这不是重试次数不够，是判据
  错了，已按「传输失败只作废当前 tick、睡到下一个间隔接着问，直到 `SUNO_TIMEOUT` 才 504」改掉，
  机制与故障注入实测见 4 节的轮询容错。本次的两条音频靠事后免费重查取回（30.840 s / 61.008 s
  都下到了），没再花钱。

```bash
curl -X POST http://127.0.0.1:8000/api/generate \
  -F "prompt=夜晚城市 lo-fi，温暖克制的钢琴；副歌释放情绪

[Chorus 1]

让风带走昨天
我们向黎明启程" \
  -F "duration=12" \
  -o take.wav
```

续写同一个片段、从第 10 秒接新词（留空歌词则为器乐尾段）：

```bash
curl -X POST http://127.0.0.1:8000/api/generate \
  -D /tmp/head.txt \
  -F "prompt=夜晚城市 lo-fi，温暖克制的钢琴；情绪线索

[Chorus 1]

把灯留给未归的人" \
  -F "continue_clip_id=clip-4" \
  -F "continue_at=10.000" \
  -F "max_clips=2" \
  -o continue.wav
# 响应头 X-Suno-Clip-Id 给出这一版音频的片段 ID，可用于下一次续写
# 第 2 条片段在 X-Suno-Extra-Clips 里，解码：grep -i x-suno-extra-clips /tmp/head.txt | awk '{print $2}' | base64 -d
```

后端实现（`backend/api/generate.py`）：

1. **拆分 prompt**：第一个「标签行」（整行只有一对 ASCII 中括号，如 `[Verse 1]` / `[Hook]` /
   `[Instrumental]`）之前的文本作为 `tags`，标签及其后的内容作为 `prompt`（歌词）；
   去掉标签行后没有正文时置 `make_instrumental=true`。
   切分点与标签剔除共用 `TAG_LINE` 这一个模式定义，不再各写一份词表——早前切分只认
   `intro/verse/pre-chorus/chorus/bridge/outro` 六个词，`[Hook]`、`[Instrumental]`、`[808 bass]`
   这类标签找不到切分点，会把风格文本整段当歌词提交（`tags` 落到默认值），而剔除用的
   `ANY` 模式又把这些行当标签，两处判定不一致正是纯器乐误判的来源之一。
   只认 ASCII 方括号：`sectionTag()` 会先把中英标签归一成 `[Verse 1]` / `[Chorus]` 等英文形态，
   `【副歌】` 这类写法仍按风格文本处理。
   - **人声保底预处理**（`_prepare_vocal_payload`，全曲与续写共用同一个提交入口 `_submit`，
     两条路径都生效）：Suno 遇到 `lo-fi` 这类词或以 `[Intro]` / `[Instrumental]` 开头时，
     常直接忽略歌词出一段纯音乐，因此提交前把 `tags` 里的 `lo-fi` / `lofi` 剔除
     （按分隔符切开重组，顺手消掉多余空格与空片段）、末尾强制追加
     `, clear vocal, lead vocal, vocal-driven`；歌词文本若开头没有中括号段落标记则补
     `[Verse 1]\n`，若开头是 `[Intro]` / `[Instrumental]` 则替换成 `[Verse 1]`。
     歌词只剩标签的纯器乐请求（续写留空歌词）不走这条预处理，保留用户主动要器乐的语义。
     审计日志：`suno payload prepared: instrumental=… continue=… tags=… prompt_head=…`。
     实测（`chirp-v3-5`，1 个真实任务）：带 `lo-fi` 的风格串被清成
     `夜晚城市, 温暖克制的钢琴与柔和鼓点, …, clear vocal, lead vocal, vocal-driven`，
     上游把它重写成含 `clear Chinese lead vocal … talk-sing …` 的人声描述，
     双生两条片段（32.1s / 61.9s）的 `tags` 都带人声指向，`make_instrumental=false`。
2. **提交任务**：`POST {SUNO_API_BASE}/suno/submit/music`，
   Header 注入 `Authorization: Bearer <SUNO_API_KEY>`，Payload 为
   `{ prompt, tags, make_instrumental, wait_audio: false }`（配置 `SUNO_MODEL` 时追加 `mv`）；
   携带续写参数时再追加 `continue_clip_id` 与 `continue_at`（秒，保留 3 位小数）。
   `continue_clip_id` 只允许 `[A-Za-z0-9_-]{1,64}`，否则返回 422。
   - **模型版本字段名是 `mv` 不是 `model`**：网关的 `/suno/submit/music` 文档字段表只有
     `prompt / mv / title / tags / make_instrumental / task_id / continue_at / continue_clip_id /
     gpt_description_prompt / notify_hook`，`mv` 取 `chirp-v3-0 / chirp-v3-5 / chirp-v4`。
     此前代码写的是 `payload["model"]`，配了 `SUNO_MODEL` 也等于没配（上游忽略未知字段）。
     **未实跑验证**：验这个要么烧一次真实任务（≈39 万额度），要么骗过网关的字段校验 ——
     而校验发生在鉴权之后，不带 key 连伪造路径都只回 401，观测不到「字段吃不吃」。
     默认 `SUNO_MODEL` 未设置，这条改动对现有行为零影响。
   - **上游没有任何时长字段可喂**：文档里既没有 `duration / custom_duration / max_duration`，
     成功响应也只有 `submit_time / start_time / finish_time / metadata.duration`（整体时长，
     没有逐词时间戳），所以「这一段要 8 秒」只能在拼接侧兑现，见 §2.5 的 `max_duration_sec`。
   - **建单前的退避重试**（`_post_submit`）：一段的多次任务建单是并发提交的，上游网关忙时接入队列会排队，
     TCP/TLS 握手在 `connect=10s` 内完不成抛 `ConnectTimeout`（实测口径是当时每段 3 并发：1 条进、
     2 条在 10.01s / 10.016s 整点失败；现在每段并发 2 条，压力更小但判据与重试参数不变）。
     这类请求的字节还没离开本机，重试不会重复建单扣费，因此按 1s、3s 退避最多再试
     2 次（最坏 3 次尝试 / 约 34s）；`ReadTimeout`、`WriteTimeout` 等「可能已经送达上游」的失败**一律不重试**，
     直接 502。异常类名与耗时都写进日志和 `detail`——httpx 的超时异常 `str()` 是空串，
     此前 502 文案只剩一个光秃秃的冒号，看不出卡在哪个阶段。
3. **轮询**：每 `SUNO_POLL_INTERVAL` 秒（默认 5）请求 `GET {SUNO_API_BASE}/suno/fetch/{task_id}`，
   直到 `status` 为 `COMPLETE` / `SUCCESS` 并拿到落定片段的 `audio_url`；`FAILED` / `ERROR` 立即失败，
   超过 `SUNO_TIMEOUT`（默认 300s）返回 504。解析函数 `_fetch_state` 返回
   `(status, 片段列表, 失败信息)`，片段列表按上游顺序保留**全部**已产出成品地址的片段，
   调用方按 `max_clips` 截取，不再只读 `data[0]`。
   - **轮询容错分两层**（`_poll_once` 的退避阶梯 + `_wait_for_audio` 的 tick 循环）：单次查询按
     1s / 2s / 4s 退避、最多 4 次尝试，覆盖传输层异常（连接、读写、协议）和上游 `429 / 5xx`。
     **阶梯耗尽只作废当前这一个 tick**（抛 `PollTickLost`），外层睡到下一个 `SUNO_POLL_INTERVAL`
     接着问，直到 `SUNO_TIMEOUT` 才报 504。改这条是因为实测：代理会连续几十秒随机掐 TLS，
     9.9s 的一口阶梯必被击穿 —— 那轮真跑里任务在放弃后 5.6s 就 `SUCCESS` 了（见 §2.3）。
     轮询是幂等 GET、不建单也不扣费，而任务此时**已经建单扣费**，所以「这一 tick 问不到」
     不构成判死它的理由。不可重试的 4xx（401 / 404）仍然直接失败，重试也救不回来。
     所有失败文案（5xx、上游 `FAILED`、完成却无音频地址、504）都带上 `task_id`，
     拿着它可以免费重查取回已扣费音频 —— §2.3 那轮的两条音频就是这么取回的。
     单个 tick 的阶梯最坏约 47s（4 次 10s 连接超时 + 7s 退避），所以 `_poll_once` 也收 `deadline`：
     下一次退避会越过总预算时就地作废这个 tick，不等阶梯跑完。**代价是代理抖动时「生成候选」
     按钮可以挂满 `SUNO_TIMEOUT`（300s）而不是 10s 就报错**，这是换了「不判死」之后的新形状。
     故障注入实测（本地网关，零成本；阶梯临时压到 0.05 / 0.1 / 0.2s 好复现）：第一个 tick 的 4 次尝试
     全掐、第 5 次成功 → 拿到 2 条片段，耗时 0.69s、`polls=5`；一直掐 → 作废 16 个 tick 共 61 次询问，
     10.25s 后报 504（预算 10s，只溢出 0.25s）；401 → 只问 1 次、0.01s 直接 502。
     走完整路由再验一次错误形状（`TestClient` + 一直掐的网关，真实 1/2/4s 阶梯，预算 10s）：
     504、10 次询问、5 个 tick 作废、**10.33s**（改 `deadline` 入阶梯前是 14.41s），
     detail 里带 `task_id` 与「总预算用完」。
     `SUNO_TIMEOUT` 有 `max(10, …)` 下限，配更小的值会被静默抬到 10s。
4. **兼容网关响应包**：第三方网关的业务码并不统一，成功形态可能是
   `{"code": 200, "data": {"task_id": "..."}}`，也可能是 `{"code": "success", "data": "task_xxx"}`
   （`data` 直接是任务 ID 字符串）；轮询结果里的片段还嵌了一层 `data.data[]`，其中
   `status: "streaming"` 阶段的 `audio_url` 是流地址、不能直接下载，因此只接受片段状态落定后的地址。
   `code` 不在成功集合内时按业务错误处理并透出上游 `message`。
5. **片段 ID 透出**：轮询结果里每个片段自带 `clip_id`，落定后连同音频地址一起返回；
   接口把第一条写进响应头 `X-Suno-Clip-Id`，其余序列化成 base64 JSON 放进 `X-Suno-Extra-Clips`，
   前端保存后可作为下一次续写的 `continue_clip_id`。
6. **下载与转码**：用 httpx 逐条下载上游音频到 `static/output/`，再用 ffmpeg 统一转成
   44.1 kHz / 立体声 / 16-bit WAV（文件名 `suno_<ts>_<rand>.wav`、`suno_<ts>_<rand>_1.wav` …），
   最后 `FileResponse(media_type="audio/wav")` 返回第一条；任一后续片段失败时回收已写盘的文件。
   下载失败的 502 文案写异常类名、耗时与**已落盘字节数**——httpx 超时异常的 `str()` 是空串，
   只内插异常本身只会剩下一个冒号；字节数则能区分「握手后没开始传」和「断流」。
   下载本身也按 2s / 5s 退避、最多 3 次尝试，覆盖传输层异常与上游 `429 / 5xx`（同样幂等、不扣费）；
   每次重试都重新以 `wb` 打开文件并把字节数归零，半截音频不会拼进新数据。48 MB 上限和不可重试的
   4xx 直接失败，不消耗重试次数。
   `/api/lyrics` 的 LLM 调用失败同样透出异常类名、耗时与目标端点。
   - **三条重试路径的验证方式**（建单、轮询、下载共用一套办法，都不额外扣费）：
     假 client 注入异常跑矩阵（提交 5 例、轮询 7 例、下载 7 例）、本地故障 HTTP 服务器回放已付费资产、
     以及拿真实 CDN 地址复下已扣费片段。回放一例是「第 1 次只发 400000/5660462 字节就断连
     （`RemoteProtocolError`，已落盘 393216 字节）→ 第 2 次 `503` → 第 3 次给完整音频」，
     `_materialize` 产出 WAV 时长 32.088s 与源资产一致，总耗时 7.13s 即 2s + 5s 退避。
     真实网络一例是把 `read` 压到 0.02s 打向 `cdn2.suno.show` 的已扣费片段地址：
     两次 `ReadTimeout（已落盘 0 字节）` 后按「尝试 3/3」耗尽报
     `下载 Suno 音频失败：ReadTimeout（10.0s / 已落盘 0 字节 / 尝试 3/3）`，不留半截文件；
     同一地址改用正常超时 2.12s 下完 977252 字节。健康网络下一次真实生成全程不进重试分支
     （建单 2.5s、出成品 31.8s、下载转码 6.0s，`transient failure` 日志 0 条），
     所以重试必须靠上面两种注入才能验到。
   - 重试发生时会打 WARNING，联调按这几个前缀 grep：`suno submit pre-send failure`、
     `suno fetch <task_id> transient failure` / `suno fetch <task_id> upstream HTTP`、
     `suno download <文件名> transient failure` / `suno download <文件名> upstream HTTP`。

| 环境变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `SUNO_API_BASE` | 是 | — | 代理服务根地址，例如 `https://your-suno-proxy.example.com` |
| `SUNO_API_KEY` | 是 | — | Bearer Token；缺失时接口直接返回 500 |
| `SUNO_MODEL` | 否 | — | 需要指定模型版本时以 `mv` 字段追加到提交 Payload（`chirp-v3-0 / chirp-v3-5 / chirp-v4`） |
| `SUNO_POLL_INTERVAL` | 否 | `5` | 轮询间隔（秒，可为小数），测试时可调小 |
| `SUNO_TIMEOUT` | 否 | `300` | 单次生成最长等待（秒） |
| `SUNO_VERBOSE_LOG` | 否 | `0` | 置 `1` 时把提交 Payload 与上游原始响应（HTTP 状态码 + JSON）打进后端日志，用于扣费接口联调留痕；不打印任何密钥 |
| `FFMPEG_BINARY` | 否 | imageio-ffmpeg 内置 | 自定义 ffmpeg 可执行文件路径 |

错误处理：未配置 `SUNO_API_KEY` 返回 500（`未配置 SUNO_API_KEY，請檢查後端環境變數。`），
上游提交/查询/下载失败返回 502（会把上游 `message` 透出，例如余额不足时的
`预扣费额度失败…`），生成超时返回 504，转码失败返回 500。

前端处理（`frontend/src/lib/musicgen.ts`）：

1. 校验响应 `Content-Type` 与 `RIFF/WAVE` 魔数，`URL.createObjectURL(blob)` 后读取真实时长；
   非 2xx、非 WAV 或解码失败都会提示错误。
2. 后端不可达时提示检查 FastAPI 与 Suno 代理是否已启动。
3. 生成失败会同时写入当前段的错误面板和底部 Toast（包括未配置 Key 的 500）。
4. 生成结果只存在于内存，锁定后才落盘（见 2.4）。
5. 每个候选保存 `X-Suno-Clip-Id` 返回的 `clipId`（`Take.clipId`）；`continueTakes()` 用它发起续写，
   并带上 `max_clips=2`，遍历 `X-Suno-Extra-Clips` 解码出的数组把每条残值片段下载成 Blob 后
   以「续写分支 A / B」标签追加为该段的独立子分支（卡片序号沿用「候选项 04 / 05」，都停在待选择），不覆盖已锁定的音频；
   残值片段下载失败只少一个候选，不会作废整次续写。

### 2.4 `POST /api/assets` — 候选落盘

`blob:` URL 只存在于浏览器，`/api/stitch` 无法读取，因此锁定 Take 时先上传 WAV：

```bash
curl -X POST http://127.0.0.1:8000/api/assets \
  -H "Content-Type: audio/wav" --data-binary @take.wav
```

```json
{
  "audio_url": "http://127.0.0.1:8000/static/generated/take_1793270165_9c3e4a.wav",
  "filename": "take_1793270165_9c3e4a.wav",
  "bytes": 1764044
}
```

- 请求体为原始 WAV 字节（非 multipart），仅接受 `RIFF/WAVE`；上限 `MAX_ASSET_BYTES` 直接引用
  `/api/stitch` 拉取远程音频的 `MAX_DOWNLOAD_BYTES`，即 **60 MB**。两处曾经各定各的口径
  （落盘 32 MB / 拉取 60 MB），同一条音频能不能进系统取决于它走了哪条路，这在产品上没有意义。
- 44.1 kHz 双声道 16-bit 是 176,400 B/s，所以 60 MB ≈ **356 秒** WAV；413 文案由常量推出，
  实测为 `音频超过 60 MB 上限（约 356 秒 WAV）`。`Content-Length` 超限时在读取请求体之前直接拒绝，
  因此最坏情况只是一次 `request.body()` 把 60 MB 读进内存，不会被大 body 撑爆。
- **32 MB 上限造成的真实事故**：32 MB ÷ 176,400 B/s = **190.2 s**，而上游给无词段落（`make_instrumental`）
  经常一次生成整首歌。实测一条已扣费的 192.6 s 尾奏 take（33,979,014 B）被 `POST /api/assets`
  以 413 拒掉 —— 前端只在浮层里报一句「保存音频失败：音频超过 32 MB 上限」，浮层 3.2 s 后自动消失，
  段落仍是「待选择」，钱花了但候选永远锁不上。改到 60 MB 后同一条 take 返回 200（`bytes=33979014`）。
  这条事故的另一半（提示本身活不过读完的时间）已由「提示分级」修掉：`保存音频失败` 现在是
  `error` 级常驻浮层，见第 4 节。
- 落盘目录 `backend/static/generated/`，文件名由时间戳 + 随机串生成。
- 前端在「锁定」时上传；失败则不锁定并提示，避免拼接时引用失效地址。这条失败是 `error` 级浮层，
  常驻到手动关闭（见第 4 节「提示分级」），但后端文案仍要自解释 —— 上一条只写「上限」不写秒数，
  用户即使来得及读也要自己算 60 MB 能用多久。
- 这是系统里唯一的音频上传路径：生成请求不再携带音频，`/api/assets` 只服务于最终拼接。

### 2.5 `POST /api/stitch` — 多段拼接

请求（`StitchRequest`）：

```json
{
  "segments": [
    { "id": "intro-1",  "audio_url": "http://127.0.0.1:8000/static/generated/take_1793270005_199b9f.wav",  "crossfade_ms": 8 },
    { "id": "verse-1",  "audio_url": "http://127.0.0.1:8000/static/generated/take_1793270165_9c3e4a.wav",  "crossfade_ms": 8 },
    { "id": "chorus-1", "audio_url": "http://127.0.0.1:8000/static/generated/take_1793270169_1a84eb.wav",  "crossfade_ms": 8 }
  ],
  "output_name": "museblocks-mix"
}
```

- `segments` 数量 2–16，顺序即最终拼接顺序；音频地址必须是后端可访问的 `http(s)` 或 `/static/...`。
- `crossfade_ms` 表示「本段与**下一段**」的交叉淡化时长（0–5000），最后一段的该值被忽略；
  UI 固定传 8 ms，仅用于消除接缝爆音，听感上等价于硬切。
- `max_duration_sec` 选填（`0 < x ≤ 600`）：本段在成品里最多保留几秒，超长部分物理裁掉。
  这是「这一段只要 8 秒」唯一的兑现点 —— 上游没有任何时长字段可喂（见 §2.3），
  空歌词的纯器乐段一律写满一整首，实测三条前奏候选 182.568 / 192.624 / 177.240 s。
  UI 只对**无歌词段落**传这个值，取 `plannedTruncateSec(section)`（`lib/lyrics.ts`，
  与 `estimateDuration` 同一套规则：无词的 intro/outro 8s、其余无词段 12s）；
  有歌词的段落时长由唱词决定，裁在乐句中间比留一段长尾更糟，所以不传。
  这里刻意不用 `section.durationSec`：生成后它会被 `applyNaturalDuration()` 覆写成返回音频的
  实际长度（时间轴上那块前奏现在显示 182.6s 就是它），拿 182.6 去裁 182.6 等于什么都没做。
- `output_name` 仅允许 `[A-Za-z0-9_-]`，默认 `mix`。

响应（`StitchResponse`）：

```json
{
  "audio_url": "http://localhost:8000/static/output/doc-example_1793766387_intro-1.wav",
  "filename": "doc-example_1793766387_intro-1.wav",
  "duration": 491.883,
  "peak_dbfs": -2.97,
  "sample_rate": 44100,
  "crossfade_ms": [8, 8, 8],
  "segment_order": ["intro-1", "verse-1", "chorus-1"],
  "segment_start_sec": [0, 176.559, 349.166],
  "head_trim_sec": [0, 0.023, 0.246],
  "tail_trim_sec": [1.441, 0.018, 0.005]
}
```

上面这份响应是一次真实调用的原样输出（三段各约 143–178 s）。核对得上：
`491.883 = (178.008 − 1.441) + (172.656 − 0.023 − 0.018) + (142.968 − 0.246 − 0.005) − 2 × 0.007982`，
`176.559 = 178.008 − 1.441 − 0.007982`（重叠 8 ms = 352 采样 = 7.982 ms）。

- `duration` 为实际成品时长：`Σ 各段时长 − Σ 头部剔除 − Σ 尾部剔除 − Σ 交叉淡化重叠`。
- `segment_start_sec` 与 `segment_order` 一一对应，给出每段在成品时间轴上的起点（秒，保留 3 位）。
  淡化区是两段共用的样本，所以起点会往前挪一个重叠长度（首段 0，其后 `已混长度 − 重叠采样 / 采样率`）；
  前端 `locateMixSegment()` 用它把「全曲时间点」映射回「段落 + 段内偏移」。
  反过来也成立：相邻起点之差再加一个重叠就是**该段在成品里真实占用的长度**（最后一段占到结尾），
  `mixSegmentSpans()` 就靠这个把「成品实际占用」色带画出来（见第 4 节），不需要后端新增任何字段。
- `head_trim_sec` 给出每段拼接时被物理剔除的头部静音（首段恒为 0）。段内偏移要换算回源片段
  自己的时间轴（续写用的 `continue_at` 指的是原始 clip），必须把这段垫片加回去，
  因此 `locateMixSegment()` 计算 `全曲时间 − 段起点 + 该段 head_trim`。
- `tail_trim_sec` 给出每段被物理剔除的尾部静音，仅用于审计：尾部裁切只缩短**自己这段的结尾**，
  不改自己这段的起点，所以段内偏移公式不需要它；下一段的起点会随之提前，
  而那个值由服务端在 `segment_start_sec` 里直接给出，前端不需要二次推算。
- `truncate_to_sec` / `truncate_snapped` 是 `max_duration_sec` 的回执：前者给出该段实际保留的秒数
  （没给上限、或素材本来就不超限 → `null`），后者给出切点有没有吸到 ±0.5 s 内的结构边界候选。
  真实调用（三段各约 177–193 s，上限分别 27.5 / 8 / 不传）：

  ```json
  {
    "duration": 212.921,
    "segment_start_sec": [0, 27.769, 35.761],
    "head_trim_sec": [0, 0.382, 0.027],
    "tail_trim_sec": [0.184, 1.125, 0.053],
    "truncate_to_sec": [27.777, 8.0, null],
    "truncate_snapped": [true, false, false]
  }
  ```

  `212.921 = 27.777 + 8.000 + (177.240 − 0.027 − 0.053) − 2 × 0.007982`：裁切发生在头部/尾部静音
  剔除**之后**，所以先裁静音再量 8 s，保留的是内容而非 8 s 静音。
  第一段吸到了 27.777 s 的候选（目标 27.5，误差 +0.277 s，日志 `discarded=154.607s`），
  第二段窗内无候选就硬停在 8.000 s —— 这三条前奏素材的前 10.5 s 探测窗内确实一条候选都没有，
  整首 182.378 s 也只有 27.777 / 53.981 两条（纯氛围铺底，本来就没有段落切换）。
- 错误映射：非法音频源 400、文件不存在 404、其他 500、`output_name` 非法 422、
  `max_duration_sec` 越界（≤0 或 >600）422。

### 2.6 `POST /api/structure/boundaries` — 结构边界候选

只服务于一件事：给成品波形的**播放头吸附**提供可选落点，不做自动分段。
clip 级段边界由 `/api/stitch` 的 `segment_start_sec` 直接给出毫秒真值，无需检测；
这个接口补的是「单个 clip 内部的结构」——段边界信息完全看不到的那部分。算法与标定见 3.3。

| 字段 | 类型 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `audio_url` | str | 必填 | ≤2048 字符 | 与 `/api/stitch` 同源：`/static/...` 或白名单远程 URL |
| `floor` | float | `0.09` | `0 < floor < 1` | 新颖度固定绝对下限，调低 → 候选更密 |
| `gap_ms` | int | `1000` | `100 ≤ gap_ms ≤ 30000` | 相邻候选的最小间距 |

响应（`snap-seq2` 成品，491.883 s，实测抓下来的真实返回，列表做了省略）：

```json
{
  "audio_url": "/static/output/snap-seq2_1793774333_suno_179.wav",
  "duration": 491.883,
  "sample_rate": 44100,
  "floor": 0.09,
  "gap_ms": 1000,
  "candidates_sec": [1.991, 54.375, 108.768, 111.322, 112.332, 176.373, 178.138, "..."],
  "candidate_strengths": [0.1289, 0.1271, 0.1125, 0.2378, 0.1106, 0.1593, 0.1464, "..."],
  "compute_ms": 1068
}
```

- `candidates_sec` 已按时间升序、保留 3 位小数，与 `candidate_strengths` 一一对应。
  强度即该点新颖度（默认参数下必 ≥ `floor`），前端只用它决定刻度线深浅，不再做二次判阈。
- 上面两条真接缝 `176.559 / 349.166` 分别被检出为 `176.373(0.1593) / 349.129(0.1081)`，
  即 −0.186 s / −0.037 s；其余 22 个是同一首里真实存在但**不是**拼接点的新颖度峰（换段、进唱、加乐器）。
- 音频整段读入内存做单遍 numpy 计算，不写任何产物文件；`compute_ms` 覆盖「读取 + 解码 + 检测」
  （不含 HTTP 往返）。四条不同长度的成品实测 179 / 285 / 339 / 1068 ms（75.057 / 235.395 /
  281.771 / 491.883 s），约 1.2–2.4 ms 每音频秒，所以前端每次拿到新成品现算，不加缓存。
- 错误映射：非法音频源 400、音频不存在 404、其他 500、参数越界 422（pydantic）。

### 2.7 `GET /api/analyze_structure` — 深度学习结构分段

给成品补上「clip 内部的结构」的**语义版本**：谱形状检测器只能给时间点，这个接口给
`intro / verse / chorus / bridge / break / solo / instrumental / outro` 标签加区间。
算法、容器拆分与精度实测见 3.4。

| 参数 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `clip_id` | str | 1–64 字符，必填 | 三选一：Suno 片段 ID（走 `data/clip_index.json` 索引）、落盘文件名或其去后缀 stem、`/static/...` 资源路径 |

`clip_id → 文件` 的映射在补 `utils/clip_index.py` 之前并不存在：落盘名 `suno_{时间戳}_{6位}`
里那 6 位取自 `uuid4().hex`，是本地随机数不是 Suno 的 clip_id，而 clip_id 只出现在响应头
`X-Suno-Clip-Id` 里，下载完就丢了。现在 `_materialize` 每落一个片段就 `clip_index.record()`
一笔（原子写 `backend/data/clip_index.json`，已 gitignore），历史成品只能按文件名访问。

成功响应（真实返回，`segments` 已保证无缝、无洞、Σ = `duration_sec`）：

```json
{
  "clip_id": "suno_1793678392_03ddbf",
  "filename": "suno_1793678392_03ddbf.wav",
  "duration_sec": 178.008,
  "model": "harmonix-fold0",
  "degraded": false,
  "segment_count": 7,
  "segments": [
    { "label": "intro", "start": 0.0, "end": 13.42 },
    { "label": "verse", "start": 13.42, "end": 26.84 }
  ],
  "analyze_ms": 41238,
  "backend": { "route": "analyzer", "analyzer_url": "http://analyzer:8100", "model": "harmonix-fold0", "timeout_sec": 1434.0, "queue_budget_sec": 900.0, "sec_per_audio_sec": 3.0, "work_dir": "/tmp/museblocks-structure" },
  "matched": "clip_index",
  "task_id": "…",
  "recorded_at": 1793843333
}
```

- `duration_sec` 一律取本地 WAV 头（`wave` 模块），不信模型或上游回传的数字。
- `backend.timeout_sec` 不是全局常量，是**这条请求按音频时长现算的上限**：
  `排队预算 max(AIO_QUEUE_TIMEOUT, 1.5 × 时长) + 推理预算 max(STRUCTURE_ANALYZER_TIMEOUT, 3 × 时长)`。
  178.008 s 的成品 = 900 + 534 = **1434.0 s**；`duration_sec` 为 0 的健康检查拿到 900 + 180 = 1080.0 s。
  两段各自的意义与实测倍率见 3.4。
- 推理在 analyzer 容器里跑，主后端用 `run_in_threadpool` 等待，事件循环不阻塞；`analyze_ms` 覆盖
  「排队 + 分轨 + 谱特征 + 推理 + 归一化」全程 —— 计时起点在拿锁之前（`_guarded_analyze` 的
  `started`），所以排在别人后面时这个数主要是等待，实测见过缓存命中只有几十毫秒、`analyze_ms`
  却报 631103 的那次（见 3.4 的排队复现）。
- **降级契约**：模型装不上 / 分析服务不可达 / 返回不可解析时，`logger.exception` 打完整栈，
  HTTP 500 + `degraded: true` + `detail`（截断到 300 字符）+ 单段兜底
  `{"label": "unknown", "start": 0.0, "end": duration_sec}`，前端 DAG 拿到的是合法区间而不是空数据。
  主容器不含 torch 时实测即走此分支（`detail` 为
  `StructureAnalysisError: 分析依赖不可用: ModuleNotFoundError: No module named 'allin1'`，13 ms 返回）。
  当前默认走 analyzer 容器，这条分支用零成本注入复现：给主后端一个指向死端口的
  `STRUCTURE_ANALYZER_URL`，178.008 s 的成品 **258.5 ms** 返回
  `分析服务不可达(1434s 超时上限): ConnectError: [Errno 111] Connection refused`，兜底段
  `{"label": "unknown", "start": 0.0, "end": 178.008}`。文案里带上超时上限是为了区分
  「连不上」（毫秒级）和「跑太久」（贴着上限）。
- 错误映射：路径越界 400、找不到音频 404、参数缺失 422、分析失败 500（带兜底段）。

### 2.8 `POST /api/structure/jobs` + `GET /api/structure/jobs/{job_id}` — 异步结构分析

2.7 那条同步接口让调用方举着一条 HTTP 连接等完整条流水线。整曲推理是 **1.05 倍实时**
（3.4 有实测），一首 8 分钟的成品就是 8 分钟以上的等待：中间任何一层代理、浏览器或
Vite/Next 的重试把连接掐断，前面跑的分轨就白烧了。所以任务化：提交立刻返回 `job_id`，
结果靠轮询取，页面重挂不影响后台继续算。

| 接口 | 参数 | 响应 |
| --- | --- | --- |
| `POST /api/structure/jobs` | body `{ "clip_id": "…" }`（1–64 字符，解析规则同 2.7） | 新任务 `202`；命中同一条未失败任务 `200` + `reused: true` |
| `GET /api/structure/jobs/{job_id}` | 路径参数 | 任务快照；表里没有（进程重启过）→ `404` |

响应体是 2.7 的字段加上任务状态：`status ∈ queued / running / done / failed`、`final`
（`done`/`failed` 为真）、`elapsed_sec`（提交到此刻的墙钟，排队与推理都算）、
`analyze_ms`（仅推理段）、`reused`、`model`。2.7 那个 `degraded` 也一起返回，但它在这里
和 `status == "failed"` 完全同义（`public()` 就是这么算的），留着只为两份响应同构 ——
前端只读 `status`，不再自己造「done 且降级」这种到不了的分支。

四条机制：

- **按 clip 去重**：`JobTable._by_clip` 让「连点三下波形」只跑一遍分轨 —— 那是 1 倍实时级别
  的开销，重复提交回来的是同一个 job。
- **failed 不进复用**：降级往往是分析服务重启、权重没落卷这类临时故障，复用会把这条成品
  永远钉在兜底段上；再提交一次就重新排队。
- **同时只跑一个**：`asyncio.Semaphore(1)`。分析容器内部本来就是一条流水线（`_inference_lock`），
  主后端这边再并发只会把请求堆进对端的排队预算（`max(AIO_QUEUE_TIMEOUT=900, 1.5 × 时长)`）里等 429 然后降级。
  槽位等待另有 `_SLOT_TIMEOUT_SEC = 3600` 兜底，超时也收敛成 `failed` + 兜底段，不外抛。
- **状态有界**：只留最近 64 条，按完成时间淘汰；表在进程内存里，`--reload` 重启即丢，
  所以 404 是预期分支 —— 前端 `runStructureJob` 会重新提交一次；再 404 就判死并把状态落回
  `failed`，否则一个已被重启抹掉的任务会把轮询挂成永续（每 3 s 问一次永远问不到的 id）。

失败仍然不是异常：`failed` 带着 `detail` 和 `fallback_segments(duration)` 的单段区间，
与 2.7 的降级契约同构，前端拿到的一定是合法结构。

### 2.9 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务信息（`provider: suno-proxy`） |
| GET | `/api/health` | `{ status, provider, suno_configured, sample_rate }` |
| GET | `/static/...` | 落盘候选、生成 WAV 与拼接产物 |

---

## 3. 音频算法

### 3.1 上游转码

Suno 代理返回的通常是 mp3（也可能是 m4a/wav）。后端下载原始文件到 `static/output/`
后统一执行 ffmpeg 转码（二进制由 `imageio-ffmpeg` 提供，无需系统安装）：

```bash
ffmpeg -y -i source.mp3 -ar 44100 -ac 2 -c:a pcm_s16le output.wav
```

这样前端与 `/api/stitch` 只需要处理一种格式，采样率与声道也保持一致；
原始压缩文件在转码完成后删除，只保留 WAV。

### 3.2 拼接 DSP（`utils/audio_processor.py`）

1. **统一格式**：所有输入重采样到 44.1 kHz、双声道、16-bit；`_load_audio()` 兼容远程 URL
   （scheme + host 白名单、15 秒超时、重定向后再次校验、60 MB 上限）与本地 `/static/...`。
2. **等功率交叉淡化**（`_crossfade`）：交叉区内 A 段乘 `cos θ`、B 段乘 `sin θ`，`θ ∈ (0, π/2)`，
   两端采样点各偏移半个采样，避免曲线端点为 0 导致的能量凹陷与爆音：
   ```python
   t = (np.arange(length) + 0.5) / length
   theta = t * np.pi / 2
   fade_out, fade_in = np.cos(theta), np.sin(theta)
   ```
   当前前端固定传 `crossfade_ms = 8`，重叠区只有约 350 个采样点，听感上是硬切，
   既保留算法兼容性，又避免 Suno 成品人声在长交叉区里出现重影。
3. **首尾静音绝对裁切**（`_trim_head_silence` / `_trim_tail_silence`，同一个阈值）：Suno 续写出来的
   音频头部带约 270 ms 数字静音，直接进交叉淡化会让拼接点前面出现一段听感真空。拼接循环在处理
   **非首段**时先按帧取声道最大绝对值，找到第一个达到阈值（默认 `HEAD_TRIM_THRESHOLD_DBFS = -50`，
   即幅度 0.00316）的帧作为新起点，把之前的静音帧物理剔除，再与前一段的截断点做 8 ms 交叉淡化；
   全段都不超阈值时不裁，避免交出空数组。首段保留原始片头留白，不裁头部。
   尾部同理：从后往前找最后一个达到阈值的帧，之后的帧一律剔除。这条是**必需的**而不是美化——
   6 个已付费成品自带的尾部数字静音实测（同一判据，声道最大绝对值 ≥ −50 dBFS）为
   `700f18` 0.705s、`700f18_1` 0.623s、`7d0507` 0.021s、`03ddbf` 1.441s、`ea3715` 0.018s、
   `b37b93` 0.005s；不裁的话每个接缝前面都留着最多 1.4s 死寂，成品末尾同样拖着静音。
   裁完之后 4 组探测混合（共 11 个段）的成品末尾静音只剩 **2.8 / 7.9 / 15.9 / 2.9 ms**（即首尾 12 ms
   余弦淡变区的残余），`duration` 与 `segment_start_sec` 对上「段长 − 头裁 − 尾裁 − 7.982 ms 重叠」
   恒等式，最大残差 0.9 ms（来自响应里保留 3 位小数的舍入，不是 DSP 误差）。
   `700f18`、`700f18_1`、`7d0507`、`03ddbf`、`b37b93` 这 5 条分别落在「首段」和「后续段」两个分支
   各一次，裁掉的尾部长度逐位相同（`ea3715` 只作为中间段出现一次），说明这条判据与它在序列里的位置无关。
   每段无条件打印 `silence_trimmed: head={帧数} frames ({ms}) tail={帧数} frames ({ms}) at
   threshold -50.0dBFS`（裁到 0 帧也打印，用来区分「这段本来没有垫片」和「算法没跑」），
   汇总进 `head_trim_ms / tail_trim_ms`，并通过 `StitchResponse.head_trim_sec / tail_trim_sec` 回传。
   注意别把这套 −50 dBFS 判据当成结构检测的门控用到音频分析上：尾裁落地后重测 7 条接缝，
   仍有 3 条在 ±2 s 窗口内有 40.5–58.0% 的帧低于 −40 dBFS（门前静音 1.36–2.5 s），原因是**这些段落
   本身就是淡出收尾**而不是数字垫片——低电平的段尾和静音在电平判据下长得一样，用它做门控会把真边界杀掉。
   反过来这些接缝 ±1 s 内的峰值仍是 −50 dBFS 阈值的 94–184 倍，所以两套判据的松紧差了约 24 dB 再加
   RMS/峰值的口径差，不能混用。
4. **按段时长上限**（`_truncate_to_length`，`CUT_SNAP_SEC = 0.5`）：首尾静音剔完之后，若该段给了
   `max_duration_sec` 且仍然超长，就物理裁到目标长度。切点不硬停在秒数上 —— 先在
   `[目标 − 0.5s, 目标 + 0.5s]` 内取最近的谱新颖度候选（`structure_detector.boundary_candidates`，
   定位精度见 §3.3 的 MAE 0.062s），窗内没有候选才回到目标秒数本身；所以 `truncate_to_sec`
   可能比请求的上限**多** ≤0.5 s，这是设计而非 bug。裁切发生在静音剔除之后、交叉淡化之前，
   两个分支（首段 / 后续段）都会走。
   探测窗只取 `目标 + 0.5s + max(WINDOW_SEC, GUARD_SEC)`：候选出不了这个范围，整首扫描是白花时间
   —— 实测 182.378 s 整首 787.5 ms，10.5 s 探针只要 13.1 ms。这里必须让出 `WINDOW_SEC = 2.0`
   而不是只让 `GUARD_SEC = 1.0`：`novelty_curve` 要求切点右侧凑满一整个 2 s 块
   （`index <= total − window_frames`），只让 1 s 会把窗口右半边的候选全吃掉 ——
   目标 27.5 s 配 29.0 s 探针时 27.777 s 那条候选直接消失，探针改成 30.0 s 才回来。
   每次裁切打印 `duration_truncated: kept / target / cut / snapped / discarded / probe / candidates`。
   上限大于素材、或素材本来就不超限时原样返回，`truncate_to_sec` 为 `null`。
5. **防爆音**：
   - 去直流偏置（`mixed -= mixed.mean(axis=0)`）；
   - 成品首尾 12 ms 余弦淡入/淡出（`_apply_edge_fade`）；
   - 峰值限制到 −1 dBFS，**仅做衰减不做放大**（`_limit_peak`），避免削波同时不引入额外失真。
6. **写出**：pydub `from_wav` → Python `wave` 模块直接写 WAV，因此在没有 ffmpeg 的环境也能工作。
7. **多段顺序拼接**（`stitch_sequence`）：用 `pending_crossfade` 把「上一段的交叉淡化时长」应用到
   当前接缝，保证语义与请求中的 `crossfade_ms` 一致；每个接缝打印
   `prev_end / overlap / segment_start / next_len / head_trim` 的毫秒级审计日志。

### 3.3 结构新颖度检测（`utils/structure_detector.py`）

容器里只有 numpy（无 torch / librosa / scipy / soundfile / pedalboard），特征全部手写：

1. **谱形状特征**：`n_fft=2048`、`hop=512`（44.1 kHz 下约 11.6 ms/帧）的 STFT，双声道先降 mono；
   `32` 个三角 mel 滤波器铺在 50–8000 Hz，按频点带宽归一（`2 / (edge[i+2] - edge[i])`）；
   每帧 log-mel **减掉该带在全部帧上的均值**再做 L2 归一化，得到单位长度的「谱形状」。
   去带均值是为了让判据只看斜率变化，不被整体响度和常驻低频（底鼓、贝斯）带着走，
   L2 归一化则消掉响度差 —— 这两步是下面能用**固定绝对**阈值的前提。
2. **新颖度**：`nov[i] = 1 − cos( mean(shape[i+1−K … i]), mean(shape[i+1 … i+K]) )`，
   `K = WINDOW_SEC = 2.0 s` 折算的帧数（170 帧）。左右两块各取平均谱形状再算 cosine distance，
   量纲天然绝对（0 = 两块同形），所以不需要按素材重新标定统计量。
3. **边界时刻**：接缝正好夹在左右块之间，标记为 `times[i] = (i + 0.5) * HOP / SR`，
   **不是** `(i + K) * HOP / SR`。标定第一版写成后者，7 条接缝全部系统偏晚 +1.976 s（正好一个块长），
   这是块对比类特征最容易踩的标签错位，改对后偏差均值降到 −0.026 s。
4. **有效性掩码**：`i` 必须落在 `[K−1, T−K]`，否则左右块越界、被零填充污染，
   会在音频首尾凭空造出约 2.1 s 处的强假候选；两端再各留 `GUARD_SEC = 1.0 s` 不检测。
   越界与门控外的帧直接置 NaN，不参与选峰。
5. **选峰**：贪心，按新颖度从高到低取，取走后就地屏蔽 `gap_sec` 内的其它帧，最后按时间升序返回。
   `floor` / `gap_sec` 由接口透传，默认 `0.09 / 1.0 s`；每次数打印
   `structure_candidates: N points floor=… gap=… analyzed=…`。

**为什么用固定绝对下限而不是 `mean + kσ`**：`mean + kσ` 依赖「这段音频自己的统计量」，
同一条 clip 单独分析和嵌进 8 分钟的混合里分析，均值与 σ 差好几倍，候选数量随素材时长漂移，
短前奏段的真边界会被自己的统计量吃掉。实测在同一批混合上，绝对下限换素材换长度都稳定，
这也是本节所有门槛都写成 0.09 这种绝对值、而不是「本曲第 90 百分位」的原因。

**标定结果**（4 组本地重拼的探测混合，共 1084.106 s / 11 段 / 7 条接缝；
真值取尾裁落地后 `/api/stitch` 回传的 `segment_start_sec`，判定窗口 ±1 s；全部零成本）：

| 参数 | 召回 | 有符号偏差 | MAE | 候选总数 | 精确率 | 假候选/15s | ±1s 覆盖 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **floor 0.09 / gap 1.0 s（生产默认）** | **7/7** | **−0.026 s** | **0.062 s** | **65** | **10.8%** | **0.80** | **11.1%** |
| floor 0.16 / gap 1.0 s | 2/7 | — | — | 17 | — | — | — |
| floor 0.09 / gap 3.0 s | 6/7 | — | — | 46 | — | — | — |

- **floor 必须低到 0.09**：尾裁落地后真接缝处的新颖度只剩 0.105–0.189（`349.166` 那条只有 0.1081），
  旧值 0.16 会杀掉 5/7 条。裁掉段尾静音同时也削掉了接缝处最强的对比能量，这是门控必须重标定的原因。
- **gap 必须收到 1.0 s**：3 s 的最小间距让一条真边界被 3 s 内更强的假峰挤掉（6/7）；
  吸附场景允许可选点靠得近，不必强行拉开。
- 精确率 10.8% 是**可接受的代价**：这些点不当决策依据，只当鼠标落点的可选目标，
  代价是波形上每 15 s 多出约 0.8 根刻度线；±1 s 窗口覆盖 11.1% 的时间轴
  （只放真接缝时只有 1.3%），说明「随手一点就吸到某个候选」的概率被刻意压低。
  表里的 ±1 s 只是**标定口径**，产品实际用的吸附窗是 ±0.5 s（覆盖 6.0%），理由与像素代价见 4 节。
- 不要在这里叠 3.2 那套 −50 dBFS 电平门控（更别用 −40 dBFS），淡出收尾的段尾会把 3/7 条真边界打掉。

### 3.4 深度学习结构分析（`utils/structure_analyzer.py` + allin1）

3.3 的检测器只给时间点，给不出「这是副歌」；歌词分段又只到段落粒度，clip 内部的实际演唱结构
（前奏多长、哪里进唱、哪里反复）没人知道。这里引入 All-In-One Music Structure Analyzer
（`allin1==1.1.0`，PyTorch 复现版）补语义标签。

**精度口径先讲清楚**（这决定了它的数字能不能和 3.3 混着看）：模型在 100 FPS 的帧网格上工作
（`config.py`：`sample_rate=44100`、`hop_size=441` → 一格 0.01 s），输出的 start/end 能报到毫秒，
但那是**量化分辨率**不是**定位精度** —— 边界定位误差是 ±0.1~0.5 s 量级。拼接时算出的
`segment_start_sec` 才是 clip 级毫秒真值，两者不能互换。它不可替代的价值是语义标签，
那是谱形状检测器原理上给不出的。

**推理链**（`allin1.analyze()` 无条件跑完，没有关掉的开关）：

1. `demix()`：`subprocess python -m demucs.separate --name htdemucs --device cpu`，分离 4 轨再喂模型；
   4 轨 float WAV 一首曲子就上百 MB，所以本项目传 `keep_byproducts=False`，只留结果 JSON。
2. `extract_spectrogram()`：madmom 的 STFT / mel / chroma / beat-down 特征链。
3. DiNAT（dilated neighborhood attention transformer）推理 + 后处理（`argrelextrema` 选段、
   metrical 对齐）→ `List[Segment(start, end, label)]`，单位秒。

**结果归一化**（`normalize_segments`，把它给的段变成能物理裁切的段）：边界只在「切点」上取整一次，
相邻两段共用同一个数，因此 `Σ(end − start) == duration` 且 start/end 严格单调，前端不需要再缝合；
长度不足 `MIN_SEGMENT_SEC = 2.0 s` 的段并回前一段（首段过短则直接去掉它的切点），
未知标签映射成 `section`，`inst` 映射成 `instrumental`。

**精度怎么量**（`backend/scripts/calibrate_structure.py`，只用标准库）：真值不靠人工标注 ——
探测混合是用本项目的 `/api/stitch`（8 ms 微交叉淡化）把**已落盘的成品**重拼出来的，接缝时刻取自响应里的
`segment_start_sec[1:]`，那是采样点级的毫秒真值；整个过程不打 Suno、不花钱。清单
`backend/data/structure_probe.json`（提交进仓库）记着 4 条混合、共 1364.778 s / 13 段 / 9 条接缝
（31.375 ~ 404.406 s，短前奏与超长成品都覆盖到）。`--build-probes` 重拼一遍会逐位复现同一批接缝
（2026-11-05 新旧两批独立拼出，9 条完全一致 → 拼接路径是确定性的，这份尺子可以反复用）。
打分对两个检测器用同一段代码：召回按接缝取最近候选、精确率是「一个候选只认一条接缝」的贪心、
覆盖率按 ±窗口排序并集算，汇总按**接缝数**加权而不是按文件平均（否则 2 条接缝的 587 s 成品和
3 条接缝的 157 s 成品会被同权看待）。
同一把尺子还要回答一个产品问题：allin1 的段边界该怎么和检测候选一起用。脚本给两条路线各打一分 ——
`narrow_score` 是「用段边界把候选限制在 ±0.5/1/2/5 s 内」，报每档窗口留下多少候选、以及**真接缝的最近
候选还活不活着**（这条才是判据，候选少不是目标）；`union_score` 是前端实采的「候选 ∪ 段边界」，
报 ±0.5 s 内可吸接缝在并集前后各是多少、以及模型额外贡献了几个刻度点。结论见下面的表与 4 节。

注意这与 3.3 那张表**不是同一把尺子**：3.3 标定用的探测混合是 1084.106 s / 11 段 / 7 条接缝，
本节的 9 条接缝集是重拼的扩充版，两边数字不能直接横向引用。

两边拿同一把尺子打分（`--markdown` 直接出表，2026-11-05 那轮冷跑，`--rss` 同步采样分析容器）：

| 检测器 | 候选数 | 召回@0.5s | 召回@1.0s | MAE@1.0s | 偏差@1.0s | 精确率@1.0s | 假候选/15s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| allin1 结构分段 | 62 | 3/9 | 3/9 | 0.204 s | 0.204 s | 4.8% | 0.65 |
| 谱形状新颖度 | 68 | 6/9 | 7/9 | 0.215 s | 0.127 s | 10.3% | 0.67 |

MAE 打平（0.2 s 出头），差别全在**召回**：模型只在它认为「段变了」的地方给点，9 条真接缝只覆盖到 3 条；
检测器覆盖到 6 条。两边各有各看不见的接缝，所以前端取并集（机制见 4 节）：

| 成品 | 时长 | 检测候选 | 模型新增 | 刻度合计 | ±0.5 s 可吸接缝 | ±0.5 s 覆盖率 |
| --- | --- | --- | --- | --- | --- | --- |
| dlp-A | 491.883 s | 24 | +12 | 36 | 2 → 2 | 4.9% → 7.0% |
| dlp-B | 127.887 s | 16 | +5 | 21 | 1 → 2 | 12.5% → 14.4% |
| dlp-C | 157.843 s | 21 | +4 | 25 | 2 → 2 | 13.3% → 15.0% |
| dlp-D | 587.165 s | 7 | +24 | 31 | 1 → 2 | 1.2% → 5.2% |
| 合计 | 1364.778 s | 68 | +45 | 113 | **6/9 → 8/9** | **5.0% → 7.9%** |

dlp-D 最能说明为什么要并集：检测器在那条 587 s 的成品上只给出 7 个候选（±0.5 s 覆盖 1.2%，
基本点不中），模型给了 28 个段边界，可吸接缝从 1/2 变 2/2。代价写在最后一列 —— 覆盖率就是
「随手一点被吸走」的概率，全局 5.0% → 7.9%。这不是白来的，但它付的是可以看见的代价：
吸附只在成品播放器发生，且面板同时显示落点原值与吸附后的值（4 节）。
（这里的 5.0% 基线是本节这批 1364.778 s 素材；3.3 那个 6.0% 是另一批，两者别横向引用。）

**收窄被实测否决**（`narrow_score`，把「用模型边界过滤检测候选」当一次裁剪打分）：

| DL 收窄窗 | 保留候选 | 真接缝存活（±0.5 s 吸附窗内） | 收窄后精确率 |
| --- | --- | --- | --- |
| ±0.5 s | 24/68（35.3%） | 1/6 | 4.2% |
| ±1.0 s | 29/68（42.6%） | 1/6 | 3.4% |
| ±2.0 s | 37/68（54.4%） | 3/6 | 8.1% |
| ±5.0 s | 56/68（82.4%） | 6/6 | 10.7% |

「真接缝存活」是判据：分母是检测器全量下本来吸得上的 6 条接缝，分子是过滤后还能吸上几条。
±0.5 s 只剩 1/6，±1.0 s 还是 1/6，要到 ±5.0 s 才回到 6/6 —— 而那时 68 个候选已留下 56 个，
等于没收窄。原因和 dlp-B 上是同一件事：模型的边界误差不是稳定的 0.2 s，是会大到 4.42 s
（96.99 vs 真值 92.567），窗口一窄就把真候选连坐删掉。**减候选数不是目标，吸得上才是。**

模型的语义标签在这批素材上没有判别力：dlp-A 的 22 段里 16 段叫 `intro`（只有 4 种标签），
dlp-D 的 29 段只有 3 种（`break`×15、`intro`×13），段长中位数 19.7~25.5 s。所以色带只当
「这里段变了」的提示读，曲式名字别当真 —— 这也是它不参与任何时间换算的另一个理由。

墙钟（同一轮冷跑，`analyze_ms` 覆盖分轨 + 谱特征 + 推理 + 归一化全程）：
dlp-A 536.4 s（**1.09×**）、dlp-C 173.8 s（**1.10×**）、dlp-D 599.9 s（**1.02×**），
dlp-B 0.1 s（命中上一轮 bench 留在结果卷里的 JSON，走自读缓存那条路，见下面的 allin1 缺陷）。
四条合计 1310.2 s / 1364.778 s 音频 = 0.96×，扣掉那条缓存命中的是 1310.1 s / 1236.891 s =
**1.06× 实时** —— 与 bench 单独量的 1.05× 对得上，超时按「排队 + 3×」给留足了余量。
分析容器整轮峰值 RSS **4760.6 MB**（`docker stats` 每 2 s 采样）；全是缓存命中的那一轮只有 617.7 MB，
因为没起分轨子进程。对照之下检测器那条路便宜到不用缓存：四条成品分别 1920 / 310 / 283 / 1268 ms。

**容器拆分**：整曲 CPU 推理是分钟级、能吃满核，跟主后端同进程会把拼接 DSP 和 Suno 轮询一起拖死，
所以 torch 那一套单独装进 `analyzer` 容器（`Dockerfile.analyzer` + `analyzer_server.py`，
compose 里**刻意不发布宿主机端口**），主后端用 `STRUCTURE_ANALYZER_URL` 走 HTTP，主容器零 torch。
两个容器只读/读写各自的挂载，音频靠共享的 `./backend/static` 传递，所以 HTTP 只传相对路径。
分析服务内部只允许一条流水线（`_inference_lock`），后来的请求排队 `max(AIO_QUEUE_TIMEOUT, 1.5 × 时长)`
（默认下限 900 s）仍拿不到就回 429，由主后端降级成单段兜底；排队预算与调用方的 HTTP 超时是同一个函数，
理由见下面的「超时按音频时长给」。

**墙钟与线程配比**（`backend/scripts/bench_structure_threads.py` 在分析容器内测，8 核 / cpu.max 不限）：
调之前 491.883 s 的成品在分轨阶段跑了 9 分钟以上还没完（833–863% CPU、2.12 GiB RSS），
看着像「长曲必然超时」，实际是**超线程**——分轨子进程和 numpy 的 OpenBLAS 各开各的池，
日志里那串 `OpenBLAS Warning : Detect OpenMP Loop and this application may hang` 就是这件事。
两个开关是**分开的**：`AIO_THREADS → torch.set_num_threads()` 只管父进程那次 DiNAT 推理，
分轨子进程只认 `OMP_NUM_THREADS`。127.887 s 探测混合实测（分轨段，同一条音频）：

| 配比 | 分轨墙钟 | 倍率 |
| --- | --- | --- |
| `OMP=4` + `OPENBLAS/MKL=1` | 129.0 s / **126.6 s**（两轮，第二轮带 `HF_HUB_OFFLINE`） | 1.01× / **0.99×** |
| `OMP=8` + BLAS=1 | 138.0 s | 1.08× |
| `OMP=2` + BLAS=1 | 140.7 s | 1.10× |

8 核机器上 4 是分轨的甜点，给满 8 反而慢 14%。选定配比后整条流水线实测 **134.6 s / 127.887 s
= 1.05× 实时**，拆开看：分轨 126.6 s（0.99×）、谱特征 + DiNAT 推理 + 后处理一共 **8.0 s**。
也就是说「深度学习分段慢」这件事 95% 是 demucs 的账，模型本身几乎不花钱 —— 想再快只能砍分轨，
而 allin1 的 `analyze()` 里没有跳过 `demix()` 的开关。compose 里 analyzer 服务因此钉了
`OMP_NUM_THREADS=${AIO_THREADS}`、`OPENBLAS_NUM_THREADS=1`、`MKL_NUM_THREADS=1`。

**超时按音频时长给**，不是一口价，而且是**两段相加**：
`_analyzer_timeout(D) = 排队预算 + 推理预算`，其中
`排队预算 = max(AIO_QUEUE_TIMEOUT, 1.5 × D)`、`推理预算 = max(STRUCTURE_ANALYZER_TIMEOUT, 3 × D)`。
推理给 3 倍而不是实测的 1.05 倍，余量付的是模型加载、结果卷冷启动和邻居抢核 ——
这几项不随时长缩放；491.883 s 的成品推理预算 1475.6 s，正常跑完用不到 10 分钟。
排队那一半不能省：分析容器一次只跑一条流水线，排在后面的人等的是**前面那条歌**的时长，
和自己的时长无关 —— 只给 `max(180, 3 × D)` 时，127.887 s 的成品排在 587.165 s 的后面，
383.7 s 就报 `分析服务不可达(384s 超时上限): ReadTimeout`，而对端还在合法地跑分轨，白烧一遍。
两段同一个公式由 `structure_analyzer.queue_budget_sec()` 提供，主后端和分析容器共用一份，
不会出现这边改了那边漏改。现算的下限：D=0 → 900 + 180 = 1080 s，D=127.887 → 900 + 383.7 = 1283.7 s，
D=587.165 → 900 + 1761.5 = 2661.5 s，D=1200 → 1800 + 3600 = 5400 s。

排队这一段是**实测复现过**的，不是推演：把 dlp-D 的结果缓存移开制造一次冷跑、用同步端点占住
分析容器，8 s 后用任务端点提交 dlp-B（它只能在锁外等）。同一对成品在旧口径下 dlp-B 于 383.7 s
死成 `ReadTimeout`；新口径下 dlp-D 冷跑 **639.118 s / 587.165 s = 1.088× 实时**，dlp-B 等到
**631.1 s** 收敛为 `status=done`、7 段、`degraded=false`（上限 1283.7 s，余量 652 s）。
任务表里那条的 `analyze_ms = 631103` —— 这个数是**含排队的墙钟**，别当成算力：锁一放开 dlp-B
走的是缓存命中，真正的算力只有几十毫秒（同一时刻复测：dlp-B 20 ms、dlp-D 7 ms）。

**结果缓存绕开 allin1 的加载分支缺陷**：`_analyze_local` 先看 `<work>/results/<stem>.json`，
命中就直接读 JSON 返回段，根本不进 `allin1.analyze()`。这不是为了省时间（虽然确实省：
127.887 s 的成品冷跑 134.6 s，命中缓存只剩 7~20 ms、HTTP 全程约 0.1 s），而是那条加载分支是坏的 ——
1.1.0 里 `overwrite=False` 且结果已存在时 `todo_paths` 为空，`if todo_paths:` 整块被跳过，
块内才赋值的 `demix_paths` / `spec_paths` 因此不存在，收尾的 `keep_byproducts=False` 清理直接抛
`UnboundLocalError: demix_paths`，实测**第一次成功、之后同一首永远失败**。要强制重算就
`STRUCTURE_OVERWRITE=1`：先把旧 JSON 删掉再全量跑，仍然不把 `overwrite=True` 传给 allin1 ——
那样就回到它自己那条与 `keep_byproducts` 互斥的坑里了。

**权重已在卷里也要联网**：allin1 加载 fold 前会向 huggingface.co 发一次 metadata HEAD，
本机代理随机掐 TLS，那一发 HEAD 以 `ssl.SSLEOFError` 打死整条推理（bench 第一轮就死在这里，
demucs 都已经跑完了）。分析容器的 CMD 给服务进程带 `HF_HUB_OFFLINE=1`（`hf_hub_download`
退化为纯本地查找），预下载那一步保持联网，否则冷卷永远拿不到权重。

**依赖与镜像（本机 linux/aarch64 + py3.11 实测）**：

| 事项 | 结论 |
| --- | --- |
| torch 版本 | 钉 `2.1.2`，与 natten 0.14.6 / allin1 训练期同代；用 2.10 的头文件编 0.14 的 C++ 扩展有 ABI 漂移风险 |
| torch 索引 | 官方 CPU 索引的 2.1.2 **没有 aarch64 轮子**（`/whl/cpu/torch` 的 aarch64 计数 = 0），所以用 `--extra-index-url` 而非 `--index-url`：x86_64 取 `2.1.2+cpu`，aarch64 落回 PyPI 的 `manylinux2014_aarch64` 轮子。PyPI 那份在 aarch64 上本来就是 CPU 构建，`nvidia-*` / `triton` 依赖全带 `platform_machine == "x86_64"` 标记，不会被装 |
| torchaudio | demucs 4.0.1 的直接依赖，必须与 torch 同层同版本钉死，否则 pip 会顺手把 torch 顶到最新版 |
| natten | `allin1/models/dinat.py` 无条件 `from natten.functional import natten1dqkrpb / natten1dav / …`，这套 API 在 0.15 之后被删；`0.14.6` 无 CUDA 时自动编 CPU 内核，**实测编译 322.0 s** |
| madmom | PyPI 只有 sdist 且 0.16.1 对 numpy≥1.24 已坏（实测那份 sdist 里有 98 处 `np.float` / `np.int` 这类 1.24 起删除的别名，跟本镜像钉的 numpy 1.26.4 装不到一起）；取 main 分支**固定 commit 的 archive tarball**（仓库默认分支不是 `master`，写 `@master` 时 `git checkout` 直接失败；具体 URL 见 `Dockerfile.analyzer` 层 4）。不能用 `git+https`：它会 `git submodule update`，而 madmom 的 `models` 子模块在本机代理下 TLS 握手被掐（`GnuTLS, handshake failed`，重试两次都失败）。少这个子模块**不影响推理但影响 import**：`from ..models import …` 确实全在方法体内按需执行，allin1 用的 `DBNDownBeatTrackingProcessor` 是纯 Viterbi 后处理、不加载 NN 权重，但 `madmom/__init__.py` 顶层就有一句 `from . import … models …`，而 tarball 里那个目录是 gitlink 占位、pip 装完 site-packages 里连目录都没有 → `import madmom` 直接 `ImportError: cannot import name 'models' from partially initialized module`（报错信息里那句「circular import」是误导）。解法是把 `backend/vendor/madmom/models/__init__.py`（只定义 `path`，1 KB）在层 7 COPY 进去；PyPI 0.16.1 的 sdist 里确实带了这个文件，但那个版本整体用不了。它声明 `numpy>=1.13.4` + `scipy>=1.13`，与本镜像的 pin 正好对上 |
| lameenc | demucs 声明 `lameenc>=1.2`，但 1.2.0 只发布到 cp37m，py3.11/aarch64 必须取 ≥1.4（`1.9.0` 有 manylinux2014_aarch64 轮子） |
| libsndfile | `soundfile` 的「wheel 自带原生库」在 aarch64 上不成立：层 5 实际解析到的是 `soundfile-0.12.1-py2.py3-none-any.whl`（24 kB，纯 Python，不含 `.so`），import 时退回 `ctypes.util.find_library('sndfile')`，slim 基础镜像里没有 → `OSError: cannot load library 'libsndfile.so'`，连带 demucs 落盘全挂。由 `Dockerfile.analyzer` 层 6 `apt-get install libsndfile1` 补上，单独成层放在依赖之后，改它不会让 natten/madmom 的编译层失效 |
| numpy | 全栈钉 `1.26.4`：numba 0.59.1 要求 `>=1.22,<1.27`、scipy 1.13.1 要求 `>=1.22.4,<2.3`，两条上限反过来印证了这个 pin；顶到 2.x 会静默改变 3.2/3.3 的 DSP 数值 |
| 体积 | torch CPU 轮子 146.5 MB、htdemucs 权重 80.2 MB（`dl.fbaipublicfiles.com/hybrid_transformer/955717e8-8726e21a.th`，`check_hash=True`）；装完的分析镜像 **2.49 GB**（同机主后端镜像 424 MB，这就是拆容器的代价） |
| 缓存 | `HF_HOME=/data/cache/huggingface`、`TORCH_HOME=/data/cache/torch`、`STRUCTURE_WORK_DIR=/data/cache/work` 全挂在命名卷 `analyzer_cache` 上，容器重建后同一首曲子仍命中 allin1 的结果 JSON 缓存 |
| 权重初始化 | `python -m utils.structure_weights [--check] [--model M] [--manifest JSON]`：读 allin1 自己的 `NAME_TO_FILE` 决定要下哪些 fold（不在本仓库重复维护一张表），再用 `demucs.pretrained.get_model('htdemucs')` 触发官方权重下载，逐文件回报字节数。单 fold 实测只需 **81.6 MB**（`harmonix-fold0-0vra4ys2.pth` 1.34 MB + htdemucs 80.24 MB），首次下载 **6 s** 量级；CMD 起服务前自动跑一次，失败只打日志不阻塞启动（接口走降级） |
| 构建期自检 | 最后一层 `pip check` + 全部依赖 import + **按 allin1 推理链真跑一遍导入面**：madmom 的四个音频处理器、`DBNDownBeatTrackingProcessor(beats_per_bar=[2,4], fps=100)` 现场构造、`allin1.spectrogram` 与 `allin1.postprocessing.metrical`。只 `import allin1` 是不够的——用到 madmom 的正是后两处；libsndfile 与 models 替身这两个坑都是这一层逼出来的。通过的行形如 `deps-ok torch=2.1.2 numpy=1.26.4 scipy=1.13.1 madmom=0.17.dev0 allin1=1.1.0`，装歪了让构建失败，而不是等运行时降级成一段假数据才察觉 |

---

## 4. 前端状态与交互

`Studio.tsx` 是唯一的状态编排点：

| 状态 | 作用 |
| --- | --- |
| `step` | `"brief"` / `"studio"`，两步页面切换（带进入动画） |
| `structureReady` | 是否已确认过结构，决定第二页是否可用（刷新后回到第一页） |
| `songPrompt` / `lyrics` | 第一页：全局风格提示词与歌词文本 |
| `lyricsLoading` / `lyricsError` | 歌词生成进行中与错误信息 |
| `sections` | 当前时间轴顺序（初始由歌词解析生成，生成后按返回音频自然时长更新；dnd-kit 拖拽后 `arrayMove`） |
| `locked` | `{ [sectionId]: Take }`，已锁定的候选 |
| `takes` / `prompts` | 每个段落的候选列表与提示词草稿 |
| `mix` / `stitching` | 拼接结果与进行中状态 |
| `notice` | 底部 toast，`{ text, level }`；`level` 决定它会不会自动消失（见下方「提示分级」） |

关键行为：

- **两步页面**：第一页 `SongBriefPanel` 收集歌曲提示词与歌词，`briefPreview` 用 `useMemo`
  实时调用与正式构建相同的解析函数，展示将生成的段落、时长与歌词行数；
  点击「进入逐段生成」后重建时间轴并切到第二页，顶部步骤条也可在两页之间切换。
- **歌词生成**：`POST /api/lyrics` 只把结果填入歌词框，需人工确认分段预览后进入第二页，
  不再自动应用；超过 12 段会在预览中提示并截断。
- **返回与结构保护**：第二页「← 创作设定」返回第一页时保留全部候选与锁定。再次确认时，
  若歌词与提示词推导出的段落结构一致（id / 标题 / 提示词 / 歌词；**时长不参与比较**，
  因为它会被返回音频的自然长度改写）则直接返回，不重建；若有改动，会二次确认后清空
  候选 / 锁定 / 成品。重建结构时递增 `epochRef` 并清空 `requestSeq`，在途请求返回后
  因版本不匹配被丢弃并回收 Blob URL，避免旧结构的音频串入新时间轴。
- **单一生成框 + 自动切段**：第二页只有一个生成框，点击时间轴色块切换当前段；
  锁定成功后自动切到下一段，最后一段锁定后提示可以生成成品。想改上一段时点时间轴即可回跳，
  已生成的候选与锁定状态都保留。
- **显式生成**：进入页面不自动请求生成服务；选中结构块后点击「生成候选」才并发请求 3 次，
  避免误耗云端额度。当前段歌词显示在段落标题下方，可边看歌词边调提示词。
- **提示词组装**：请求只带 `prompt = 风格提示 + [Section] 标签 + 本段歌词`，不再上传
  `audio_file`，段落候选之间不传音频上下文（按成品波形点选续写见下方「按点续写」）；
  提示词输入框下方会显示当前发送的结构标签。
- **自然时长**：候选返回后按第一个候选的真实时长更新该段 `durationSec`（已锁定的段以锁定
  音频为准），锁定候选时再次校准；时间轴宽度、总长与区间显示随之变化，音频不截断不拉伸。
- **4 选 1 与落盘**：锁定前若音频是 `blob:`，先 `POST /api/assets` 落盘，成功后再写入 `locked`、
  清除旧成品，并按该候选真实时长校准时间轴；失败则提示并保持未锁定。
- **提示分级**：`notice` 是 `{ text, level }`，分级判据是「谁没做到该做的事」。`info`（用户操作
  还没满足条件、纯反馈）沿用 3.2 s 自动消失，`role="status"`；`error`（我们这边失败了 —— 网络、
  后端、扣费相关的丢失）常驻到用户点浮层里的「关闭」按钮，`role="alert"` + 玫红描边。全站 19 个
  提示站点里 5 个是 `error`：生成候选失败、保存音频失败、拼接失败，以及续写侧的「该段音频缺少
  Suno clip id」与续写异常；其余（未写歌词、续写点太靠段尾、不足 2 段不能拼接之类）仍是 `info`。
  自动消失只挂在 `level === "info"` 的 effect 上，所以 `error` 不会被定时器抹掉。
  实测（零成本：monkeypatch `window.fetch` 让 `/api/generate` 返回真实形状的 502 body，
  另起 100 ms 采样器统计 `.toast-enter` 的存在与 `role`）：502 浮层从出现到点击「关闭」
  存活 **32.5 s**（5.20 s → 37.70 s），关闭后 DOM 立即移除；`info` 的「已返回创作设定…」
  在 0.1 s 首次采样时已在、**3.30 s** 消失，且没有关闭按钮。改这条的直接原因见 2.4 的 413
  事故：钱已经花了，报错却在人读完之前消失，等于把唯一的证据藏起来。
- **播放协调**：`WaveformPlayer` 内部维护模块级 `activeInstance`，任一实例开始播放会暂停其他实例；
  `pauseAllAudio()` 在切换段落 / 重排 / 锁定 / 拼接 / 切换页面时统一调用。播放按钮的 `aria-label`
  在「播放试听 / 暂停试听」之间切换。Blob URL 在被替换时统一 `revokeObjectURL` 回收。
- **重排**：拖拽（鼠标手柄或键盘 Space + 方向键 + Space）后重新计算时间轴区间与总时长，并清空过期成品。
- **拼接**：按时间轴顺序收集已锁定 Take（至少 2 段）调用 `/api/stitch`，`crossfade_ms` 固定 8；
  未全部锁定时提示「将只拼接已锁定段落」，全部锁定后按钮高亮，展示成品波形与 WAV 下载链接。
- **成品播放指针**：成品播放时，结构时间轴底部的色带上同步滑动一根白色指针，指针所在色块同步高亮。
  `WaveformPlayer` 用 `onTimeUpdate` 上报进度——wavesurfer v8 的 `timeupdate` 由 rAF 每帧触发，
  直接透传会让整棵 `Studio` 树每秒重渲染 60 次，因此限流到 50ms（约 20Hz），色块再用
  `transition-[left] duration-75 ease-linear` 补间回平滑视觉。`Studio.tsx` 以 `mixCurrentTime`
  持有该时间，连同后端回传的 `segment_order` / `segment_start_sec` / `duration` 组成 `playhead`
  传给 `Timeline`；`sections.ts` 的 `mixPlayheadPosition()` 先定位落在哪个源片段，再按「该片段在
  成品里的实际占长」与「该色块在色带上的宽度占比」二次换算成百分比——不能直接用
  `Σ durationSec` 当分母，因为拼接会扣掉头部裁切与交叉淡化重叠，成品长度总比色带标称长度短
  几十到几百毫秒。未锁定段落不进成品，指针会直接跨过它们的色块；`playhead` 为空时色带不渲染指针。
- **成品实际占用色带**：规划色带下方再画一条按「成品里真实占用长度」分配宽度的色带，只在有成品时出现，
  段间口径差异从此同屏可读。长度全部由后端已回传的 `segment_start_sec` 反推
  （`mixSegmentSpans()`：相邻起点差 + 一个 8ms 重叠，最后一段占到成品结尾），不需要后端加字段；
  自检恒等式 `Σ lengthSec − 8ms × (n-1) = durationSec` 拿第 7 节那组毫秒真值（起点
  0 / 7.992018 / 185.144036，成品 193.144s）跑通，得 8.000 / 177.160 / 8.000 s、残差 2.8e-11 ms。
  两条色带各带一根指针：上面白色是 `mixPlayheadPosition()` 的比例换算，下面 emerald 是
  `timeSec / durationSec` 的成品真实位置 —— 两根分开正是「规划 ≠ 成品」的读数；每块还有
  `title`（「前奏 · 成品占 8.00s / 规划 192.6s」），标题行给出「成品 23.98s · 规划 208.6s 的 11%」
  与未锁定段数。真实链路实测（`/api/generate` monkeypatch 成已扣费静态 WAV，零扣费；拼接走真后端）：
  无词前奏拿到 192.6s 音频、被 `max_duration_sec=8` 裁到 8.0s，规划总长 208.6s 而成品 23.984s，
  前奏块宽度 436.7px → 157.7px（92.3% → 33.4%，三段各 8.00s 与 `truncate_to_sec=[8, null, null]`
  吻合），播到 3.90s 时两根指针停在 45.06%（规划轴）与 16.26%（成品轴）。
- **按点续写**：`WaveformExtendPlayer.tsx` 承接成品波形与续写控件，落点、歌词与请求中状态都是组件
  内部 state（`Studio.tsx` 用 `key={mix.filename}` 挂载，换新成品即重置）；结果通过 `onTakes` 回写
  段落候选。成品波形点击（wavesurfer `interaction`）记下落点并显示白色标记线，用
  `segment_start_sec` 反查「哪一段 · 段内第几秒」（加上该段的 `head_trim_sec` 才回到源片段自身时间轴）；
  在续写歌词框写词后点「从此处续写」，以该段的 `clipId` + 段内偏移、`max_clips=2` 调用
  `/api/generate`，主片段与 `X-Suno-Extra-Clips` 里的残值片段一并作为新候选追加到该段下方
  （「候选项 04 / 05」，都是待选择，已锁定的那条不动），试听后再决定是否锁定。
  落点离段首/段尾过近（< 0.2s / 剩余 < 0.5s）或该段缺少片段 ID 时按钮禁用并说明原因。
- **播放头吸附**：`WaveformExtendPlayer` 挂载后对成品调一次 `POST /api/structure/boundaries`
  （491.883 s 的成品服务端 1068 ms 返回，见 2.6），候选以 `StructureCandidate[]` 透传给 `WaveformPlayer`。
  请求与波形加载并发，所以候选存放在 `snapPointsRef / snapWindowRef / onSnapRef` 里由 effect 同步，
  **不进** wavesurfer 实例的 `useEffect` 依赖 —— 否则候选晚到几毫秒会把播放器重建一遍。
  `status === "ready"` 后波形区叠加一组白色刻度线（`pointer-events-none`，不拦点击），
  透明度 `0.18 + min(0.42, strength)`，勉强过线的弱候选淡、强边界亮。
  wavesurfer v8 的 `interaction` 在按住拖拽时**逐帧**触发，因此落点用 `SNAP_SETTLE_MS = 90` 的
  debounce 等指针静止后判定一次，命中就 `instance.seekTo(sec / duration)` 把播放头挪到候选点，
  再把这个（不是鼠标那个）时间上报，「第 N 段 · 段内第 X 秒」显示的即是吸附后的落点，
  下面补一行「已吸附 176.27s → 176.37s」让人确认到底落在哪。
  吸附窗口 `SNAP_WINDOW_SEC = 0.5` 是**误吸率与可点性**的折中，与检测精度无关（候选自身 MAE 只有 0.062 s）：
  窗口决定一根刻度在波形上占几个像素，而波形是把整条时间轴压扁进容器宽度的。
  浏览器实测波形容器 477 px：

  | 成品时长 | s/px | 一根刻度的可点宽度 | 瞄准接缝所在像素及其左右一格 |
  | --- | --- | --- | --- |
  | 75.057 s | 0.157 | 6.4 px | 3/3 吸上 |
  | 235.395 s | 0.494 | 2.0 px | 4/6 |
  | 281.771 s | 0.591 | 1.7 px | 3/6 |
  | 491.883 s | 1.031 | 0.97 px | 2/6 |

  全轴被任一候选覆盖的比例：±0.5 s → **6.0%**（其中假候选 5.4%）、±1.0 s → 11.1%（假 10.0%），
  真接缝只占 1.3% —— 收到 0.5 s 把「随手一点被假吸附」的概率砍了近一半。
  代价在长成品上很直接：刻度只占约 1 px，相邻像素一个吸一个不吸。逐格实测（535.557 s 成品）：
  px157 落点 176.273 吸到 176.373，但 px156（175.151，差 1.22 s）与 px158（177.396，差 1.02 s）都不吸；
  px179（200.974）吸到假候选 201.416，px180（202.097，差 0.681 s）不吸 —— 这一格在 ±1.0 s 下本来会吸。
  同一次实测里第三条真接缝 392.84 怎么点都不吸，因为**检测本身漏了**（最近候选 390.531 差 1.31 s）：
  标定集只有 4 组混合 7 条接缝，换素材的召回不保证。
  窗口曾按 MAE 写成 0.25 s，那是彻底错的（亚像素目标点不中，点 176.053 s 吸不到 176.373）；
  0.5 s 是「少误吸」这一端，1.0 s 是「保证吸得上」那一端（瞄准 ±1 像素 19/21 vs 12/21）。
  长成品点不中不该靠继续放大这个常数来救 —— 常数一放大，误吸覆盖跟着涨；
  要救的是像素本身，见下条**波形横向缩放**。
  只吸附检测候选，**不**吸附 clip 级段边界：段边界在结构时间轴上就是色块分界线，已经看得见，
  而且吸到段起点会让续写偏移变成「该段第 0 秒」，正好撞上 `continue_at` 的 0.2 s 下界把按钮锁死。
  分析失败只在 console 报错，退化成「没有吸附」，试听与续写不受影响；面板显示「结构候选 0 个」。
- **模型分段做加法，不做减法**：`WaveformExtendPlayer` 挂载后除了检测候选，还会
  `POST /api/structure/jobs` 提交一次异步分析（2.8）并按 3 s 轮询 `GET /api/structure/jobs/{job_id}`；
  GET 回 404（`--reload` 清空了进程内任务表）只重提一次，反复 404 就把状态落到 `failed` ——
  不然轮询会跟着一个已经不存在的任务永远转下去；整曲推理是分钟级的，
  状态行必须自己报进度，否则界面看着像卡死：「模型分段 · 排队中（分析容器一次只跑一首，已等 Ns）」
  →「分析中（已用 Ns，含在服务内排队；分轨占约 1 倍音频时长）」→「模型分段 · 7 段 · 2:14 · 复用已有结果」，
  降级与失败各自带上 `detail`。`queued` 只表示主后端的任务还没起线程；线程起了就转 `running`，
  而分析容器内部那条 `_inference_lock` 的排队对调用方不可见，所以文案写「含在服务内排队」而不是谎报。
  模型段边界是**并进**吸附目标而非替换：`sections.ts` 的 `mergeSnapPoints(candidates, segments)`
  取「检测候选 ∪ `segments[1:].start`」，距任一检测候选 ≤ 0.05 s（`MERGE_DEDUPE_SEC`）的边界丢掉，
  剩下的标 `source: "model"` 后按时间排序。`WaveformPlayer` 的刻度线按来源分色 —— 检测候选是白色
  透明度分级，模型边界是天蓝实线 `rgba(125,211,252,.72)`，一眼能看出这根刻度是哪种证据给的；
  吸附本身不分来源，`SNAP_WINDOW_SEC = 0.5` 一个窗口吃两种刻度。模型没回来时就是原来的全量候选，
  分析中不会让吸附能力变弱。
  为什么不走「用模型边界收窄检测候选」这条更省事的路线：收窄在 dlp-B（127.887 s / 3 段，真值接缝
  31.375 与 92.567）上是**净损失**。模型给的第二个边界 96.99 s 相对毫秒真值 92.567 s 偏了 **4.42 s**，
  把候选限制在段边界 ±0.5 / ±1 / ±2 s 内，只剩 4 / 5 / 7 个候选，两条真接缝的「±0.5 s 内还有没有候选」
  存活率 **0/2**（±5 s 才回到 1/2，但那时 16 个候选已砍到 13 个，等于没收窄）。
  改成并集后：模型的 6 个段边界里 5 个是检测器没有的（114.03 与候选 114.004 相距 0.026 s，被去重吃掉），
  刻度数 16 → **21**，±0.5 s 可吸接缝 **1/2 → 2/2** —— 31.375 这条检测器最近候选差 2.53 s（吸不上），
  模型给的 31.50 只差 **0.125 s**。9 条接缝全集上的对照见 3.4。
  浏览器 DOM 实测过这套并集渲染：波形里有 21 根 `w-px` 刻度，其中 5 根取到模型色
  `rgba(125,211,252,.72)`，第一根 `left=11.7447%` 换算回来正是 15.02 s；在 15.26 s 处点一下，
  状态行报「已吸附 15.26s → 15.02s」，白色续写标记落在同一根蓝刻度上 —— 15.02 附近检测器一个候选都没有，
  这条吸附能力是模型给的。默认档 461 px / 127.887 s = 3.6 px/s，±0.5 s 窗口只有 1.8 px，
  要看清刻度得切 ×4 / ×8（见下条缩放）。
  段边界另外画成一条按时长比例分宽的语义色带（`SEGMENT_LABEL_CN` 译名 + `title="前奏 00:00–00:15"`，
  下面一行列出各边界时刻），只做「这段是什么」的判断，**不参与**任何时间换算 —— 模型的定位精度
  （±0.1~0.5 s，长边界可到数秒）撑不起裁切真值，真值仍然只有 `segment_start_sec` 那一份。
- **波形横向缩放**：`WaveformPlayer` 的 `zoomable`（只有成品播放器开，段落候选条不需要）在播放键右侧加一排
  `适配 / ×2 / ×4 / ×8`。档位是**可视宽度的倍数**而不是固定 px/s，所以浏览器窗口变窄时缩放比例不变；
  可视宽度由 `ResizeObserver` 量得（`observe` 时立刻回调一次，省掉一次 effect 内的同步 setState）。
  缩放时给波形套一层 `overflow-x-auto` 视口，内层内容 div 给
  `width = ceil(可视宽度 × 档位 / 4) × 4` px —— 4 就是 `barWidth + barGap`，wavesurfer 会把画布宽度
  向下取整到这个条形网格，不对齐就让右端差出几像素、刻度线和播放头错位。
  这样 wavesurfer 读到的 `parentWidth` 正好等于内容宽度，`isScrollable` 为假、`fillParent` 生效，
  画布铺满内容宽度，**不需要调 `zoom(minPxPerSec)`**：它内部对容器挂着 100 ms 防抖的 ResizeObserver，
  改内联宽度就触发 `reRender`。刻度线与白色标记线仍按百分比定位在内容 div 上，跟着一起滚，不用换算坐标。
  播放头跟走是自己实现的：wavesurfer 的 `scrollIntoView` 只在它自己认为画布可滚动时才跑，
  这里它认为不可滚动，所以 `timeupdate` 里用 `player.getWidth()` 换算指针像素，越过视口右沿就把
  `scrollLeft` 设成 `指针 − 0.8 × 视口宽`（左沿同理用 0.2，吸附把落点挪出屏幕时也会拉回来）。
  浏览器实测 491.883 s 成品（可视宽度 477 px）：适配 1.031 s/px（±0.5 s 窗 ≈ 1.0 px）→
  ×4 内容 1908 px、0.258 s/px、窗 ≈ 3.9 px → ×8 内容 3816 px、0.129 s/px、窗 ≈ 7.8 px，
  面板上直接把这笔账写出来（「1px = 0.258s · ±0.5s 吸附窗 ≈ 3.9px · 波形可横向滚动」）。
  ×4 下瞄准 176.70 s → 量化落点 176.59 → 吸到候选 176.373；×8 下瞄准 349.55 s → 349.45 → 吸到 349.129，
  此时 React 标记线与 wavesurfer 播放头只差 0.01 px，刻度线的像素位置与接口返回的候选秒数逐条吻合。
  跟走也实测过：从 176.37 s 起播、视口右沿压在 177.9 s，播到 193.6 s 时 `scrollLeft` 已从 213 自动挪到 302，
  指针稳定停在视口 449/477 px 处；退回「适配」后内容宽度回到 477 px、`scrollLeft` 归零，刻度与指针依旧对齐。
  局限：`dragToSeek` 按住往右拖时 wavesurfer 的贴边自动卷动不生效（它那边判定为不可滚动），
  拖出屏幕外要靠滚动条或触控板横滚再重新落点。

---

## 5. 运行方式

### 5.1 Docker 容器化开发（推荐）

前置：Docker Desktop（含 Compose v2）。启动前先停掉本机直接运行的 3000 / 8000 端口服务。

在根目录创建 `.env`（不要提交到版本库），填入真实 Suno 代理与歌词 LLM：

```bash
SUNO_API_BASE=https://your-suno-proxy.example.com
SUNO_API_KEY=sk-your-key
# 歌词 LLM（OpenAI 兼容）：LLM_API_KEY 不填时复用 SUNO_API_KEY
LLM_API_BASE=https://your-gateway.example.com/v1
LLM_MODEL=gpt-4o-mini
# 可选：SUNO_MODEL / SUNO_POLL_INTERVAL / SUNO_TIMEOUT / LLM_API_KEY
# 可选：深度学习结构分析（不填也能跑，接口自动降级，见 3.4）
AIO_MODEL=harmonix-fold0         # allin1 模型：单 fold，或 harmonix-all（8 fold 集成，慢约 8 倍）
AIO_QUEUE_TIMEOUT=900            # 分析容器的排队预算下限；compose 同时注入两个容器（口径要一致）
STRUCTURE_ANALYZER_TIMEOUT=180   # 推理预算的下限
STRUCTURE_ANALYZER_SEC_PER_SEC=3 # 推理预算的倍率：总上限 = max(900, 1.5×时长) + max(180, 3×时长)
```

```bash
docker compose up --build        # 首次构建镜像并启动（ffmpeg 由 imageio-ffmpeg 提供）
docker compose logs -f backend   # 查看后端日志（写词 / 提交 / 轮询 / 拼接）
docker compose down              # 停止并移除容器
```

启动后：

| 服务 | 容器 | 地址 |
| --- | --- | --- |
| 前端（Next dev / Turbopack） | `museblocks-frontend` | http://127.0.0.1:3000 |
| 后端（uvicorn --reload） | `museblocks-backend` | http://127.0.0.1:8000 |
| 结构分析（allin1 / uvicorn） | `museblocks-analyzer` | 仅 compose 内网 `analyzer:8100`，**不发布宿主机端口** |
| Suno API（后端代理） | 不在容器内 | FastAPI → Suno，密钥只放后端环境 |
| 歌词 LLM（后端代理） | 不在容器内 | FastAPI → `/chat/completions`，密钥只放后端环境 |

容器编排要点（见 `docker-compose.yml`）：

- **源码热更新**：`./backend`、`./frontend` 以 bind mount 挂进容器；uvicorn `--reload` 监听 `/app` 下的
  `.py` 变化，Next dev 负责前端 HMR。
- **依赖隔离**：`frontend_node_modules`、`frontend_next` 命名卷挂载到 `/app/node_modules`、`/app/.next`，
  防止宿主机目录覆盖镜像内依赖，也避免 macOS 上 bind mount 的构建缓存性能问题。
- **结构分析容器**：`analyzer` 与主后端共用 `./backend` 代码但镜像独立（`Dockerfile.analyzer`），
  torch / demucs / madmom / natten 全留在那里，主容器保持 slim —— 否则每次改依赖都要付一次
  natten 源码编译（实测 322.0 s）的账。它以**只读**方式挂载 `./backend`，权重与结果缓存写在命名卷
  `analyzer_cache`；启动命令先跑 `python -m utils.structure_weights` 预下载权重，失败也只打印不回绝，
  接口随即走降级分支。`STRUCTURE_ANALYZER_URL` 留空即完全关闭这条链路（此时主容器需要自己装
  `requirements-ml.txt`，否则走同样的降级）。
- **健康检查**：后端用 Python `urllib` 请求 `/api/health`；前端用 Node `fetch` 探测 `/`
  （slim 镜像不含 curl/wget）。前端通过 `depends_on: service_healthy` 等后端就绪后再启动。
- **音频产物留存**：Suno 返回的 WAV、落盘候选与拼接成品写入挂载目录
  `backend/static/generated|output`，重建容器不会丢失。
- **Suno 代理**：浏览器只请求后端 `/api/generate`，Suno 的提交、轮询、下载与转码都在
  `backend/api/generate.py` 内完成；容器无需暴露模型端口，前端也不接触密钥。
  未配置 `SUNO_API_KEY` 时接口返回 500，前端会显示错误面板与 Toast。
- **LAN 访问**：浏览器若通过局域网 IP 打开前端，需要在根目录创建 `.env`：

  ```bash
  NEXT_PUBLIC_API_BASE=http://192.168.0.150:8000
  ALLOWED_DEV_ORIGINS=192.168.0.150
  ```

  然后 `docker compose up --build`。`NEXT_PUBLIC_API_BASE` 在客户端 bundle 编译期注入，
  若旧值仍生效，清一次构建缓存：`docker compose down -v && docker compose up --build`。
  `ALLOWED_DEV_ORIGINS` 会合并进 `allowedDevOrigins`，避免 Next 16 拦截 HMR / 字体资源。
  CORS 当前全开，仅适用于本地开发。

常见操作：

```bash
# 依赖变更（package.json / requirements.txt）后重建镜像与依赖卷
docker compose down -v && docker compose up --build

# 只重建并重启某个服务
docker compose up -d --build backend

# 清理所有容器、网络和（前端依赖/缓存）卷
docker compose down -v
```

- 后端依赖 `imageio-ffmpeg`（pip wheel 自带 ffmpeg），用于把 Suno 返回的 mp3/m4a 转成 WAV；
  如需指定系统 ffmpeg，可设置 `FFMPEG_BINARY`。
- 单段生成是同步长请求：提交后后端持续轮询，通常需要 1–3 分钟；前端 4 个候选来自 **2 个并发上游
  任务**（每任务产双生），并发数比早期 3 个任务时更低。
- 若 macOS 上文件改动未触发容器内热更新（偶见于部分 Docker Desktop 文件共享实现），
  执行 `docker compose restart backend frontend` 即可。

### 5.2 本地直跑（不使用 Docker）

后端：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SUNO_API_BASE=https://your-suno-proxy.example.com
export SUNO_API_KEY=sk-your-key
export LLM_API_BASE=https://your-gateway.example.com/v1
export LLM_MODEL=gpt-4o-mini
uvicorn main:app --host 127.0.0.1 --port 8000
```

本地直跑的依赖已包含 `imageio-ffmpeg`，转码不需要额外安装系统 ffmpeg；
如需使用系统二进制可设置 `FFMPEG_BINARY=/path/to/ffmpeg`。

前端：

```bash
cd frontend
npm install
npm run dev        # http://127.0.0.1:3000
```

- 后端地址通过 `NEXT_PUBLIC_API_BASE` 覆盖，默认 `http://127.0.0.1:8000`；
  生成同样走该地址的 `/api/generate`，前端没有单独的模型地址配置。
- `next.config.ts` 已配置 `allowedDevOrigins: ["127.0.0.1", "localhost"]`，避免 Next 16 拦截开发资源。
- 如果改动了 `.env*` 或 `next.config.ts`，需要重启 Next dev 进程。

---

## 6. 后续演进

1. **整曲一次性生成**：当前按段独立请求 Suno，段间音色、速度与混响可能漂移；可增加
   「整首歌词一次生成 + 按标签对齐切分」模式，或把前一段音频与段落标签一起交给后端做参考，
   在保留模块化编辑的同时提升连贯性。
2. **歌词可控性**：为 `/api/lyrics` 增加押韵、字数、人称、中英混排等参数，并用结构化输出
   （JSON Schema 或 function calling）替代文本后校验，失败时自动重试或多候选择优。
3. **进度推送与缓存**：Suno 单次生成耗时较长，当前是同步长请求；可把轮询结果通过 SSE /
   WebSocket 推给前端显示进度，并以 `sha256(prompt | tags | model)` 缓存结果，避免重复计费。
4. **拼接质量**：即使采用 8ms 微淡入淡出，段落间仍可能有响度/节奏差异；可做响度归一化、
   BPM 估计与节拍对齐，再处理接缝。
5. **数据持久化**：`locked`、歌词与歌曲提示词仅存于内存，刷新后丢失；可加会话表记录
   创作设定、时间轴与锁定音频。
6. **时长模型**：初始时长仍按「每句 4s」估算，可结合语速、BPM 与段落角色做更精细的预览。
7. **结构检测升级**：3.3 的谱形状新颖度只够当吸附候选（精确率 10.8%，且对 floor 敏感）。
   要让它参与自动分段，需要换成训练过的边界检测模型，而这类模型基本依赖 torch ——
   当前镜像刻意只装 numpy，引入推理运行时（体积、启动耗时、CPU 上的单遍延迟）是主要代价。

---

## 7. 已知限制

- 歌词默认由真实 LLM 生成（`gpt-4o-mini`，可通过 `LLM_MODEL` 更换）：输出非确定，
  同一 `seed` 不保证复现；未配置 `LLM_API_BASE` / `LLM_API_KEY` 时回退本地模板，
  主题与句式集合有限。歌词请求为同步调用，通常数秒，读取超时 90s。
- 歌词会作为 prompt 的一部分送入 Suno 参与演唱，但每段独立生成，段间音色、速度与编曲
  一致性由模型自行决定，可能出现衔接风格漂移。
- 段落时长按「每句 4s + 4s 余量」启发式估算，用于第一页预览、首次生成占位，以及**无歌词段落的
  拼接裁切上限**（`max_duration_sec`，见 §2.5）；有歌词的段落不裁，实际时长以返回音频为准，
  无歌词的前奏/尾奏初始 8s 即裁到 8s（切点可因吸附多留 ≤0.5s）。
- 每段时长由 Suno 按歌词决定，时间轴随返回音频展开；成品总长为
  `Σ 各段（裁切后）时长 − 8ms × 接缝数`。**含无词段落时成品会明显短于时间轴总长**：
  时间轴那块按自然长度展开成 182.6s 的前奏，进成品只占 8s。宽度对不上不再是隐性坑 —— 规划色带
  下方有第二条「成品实际占用」色带同屏摊开（见第 4 节），但它只解决「看得见」，不改口径：
  规划轴仍是「这段打算写多长」，成品轴才是听得到的长度，两者不做映射也不该做。
  实测三段（182.6 / 177.2 / 192.6 s，时间轴总长 552.4s）全锁 Take A 走完整链路：
  `truncate_to_sec=[8.0, null, 8.0]`、`segment_start_ms=[0, 7992.018, 185144.036]`、
  成品 193.144s。三段实际占用 8.000 / 177.160 / 8.000 s（中间段由 `185.144036 − 7.992018 + 8ms`
  反推），恒等式 `Σ 保留时长 − 8ms × 接缝数 = 193.160 − 0.016 = 193.144` 成立，即时间轴的 35%。
- 歌词结构最多 12 段，超出截断；更新结构会清空候选、锁定与成品（存在内容时二次确认）。
- Suno 由后端代理调用，前端不直连模型；生成耗时、并发与配额由后端与 Suno 账户决定。
- 结构边界检测（3.3）**只做吸附辅助，不做自动分段**：召回 7/7 但精确率 10.8%，
  每 15 s 约 0.80 个假候选，单靠它无法判定「哪里换段」；给出的是 ±0.1 s 量级的估计，
  毫秒真值只存在于 `/api/stitch` 回传的 `segment_start_sec` 里，两者不能互换。
  标定样本是 4 组本地重拼混合（1084.106 s / 7 条接缝），素材越少接缝越弱时门槛需要重标。
  吸附本身还受波形横向分辨率限制：492 s 成品在适配宽度下刻度只约 1 px，要靠 4 节的横向缩放才点得准。
- 深度学习结构分析（3.4）给的是**语义段**不是**裁切真值**：模型在 100 FPS（0.01 s）的帧网格上工作，
  毫秒字段是格式化精度，边界定位误差在 ±0.1~0.5 s 量级，仍不能替代 `segment_start_sec`。
  标签词表固定来自 Harmonix（intro/outro/break/bridge/inst/solo/verse/chorus），与本项目歌词的
  `[Verse]/[Chorus]/[Bridge]` 段落名不是一一映射，未知标签统一落成 `section`。
  推理是分钟级 CPU 活儿且分析容器一次只跑一条流水线（排队 120 s 拿不到就 429 → 主后端降级），
  所以它不进生成主链路，只在成品就绪后按需调用；权重首次下载需要联网（HF + fbaipublicfiles）。
- `/api/generate` 是同步长请求：提交后在后端轮询到完成才返回，期间浏览器连接一直挂起；
  前端目前只显示「生成中」，没有中间进度。
- 一次任务的两条双生片段都会取回并各挂一个候选（`max_clips=2`，机制见 §2.3）；同一段的 4 个候选
  背后是 2 个独立任务，同一任务的两条片段共享 prompt，因此相关性高于跨任务的两条。
- 未配置 `SUNO_API_KEY` 时接口返回 500，页面会提示检查后端环境变量。
- 生成的音频以 Blob 存在内存中，锁定后才落盘；页面刷新会丢失未锁定的候选与歌词设定。
- 两步流程（`step`）是客户端状态，不写入 URL：刷新后会回到第一页，需重新确认结构；
  已落盘的锁定 Take 地址也会随之丢失，当前不支持跨会话恢复。
- 音频统一重采样到 44.1 kHz，长素材会带来内存开销（当前上限 60 MB）。
- 拼接是 8ms 微淡入淡出的时域硬切，不做节拍对齐 / BPM 匹配 / 响度归一化；
  若两段响度差异明显，接缝仍可能可闻。
- 对外只返回 WAV；上游 mp3/m4a 的转码依赖 `imageio-ffmpeg` 自带的 ffmpeg（pip 安装即可）。
- 当前 Dockerfile 面向开发（`next dev` + `uvicorn --reload`），未提供生产镜像；上线前需改用
  `next build` + standalone 输出与多 worker 运行。

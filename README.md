# MuseBlocks — 基于歌词分段的 Web 端模块化 AI 音乐生成系统

输入歌曲提示词与歌词，系统按歌词标签自动划分前奏 / 主歌 / 预副歌 / 副歌 / 桥段 / 尾奏。
创作分为两步：第一页完成歌词与风格设定并实时预览分段，确认后进入第二页逐段生成；
每段 3 选 1 锁定后，生成框自动切到下一段（可点时间轴回跳），最后由后端把各段按
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
│    ├─ TakeCard.tsx       3 个候选卡片（试听 + 锁定，锁定后自动切下一段）      │
│    ├─ WaveformExtendPlayer.tsx 成品波形取点与 continue_at 续写提交            │
│    └─ WaveformPlayer.tsx wavesurfer.js 波形；模块级单实例播放协调              │
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
└───────────────┬────────────────────────────────────────────────────┘
                │ HTTPS / SDK
┌───────────────▼────────────────────────────────────────────────────┐
│ Suno API                                                           │
│ 按歌词自动决定演唱时长，返回完整 audio/wav                           │
└────────────────────────────────────────────────────────────────────┘
```

开发期两个服务分别运行在独立容器中；浏览器只访问 `127.0.0.1:8000`（CORS 全开），
Suno 由后端代理调用，浏览器不再直连模型服务，也无需处理模型侧 CORS。容器编排见第 5 节。

### 目录结构

```
.
├── README.md                     # 本文件（全局架构 / Repo Wiki）
├── docker-compose.yml            # 开发容器编排：frontend + backend
├── backend/
│   ├── Dockerfile                # python:3.11-slim；ffmpeg 由 imageio-ffmpeg 提供
│   ├── .dockerignore
│   ├── main.py                   # FastAPI 入口、CORS、静态目录
│   ├── requirements.txt
│   ├── api/
│   │   ├── assets.py             # POST /api/assets（候选 WAV 落盘）
│   │   ├── generate.py           # POST /api/generate（Suno 提交 + 轮询 + 转 WAV）
│   │   ├── lyrics.py             # POST /api/lyrics（LLM 写词 + 本地模板兜底）
│   │   └── stitch.py             # POST /api/stitch
│   ├── utils/
│   │   └── audio_processor.py    # 采样统一、等功率交叉淡化、防爆音、峰值限制、WAV 写出
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
- **初始时长启发式**（仅用于第一页预览与首次生成占位）：`4s × 歌词行数 + 4s`，
  主歌/副歌上限 60s、预副歌/桥段 40s、前奏/尾奏 30s，下限 8s；无歌词的前奏/尾奏固定 8s。
  实际时长以 Suno 返回音频为准，生成后时间轴自动改为自然长度。
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
- **3 选 1**：每段并发 3 次请求得到 3 个候选；任一失败则整体失败并回收已生成的 Blob URL。
  页面不会自动生成，需点击「生成候选」，避免误耗云端额度。
  批量候选保持 `max_clips=1`（每个任务只留第一条），以免同一任务的两条片段把「3 选 1」撑成 6 选 1；
  只有续写路径请求双生片段。

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
   `{ prompt, tags, make_instrumental, wait_audio: false }`（配置 `SUNO_MODEL` 时追加 `model`）；
   携带续写参数时再追加 `continue_clip_id` 与 `continue_at`（秒，保留 3 位小数）。
   `continue_clip_id` 只允许 `[A-Za-z0-9_-]{1,64}`，否则返回 422。
   - **建单前的退避重试**（`_post_submit`）：一段 3 个候选是并发提交的，上游网关忙时接入队列会排队，
     TCP/TLS 握手在 `connect=10s` 内完不成抛 `ConnectTimeout`（实测 3 并发里 1 条进、2 条在 10.01s /
     10.016s 整点失败）。这类请求的字节还没离开本机，重试不会重复建单扣费，因此按 1s、3s 退避最多再试
     2 次（最坏 3 次尝试 / 约 34s）；`ReadTimeout`、`WriteTimeout` 等「可能已经送达上游」的失败**一律不重试**，
     直接 502。异常类名与耗时都写进日志和 `detail`——httpx 的超时异常 `str()` 是空串，
     此前 502 文案只剩一个光秃秃的冒号，看不出卡在哪个阶段。
3. **轮询**：每 `SUNO_POLL_INTERVAL` 秒（默认 5）请求 `GET {SUNO_API_BASE}/suno/fetch/{task_id}`，
   直到 `status` 为 `COMPLETE` / `SUCCESS` 并拿到落定片段的 `audio_url`；`FAILED` / `ERROR` 立即失败，
   超过 `SUNO_TIMEOUT`（默认 300s）返回 504。解析函数 `_fetch_state` 返回
   `(status, 片段列表, 失败信息)`，片段列表按上游顺序保留**全部**已产出成品地址的片段，
   调用方按 `max_clips` 截取，不再只读 `data[0]`。
   - **轮询容错**（`_poll_once`）：单次查询按 1s / 2s / 4s 退避、最多 4 次尝试，覆盖两类瞬时故障——
     传输层异常（连接、读写、协议）和上游 `429 / 5xx`。轮询是幂等 GET，不建单也不扣费，所以重试
     没有重复计费风险；而任务此时**已经建单扣费**，一次网络抖动就作废整条任务得不偿失。
     不可重试的 4xx（401 / 404）直接失败，重试也救不回来。所有失败文案（含 5xx、上游 `FAILED`、
     完成却无音频地址、504 超时）都带上 `task_id`，前端报错时能直接拿去人工复查。
     一次轮询内部的重试最多消耗约 47s（4 次连接超时 + 7s 退避），仍受 `SUNO_TIMEOUT` 总预算约束。
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
| `SUNO_MODEL` | 否 | — | 需要指定模型版本时追加到提交 Payload |
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

- 请求体为原始 WAV 字节（非 multipart），上限 32 MB，仅接受 `RIFF/WAVE`。
- 落盘目录 `backend/static/generated/`，文件名由时间戳 + 随机串生成。
- 前端在「锁定」时上传；失败则不锁定并提示，避免拼接时引用失效地址。
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
- `head_trim_sec` 给出每段拼接时被物理剔除的头部静音（首段恒为 0）。段内偏移要换算回源片段
  自己的时间轴（续写用的 `continue_at` 指的是原始 clip），必须把这段垫片加回去，
  因此 `locateMixSegment()` 计算 `全曲时间 − 段起点 + 该段 head_trim`。
- `tail_trim_sec` 给出每段被物理剔除的尾部静音，仅用于审计：尾部裁切只缩短**自己这段的结尾**，
  不改自己这段的起点，所以段内偏移公式不需要它；下一段的起点会随之提前，
  而那个值由服务端在 `segment_start_sec` 里直接给出，前端不需要二次推算。
- 错误映射：非法音频源 400、文件不存在 404、其他 500、`output_name` 非法 422。

### 2.6 其他端点

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
   注意别把这套 −50 dBFS 判据当成结构检测的门控用到音频分析上：接缝 ±2 s 内有 40.6–66.6% 的帧
   会因为「上一段留了尾静音」而低于 −40 dBFS，那时它会把真边界一起杀掉。
4. **防爆音**：
   - 去直流偏置（`mixed -= mixed.mean(axis=0)`）；
   - 成品首尾 12 ms 余弦淡入/淡出（`_apply_edge_fade`）；
   - 峰值限制到 −1 dBFS，**仅做衰减不做放大**（`_limit_peak`），避免削波同时不引入额外失真。
5. **写出**：pydub `from_wav` → Python `wave` 模块直接写 WAV，因此在没有 ffmpeg 的环境也能工作。
6. **多段顺序拼接**（`stitch_sequence`）：用 `pending_crossfade` 把「上一段的交叉淡化时长」应用到
   当前接缝，保证语义与请求中的 `crossfade_ms` 一致；每个接缝打印
   `prev_end / overlap / segment_start / next_len / head_trim` 的毫秒级审计日志。

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
| `notice` | 底部 toast |

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
- **3 选 1 与落盘**：锁定前若音频是 `blob:`，先 `POST /api/assets` 落盘，成功后再写入 `locked`、
  清除旧成品，并按该候选真实时长校准时间轴；失败则提示并保持未锁定。
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
- **按点续写**：`WaveformExtendPlayer.tsx` 承接成品波形与续写控件，落点、歌词与请求中状态都是组件
  内部 state（`Studio.tsx` 用 `key={mix.filename}` 挂载，换新成品即重置）；结果通过 `onTakes` 回写
  段落候选。成品波形点击（wavesurfer `interaction`）记下落点并显示白色标记线，用
  `segment_start_sec` 反查「哪一段 · 段内第几秒」（加上该段的 `head_trim_sec` 才回到源片段自身时间轴）；
  在续写歌词框写词后点「从此处续写」，以该段的 `clipId` + 段内偏移、`max_clips=2` 调用
  `/api/generate`，主片段与 `X-Suno-Extra-Clips` 里的残值片段一并作为新候选追加到该段下方
  （「候选项 04 / 05」，都是待选择，已锁定的那条不动），试听后再决定是否锁定。
  落点离段首/段尾过近（< 0.2s / 剩余 < 0.5s）或该段缺少片段 ID 时按钮禁用并说明原因。

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
| Suno API（后端代理） | 不在容器内 | FastAPI → Suno，密钥只放后端环境 |
| 歌词 LLM（后端代理） | 不在容器内 | FastAPI → `/chat/completions`，密钥只放后端环境 |

容器编排要点（见 `docker-compose.yml`）：

- **源码热更新**：`./backend`、`./frontend` 以 bind mount 挂进容器；uvicorn `--reload` 监听 `/app` 下的
  `.py` 变化，Next dev 负责前端 HMR。
- **依赖隔离**：`frontend_node_modules`、`frontend_next` 命名卷挂载到 `/app/node_modules`、`/app/.next`，
  防止宿主机目录覆盖镜像内依赖，也避免 macOS 上 bind mount 的构建缓存性能问题。
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
- 单段生成是同步长请求：提交后后端持续轮询，通常需要 1–3 分钟；前端 3 个候选会并发
  对应 3 个上游任务。
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

---

## 7. 已知限制

- 歌词默认由真实 LLM 生成（`gpt-4o-mini`，可通过 `LLM_MODEL` 更换）：输出非确定，
  同一 `seed` 不保证复现；未配置 `LLM_API_BASE` / `LLM_API_KEY` 时回退本地模板，
  主题与句式集合有限。歌词请求为同步调用，通常数秒，读取超时 90s。
- 歌词会作为 prompt 的一部分送入 Suno 参与演唱，但每段独立生成，段间音色、速度与编曲
  一致性由模型自行决定，可能出现衔接风格漂移。
- 段落时长按「每句 4s + 4s 余量」启发式估算，仅用于第一页预览与首次生成占位；
  实际时长以返回音频为准，无歌词的前奏/尾奏初始为 8s。
- 歌词结构最多 12 段，超出截断；更新结构会清空候选、锁定与成品（存在内容时二次确认）。
- 每段时长由 Suno 按歌词决定，时间轴随返回音频展开；成品总长为
  `Σ 各段自然时长 − 8ms × 接缝数`，基本等于时间轴总长。
- Suno 由后端代理调用，前端不直连模型；生成耗时、并发与配额由后端与 Suno 账户决定。
- `/api/generate` 是同步长请求：提交后在后端轮询到完成才返回，期间浏览器连接一直挂起；
  前端目前只显示「生成中」，没有中间进度。
- 上游一次返回多个 clip 时只取第一个可下载音频；同一个 prompt 的 3 个候选是 3 个独立任务。
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

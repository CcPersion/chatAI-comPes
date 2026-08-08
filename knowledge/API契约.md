---
type: knowledge
author: architect
status: draft
created: 2026-08-05
---

# API 契约: local-ai-companion-v2

> 本文档定义 v2 各组件间的接口契约（端点、端口、协议、请求/响应、错误码）。所有接口均为**本机内部调用**，无公网暴露。
>
> 约定：
> - 本文档为 **API 契约最终解释权**归属方（Architect），前后端/组件分歧时以此为准。
> - 接口中标注「实现时确认」的字段/路径，以零度教程对应实现为准，由 Developer 落地后回填，不得单方面改语义。

---

## 1. 认证方式

- **无认证、无 Cookie、无 JWT、无 API Key**。单用户本机应用。
- 所有服务默认仅监听 `127.0.0.1`（WSL2 内）并通过 WSL2 localhost 转发对 Windows 浏览器可见。
- **已知例外**：Windows 侧 llama-server 零度教程默认 `--host 0.0.0.0`（WSL2 经主机 IP 访问需要）。见《架构设计》§8 安全偏离，建议改为 `127.0.0.1`（WSL2 localhostForwarding 可用时）或加防火墙入站限制。
- 安全底线（CONSTITUTION SEC-14）：如需配置 CORS，仅允许 `http://localhost:7860` 来源，禁止 `*` + credentials。

---

## 2. 组件间接口总览

| 调用方 | 被调方 | 协议 | 地址 | 用途 |
| --- | --- | --- | --- | --- |
| 浏览器 | speech-to-speech（Gradio） | HTTP + WebSocket | `http://localhost:7860` | 页面加载、音频上行、文本聊天、音频下行播放 |
| 浏览器 | LiveTalking / avatar-sync | WebRTC | `http://localhost:8010` / `:8011` | 数字人视频画面接收 |
| speech-to-speech（WSL2） | llama.cpp server（Windows） | HTTP（SSE） | `http://localhost:8090` | 大模型流式对话 |
| speech-to-speech（WSL2） | avatar-sync.js | HTTP/WebSocket | `http://localhost:8011` | 转发 TTS 音频驱动口型 |
| avatar-sync.js | LiveTalking | HTTP/WebSocket | `http://localhost:8010` | 音频转发 + WebRTC 信令 |
| 浏览器（探活） | 各服务 | HTTP GET | 见各节 | 连接状态提示（US-08） |

---

## 3. llama.cpp server（Windows :8090）

> OpenAI 兼容 API。llama.cpp 的 `llama-server` 原生提供。流式使用 SSE。

### 3.1 POST /v1/chat/completions（流式对话）

请求体（OpenAI 兼容）：

```json
{
  "model": "qwen3-8b",
  "messages": [
    {"role": "system", "content": "你是……（角色人设）"},
    {"role": "user", "content": "今天心情不太好"}
  ],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 1024
}
```

流式响应（SSE，每行 `data: {json}`）：

```json
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"qwen3-8b",
       "choices":[{"index":0,"delta":{"content":"我"},"finish_reason":null}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"qwen3-8b",
       "choices":[{"index":0,"delta":{"content":"在的"},"finish_reason":null}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"qwen3-8b",
       "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

- 消费者逐 chunk 拼接 `choices[0].delta.content`；按标点/长度拆句后尽早送 TTS（PRD：首句尽快出声，不等全文）。
- `stream=false` 时返回完整 JSON：`{"choices":[{"message":{"content":"..."},...}], ...}`（文字兜底/非流式场景）。

### 3.2 GET /health（探活）

响应 200：

```json
{"status":"ok","model":"qwen3-8b","slots_idle":1}
```

- 启动脚本轮询此端点确认就绪后再声明"已连接"。

### 3.3 GET /v1/models（可选）

响应 200：

```json
{"object":"list","data":[{"id":"qwen3-8b","object":"model","owned_by":"local"}]}
```

### 3.4 错误

| HTTP | 触发 | 说明 |
| --- | --- | --- |
| 401 | 配置了 `--api-key` 且未携带 | 本项目不配置 API Key，一般不出现 |
| 503 | 模型加载中/显存不足 | 重启或换小模型 |

---

## 4. speech-to-speech 管线（WSL2 Gradio :7860）

> 用户主界面。Gradio 的 queue 机制使用 WebSocket（`/gradio_api/queue/join` 等）与 HTTP 上传接口，具体路径由 Gradio 生成。**契约层面定义语义接口**，落地路径以 Gradio 实际生成为准。

### 4.1 页面

- `GET /` → HTML 页面（数字人画面嵌入 + 语音球 + 聊天记录 + 输入框 + 麦克风 + 停止 + 连接状态）。

### 4.2 音频上行（语音输入，US-02）

- 方式：Gradio 音频组件（`type="numpy"`/`filepath`）经其内部 WebSocket/上传接口提交麦克风录音片段。
- 语义：提交的音频片段（WAV/PCM，16kHz 单声道）进入管线，依次执行 VAD → faster-whisper → LLM → TTS。
- 返回：转写文本（进聊天记录）、AI 回复文本（流式）、TTS 音频。

### 4.3 文本下行（AI 回复流式，US-06 文字兜底）

- 方式：Gradio 聊天组件流式输出。
- 语义：`user` 消息进 LLM 转发，回复 token 逐段推送前端展示。

### 4.4 音频下行（TTS 播放）

- 方式：Gradio 音频输出组件 / WebSocket 推送。
- 语义：Qwen3-TTS 合成的音频回传浏览器经 WebAudio 播放；同时该音频复制一份经 avatar-sync.js 转发驱动口型（见 §5）。

### 4.5 控制接口（打断 / 停止，US-04）

- 方式：Gradio 按钮组件事件（HTTP POST 到 Gradio queue）。
- 语义：用户点击停止或重新说话 → 取消 LLM 后续流、TTS 队列、音频播放与说话状态；≤500ms 内静音闭嘴。

### 4.6 配置项（voice.yaml，US-09）

| 键 | 含义 | 默认建议 |
| --- | --- | --- |
| `VAD_THRESH` | VAD 静音能量阈值 | 实现时按环境校准（低=灵敏，高=迟钝） |
| `MIN_SILENCE_MS` | 判定句尾的最小静音时长 | ~700ms |
| `MAX_AUDIO_SEC` | 单次录音上限 | 15~30s |
| `ASR_MODEL` | faster-whisper 模型 | `large-v3` |
| `ASR_LANG` | 转写语言 | `zh` |
| `LLM_BASE_URL` | llama-server 地址（WSL2→Windows） | `http://localhost:8090` |
| `LLM_MODEL` | 大模型名 | `qwen3-8b` / `qwen3-14b` |
| `TTS_MODEL` | Qwen3-TTS 模型路径 | `~/setup/models/Qwen3-TTS-1.7B` |
| `TTS_REF_WAV` | 音色克隆参考音频 | `~/setup/ref.wav` |
| `TTS_REF_TEXT` | 参考音频的对应文本（克隆用） | 安装时配置 |
| `LIVETALKING_URL` | 口型驱动目标 | `http://localhost:8010` |
| `AVATAR_SYNC_URL` | 音频转发目标 | `http://localhost:8011` |

---

## 5. avatar-sync.js（WSL2 Node，:8011）

> 职责：接收 speech 管线的 TTS 音频，转发给 LiveTalking 驱动口型；作为数字人背景层/WebRTC 画面接入点。

### 5.1 POST /api/audio（接收 TTS 音频并转发口型）

请求（multipart 或 JSON，实现时确认）：

```
multipart: audio 字段（WAV/PCM 16kHz 单声道）
JSON:      { "audio_base64": "...", "sample_rate": 16000, "session_id": "..." }
```

响应 200：

```json
{"ok": true, "accepted": true, "latency_ms": 12}
```

- 语义：接收一段 TTS 音频，立即转发到 LiveTalking（`POST /api/tts`）驱动口型；同一 `session_id` 保证同一轮内不混音。

### 5.2 WebRTC 画面接入

- 方式：WebSocket 信令 + WebRTC（具体端点路径实现时确认，常用 `/ws`、`/offer`）。
- 语义：向浏览器提供数字人实时视频流（来自 LiveTalking 的 wav2lip256 输出，作为页面背景层）。

### 5.3 GET /health

响应 200：

```json
{"status":"ok","service":"avatar-sync","upstream":"livetalking:ok"}
```

---

## 6. LiveTalking（WSL2 Python :8010）

> 职责：接收 TTS 音频 → wav2lip256 实时口型 → WebRTC 视频推送。

### 6.1 POST /api/tts（接收音频驱动口型）

请求（multipart 或 JSON，实现时确认）：

```
multipart: audio 字段（WAV/PCM，来自 avatar-sync.js 转发）
JSON:      { "audio": "<base64>", "sample_rate": 16000, "frame_rate": 30 }
```

响应 200：

```json
{"ok": true, "status": "driving", "fps": 30}
```

- 语义：LiveTalking 以 `idle.mp4` 为底版，用该音频实时合成 talking-head 帧并经 WebRTC 推送。

### 6.2 WebRTC 信令（数字人视频）

- 方式：WebSocket 信令端点（常用 `/ws`，实现时确认），交换 SDP offer/answer 与 ICE candidate。
- 语义：浏览器（或 avatar-sync.js 代理）接收 WebRTC 视频流。

### 6.3 GET /health

响应 200：

```json
{"status":"ok","service":"livetalking","model":"wav2lip256","base":"idle.mp4"}
```

---

## 7. 错误码汇总

### 7.1 HTTP 状态码

| 状态码 | 含义 | 典型场景 |
| --- | --- | --- |
| 200 | 成功 | 正常请求 |
| 400 | 请求体不合法 | 音频格式/时长非法、缺少字段 |
| 404 | 路径不存在 | 端点拼写错误 |
| 408 | 处理超时 | ASR/LLM/TTS 超时 |
| 500 | 服务内部错误 | 管线异常、模型推理失败 |
| 503 | 服务未就绪 | 模型加载中、服务未启动（连接状态提示） |
| 507 | 显存/内存不足 | OOM（PRD 验收 7：必须给出可理解提示） |

### 7.2 应用级错误码（统一错误体）

统一错误响应体：

```json
{
  "error": {
    "code": "LLM_ERR_002",
    "message": "本地大模型显存不足，请切换 8B 模型或关闭其他组件",
    "detail": "本地日志可查，不向前端暴露堆栈"
  }
}
```

| code | 含义 | 关联组件 | 前端提示示例 |
| --- | --- | --- | --- |
| `LLM_ERR_001` | llama-server 不可达 | ① | "本地大模型服务未启动，请先运行 start-llama.bat" |
| `LLM_ERR_002` | 模型加载失败/显存不足 | ① | "显存不足，请切换 8B 模型或关闭其他组件" |
| `LLM_ERR_003` | LLM 流式输出异常/超时 | ① | "大模型回复超时，请重试" |
| `ASR_ERR_001` | 未检测到有效语音 | ② | "没有听清，请再说一次" |
| `ASR_ERR_002` | 转写失败 | ② | "语音识别失败，可用文字输入" |
| `TTS_ERR_001` | TTS 引擎异常 | ② | "语音合成失败，仅显示文字回复" |
| `TTS_ERR_002` | 音色克隆参考音频缺失/非法 | ② | "请上传 5-15 秒清晰单人声参考音频" |
| `LIP_ERR_001` | wav2lip256 推理失败 | ④ | "口型驱动异常，数字人画面可能停用" |
| `WRT_ERR_001` | WebRTC 连接失败 | ③④ | "数字人画面连接失败，请刷新页面" |
| `CFG_ERR_001` | 配置非法（voice.yaml） | ② | "配置文件有误，请检查 voice.yaml" |

### 7.3 打断/取消语义（非错误）

- 打断（用户重说/点停止）是**正常控制流**，不产生错误码；通过取消令牌（cancel token / 生成器取消）实现，返回当前已完成的文本与状态 `interrupted: true`，前端据此静音闭嘴。

---

## 8. 速率限制与并发

- **无传统限流**（单用户本机）。
- **并发保护**（SEC-16 弱化实现）：Gradio 层防止重复提交/连点导致管线重入；同一时间只允许一个活跃会话（单会话约束）。
- 打断后必须释放资源再接受新一轮，禁止并发 TTS 队列叠加（US-04 验收"不串音"）。

---

## 9. 探活与就绪约定（一键启动）

| 服务 | 探活端点 | 就绪条件 |
| --- | --- | --- |
| llama-server（Windows :8090） | `GET /health` | 200 且 `status=ok` |
| LiveTalking（WSL :8010） | `GET /health` | 200 且 `status=ok` |
| avatar-sync.js（WSL :8011） | `GET /health` | 200 且 `status=ok`、`upstream=livetalking:ok` |
| speech-to-speech（WSL :7860） | `GET /` | 200 HTML |

启动脚本按顺序（llama → livetalking → voice）轮询就绪，全部就绪后输出"就绪"信号；Gradio 页面连接状态提示逐服务点亮（US-08）。

---

## 10. 实现时待确认项（回填区）

| # | 待确认 | 责任方 | 状态 |
| --- | --- | --- | --- |
| C1 | avatar-sync.js 实际监听端口（暂定 8011）与信令端点路径 | Developer（对照零度教程） | 待回填 |
| C2 | LiveTalking WebRTC 信令端点路径（`/ws`？） | Developer（对照零度教程） | 待回填 |
| C3 | Gradio 音频上行/下行的实际内部端点路径 | Developer | 待回填 |
| C4 | `voice.yaml` 各参数默认值（VAD_THRESH 等）按实测校准 | Developer + QA | 待回填 |
| C5 | 上传参考音频的大小上限最终值（建议 ≤15MB） | Developer + QA | 待回填 |

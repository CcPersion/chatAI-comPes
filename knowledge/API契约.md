---
type: knowledge
author: architect
status: draft
created: 2026-08-05
updated: 2026-08-10
---

# API 契约：local-ai-companion-v2 VoxCPM TTS

> 本契约只定义本机单用户语音管线的语义接口。实现阶段可以拆成 Python Worker、Gradio 内部适配器和 Node bridge，但不得改变字段语义、代际取消和采样率规则。
>
> `CONSTITUTION.md` 当前缺失；安全基线待补。本契约不伪造 SEC-01~20 检查结论。

## 1. 版本和通用约定

- `protocol_version`: `tts.v2`。
- 默认模型：`VoxCPM2`；备用模型：`VoxCPM1.5`。
- 活跃模型只能是上述两个之一。`Qwen3-TTS`、`CosyVoice`、`Edge TTS` 不出现在运行时 fallback。
- 传输默认绑定本机地址；不提供公网认证模型。若服务被绑定到非本机地址，必须先补安全基线和访问控制。
- JSON 使用 UTF-8；时间戳为 Unix milliseconds；ID 使用 UUID/短 UUID；错误消息面向用户，堆栈只写本地日志。
- 所有合成请求都必须携带或由服务生成 `request_id`、`conversation_id`、`generation_id`。`generation_id` 是打断和丢弃旧音频的唯一代际键。

统一错误体：

```json
{
  "error": {
    "code": "TTS_REF_003",
    "message": "参考音频与参考文本不匹配或文本为空",
    "request_id": "req_01J...",
    "retryable": false
  }
}
```

不返回：绝对模型路径、完整本地文件路径、Python/Node 堆栈、完整音频 base64、内部环境变量。

## 2. `GET /api/tts/health`：TTS 健康检查

### 2.1 响应

HTTP `200` 表示 Worker 可响应探活，不代表已经可合成；`503` 表示未就绪。

```json
{
  "status": "ready",
  "service": "voxcpm-worker",
  "protocol_version": "tts.v2",
  "model": {
    "id": "VoxCPM2",
    "role": "default",
    "loaded": true,
    "device": "cuda:0",
    "sample_rate": 48000,
    "streaming": true,
    "clone_modes": ["prompt", "ultimate", "design"]
  },
  "queue": {"active": false, "depth": 0, "max_depth": 1},
  "gpu": {"peak_gib": 10.8, "budget_gib": 15.0},
  "active_generation_id": null,
  "timestamp_ms": 1786300000000
}
```

`status` 枚举：`starting`、`warming`、`ready`、`busy`、`degraded`、`error`。`degraded` 必须说明 `model.role=standby` 或 `reason`，不得伪装成默认模型。

### 2.2 版本字段要求

健康检查必须能回答：实际模型版本、TTS 服务代码版本、协议版本、原生采样率、是否支持流式、当前 reference_id、GPU 设备、队列深度和最近峰值显存。不得只返回一个 `tts: ready`。

## 3. `GET /api/tts/version`：版本信息

```json
{
  "protocol_version": "tts.v2",
  "service_version": "0.1.0",
  "model_id": "VoxCPM2",
  "model_revision": "local-manifest-sha256:...",
  "voxcpm_package_version": "2.x",
  "sample_rate": 48000,
  "audio_format": "pcm_s16le_mono",
  "features": {
    "streaming": true,
    "reference_audio": true,
    "reference_text": true,
    "isolated_reference": true,
    "controllable_cloning": true,
    "voice_design": true,
    "offline_only": true
  }
}
```

VoxCPM1.5 返回同一结构，但 `model_id=VoxCPM1.5`、`sample_rate=44100`、`isolated_reference=false`、`controllable_cloning=false`、`voice_design=false`。该字段用于验收和诊断，不用于客户端硬编码采样率。

## 4. `POST /api/tts/reference-audio`：上传与校验参考音频

### 4.1 请求

`multipart/form-data`：

> 用户侧 multipart 上传由主应用处理并保存到受控的 `UPLOAD_DIR`；随后主应用通过仅绑定 loopback 的 Worker 内部 JSON 接口 `{path, prompt_text}` 更新参考音频。Worker 不接受公网文件路径，也不暴露完整路径给前端。

- `audio`: 音频文件，允许 `wav/mp3/m4a/flac`，最终统一解码为 mono。
- `prompt_text`: 可选但对克隆强烈建议；VoxCPM1.5 的 continuation 模式必填。
- `label`: 可选的人类可读名称，不作为路径。

限制：5–15 秒、大小不超过配置上限、单人声、可解码、不能全静音/严重削波。服务必须按实际内容校验，不能只看扩展名和 MIME。

### 4.2 成功响应 `201`

```json
{
  "reference_id": "ref_8f2c...",
  "status": "validated",
  "duration_ms": 6820,
  "source_sample_rate": 44100,
  "model_input_sample_rate": 16000,
  "channels": 1,
  "sha256": "short-or-full-hash-as-policy-allows",
  "prompt_text_required": true,
  "clone_modes": ["prompt", "ultimate"],
  "created_at_ms": 1786300000000
}
```

不把完整保存路径返回给前端。上传成功后不自动开始合成；若 Worker 尚未加载，reference 可先入库，状态为 `validated_pending_model`。

### 4.3 校验失败

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `TTS_REF_001` | 缺少文件、扩展名/MIME 不允许或无法解码。 |
| 400 | `TTS_REF_002` | 时长低于 5 秒或高于 15 秒、全静音、削波严重。 |
| 400 | `TTS_REF_003` | VoxCPM1.5 必需的参考文本为空，或文本格式不可用。 |
| 413 | `TTS_REF_004` | 文件大小超过上限。 |
| 507 | `TTS_REF_005` | 本地存储或音频预处理资源不足。 |

## 5. `POST /api/tts/synthesize`：合成和真实流式音频事件

### 5.1 请求

默认 `Accept: application/x-ndjson`，服务必须逐行 flush；不得等整段音频生成完成后才返回。`stream=false` 仅供诊断/离线导出，交互路径禁止使用。

```json
{
  "request_id": "req_01J...",
  "conversation_id": "conv_01J...",
  "generation_id": "gen_42",
  "text": "今天辛苦了，先慢慢说，我在听。",
  "model_id": "VoxCPM2",
  "reference_id": "ref_8f2c...",
  "prompt_text": "参考音频逐字转写文本",
  "clone_mode": "ultimate",
  "stream": true,
  "seed": 42,
  "cfg_value": 2.0,
  "inference_timesteps": 10
}
```

约束：

- `text` 是一个已提交的短句/子句，不是整轮无限长回复；Orchestrator 负责分句。
- `model_id` 缺省使用当前健康检查中的默认模型；若显式指定的版本未加载，返回错误，不自动换版本。
- `reference_id` 必须已经校验；`prompt_text` 在需要时必须与参考音频对应。
- 交互默认 `retry_badcase=false`，不得让不可取消的重试阻塞停止按钮。

### 5.2 NDJSON 事件

每行是一个 JSON 对象；音频为 `pcm_s16le_mono` 的 base64 块，仅为本机协议的可移植表示。实现也可以增加二进制本地优化，但事件字段和顺序必须保持一致。

开始：

```json
{"type":"generation.started","request_id":"req_01J...","generation_id":"gen_42","model_id":"VoxCPM2","sample_rate":48000,"audio_format":"pcm_s16le_mono","started_at_ms":1786300000100}
```

音频元数据/块：

```json
{"type":"audio.chunk","request_id":"req_01J...","generation_id":"gen_42","sequence":0,"sample_rate":48000,"duration_ms":320,"is_first":true,"is_last":false,"audio_base64":"..."}
```

要求：

- `sequence` 从 0 单调递增；块内容是连续 PCM，不带 WAV 头。
- `sample_rate` 必须是真实模型输出采样率；VoxCPM2 为 48000，VoxCPM1.5 为 44100。
- 下游必须丢弃 `generation_id` 不匹配、序号重复或逆序的块，并记录 `TTS_STREAM_002` 诊断事件。
- `is_first` 只在第一块为 true；正常完成的最后一块 `is_last=true`。

完成：

```json
{"type":"generation.completed","request_id":"req_01J...","generation_id":"gen_42","chunks":8,"audio_duration_ms":2360,"first_audio_latency_ms":640,"completed_at_ms":1786300002500}
```

取消：

```json
{"type":"generation.cancelled","request_id":"req_01J...","generation_id":"gen_42","reason":"user_interrupt","last_sequence":3,"cancelled_at_ms":1786300001200}
```

错误：

```json
{"type":"error","request_id":"req_01J...","generation_id":"gen_42","error":{"code":"TTS_VRAM_001","message":"当前显存预算不足，未生成新的音频","retryable":true}}
```

### 5.3 非流式诊断

当 `stream=false` 且调用方显式请求 `Accept: audio/wav` 时，返回完整 WAV，并在响应头返回 `X-TTS-Model-Id`、`X-TTS-Sample-Rate`、`X-Generation-Id`。该路径只能用于固定句听感、音色克隆和回归测试，不能作为交互播放实现。

## 6. `POST /api/tts/generations/{generation_id}/cancel`：取消/打断

请求：

```json
{
  "conversation_id": "conv_01J...",
  "request_id": "req_01J...",
  "reason": "user_interrupt"
}
```

返回 `200`，取消接口幂等：

```json
{
  "ok": true,
  "generation_id": "gen_42",
  "state": "cancelling",
  "dropped_pending_units": 1
}
```

取消语义：

- 取消 LLM 后续文本、TTS active/pending 队列、浏览器 ring buffer 和 avatar-sync 待发块。
- 旧 generation 的晚到块必须丢弃，不能依赖客户端“希望它不播放”。
- Worker 在当前可取消边界停止；服务应记录 `cancel_requested_at`、`cancel_ack_at`、`last_sequence`，目标端到端静音/闭嘴 ≤500ms。
- 找不到 generation 时仍返回 `200 state=already_finished`；请求格式非法返回 `400 TTS_REQ_001`。

## 7. 音频下游和 LiveTalking 契约

### 7.1 BrowserAudioSink

Orchestrator 将 `audio.chunk` 按 generation/sequence 写入一个持续 AudioWorklet/ring buffer。播放器只接受当前 generation，收到 `generation.cancelled` 立即清空 buffer 并静音。禁止一块一个 `<audio>` 或一块一次 Gradio `Audio` 更新。

### 7.2 `POST avatar-sync.js /api/audio`

这是现有桥接的目标语义，开发实现必须补齐代际与采样率处理：

请求头：

- `X-Session-Id`
- `X-Generation-Id`
- `X-Audio-Sample-Rate`：真实输入采样率
- `X-Audio-Sequence`

请求体：WAV 或明确声明的 mono PCM。桥接必须校验大小、采样率和 generation，按序转发到 LiveTalking；不能只改 header 把 48kHz/44.1kHz 当成 16kHz。

成功 `200`：

```json
{"ok":true,"accepted":true,"session_id":"sess_01J...","generation_id":"gen_42","sequence":0,"input_sample_rate":48000,"forward_sample_rate":16000,"latency_ms":18}
```

### 7.3 `POST avatar-sync.js /api/audio/cancel`

```json
{"session_id":"sess_01J...","generation_id":"gen_42","reason":"user_interrupt"}
```

返回 `200` 后，桥接不得再向 LiveTalking 发送该代音频；若 LiveTalking 没有原生 stop API，桥接至少清空本地转发队列并发送可验证的静音/闭嘴控制。

### 7.4 `GET avatar-sync.js /health`

```json
{"status":"ok","service":"avatar-sync","protocol_version":"tts.v2","upstream":"livetalking:ok","accepted_sample_rates":[16000],"active_generation_id":"gen_42"}
```

`upstream=unreachable` 时服务可达不等于数字人就绪；总健康状态必须区分 bridge ready 和 LiveTalking ready。

## 8. 错误码汇总

| code | HTTP | 含义 | 可重试 |
| --- | --- | --- | --- |
| `TTS_REQ_001` | 400 | 请求字段缺失、文本为空、参数范围非法 | 否 |
| `TTS_REF_001`~`005` | 400/413/507 | 参考音频缺失、格式/时长/文本/大小/存储失败 | 依具体错误 |
| `TTS_MODEL_001` | 503 | 模型文件缺失、校验失败或版本不可用 | 修复本地安装后 |
| `TTS_MODEL_002` | 503 | 模型仍在加载/预热 | 是 |
| `TTS_DEP_001` | 503 | PyTorch/CUDA/VoxCPM 依赖不可用 | 修复环境后 |
| `TTS_VRAM_001` | 507 | 显存预算不足或 OOM 被拦截 | 切换 `safe-v15`/`exclusive` |
| `TTS_QUEUE_001` | 409 | 已有 active generation 或队列已满 | 取消旧轮后 |
| `TTS_STREAM_001` | 502 | Worker 音频流中断 | 是 |
| `TTS_STREAM_002` | 502 | 音频块乱序、重复或代际不匹配 | 当前轮否 |
| `TTS_CANCEL_001` | 409 | 取消超时，Worker 正在回收 | 等待状态事件 |
| `LIP_ERR_001` | 502 | LiveTalking/wav2lip256 转发失败 | 可降级为仅浏览器音频 |
| `WRT_ERR_001` | 502 | WebRTC 信令或数字人画面失败 | 刷新/重连 |
| `CFG_ERR_001` | 500 | voice.yaml 或 profile 非法 | 修复配置 |

## 9. 运行状态和可观测字段

所有成功/失败事件和本地日志至少关联：`request_id`、`conversation_id`、`generation_id`、`model_id`、`reference_id`、`sequence`。记录排队、首块、完成、取消、桥接转发、显存峰值和错误码；默认不记录完整音频 base64、完整参考音频和绝对路径。

关键延迟指标：

- `user_speech_end -> asr_done`
- `asr_done -> llm_first_token`
- `llm_first_token -> text_unit_committed`
- `text_unit_committed -> tts_first_audio`
- `tts_first_audio -> browser_audio_started`
- `audio_chunk_sent -> avatar_audio_accepted`
- `cancel_requested -> browser_silenced`

## 10. 兼容性和实现门禁

- 旧 `TTS_BACKEND` 多分支不属于本契约；实现完成后配置应以 `VOXCPM_MODEL_ID=VoxCPM2|VoxCPM1.5` 和 profile/worker 状态为准。
- 旧 Qwen3-TTS/CosyVoice API 不得作为 fallback，也不得在测试失败时被重新启用来“让测试通过”。
- Developer 必须先实现 health/version/reference/synthesize/cancel 的契约测试，再接入 Gradio 和 avatar-sync。
- HTTP 200、最终 WAV、静态 HTML、后端单元测试都不能单独证明真实流式、浏览器播放、麦克风、打断或口型同步通过；这些属于后续真实运行和浏览器验收。
- `CONSTITUTION.md` 补齐前，安全审查状态保持“待补”，不得在 API 文档或日志中写 SEC-01~20 已通过。

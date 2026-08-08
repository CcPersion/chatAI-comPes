# API 实现说明 — local-ai-companion-v2 Backend

> 本文档说明 `app.py` 中各模块如何对应 `knowledge/API契约.md` 定义的接口契约。

---

## 1. 组件对契约的映射

| 契约章节 | 契约接口 | 实现位置 (app.py) | 说明 |
| --- | --- | --- | --- |
| API §3 | llama.cpp POST /v1/chat/completions | `LLMClient.stream_chat()` | 使用 httpx 流式消费 SSE。URL 来自 voice.yaml 的 LLM_BASE_URL。 |
| API §3 | llama.cpp GET /health | `LLMClient.check_health()` 与 `check_service("llama-server", ...)` | 用于连接状态探测。 |
| API §4.1 | 页面 GET / | `create_ui()` → Gradio 自动处理 | Gradio 默认提供完整 HTML 页面，含数字人画面、语音球、聊天记录等。 |
| API §4.2 | 音频上行 | `handle_audio_input()` | 通过 Gradio `gr.Audio(sources=["microphone"])` 组件采集，事件 `.stop_recording()` 触发管线。 |
| API §4.3 | AI 回复流式下行 | `handle_text_input()` 与 `handle_audio_input()` | LLM 流式 token 逐段通过 Gradio generator yield 推送到前端 Chatbot 组件。 |
| API §4.4 | TTS 音频下行 | `ConversationPipeline._synthesize_and_dispatch()` | 路径A: 音频回传 Gradio 音频组件播放；路径B: 转发 avatar-sync.js 驱动口型。 |
| API §4.5 | 打断/停止 | `handle_stop()` → `pipeline.stop()` → `state_machine.transition(IDLE)` | 设置取消令牌 cancel_event，取消 LLM 流/TTS 队列/播放。 |
| API §4.6 | 配置项 | `voice.yaml` → `config` 字典 | 所有可调参数从 voice.yaml 读取，带默认值回退。 |
| API §5.1 | avatar-sync POST /api/audio | `forward_audio_to_avatar()` | 将 TTS 音频以 WAV 格式 POST 到 AVATAR_SYNC_URL。 |
| API §5.3 | avatar-sync GET /health | `check_service("avatar_sync", ...)` | 用于连接状态探测。 |
| API §6.1 | LiveTalking POST /api/tts | `avatar-sync.js` 中 `forwardToLiveTalking()` | 由 avatar-sync.js 转发，app.py 不直接调用。 |
| API §6.3 | LiveTalking GET /health | `check_service("livetalking", ...)` | 用于连接状态探测。 |
| API §7.2 | 统一错误体 | `error_response()` 和 `PipelineError.to_dict()` | 所有错误使用 `{"error":{"code":"XX_ERR_XXX","message":"...","detail":"..."}}` 格式。 |

---

## 2. 模块说明

### 2.1 配置管理 (`find_config()`)

- 按优先级查找 voice.yaml：`~/setup/voice.yaml` > `项目根/config/voice.yaml` > `当前目录/voice.yaml`
- 所有配置值从 YAML 读取，带默认值回退
- TTS_MODEL_PATH、TTS_REF_WAV、UPLOAD_DIR 使用 `os.path.expanduser()` 展开 `~`

### 2.2 数字人状态机 (`AvatarStateMachine`)

- 4 状态枚举: `AvatarState.IDLE, LISTENING, THINKING, SPEAKING`
- 线程安全的状态转换（`threading.Lock`）
- 取消令牌 `cancel_event` 贯穿 LLM 流/TTS/播放，打断时设置
- 状态变化回调机制（前端可订阅）

**状态流转规则**（对应架构设计 §2）:
```
IDLE --(用户开始录音)--> LISTENING --(输入完成)--> THINKING --(AI首句语音就绪)--> SPEAKING
  ^                        ^                       |                          |
  +-------(回复结束)--------+-------(用户打断/停止)-----------------------------+
```

### 2.3 VAD 静音检测 (`VADDetector`)

- 优先使用 webrtcvad（WebRTC VAD 引擎），回退到能量阈值检测
- `detect_silence_boundary()`: 判定单个音频帧是否为静音
- `find_sentence_boundary()`: 查找连续静音超过 MIN_SILENCE_MS 的句尾位置
- 参数从 voice.yaml 读取：VAD_THRESH（静音阈值）、MIN_SILENCE_MS（最小静音时长）

### 2.4 ASR 引擎 (`ASREngine`)

- 使用 faster-whisper 的 `WhisperModel`
- 模型下载到 `~/setup/models/faster-whisper-large-v3/`
- CUDA 优先，CPU 回退
- `transcribe()`: 接收 float32 音频 numpy 数组，返回中文文本
- 错误码: ASR_ERR_001（未检测到语音）、ASR_ERR_002（转写失败）

### 2.5 LLM 转发 (`LLMClient`)

- 使用 httpx.Client 流式消费 llama-server 的 SSE 响应
- `stream_chat()`: Generator 逐 token yield，支持 cancel_event 打断
- 按自然停顿拆句：匹配中文（。！？）和英文（.!?）标点，立即拆分送 TTS
- 错误码: LLM_ERR_001（不可达）、LLM_ERR_002（显存不足）、LLM_ERR_003（超时/异常）
- SEC-03: URL 来自配置，httpx 方法参数由代码构造，不拼接用户输入

### 2.6 TTS 引擎 (`TTSEngine`)

- 使用 HuggingFace transformers 加载 Qwen3-TTS 1.7B
- `synthesize()`: 接收文本 + 参考音频路径，返回 float32 音频 numpy 数组
- `update_ref_audio()`: 用户上传新参考音频后更新
- `load_ref_audio()`: 加载参考音频并重采样到 16kHz
- 支持 cancel_event 取消
- 错误码: TTS_ERR_001（合成失败）、TTS_ERR_002（参考音频缺失/非法）

### 2.7 音频上传处理 (`handle_audio_upload()` / `validate_ref_audio()`)

- 文件名防路径遍历：使用 basename + SHA256 哈希生成安全名称
- 扩展名白名单：wav, mp3, m4a, flac
- 大小上限：MAX_UPLOAD_SIZE_MB（默认 15MB）
- 时长校验：MIN_REF_AUDIO_SEC ~ MAX_REF_AUDIO_SEC（默认 5-15 秒）
- 通过校验后更新 TTS 参考音频路径

### 2.8 对话管线 (`ConversationPipeline`)

- `process_voice()`: 语音输入完整管线（VAD→ASR→LLM→TTS→双路分发）
- `process_text()`: 文字输入管线（LLM→TTS→双路分发），对应 US-06 文字兜底
- `_synthesize_and_dispatch()`: TTS 合成 + 双路分发
  - 路径A: yield audio 事件 → Gradio 播放给用户
  - 路径B: `forward_audio_to_avatar()` 异步转发到 avatar-sync.js
- `stop()`: 触发取消令牌，≤500ms 内停止
- `clear_history()`: 清除对话历史（保留 system prompt）
- 对话历史使用滑动窗口（最多 20 轮）

### 2.9 音频转发 (`forward_audio_to_avatar()`)

- 将 float32 音频转为 WAV 格式（int16 PCM）
- POST 到 `${AVATAR_SYNC_URL}/api/audio`
- 携带 X-Session-Id 和 X-Audio-Sample-Rate 头
- 失败时降级（不阻塞主链路），仅记录 debug 日志

### 2.10 服务探活 (`check_service()` / `get_service_status()`)

- 逐服务探活：llama(:8090)、asr(本地)、tts(本地)、livetalking(:8010)、avatar_sync(:8011)
- 返回状态：connected / disconnected / degraded / timeout / error / ready
- SEC-17: 只返回状态字符串，不暴露模型路径等内部配置
- 整体状态：全部 OK 为 "ready"，否则 "partial"

---

## 3. 错误码映射

| 契约错误码 | 触发条件 | 代码位置 | 用户可见消息 |
| --- | --- | --- | --- |
| LLM_ERR_001 | llama-server 不可达 | `LLMClient.stream_chat()` → httpx.ConnectError | "本地大模型服务未启动，请先运行 start-llama.bat" |
| LLM_ERR_002 | 模型加载失败/显存不足 | `LLMClient.stream_chat()` → HTTP 503 | "显存不足，请切换 8B 模型或关闭其他组件" |
| LLM_ERR_003 | 流式输出异常/超时 | `LLMClient.stream_chat()` → Exception | "大模型回复超时，请重试" |
| ASR_ERR_001 | 未检测到有效语音 | `ASREngine.transcribe()` | "未检测到有效语音，请再说一次" |
| ASR_ERR_002 | 转写失败 | `ASREngine.transcribe()` | "语音识别失败，可用文字输入" |
| TTS_ERR_001 | TTS 引擎异常 | `TTSEngine.synthesize()` → Exception | "语音合成失败，仅显示文字回复" |
| TTS_ERR_002 | 参考音频缺失/非法 | `handle_audio_upload()` / `validate_ref_audio()` | "请上传 5-15 秒清晰单人声参考音频" |
| CFG_ERR_001 | 配置非法 | `find_config()` | 运行时输出具体错误，引导修正 voice.yaml |

---

## 4. SEC 安全要点

| SEC | 措施 | 位置 |
| --- | --- | --- |
| SEC-02 | 禁止 eval()/exec()，LLM 输出只作文本处理 | 全文件无动态代码执行 |
| SEC-03 | 子进程调用用 httpx/requests 库，URL 来自配置 | `LLMClient`, `forward_audio_to_avatar()`, `check_service()` |
| SEC-04 | Gradio 默认 HTML 转义 | 使用 gr.Chatbot 和 gr.Markdown，不自定义 innerHTML 插模型输出 |
| SEC-05 | 上传路径限定 + 文件名清理 | `handle_audio_upload()` 使用 basename + SHA256 |
| SEC-11 | 异常不暴露堆栈 | `error_response()`, `PipelineError`, Gradio 事件 try/except |
| SEC-12 | 扩展名白名单 + 大小上限 + 时长校验 | `validate_ref_audio()` |
| SEC-17 | 响应不过度暴露字段 | `get_service_status()` 仅返回状态字符串 |

---

## 5. 启动与运行

```bash
# Windows (先启动)
start-llama.bat

# WSL2 (按顺序)
bash ~/setup/start-livetalking.sh    # 可选：若 LiveTalking 独立运行
bash ~/setup/start-voice.sh          # Gradio :7860
```

浏览器打开 `http://localhost:7860`。

停止:
```bash
bash ~/setup/stop-all.sh    # WSL2 侧
stop-all.bat                # Windows 侧
```

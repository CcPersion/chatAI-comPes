# VoxCPM 重写 QA 测试报告

> 执行日期：2026-08-10；本次更新：pytest 6 passed 与 Worker 契约补强复核  
> 项目路径：`D:\\codexWorkSpec\\chatAI-comPes`  
> 报告性质：QA 验证报告；本轮未修改业务代码。

## 1. 结论

**门禁结论：阻塞，不通过产品验收。**

当前实现通过了 6 项 pytest、静态检查、Python 编译和不加载真实模型的 HTTP 流式/取消测试；Worker 的未就绪 503、ready/busy/error、version/features/audio_format、最后音频块和客户端代际校验已补强。但真实 VoxCPM 模型仍无法启动，且参考音频上传契约、avatar-sync 下游和真实产品链路仍未完成。因此本轮只能证明“协议骨架和新增校验可运行”，不能证明 RTX 5060 Ti 上的真实语音质量、显存、延迟、浏览器播放、打断或口型同步。

## 2. 执行证据

### 2.1 已执行命令和结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| Python 编译 | 通过 | `python -m py_compile outputs/backend/app.py outputs/backend/voxcpm_client.py outputs/backend/voxcpm_worker.py`，退出码 0 |
| 全量 pytest | 通过 | `python -m pytest -q`，当前 `6 passed`；新增参考音频目录边界测试 |
| 协议测试 | 通过 | `tests/test_voxcpm_protocol.py` 覆盖 health/version、FakeModel NDJSON、取消 registry、采样率 profile |
| Worker 契约补强 | 通过静态复核 | 未就绪 health 返回 503；health 状态含 ready/busy/error；version 含 features/audio_format；最后 `audio.chunk` 设置 `is_last=true` |
| 客户端流校验 | 通过静态复核 | `generation_id`、连续 `sequence` 和 generation 内稳定 `sample_rate` 不符合时抛出 `TTS_STREAM_002` |
| 参考目录边界 | 通过静态复核 | `update_reference()` 将候选路径限制在配置的 `reference_root` 内，越界返回权限错误 |
| Shell 语法 | 通过 | `bash -n scripts/start-voxcpm-worker.sh scripts/install-voxcpm.sh`，退出码 0 |
| 主应用导入 | 通过 | 导入后打印 `VoxCPM2 balanced-v2 48000 http://127.0.0.1:8020` |
| Git diff 检查 | 通过 | `git diff --check` 无空白错误；仅有换行格式提示 |
| GPU 设备探测 | 仅硬件证据 | `NVIDIA GeForce RTX 5060 Ti, 16311 MiB`；未运行模型负载 |
| FakeModel HTTP 流式 | 通过 | `health_http ok True VoxCPM2`；事件为 `generation.started → audio.chunk → generation.cancelled`；采样率 48000 |

### 2.2 真实模型阻塞证据

以下依赖当前不存在或不可用：

- 当前 Python：`ModuleNotFoundError: No module named 'torch'`。
- 当前 Python：`ModuleNotFoundError: No module named 'voxcpm'`。
- 专用环境 `.venv-voxcpm` 不存在。
- 配置中的本地模型目录 `/root/setup/models/VoxCPM2` 不存在。
- 配置中的参考音频 `/root/setup/ref-fast.wav` 不存在。
- 直接启动 Worker 的结果为：`VoxCPM worker not started: No module named 'torch'`。

因此未执行真实 `VoxCPM.from_pretrained()`、`generate_streaming()`、GPU 显存峰值、真实音频听感和首包延迟测试。

## 3. 已通过项目

### 3.1 默认模型、备用 profile 和采样率

- `config/voice.yaml:62-69`：默认 `VoxCPM2`、`balanced-v2`、48000Hz。
- `outputs/backend/voxcpm_worker.py:26-30`：`balanced-v2 → VoxCPM2 → 48000Hz`；`safe-v15 → VoxCPM1.5 → 44100Hz`。
- `outputs/backend/voxcpm_worker.py:298-300`：CLI 默认模型和 profile 同样为 VoxCPM2/`balanced-v2`。
- `tests/test_voxcpm_protocol.py` 验证两个 profile 的原生采样率不混用。

**结果：通过静态/协议层验证。**

### 3.2 本地加载和显式 CUDA

`outputs/backend/voxcpm_worker.py:103-125` 具备以下保护：

- CUDA 不可用时直接报错，拒绝 CPU fallback。
- 模型目录不存在时拒绝启动。
- 调用 `VoxCPM.from_pretrained(..., local_files_only=True, device="cuda")`。
- 加载后校验模型实际采样率和配置采样率。

`outputs/backend/voxcpm_worker.py:146-172` 的 health 已补充：

- 未就绪状态返回 `error`，`Handler.do_GET()` 在 `outputs/backend/voxcpm_worker.py:287-294` 对未就绪返回 HTTP 503。
- 就绪/忙碌状态分别返回 `ready`/`busy`，并保留 `error` 字段。
- 返回 model role、queue/max_depth、GPU budget/peak、active generation、timestamp、真实采样率和 `audio_format`。

`outputs/backend/voxcpm_worker.py:181-203` 的 version 已补充 `model_revision`、`voxcpm_package_version`、`audio_format` 和 features，并声明 `local_files_only=true`。

**结果：通过静态验证；真实 CUDA 加载未覆盖。**

### 3.3 NDJSON、流式、取消和客户端顺序校验

- `outputs/backend/voxcpm_worker.py:237-268` 使用 `generate_streaming()` 产生 `generation.started`、`audio.chunk`、完成/取消/错误事件；最后一个非空音频块在 `:263-265` 设置 `is_last=true`，事件包含 `audio_format`。
- `outputs/backend/voxcpm_worker.py:53-87` 使用 generation-scoped registry，禁止同时运行两个 generation。
- `outputs/backend/voxcpm_worker.py:308-311` 提供取消 endpoint。
- `outputs/backend/voxcpm_client.py:32-67` 发送 `Accept: application/x-ndjson`，并拒绝 generation 不匹配、sequence 非单调或同一 generation 内采样率变化的音频块。
- FakeModel HTTP 测试实际得到取消响应 `state=cancelling`，服务端事件包含 `generation.cancelled`。

**结果：协议骨架通过；真实 CUDA 取消边界和端到端 500ms 未覆盖。**

### 3.4 参考音频目录边界

- `outputs/backend/voxcpm_worker.py:99-105` 接收配置的 `reference_root`。
- `outputs/backend/voxcpm_worker.py:210-221` 在更新参考音频前检查文件存在，并拒绝位于配置目录之外的路径。
- 该项只证明本地路径边界校验；尚未证明完整的 multipart 上传、时长、静音、削波和格式校验。

**结果：目录越界校验通过；完整参考音频上传契约仍未通过。**

### 3.5 旧 TTS 活跃路径

- `outputs/backend/app.py:660-1109` 的旧 Qwen3-TTS、CosyVoice、Edge 代码位于明确的三引号 inert legacy block 中。
- 活跃 `_get_tts()` 为 `outputs/backend/app.py:1114-1121`，只创建 `VoxCPMClient`。
- 活跃对话路径 `process_text/process_voice → _synthesize_and_dispatch` 使用 `stream_synthesize`，见 `outputs/backend/app.py:1319-1400、1402-1460`。
- `config/voice.yaml` 已不再提供旧 `TTS_BACKEND` 选择键；主依赖文件也说明 VoxCPM 使用独立环境。

**结果：旧 TTS 未发现活跃实例化路径；旧代码仍保留为 inert backup，属于后续清理范围，不是本轮阻塞的唯一原因。**

## 4. 未通过或存在契约偏差

以下问题仍来自只读审查，未在本轮修复；本次已从问题列表移除 health 503、ready/busy/error、version/features/audio_format、最后块 `is_last` 和客户端 generation/sequence/sample-rate 校验问题。

| 严重度 | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| 已修复 | health/version 契约字段 | Worker 未就绪返回 503；health 提供 ready/busy/error、role、queue、GPU budget、timestamp；version 提供 revision、package version、audio_format、features | 仍需真实模型运行验证 GPU peak 和版本值 |
| P1 | reference-audio 上传契约仍不完整 | 契约要求 multipart、5–15 秒、格式/静音/削波校验和 201，见 `knowledge/API契约.md:97-136`；当前 `outputs/backend/voxcpm_worker.py:296-307` 仍接收 JSON 本地路径并返回 200 | 目录边界已收紧，但参考音频内容校验和上传语义仍未验收 |
| 已修复 | 最后一块和事件元数据 | Worker 采用一块 look-ahead，正常最后块 `is_last=true`，started/chunk/completed 均带 `audio_format` | 仍需真实模型验证实际块边界 |
| P1 | 下游音频代际/序号链路未完成 | `outputs/backend/app.py:1596-1641` 合并段后直接调用 `forward_audio_to_avatar`；`app.py:1235-1295` 直接 POST LiveTalking `/humanaudio`，没有契约要求的 generation/sequence headers 和 avatar-sync `/api/audio` 语义 | 无法证明旧音频会被丢弃，也无法证明口型链路采样率适配 |
| 已修复 | 客户端 generation/sequence 校验 | `voxcpm_client.py` 拒绝代际不匹配、重复/逆序 sequence 和同一 generation 内采样率变化 | 仍需浏览器/LiveTalking 端到端验证丢弃效果 |
| P1 | 浏览器真实播放器未验收 | 契约要求持续 AudioWorklet/ring buffer，见 `knowledge/API契约.md:243-247`；本轮没有浏览器运行记录 | 不能证明连续播放、低空洞、停止和打断体验 |

## 5. 未覆盖项

- 真实 VoxCPM2 和 VoxCPM1.5 权重加载、音色克隆、中文听感和音质盲听。
- RTX 5060 Ti 16GB 上 VoxCPM2 + LLM + ASR + LiveTalking 的峰值显存和 OOM 稳定性。
- 冷启动/暖机首包 P50/P95，PRD 的 2.5s/4s 指标。
- 至少 10 轮对话、长回复连续性和超过 300ms 的非预期空洞。
- 浏览器麦克风、AudioWorklet/ring buffer、播放、重说和 ≤500ms 打断。
- avatar-sync、LiveTalking、wav2lip256、WebRTC 的真实口型同步。
- 断网后的完整本地链路。
- `CONSTITUTION.md` 缺失，因此安全基线仍为待补，未作安全通过结论。

## 6. 下一步门禁

1. 准备 Python 3.10–3.12 的 `.venv-voxcpm`，安装 `outputs/backend/requirements-voxcpm.txt`。
2. 在本地准备并核验 VoxCPM2 权重、VoxCPM1.5 权重和 5–15 秒参考音频；启动 Worker 后记录 `/api/tts/health`、`/api/tts/version`。
3. 继续补齐 reference-audio 的正式 multipart/201 外部契约，或保持当前“主应用 multipart + Worker loopback JSON”边界并为两层分别增加测试。
4. 接入并验证 avatar-sync 的 generation/sequence/采样率适配，再做真实浏览器播放和打断测试。
5. 仅在真实 GPU、浏览器、听感和 LiveTalking 证据齐全后，重新申请 QA 门禁。

## 7. 本轮文件变更

本轮 QA 更新以下报告文件：

- `outputs/qa/voxcpm重写测试报告.md`

未修改 Worker、客户端、应用、配置或测试业务代码；本轮仅更新本报告。

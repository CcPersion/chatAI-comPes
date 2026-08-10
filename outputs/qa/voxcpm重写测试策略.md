# VoxCPM 重写 QA 测试策略

> 执行日期：2026-08-10  
> 项目：`D:\\codexWorkSpec\\chatAI-comPes`  
> QA 范围：只验证当前 VoxCPM Worker、客户端、应用接入和已有协议测试；不修改业务代码。

## 1. 验证目标

1. 确认默认路线为 VoxCPM2，`safe-v15` 明确对应 VoxCPM1.5。
2. 确认 Worker 只从本地路径加载，并显式使用 CUDA，不静默回退 CPU。
3. 验证 NDJSON 流式事件、音频采样率、代际取消和单并发队列协议。
4. 检查旧 Qwen3-TTS、CosyVoice、Edge TTS 是否仍存在于活跃调用路径。
5. 区分静态/协议验证与真实模型、GPU、浏览器、LiveTalking 验收，避免把低等级证据当成产品通过。

## 2. 测试层级

| 层级 | 检查内容 | 本轮执行方式 |
| --- | --- | --- |
| L0 静态 | 配置、默认 profile、采样率、CUDA、本地加载、旧路径 | 源码/配置核对 |
| L1 单元/协议 | Registry、NDJSON 事件顺序、取消、HTTP endpoint | pytest + 内存 FakeModel HTTP 测试 |
| L2 运行时 | VoxCPM 权重、真实 CUDA 推理、显存峰值、首包延迟 | 受环境阻塞，未通过 |
| L3 产品 | 浏览器播放、麦克风、打断、口型、LiveTalking/WebRTC | 本轮未执行，必须后续真实验收 |

## 3. 通过标准

- 默认配置和 Worker CLI 默认值均指向 VoxCPM2/`balanced-v2`。
- VoxCPM1.5 只能通过 `safe-v15` 使用，并保持 44.1kHz；VoxCPM2 保持 48kHz。
- `VoxCPM.from_pretrained()` 必须带 `local_files_only=True` 和 `device="cuda"`。
- Worker 流式输出顺序为 `generation.started` → `audio.chunk`* → `generation.completed`，取消时以 `generation.cancelled` 结束。
- 旧 TTS 不得被 `_get_tts()` 或主对话管线实例化。
- 真实模型、GPU 和浏览器结果必须有独立证据，不能由静态测试替代。

## 4. 重点风险检查

以下项目作为报告中的明确检查项：

- 健康接口字段和 HTTP 状态是否完全符合 `knowledge/API契约.md`。
- 参考音频接口是否实现 multipart 上传、时长/静音/削波校验和 201 响应。
- 最后一块音频的 `is_last`、`audio_format`、generation/sequence 校验是否完整。
- 应用到 avatar-sync/LiveTalking 的发送是否携带 generation、sequence、真实采样率并按契约重采样。
- 浏览器是否存在持续 AudioWorklet/ring-buffer 播放器，而不是按块创建 Gradio 音频更新。


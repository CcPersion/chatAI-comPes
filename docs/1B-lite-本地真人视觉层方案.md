# ⚠️ DEPRECATED — v1 视觉方案已废弃
# v2 视觉层: wav2lip256 + LiveTalking 实时口型驱动 (WSL2)
# 部署见 docs/完整部署指南.md
# 此文档仅供参考。

# 1B-lite 本地真人视觉层方案 (v1 弃用)

## 为什么降级

Unreal + MetaHuman + Pixel Streaming 是重型 3D 路线，学习、安装、资产和调试成本高。当前目标调整为先跑通本地语音伴侣完整闭环，再逐步提高视觉效果。

## 目标

第一版视觉层只要求：

- Web 页面显示写实女性视觉形象。
- AI 回复时有声音、字幕和视觉反馈。
- 后续可接入音频驱动嘴型、表情和轻微动作。
- 全流程本机运行，不依赖 OpenAI、D-ID、HeyGen 等云端推理接口。

## 不验收的内容

- 不验收 Unreal 实时渲染。
- 不验收 MetaHuman。
- 不验收 Pixel Streaming。
- 不宣称 3D 数字人完成。

## 推荐阶段

1. 视觉占位
   - 使用一张许可清晰的写实女性图作为视觉中心。
   - 根据状态显示待机、倾听、思考、说话的轻量动效。
2. 本地语音闭环
   - VAD：Silero VAD。
   - ASR：SenseVoice 或 Whisper。
   - LLM：Ollama 或 llama.cpp。
   - TTS：CosyVoice 或 Qwen3-TTS。
3. 轻量 talking head
   - 候选：LivePortrait、MuseTalk、Wav2Lip、SadTalker、Hallo、EchoMimic。
   - 用 TTS 音频驱动嘴型。
   - 用 LLM 情绪标签驱动表情/动作预设。
4. 3D 升级
   - 在本地闭环稳定后，再替换视觉层为 Unreal + MetaHuman。

## 改动影响

- 前端改动小：原本 Pixel Streaming iframe 区域替换为视觉组件。
- 后端改动中等：保留 Ollama 流式聊天，新增 ASR/TTS/视觉任务编排。
- Unreal 当前不再是阻塞项。
- 原 1A 文本闭环可以复用。

## 验收标准

- 本机可启动 Web 和后端。
- 用户可文字或语音输入。
- 本机 ASR 转写语音。
- 本机 LLM 流式回复。
- 本机 TTS 生成语音。
- 页面播放语音并显示写实女性视觉反馈。
- 停止按钮能取消 LLM、TTS、音频播放和视觉说话状态。

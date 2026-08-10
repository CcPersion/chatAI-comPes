# VoxCPM 重写评审报告

> 评审日期：2026-08-10  
> 评审范围：当前工作区的 VoxCPM Worker、客户端、主应用接入、配置、QA 证据与门禁状态。

## 结论

**有条件通过代码提交；不通过真实产品验收。**

当前实现已经形成可审查的 VoxCPM-only 代码路径，允许提交 Git 保存。但在独立环境、真实模型、浏览器播放器、LiveTalking/avatar-sync 和听感数据补齐前，不得宣称“语音效果已解决”或“Realtime 产品验收通过”。

## 已确认

| 项目 | 结论 | 证据 |
|---|---|---|
| 重写前备份 | 通过 | Git commit `3b808b3` |
| 默认路线 | 通过 | `config/voice.yaml`：VoxCPM2 / `balanced-v2` / 48kHz |
| 备用路线 | 通过 | `voxcpm_worker.py`：VoxCPM1.5 / `safe-v15` / 44.1kHz |
| 本地加载与 GPU | 代码通过 | `local_files_only=True`、`device="cuda"`，无 CPU fallback |
| 流式协议 | 代码/协议测试通过 | `generate_streaming`、NDJSON、generation registry、取消、`is_last` |
| 客户端保护 | 代码通过 | generation、sequence、sample-rate 校验 |
| 旧 TTS | 活跃路径已移除 | `app.py:660-1109` 仅为 inert legacy block；`_get_tts()` 只创建 `VoxCPMClient` |
| 自动启动 | 已接入 | `scripts/start-all.sh` 增加 8020 Worker |
| 静态门禁 | 通过 | py_compile、pytest（6 passed）、bash -n、YAML parse、git diff --check |

## P1 / P2

### P1：真实运行环境未准备

当前环境缺少独立 `.venv-voxcpm`、`torch`、`voxcpm`、本地模型权重和参考音频，因此没有真实 `VoxCPM.from_pretrained()`、GPU 峰值显存、首包延迟、音质或听感证据。

### P1：浏览器与数字人链路未验收

当前 QA 没有真实浏览器 AudioWorklet/ring-buffer、麦克风、打断和 LiveTalking/avatar-sync 端到端记录。现有应用仍保留历史的 LiveTalking 直发逻辑；generation/sequence 头部和 48kHz/44.1kHz 到下游采样率的真实适配尚未完成验收。

### P2：旧 inert 代码仍占据 app.py

旧 Qwen3-TTS/CosyVoice/Edge 实现已被三引号包裹，不会执行，但仍增加维护噪声。后续可在确认 Git 备份可恢复后删除该 inert block。

### P2：参考音频双层接口边界

主应用负责 multipart 上传、文件校验和受控保存；Worker 只接受 loopback JSON 路径更新。该边界已写入 API 契约，但仍需补一组主应用 multipart 与 Worker 内部 JSON 的独立 HTTP 测试。

### 安全基线

仓库没有 `CONSTITUTION.md`，因此本评审不宣称安全基线通过；当前仅确认 loopback 绑定、本地模型加载、上传目录边界和不暴露完整路径等实现约束。

## 提交建议

允许提交当前代码作为 VoxCPM 重写版本的开发基线；提交后下一阶段必须先安装独立 Worker 环境并准备模型/参考音频，再重新申请 QA 门禁。若真实运行失败，不得重新启用 Qwen3-TTS/CosyVoice 作为隐藏 fallback。

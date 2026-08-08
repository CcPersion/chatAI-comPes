# ⚠️ 已弃用 / Deprecated

以下为 **v1** 遗留内容，已被 **v2**（`outputs/backend/app.py` + Gradio + WSL2 全语音管线）替代。

**v1 概览**：Node.js orchestrator + React/Vite 前端 + Unreal Pixel Streaming。代码（`apps/`）未提交到此仓库，仅有文档残留。

**v2 入口**：`outputs/backend/app.py`，部署指南见 `docs/完整部署指南.md`。

## v1 遗留文件清单

| 文件 | 说明 |
|------|------|
| `.env.example` | v1 Node.js 方案环境变量（端口 3100/5173） |
| `docs/安装与启动.md` | v1 npm/dev 启动流程 |
| `docs/项目方案.md` | v1 React + Node + Unreal 架构 |
| `docs/项目目标与约束.md` | v1 目标定义 |
| `docs/项目状态.md` | v1 执行状态 |
| `docs/1B-Unreal-PixelStreaming-操作清单.md` | v1 Unreal 操作手册 |
| `docs/1B-lite-本地真人视觉层方案.md` | v1 降级视觉方案 |
| `unreal/CyberCompanion_1B/` | v1 Unreal 工程（未完成） |

**请勿基于以上 v1 文件进行开发。** 所有当前工作应围绕 v2 知识库（`knowledge/`）和部署指南（`docs/完整部署指南.md`）。

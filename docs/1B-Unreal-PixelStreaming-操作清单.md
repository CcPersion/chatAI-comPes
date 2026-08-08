# ⚠️ DEPRECATED — v1 Unreal 路线已放弃
# v2 视觉层改为 wav2lip256 + LiveTalking + WebRTC (WSL2)
# 此文档仅供参考。

# 1B Unreal Pixel Streaming 操作清单 (v1 弃用)

## 目标

阶段 1B 只验收一件事：浏览器看到本机 Unreal 实时渲染的写实真人数字人画面，并且角色处于自然待机状态。

1B 不接 ASR、TTS、口型、表情联动和大模型状态控制。这些属于后续阶段。

## 当前项目内准备

运行环境盘点：

```powershell
npm.cmd run check:1b-env
```

脚本会检查：

- 常见 Unreal 安装目录和 `UnrealEditor.exe`。
- 当前项目和常见 Unreal Projects 目录下的 `.uproject`。
- `PixelStreamingInfrastructure`、`SignallingWebServer`、`PixelStreaming`、`PixelStreaming2` 线索。
- MetaHuman、Quixel、Fab、Bridge、Megascans 资产线索。
- `80`、`8888`、`3100`、`5173` 端口占用。

脚本输出包含人类可读结论和 JSON。`ready=false` 代表环境尚未具备 1B 验收条件，不代表脚本失败。

## 从无环境到 1B 的手动步骤

1. 安装 Epic Games Launcher。
2. 登录 Epic 账号。
3. 安装 Unreal Engine 5.x。
4. 安装 Unreal 需要的 Visual Studio / Build Tools 和 Windows SDK。
5. 从 Epic / Fab / MetaHuman Creator 准备一个许可清晰的写实女性 MetaHuman 或同等级真人资产。
6. 创建最小 Unreal 项目，建议先用 Blank 或 Third Person 模板。
7. 导入真人资产，放入简单场景、灯光和相机。
8. 配置自然待机动画，至少包含呼吸、眨眼、轻微头部或眼神变化。
9. 启用 Pixel Streaming 或 Pixel Streaming 2 插件。
10. 准备 Pixel Streaming Infrastructure / Signalling Web Server。
11. 用 Standalone Game 或打包应用启动 Unreal，启动参数包含：

```text
-PixelStreamingURL=ws://127.0.0.1:8888
```

12. 启动 Signalling/Web Server。
13. 浏览器访问 `http://127.0.0.1`，确认画面来自实时 Unreal 渲染。

## 禁止伪造项

- 不用静态图片代替 Unreal 实时画面。
- 不用预录视频代替 Pixel Streaming。
- 不用二次元 Live2D 或卡通模型代替写实真人资产。
- 不创建空壳 `.uproject` 冒充已完成 Unreal 工程。
- 不把 Web iframe 可访问当作 1B 通过。
- 不把 Pixel Streaming URL 已配置当作 Pixel Streaming 已连接。

## 1B 验收证据

通过 1B 前必须提供：

- `npm.cmd run check:1b-env` 输出，显示 Unreal、`.uproject`、Pixel Streaming 和资产线索具备。
- Unreal 工程路径和 Unreal 版本。
- Pixel Streaming Signalling/Web Server 启动证据。
- Unreal streamer 已连接到 `ws://127.0.0.1:8888` 的证据。
- 浏览器访问 `http://127.0.0.1` 的截图或录屏。
- 人眼确认：角色为写实真人女性，正在自然待机，不是静态图或预录视频。

## 通过标准

- 浏览器能看到本机 Unreal 实时画面。
- 真人数字人不是占位图、录屏或假渲染。
- 角色自然待机。
- 80 和 8888 端口用途明确，Pixel Streaming 链路真实连接。

## 未通过时的处理

- 如果没有 Unreal：先安装 Unreal，不改 Web 功能。
- 如果没有资产：先准备许可清晰的 MetaHuman 或同等级真人资产。
- 如果没有 Pixel Streaming Infrastructure：先补齐官方 Signalling/Web Server。
- 如果只有 iframe 页面但没有实时画面：继续保持 1B 未验收。

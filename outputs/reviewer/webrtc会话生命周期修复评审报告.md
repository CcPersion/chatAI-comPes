---
role: Ai-Team Reviewer
date: 2026-08-10
scope: WebRTC 重连后无声无口型修复
conclusion: 通过
---

# WebRTC 会话生命周期修复评审报告

## 总评

正确性：91/100 | 安全性：94/100 | 可维护性：82/100

致命违规：0 条 | 高级违规：0 条 | 中级违规：0 条
结论：**通过（带非阻断警告）**

本次修复覆盖了已定位的两个直接根因：旧 PeerConnection 关闭时不能再删除同 session id 下的新会话对象；持久音频请求不能永久绑定建立请求时的旧对象。当前 Windows 与 `/root/setup` 中四个相关补丁/启动文件 SHA-256 一致，7860、8010、8011、8020 均在线，最近日志同时出现 current-session rebound、`notify:{'status': 'start'}` 和 `Avatar playout completed without underflow`。

以上属于工程验证，不等于产品体验验收。本次遵守约束未打开浏览器，因此**用户是否实际听到声音、声音是否连续、口型是否真实运动及同步，均未验证**。

## 关键正确性审查

### 1. identity-aware removal

通过。`scripts/livetalking-session-lifecycle.patch:5-25` 为 `remove_session` 增加 `expected_session`，并在当前映射对象与关闭连接捕获的对象不一致时返回 `False`；`scripts/livetalking-session-lifecycle.patch:30-33` 将创建该 PC 时的 `avatar_session` 传入删除调用。因而旧 PC A 关闭时，如果 session id 已映射到新对象 B，身份比较失败，B 不会被删除；只有映射仍指向 A 时才允许移除。

WSL 落盘代码与补丁一致：`/root/setup/LiveTalking/server/session_manager.py:89-107` 和 `/root/setup/LiveTalking/server/rtc_manager.py:50-58`。QA 还记录了独立进程回归断言通过，见 `outputs/qa/webrtc会话生命周期修复测试报告.md:14-17`。

### 2. persistent route 重绑定与 start 事件

通过。`scripts/livetalking-stream.patch:40-56` 在每次发帧前调用 `get_session(request, sessionid)`，不再使用请求建立时捕获的旧对象；对象变化时更新 `bound_session`、重置 `first_frame`，并在该对象接收的首帧附带 `start`。`scripts/livetalking-stream.patch:58-67` 对每个完整 20ms PCM 帧执行该逻辑。V2 升级补丁实现相同语义，见 `scripts/livetalking-stream-v2-to-v3.patch:21-37`、`:41-49`。

WSL 落盘实现位于 `/root/setup/LiveTalking/server/routes.py:112-176`。最近运行证据出现 `humanaudio_stream rebound sessionid=0`、随后 `notify:{'status': 'start'}`；最近一次后端播放记录为零 underflow。这证明音频帧进入了当时注册的当前会话对象，但不能替代浏览器端听觉和视觉验收。

### 3. 启动幂等性与升级路径

支持的完整状态下通过：

- 已应用生命周期补丁时，`scripts/start-all.sh:44-47` 通过 caller 标记跳过重复应用。
- Clocked-v2 通过 `scripts/start-all.sh:49-53` 升级至 v3。
- 已有旧 `humanaudio_stream` 且不是 v3 时，`scripts/start-all.sh:54-59` 回退 v1，再由 `scripts/start-all.sh:60-65` 应用完整 v3。
- 已是 Clocked-v3 时，上述流式补丁分支均跳过。

存在两个非阻断的恢复性风险：

1. `scripts/start-all.sh:44-47` 只检查 `rtc_manager.py` 的 caller 标记，未同时确认 `session_manager.py` 已支持 `expected_session`。若环境被人工改成“仅 caller 已补丁”的半应用状态，启动脚本会误判完成，连接关闭时可能触发参数不兼容；若仅 callee 已补丁，整包重应用会失败并因 `set -e` 停止。当前 WSL 两侧均完整，不影响本次在线结论。
2. `scripts/start-all.sh:60-65` 在完整流式补丁文件缺失且源码尚无 Clocked-v3 时会静默跳过，而不是失败退出。当前文件存在且 Windows/WSL 哈希一致，不影响本次部署，但环境重建的失败提示不够强。

QA 报告 `outputs/qa/webrtc会话生命周期修复测试报告.md:24` 所述 start-all 哈希不一致已经过时：Reviewer 当前核对两侧 SHA-256 均为 `996b2687ca615c3065401043ddb3f76f0ea14276b9f0ccab84eb8aaf3e46e003`。报告 `:25` 的补丁尾随空格属于格式警告，不影响补丁语义。

## SEC-01～20 审查

| ID | 结果 | 依据 |
|---|---|---|
| SEC-01 SQL 参数化 | 不适用 | 本次范围无数据库或 SQL。 |
| SEC-02 禁止动态代码执行 | 通过 | 补丁与启动脚本无 `eval`、`exec`、`Function` 动态执行。 |
| SEC-03 命令注入 | 通过 | 启动命令使用固定程序及带引号路径；本次新增逻辑未拼接用户输入执行命令。 |
| SEC-04 XSS | 不适用 | 本次范围不生成 HTML，也不写入 DOM。 |
| SEC-05 路径遍历 | 通过 | 音频接口的 `sessionid` 仅用于会话映射；补丁未将请求值用于文件路径。启动路径由脚本位置推导并加引号。 |
| SEC-06 硬编码密钥 | 通过 | 未新增密码、Token、API Key。 |
| SEC-07 密码哈希 | 不适用 | 无密码存储。 |
| SEC-08 JWT 过期 | 不适用 | 无 JWT。 |
| SEC-09 敏感接口权限 | 不适用 | 本次未新增支付、用户数据或管理接口；部署仍依赖本机可信边界。 |
| SEC-10 敏感日志 | 通过 | 新日志仅含 session id 与 Python 对象标识，不含密码、Token、证件号或手机号。 |
| SEC-11 异常暴露 | 通过（观察） | 服务端使用 `logger.exception` 留存堆栈，HTTP 侧经 `json_error` 返回字符串，未发现向前端返回 traceback；原始异常文本仍建议后续统一成固定错误码。 |
| SEC-12 上传校验 | 不适用 | 新流式路由接收固定 PCM 流，不是文件上传；采样率和 20ms 帧约束已校验。 |
| SEC-13 GET 修改数据 | 通过 | 音频流注册为 POST，见 `scripts/livetalking-stream.patch:77-80`。 |
| SEC-14 CORS | 不适用 | 本次补丁未修改 CORS；全局 CORS 策略未在本次限定文件中复核。 |
| SEC-15 Cookie 属性 | 不适用 | 本次无 Cookie。 |
| SEC-16 批量上限/限流 | 不适用 | 无批量操作、短信或邮件发送；持续音频连接按产品设计为长连接。 |
| SEC-17 响应字段最小化 | 通过 | 新响应仅含采样率和 streaming 状态，未返回敏感内部字段。 |
| SEC-18 依赖漏洞 | 未验证 | 本次未修改依赖清单；未执行 npm/pip 漏洞审计，不能据此宣称全项目依赖安全。 |
| SEC-19 默认密码 | 不适用 | 无账号或默认密码。 |
| SEC-20 数据库连接池 | 不适用 | 无数据库连接。 |

## 三维度评分说明

### 正确性：91/100

对象身份比较直接封堵了“旧 PC 删除新 session”的竞态，且默认 `expected_session=None` 保留原调用兼容性。持久流在每帧发送前重新解析映射，并在重绑定首帧重发 `start`，与“无声音且无口型”的下游对象失联症状一致。

扣分来自半应用补丁状态的启动恢复性，以及重绑定后仍沿用请求建立时读取的 `target_rate/target_chunk`；当前同一 LiveTalking 模型的重连会话参数一致，因此后者不是当前阻断项。

### 安全性：94/100

限定变更未引入动态执行、命令注入、路径遍历、密钥、敏感日志或 GET 写操作，致命与高级违规均为 0。服务仍依赖本地可信部署边界，SEC-18 全项目依赖漏洞审计未执行，不能扩展为全项目安全通过。

### 可维护性：82/100

`expected_session` 命名准确，返回布尔值便于测试；Clocked-v3 与 v2→v3 独立补丁提供了明确升级意图。主要扣分是启动脚本使用单个文本标记判断双文件补丁完整性、缺少半应用状态诊断，以及补丁文件存在少量尾随空格。

## 阻断项

无。

## 建议修改项

- [ ] `scripts/start-all.sh:44-47` 同时检查 caller 与 callee 两个标记；发现半应用状态时输出明确错误或执行可恢复升级。
- [ ] `scripts/start-all.sh:60-65` 在源码不是 Clocked-v3 且补丁文件缺失时失败退出，避免服务“启动成功但无持续音频路由”。
- [x] `scripts/livetalking-stream-v2-to-v3.patch` 中 QA 指出的尾随空格已清理，不再列为待办。
- [ ] 后续真实产品验收只使用用户当前唯一浏览器会话，确认一次重连后的有声、口型运动、连续性与音画同步；该项尚未验证，不属于本报告的工程通过证据。

## 最终结论

**通过。** 当前修复在代码逻辑、WSL 落盘状态和后端运行证据上闭合，未发现 blocker 或 SEC 致命/高级违规。此结论仅允许认定“工程修复已部署且后端链路正常”，**不允许认定用户端声音和口型体验已经验收通过**。

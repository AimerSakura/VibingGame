# Vibe Agent 原生迁移能力对应表

| 能力 | 原生入口或实现 |
|---|---|
| 项目选择与创建 | Godot 原生项目管理器 |
| 多轮聊天、历史、工具步骤 | 顶部 `Agent` 工作区的“对话”页，SSE 实时事件 |
| 模型与视觉模型 | Agent 设置；非敏感值保存到 Editor Settings |
| API Key | Windows Credential Manager / macOS 登录钥匙串 |
| 图片附件、拖放和剪贴板 | `.godot/agent/attachments`，历史只存元数据 |
| 外部游戏素材 | 校验并去重导入 `res://assets`，触发文件系统扫描 |
| 文件读取、搜索、写入、补丁、删除、重命名 | 原 Agent 工具在项目隔离副本中执行 |
| 未保存内容、版本冲突与恢复 | 写前哈希检查、事务日志、冲突感知的任务回退 |
| 取消、超时、重试与后台恢复 | 单项目任务锁、协作式取消、重启按钮、父进程看门狗 |
| 多编辑器实例 | 每实例随机端口、令牌和握手文件 |
| 验收计划与证据 | `Tests` 子页；变更相关测试生成、操作步骤、状态断言、截图及视觉判定、单项/全部运行 |
| 玩家行为执行 | 独立 Godot 进程与存档，动作/按键/鼠标输入、场景切换、节点属性读取、超时和取消清理 |
| 自动失败回修 | 本轮固定验收标准，最多 3 轮 Agent 修复并复跑全部测试；受阻项单独报告 |
| 修改后重新验证 | 内容指纹变化后结果过期；计划修改不能沿用旧结论，单项验证不能伪造全部通过 |
| 项目运行和预览 | Godot 原生运行栏和 `Game` 工作区 |
| Web / Windows 导出 | Godot 原生 Export 对话框及版本匹配的导出模板 |
| Godot API 文档 | Godot 原生 Search Help |
| Agent 设置 | 顶部按钮打开设置弹窗，包含模型、回答语言、清空对话和回退；对话与 Tests 使用完整宽度 |
| 分发 | 定制编辑器与 PyInstaller onedir sidecar；Windows x86_64 发布包与 macOS arm64 `.app`；目标机无需 Python |
| 账号登录方式 | API Key 模式（Windows 与 macOS）；ChatGPT / Codex 模式（macOS arm64 首发，无需 OpenAI API Key） |
| ChatGPT 登录与账号管理 | 点击 **Sign in with ChatGPT**，在浏览器完成授权后按需选择账号；显示账号元数据，支持切换账号与退出登录 / 撤销授权 |
| 凭据与令牌保护 | OAuth、访问、刷新和 ID 令牌只存操作系统凭据存储（Windows 凭据管理器、macOS 登录钥匙串），不写入项目、启动参数、环境变量、SSE 或日志；macOS 包内随附经校验且版本固定的 Codex app-server |

CLI、Agent、Harness、提示词和 API 知识库继续保留。旧网页仅在阶段验收期间保留为对照，确认能力齐全后再删除。

## 平台限制

- 仅支持 macOS arm64（Apple 芯片），不提供 Intel 或 universal 版本。
- ChatGPT / Codex 登录首发仅支持 macOS arm64；Windows 仍为 API Key 模式，现有 Windows PowerShell 脚本与行为保持不变。
- macOS `.app` 当前未签名、未公证；从网络下载后可能被 Gatekeeper 与 quarantine 拦截，需要用户确认来源后手动允许打开，是否绕过或允许打开由用户自行决定。
- 仅使用 PyInstaller onedir sidecar，不生成嵌套 `.app`；API Key 与 ChatGPT / Codex 登录令牌只保存在操作系统凭据存储（macOS 登录钥匙串、Windows 凭据管理器），不落明文。

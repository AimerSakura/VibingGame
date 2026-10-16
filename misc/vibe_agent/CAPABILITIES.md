# Vibe Agent 原生迁移能力对应表

| 能力 | 原生入口或实现 |
|---|---|
| 项目选择与创建 | Godot 原生项目管理器 |
| 多轮聊天、历史、工具步骤 | 顶部 `Agent` 工作区的“对话”页，SSE 实时事件 |
| 模型与视觉模型 | Agent 设置；按认证模式分组：API Key 模式配置接口地址、API 模型、视觉模型，ChatGPT/Codex 模式从 Codex 模型列表选择；非敏感值保存到 Editor Settings，API 与 Codex 键互不覆盖 |
| API 模型列表 | API Key 模式经兼容 OpenAI 的 `models.list` 获取；接口不支持时保留手动输入，不把 API 模型传入 Codex |
| Codex 模型列表 | ChatGPT/Codex 模式经随包 app-server `model/list` 获取 id/displayName/inputModalities/isDefault；不使用 `claude`/`gemini` 默认值兜底 |
| API Key | Windows Credential Manager / macOS 登录钥匙串 |
| 图片附件、拖放和剪贴板 | API Key 模式经 `image_url`；ChatGPT/Codex 模式经 `turn/start` 的 `localImage`，仅当模型声明 image modality；附件副本位于工作区副本 `.godot` 忽略目录，历史只存元数据 |
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
| Agent 设置 | 顶部按钮打开设置弹窗，按认证模式分组显示 API Key 字段或 ChatGPT/Codex 账号与模型列表，包含回答语言、清空对话和回退；对话与 Tests 使用完整宽度 |
| 分发 | 定制编辑器与 PyInstaller onedir sidecar；Windows x86_64 发布包与 macOS arm64 `.app`；目标机无需 Python |
| 账号登录方式 | API Key 模式（Windows 与 macOS，含 Linux 构建仅 API Key）；ChatGPT / Codex 模式（Windows x86_64 与 macOS arm64） |
| ChatGPT 登录与账号管理 | 点击 **Sign in with ChatGPT**，在浏览器完成授权后按需选择账号；显示账号元数据，从 `model/list` 选择 Codex 模型，支持切换账号与退出登录 / 撤销授权 |
| 凭据与令牌保护 | OAuth、访问、刷新和 ID 令牌只存操作系统凭据存储（Windows 凭据管理器、macOS 登录钥匙串），不写入项目、启动参数、环境变量、SSE 或日志；Windows 与 macOS 完整包内均随附经 SHA-256、目录结构、版本号与 PE/Mach-O 架构校验的固定版本 Codex app-server |

CLI、Agent、Harness、提示词和 API 知识库继续保留。旧网页仅在阶段验收期间保留为对照，确认能力齐全后再删除。

## 平台限制

- 支持 Windows x86_64 与 macOS arm64（Apple 芯片）；不提供 macOS Intel、macOS universal 或 Windows ARM64 版本。
- ChatGPT / Codex 登录在 Windows x86_64 与 macOS arm64 均可用；Windows 的 OAuth 令牌保存在凭据管理器，API Key 模式仍可作为回退方式。Linux 及其他未随包 Codex 的平台仅 API Key 模式。
- API Key 模式的模型列表依赖接口的 `models.list`；不支持时界面保留手动输入，不影响保存与文本对话。
- ChatGPT/Codex 模式不使用 `claude`/`gemini` 默认值兜底；`model/list` 不可用时模型列表为空并给出安全提示，图片输入仅限声明 `image` modality 的模型。
- macOS `.app` 当前未签名、未公证；从网络下载后可能被 Gatekeeper 与 quarantine 拦截，需要用户确认来源后手动允许打开，是否绕过或允许打开由用户自行决定。
- Windows 发布包为未签名 ZIP，仅支持 x86_64；使用 Windows 10 1803+ 自带的 `tar.exe` 解压随包 Codex 发布件，缺少时打包脚本直接报错。
- 仅使用 PyInstaller onedir sidecar，不生成嵌套 `.app`；API Key 与 ChatGPT / Codex 登录令牌只保存在操作系统凭据存储（macOS 登录钥匙串、Windows 凭据管理器），不落明文。

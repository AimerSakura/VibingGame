# VibeGodot 使用说明

## Windows 使用说明

Agent 对话支持常用 Markdown：标题、粗体、斜体、删除线、行内代码、代码块、列表、引用、表格和 HTTP(S) 链接。代码保持原样，不执行 HTML 或模型提供的 BBCode。

顶部“设置”弹窗中的“回答语言”提供“简体中文”“English”和“跟随用户输入”，保存设置后应用于后续模型请求，并在重启后保留。默认简体中文；已有历史不自动翻译。此选项控制模型回答、测试描述及审查理由，不改变工具原始日志或整个编辑器界面语言。

运行 `VibeGodot.exe`，在原生项目管理器中打开或新建项目。普通编辑器启动后会自动进入顶部 **Agent** 工作区；项目管理器、无头模式和恢复模式不会启动 Agent 后台。

1. 点击顶部“设置”。API Key 模式填写兼容模型服务的接口地址、API 模型、视觉模型和 API Key；ChatGPT/Codex 模式登录后从 Codex 模型列表中选择模型。两组配置按模式分组、互不覆盖，点击“保存设置”。API Key 使用当前 Windows 用户的凭据管理器保存，不写入项目、启动参数、环境变量或日志。
2. 在“对话”页描述需要创建、修改或检查的内容。执行步骤通过本机 SSE 实时显示；可以取消、重试失败任务、附加参考图片或导入游戏素材。
3. 参考图片存入 `.godot/agent/attachments`，导入素材存入 `res://assets`。历史只记录附件元数据，不保存大型 Base64 内容。
4. Agent 在隔离副本中修改项目，再按文件版本和编辑器未保存状态检查后写回。发生冲突会停止写入；“回退上次 Agent 修改”同样检查后续改动，避免覆盖用户工作。
5. 每次 Agent 实际修改项目文件后，“Tests”页会根据本轮需求和文件差异生成 3～6 项玩家行为测试，运行真实游戏进程，模拟动作、按键、鼠标及场景切换，并检查游戏状态；涉及画面时，API Key 模式使用已配置的视觉模型，ChatGPT/Codex 模式仅在所选 Codex 模型声明图片输入能力时进行视觉判断。测试失败会自动交回 Agent 修复并复跑全部启用测试，最多回修 3 轮。无文件修改的对话不会触发。
6. “Tests”页显示测试步骤、预期、实际结果和截图，支持单项/全部运行、重新生成、停止，以及展开高级 JSON 编辑计划。计划编辑、手动保存或外部修改会使相应验证过期；单项通过不等于全部通过。测试期间不能编辑计划或导入素材，写回前会重新确认编辑器未保存内容。
7. 每个用例在独立项目副本、进程和测试存档中运行。脚本检查是前置检查，不代表玩家行为通过；视觉模型不可用、截图失败、测试描述没有可执行步骤或证据不足时显示受阻，不会伪装成通过或直接触发游戏修复。旧版纯文本目标保留为待生成，可在高级编辑中补充步骤或停用。回退上次 Agent 修改会覆盖该任务及其自动回修。

Agent 工作区不再显示常驻设置侧栏、快速创建模板或重复的 Godot 操作按钮；清空对话和回退上次 Agent 修改保留在设置弹窗。运行、停止、Game 预览、导出和 API 文档搜索使用 Godot 自带的工具栏、工作区与菜单。Web 与 Windows 导出需要安装与本编辑器版本匹配的导出模板，请从 Godot 的导出界面或“管理导出模板”安装；程序不会静默改用其他 Godot 版本。

随包的 `godotvibe-agent.exe` 是隐藏窗口的 PyInstaller onedir 组件，运行机器无需安装 Python。每个编辑器实例启动独立 sidecar，监听 `127.0.0.1` 随机端口并使用独立 256 位令牌；编辑器退出时先请求正常关闭，超时后强制清理。

诊断日志位于当前项目 `.godot/agent/sidecar.log`。旧网页在分阶段验收期间只作为回归对照保留，不是默认入口。

测试计划保存在 `.godot/agent/acceptance_plan.json`（版本 2）；每次运行的报告、逐步结果、游戏日志、截图及独立存档位于 `.godot/agent/test_artifacts`，历史计划位于 `.godot/agent/plan_history`。状态和验证指纹由后台维护，编辑 JSON 不能直接把测试标记为通过。

开发验证：运行 `python -m unittest discover -s vibe_tools/tests -v`。设置 `GODOT_TEST_EXE` 为本次构建的 Godot 可执行文件绝对路径，可启用真实进程集成测试（移动、跳跃、鼠标点击、场景切换、截图、超时、提前退出以及失败→回修→复测→回退）。这些测试使用离线模型替身验证编排；真实主模型和视觉服务应使用自己的配置另行验证。


### Tests 结果判定

Tests 分别展示行为断言、视觉判断和运行诊断。仅在进程正常退出且执行记录完整时，退出资源未释放提示作为警告保留；行为与视觉均通过时显示“通过（有警告）”，不自动回修。未知运行错误或视觉证据不足为受阻，明确的游戏脚本、行为断言或视觉失败才进入自动回修；失败与受阻并存时保留全部原因。

`runtime.json` 中视觉步骤的 `pending` 表示截图完成但尚未判断，最终结论以 `report.json` 为准。报告增加 `rule_version`、`behavior_status`、`visual_status`、`runtime_status`、`diagnostics`、`repairable_defects` 和 `warning_count`；原有接口及状态字段保持兼容。旧判定规则的验证自动失效，历史报告保留，必须复测才能获得新版验收结论。


### 失败后的测试合理性审查

行为或视觉失败先交主模型审查原始需求、当前源码和实际执行证据。测试合理且证据充分才自动修改游戏；硬编码、时序竞态等测试问题可以提出修订，并通过独立请求复核验收覆盖后重新运行全部启用测试。无法判断或审查服务不可用时标为受阻，不修改游戏。

测试自动修订与游戏自动回修各自最多 3 轮。修订保留 ID、启用/必选状态及视觉覆盖，原始需求不变；旧计划保存在 `plan_history`，原始逐轮报告保留。Tests 显示审查理由和证据，计划中的 `review_history` 记录修订前后方案，`plan_revision` 记录测试修订轮次。游戏修复 Agent 仍无权自行删除或弱化测试。

## 账号与登录方式

编辑器支持两种模型接入方式，可在顶部“设置”弹窗中切换：

- **API Key 模式**：填写兼容模型服务的接口地址、API 模型、视觉模型和 API Key。API 模型候选来自该接口的 `models.list`；接口不支持列表时保留“手动输入模型 ID”，不会用默认字符串代替。API Key 由操作系统凭据存储保管：Windows 使用凭据管理器，macOS 使用登录钥匙串（login Keychain）。
- **ChatGPT / Codex 模式**：无需 OpenAI API Key。点击设置中的 **Sign in with ChatGPT**，按提示在浏览器中打开授权页面并点击 **Continue with ChatGPT** 完成授权；如果账号下有多个可用账号，再按提示选择要使用的账号。登录后从 Codex 的 `model/list` 列出并选择模型，该 id 只用于 Codex，绝不回落到 API 模式模型或 `claude`/`gemini` 默认值。

登录后设置界面会显示账号元数据（账号标识、名称等）。需要时可以切换账号，或退出登录 / 撤销授权；退出或撤销后需重新登录才能继续使用 ChatGPT 模式。

图片与视觉能力：API Key 模式通过接口的 `image_url` 发送图片并可使用单独的视觉模型；ChatGPT/Codex 模式只通过 Codex `turn/start` 的 `localImage` 发送附件，且仅当所选模型声明 `image` input modality 时可用。模型不支持图片时附件入口禁用，侧车也会给出可读的安全错误；附件副本位于工作区副本的忽略目录，不会被当作项目改动写回。API 与 Codex 模型配置分开持久化，切换模式后各自值保留。

OAuth 令牌、访问令牌、刷新令牌和 ID 令牌都不会写入项目文件、启动参数、环境变量、SSE 或日志，只保存在操作系统凭据存储中。界面、日志和文档不会展示任何真实令牌、授权码、Cookie 或授权地址。

Windows x86_64 与 macOS arm64 均支持 **Sign in with ChatGPT**；Windows 的 OAuth 令牌使用当前 Windows 用户的凭据管理器保存，不写入项目、启动参数、环境变量、SSE 或日志。API Key 模式仍可作为回退方式使用；Windows ARM64 暂不支持。完整发布包随附固定版本、经 SHA-256、目录结构、版本号与 PE/Mach-O 架构校验的 Codex app-server，必须分发完整发布包而不能只复制编辑器可执行文件。

## Windows x86_64 打包说明

Windows 版本在 **PowerShell** 中构建并打包（需先按仓库根目录 README 的说明安装 Visual Studio C++ 工具链与 64 位 Python 3.12）：

```powershell
python -m pip install --target .build/pythonlibs scons pyinstaller fastapi uvicorn openai "pydantic>=2,<3" pillow requests pygltflib python-multipart
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
$pythonExe = (Get-Command python -CommandType Application).Source
& $pythonExe -m SCons platform=windows target=editor arch=x86_64 production=yes debug_symbols=no d3d12=no -j4
.\misc\vibe_agent\build_sidecar.ps1 -PythonExe $pythonExe
.\misc\vibe_agent\package_windows.ps1 -PythonExe $pythonExe -SkipBuild -SkipSidecarBuild -Zip
```

`package_windows.ps1` 会先调用 `misc/vibe_agent/fetch_codex_windows.ps1`：下载固定版本 `0.146.0` 的 Windows x86_64 Codex 包（`codex-package-x86_64-pc-windows-msvc.tar.gz`，失败时回退官方 GitHub `openai/codex` 的 `rust-v0.146.0`），先取 `codex-package_SHA256SUMS` 并用其中的 SHA-256 校验，再使用 Windows 10 1803+ 自带的 `tar.exe` 解压，然后校验 `bin/codex.exe`、`codex-resources`、`codex-path`、`codex-package.json`、`codex.exe --version` 与 PE 头 machine `0x8664`。校验通过后把整个 release root 复制到发布包的 `vibe_agent/codex`，复制完成后再校验一次。下载缓存与临时文件位于 `.build/codex`。缺少 `tar.exe`、校验失败或架构不符都会直接终止打包，不会生成不完整发布包。可用环境变量 `CODEX_VERSION`、`CODEX_RELEASE_ROOT`、`CODEX_BIN` 覆盖版本或复用已有布局，但覆盖来源同样必须通过完整校验；这些覆盖不会携带任何令牌、模型或工作目录。Windows ARM64 暂不支持。

随包的 `vibe_agent/codex` 是固定版本、经过校验的 Codex app-server 组件，用于 ChatGPT / Codex 登录；请保留发布包中的全部文件，不能只复制编辑器或 `codex` 目录中的 EXE。

## macOS arm64 使用说明

macOS 版本仅支持 arm64（Apple 芯片）。构建并打包后，运行 `.build/VibeGodot-macOS-arm64/VibeGodot.app`，在原生项目管理器中打开或新建项目。普通编辑器启动后会自动进入顶部 **Agent** 工作区；项目管理器、无头模式和恢复模式不会启动 Agent 后台。

1. 点击顶部“设置”。API Key 模式填写兼容模型服务的接口地址、API 模型、视觉模型和 API Key；ChatGPT/Codex 模式登录后从 Codex 模型列表中选择模型，点击“保存设置”。API Key 使用 macOS 钥匙串保存（登录钥匙串中的 `Godot/VibeAgent/api_key` 条目），不写入项目、启动参数、环境变量或日志；接口地址、API 模型、视觉模型、Codex 模型和回答语言等非敏感项保存在编辑器设置中，API 与 Codex 模型键彼此独立。
2. 对话、参考图片、隔离修改、回退、Tests 与自动回修流程与 Windows 相同，操作步骤与判定规则见上方 Windows 段落。

后台组件随包位于 `VibeGodot.app/Contents/Resources/vibe_agent/godotvibe-agent/godotvibe-agent`（无 `.exe` 后缀，也不是嵌套 `.app`），为 PyInstaller onedir 组件，运行机器无需安装 Python。打包时还会把经过校验、版本固定的 Codex app-server 发布件随 `.app` 一起分发，用于 ChatGPT / Codex 登录与模型列表；使用者仍需使用完整发布包，不能只复制编辑器可执行文件。每个编辑器实例启动独立 sidecar，监听 `127.0.0.1` 随机端口并使用独立 256 位令牌；编辑器退出时先请求正常关闭，超时后强制清理。诊断日志位于当前项目 `.godot/agent/sidecar.log`。

构建命令：`bash misc/vibe_agent/build_sidecar.sh` 生成 arm64 onedir 后台，`bash misc/vibe_agent/package_macos.sh` 组装 `.build/VibeGodot-macOS-arm64/VibeGodot.app` 与 `.build/VibeGodot-macOS-arm64.zip`。当前产物未签名、未公证，仅支持 arm64；从网络下载后可能被 Gatekeeper 与 quarantine 拦截，确认来源可信后可在“系统设置 → 隐私与安全性”中允许打开，或执行 `xattr -dr com.apple.quarantine <VibeGodot.app>`；是否绕过或允许打开由用户自行决定。

# VibeGodot

基于 Godot 源码定制的游戏编辑器，将 AI Agent、玩家行为测试和失败回修集成到原生工作区中。你可以直接用自然语言描述需求，由 Agent 读取项目、修改场景与脚本，再运行游戏验证修改结果。

本仓库是 Godot 的定制分支，并非 Godot 官方发行版。目前提供 Windows 与 macOS arm64 编辑器与后台组件的构建、打包流程。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 原生 Agent 工作区 | 多轮对话、历史记录、工具执行进度、取消及失败重试 |
| Markdown 回答 | 支持标题、表格、粗体、列表、行内代码与代码块等常用格式 |
| 模型与语言设置 | 顶部“设置”按钮打开弹窗，按认证模式分组：API Key 模式配置接口地址、API 模型、视觉模型；ChatGPT/Codex 模式从 Codex 模型列表选择，两组配置互不覆盖 |
| 图片与素材 | 添加参考图片、拖放、粘贴剪贴板图片，以及导入游戏素材 |
| 隔离修改与回退 | 在项目副本中执行修改，写回前检查磁盘版本与编辑器未保存状态；支持整轮 Agent 修改回退 |
| Tests 自动验收 | 根据用户需求和实际文件差异生成测试点，执行真实 Godot 输入、状态断言、截图和视觉判断 |
| 失败审查与回修 | 先审查测试方案是否合理，再修订测试或交回 Agent 修复游戏，保留逐轮证据 |

Agent 工作区中的对话和 Tests 使用完整宽度，设置不再占用常驻侧栏。运行、停止、Game 预览、导出与 API 文档搜索使用 Godot 自带的工具栏、工作区和菜单。

## 开始使用

GitHub 仓库和“Download ZIP”提供的是源码，不包含编译好的 `.exe`。如果没有完整 Windows 发布包，请先按下方 [Windows 构建](#windows-构建) 编译并打包，再执行以下步骤。

1. 打开完整 Windows 发布包中的 `VibeGodot.exe`，通过项目管理器新建或打开项目。请保留同目录的 `vibe_agent` 等依赖文件，不要只复制编辑器 EXE。
2. 进入顶部 **Agent** 工作区，点击 **设置**。API Key 模式填写兼容模型服务的接口地址、API 模型、视觉模型和 API Key；ChatGPT/Codex 模式登录后从 Codex 模型列表中选择模型。
3. 选择回答语言并点击 **保存设置**。默认使用简体中文，已有历史不会自动翻译。API Key 模式的视觉验收使用单独的视觉模型；ChatGPT/Codex 模式只使用所选 Codex 模型声明的图片输入能力。
4. 在 **对话** 页描述任务，例如“增加玩家死亡后的复活倒计时，倒计时结束前禁用复活按钮”。
5. Agent 修改文件后，在 **Tests** 页查看测试步骤、实际状态、截图及最终结论。需要时可手动运行、重新生成或停止测试。

API Key 使用 Windows 凭据管理器保存；接口地址、API 模型、视觉模型、Codex 模型和回答语言保存在编辑器设置中，其中旧的 `vibe_agent/model` 与 `vibe_agent/vision_model` 只作为 API Key 模式的值，Codex 模型单独保存，切换模式时各自保留。“清空当前项目对话”和“回退上次 Agent 修改”也位于设置弹窗内。

发布包的使用者无需安装 Python。模型请求会发送到你配置的服务；代码上下文、需求及视觉测试截图可能作为请求内容发送。

## 账号与登录方式

Agent 支持两种模型接入方式，可在顶部 **设置** 弹窗中切换：

| 方式 | 适用平台 | 说明 |
| --- | --- | --- |
| API Key | Windows 与 macOS | 填写兼容模型服务的接口地址、API 模型、视觉模型和 API Key；模型列表来自该接口的 `models.list`，不支持时手动输入 |
| ChatGPT / Codex | Windows x86_64 与 macOS arm64（Apple 芯片） | 不需要 OpenAI API Key，使用 ChatGPT 账号登录；模型列表来自随包 Codex 的 `model/list`，图片输入按所选模型的 image modality 决定 |

**API Key 模式**：在“设置”中填写接口地址、模型名称和 API Key 后保存。侧车用该接口的 `models.list` 填充 API 模型候选；接口不支持列表时保留“手动输入模型 ID”，不会用任何默认字符串代替。API Key 由操作系统凭据存储保管（Windows 凭据管理器；macOS 登录钥匙串），不写入项目、启动参数、环境变量、SSE 或日志。视觉验收使用单独配置的“视觉模型”。

**ChatGPT / Codex 模式**：点击 **Sign in with ChatGPT**，在浏览器中完成授权；如果账号下包含多个可用账号，再按提示选择要使用的账号。登录后设置界面会显示账号元数据（如账号标识与名称），并从 Codex 的 `model/list` 列出可用模型；该模型 id 只用于 Codex，绝不会回落到 API 模式的主模型或 `claude`/`gemini` 默认值。需要时可以切换账号，或退出登录 / 撤销授权；退出或撤销后需重新登录才能继续使用该模式。该模式不需要 OpenAI API Key。

**图片与视觉能力**：API Key 模式通过接口的 `image_url` 通道发送图片；ChatGPT/Codex 模式只通过 Codex `turn/start` 的 `localImage` 发送附件，且仅当所选模型的 `inputModalities` 含 `image` 时可用。模型不支持图片时，“添加参考图片”、拖放与剪贴板入口会被禁用；即使绕过界面，侧车也会返回可读的安全错误。附件副本位于工作区副本的忽略目录，不会被当作项目改动写回。

无论使用哪种模式，OAuth、访问令牌、刷新令牌和 ID 令牌都不会写入项目文件、命令行参数、环境变量、SSE 事件或日志，仅保存在操作系统凭据存储中（Windows 凭据管理器、macOS 登录钥匙串）。界面、日志和文档不会展示任何真实令牌、授权码、Cookie 或授权地址。

Windows x86_64 与 macOS arm64 均支持 **Sign in with ChatGPT**。Windows 的 OAuth 令牌使用当前 Windows 用户的凭据管理器保存；API Key 模式仍可作为回退方式使用。Windows ARM64 暂不支持。完整发布包随附固定版本、经 SHA-256、目录结构、版本号和 PE/Mach-O 架构校验的 Codex app-server（Windows 位于 `vibe_agent/codex`，macOS 位于 `VibeGodot.app/Contents/Resources/vibe_agent/codex`），使用者必须分发完整发布包，不能只复制编辑器可执行文件。

## 自动测试与回修

每次 Agent 实际修改项目文件后，自动生成 3～6 个与修改相关的测试点，覆盖正常操作、边界行为和相关回归。没有文件修改的对话不触发测试；手动保存只会使相关验证结果过期。

执行流程：

1. 检查项目结构与脚本。
2. 在独立项目副本中注入测试桥接组件，为每个用例启动独立 Godot 进程和测试存档。
3. 执行场景加载、动作或按键输入、鼠标操作、等待和节点状态断言；涉及画面时采集截图并进行视觉判断。
4. 分别记录行为、视觉和运行诊断，汇总验收结果。
5. 失败时先审查测试合理性：确认是游戏缺陷后回修游戏；确认是测试方案问题后修订方案、复核覆盖并重新测试。证据不足或审查不可用时标记受阻。

游戏自动回修和测试自动修订分别最多 **3 轮**。回修后复跑本轮全部启用测试；修订需保留原始需求、用例身份、必选状态和视觉覆盖，不能靠删除失败用例或降低标准获得通过。

**脚本检查通过不等于行为验收通过。** 截图成功也不等于视觉验收通过。进程正常退出、记录完整且仅有退出资源未释放提示时，保留警告，不覆盖已通过的行为与视觉结论。未知运行错误、模型不可用或证据不足显示“受阻”，不会直接当作游戏缺陷回修。

Tests 支持单项运行、全部运行、重新生成、停止及高级 JSON 计划编辑。修改项目、计划或判定规则后需要重新验证；单项通过不代表整套通过。停止后保留已经完成的报告。

## 日志与证据

以下路径均相对于当前游戏项目：

| 路径 | 内容 |
| --- | --- |
| `chat_history.json` | 对话历史 |
| `.godot/agent/sidecar.log` | 后台诊断日志 |
| `.godot/agent/acceptance_plan.json` | 版本 2 验收计划 |
| `.godot/agent/test_artifacts/` | 逐轮报告、运行记录、日志、截图与独立测试存档 |
| `.godot/agent/plan_history/` | 历史测试计划 |

`runtime.json` 中视觉步骤的 `pending` 表示截图已采集、尚待判断；最终视觉结论以 `report.json` 为准。更完整的操作说明见 [Windows 使用说明](misc/vibe_agent/README.zh-CN.md) 和 [能力对应表](misc/vibe_agent/CAPABILITIES.md)。

## Windows 构建

以下步骤面向 Windows x64，所有命令在 **PowerShell** 中执行。编译需要联网下载源码和 Python 依赖，运行 Agent 还需自行配置模型服务。

### 1. 安装工具

- Git。
- Visual Studio 2022 或 Build Tools 2022，选择“使用 C++ 的桌面开发”，包含 MSVC x64/x86 编译工具和 Windows SDK。
- 64 位 Python 3.12，安装时启用 pip 并将 Python 加入 PATH。

安装后打开新的 PowerShell，确认 `git --version` 和 `python --version` 可用。若 SCons 找不到 MSVC，请从 Visual Studio 的 **Developer PowerShell** 重新执行构建命令。

### 2. 获取源码

```powershell
git clone --branch codex/agent-tests-and-settings https://github.com/Ryanssssssss/VibingGame.git
Set-Location VibingGame
```

该分支包含本文介绍的 Agent 和 Tests 功能。已有本地仓库时使用自己的工作目录，不必重复克隆。以下命令均在含有 `SConstruct` 的仓库根目录执行。

### 3. 安装构建和后台依赖

```powershell
python -m pip install --target .build/pythonlibs scons pyinstaller fastapi uvicorn openai "pydantic>=2,<3" pillow requests pygltflib python-multipart
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed" }
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
$pythonExe = (Get-Command python -CommandType Application).Source
```

依赖安装到本项目 `.build/pythonlibs`；后台打包脚本会从这个位置查找 PyInstaller。后续步骤使用同一个 Python。重新打开终端后，需要重新设置 `PYTHONPATH` 和 `$pythonExe`。

### 4. 编译编辑器

```powershell
& $pythonExe -m SCons platform=windows target=editor arch=x86_64 production=yes debug_symbols=no d3d12=no -j4
if ($LASTEXITCODE -ne 0) { throw "Editor build failed" }
```

生成 `bin/godot.windows.editor.x86_64.exe`。`-j4` 表示并行编译 4 个任务；内存不足时可改为 `-j2`。首次编译耗时较长，需要为源码、对象文件和发布包预留磁盘空间。

### 5. 打包 Agent 后台和完整发布包

```powershell
.\misc\vibe_agent\build_sidecar.ps1 -PythonExe $pythonExe
if ($LASTEXITCODE -ne 0) { throw "Sidecar build failed" }
.\misc\vibe_agent\package_windows.ps1 -PythonExe $pythonExe -SkipBuild -SkipSidecarBuild -Zip
```

`-SkipBuild` 和 `-SkipSidecarBuild` 只适用于前两步已经成功的情况。不要在编辑器或后台仍使用旧构建文件时覆盖打包目录；脚本会替换指定的输出目录，因此不要把游戏项目放入其中。

`package_windows.ps1` 会先调用 `misc/vibe_agent/fetch_codex_windows.ps1`：从 `releases.openai.com` 下载固定版本 `0.146.0` 的 Windows x86_64 Codex 包（`codex-package-x86_64-pc-windows-msvc.tar.gz`，失败时回退官方 GitHub `openai/codex` 的 `rust-v0.146.0`），先取 `codex-package_SHA256SUMS` 并用其中的 SHA-256 校验，再用 Windows 10 1803+ 自带的 `tar.exe` 解压，然后校验 `bin/codex.exe`、`codex-resources`、`codex-path`、`codex-package.json`、`codex.exe --version` 以及 PE 头 machine `0x8664`。校验通过后把整个 release root 复制到发布包的 `vibe_agent/codex`，复制完成后再校验一次。下载缓存与临时文件位于 `.build/codex`；缺少 `tar.exe`、校验失败或架构不符都会直接终止打包，不会生成不完整发布包。可用环境变量 `CODEX_VERSION`、`CODEX_RELEASE_ROOT`、`CODEX_BIN` 覆盖版本或复用已有布局，但覆盖来源同样必须通过全部校验；这些覆盖不会携带任何令牌、模型或工作目录。Windows ARM64 暂不支持。

默认输出：

```text
.build/
├── VibeGodot-Windows-x86_64.zip
└── VibeGodot-Windows-x86_64/
    ├── VibeGodot.exe
    ├── VibeGodot.console.exe
    ├── vibe_agent/
    │   ├── godotvibe-agent/
    │   │   ├── godotvibe-agent.exe
    │   │   └── _internal/
    │   └── codex/
    │       ├── bin/codex.exe
    │       ├── codex-resources/
    │       ├── codex-path/
    │       └── codex-package.json
    ├── licenses/
    └── 使用说明.md
```

解压完整 ZIP 后运行 `VibeGodot.exe`。分发时保留全部文件，不能只复制编辑器、sidecar 或 `codex` 目录中的 EXE；目标电脑无需安装 Python。构建产物不会随源码提交，游戏导出需要与定制编辑器版本匹配的导出模板。

### 常见构建问题

- **找不到 SCons / PyInstaller**：确认依赖安装成功，并在当前 PowerShell 中设置了上述 `PYTHONPATH`。
- **找不到 MSVC / Windows SDK**：检查 Visual Studio 的 C++ 工作负载和 SDK，使用 Developer PowerShell 重试。
- **找不到 `tar.exe`**：确认在 Windows 10 1803 或更高版本上运行；`tar.exe` 随系统提供，不需要额外安装，也不要改用 Git 或其他来源的 `tar`。
- **PowerShell 阻止脚本运行**：检查脚本来源后，按本机或组织的执行策略允许运行；不要修改系统安全策略来绕过组织限制。
- **后台缺失或启动失败**：确认运行的是完整发布目录，检查游戏项目中的 `.godot/agent/sidecar.log`。

## macOS arm64 构建

以下步骤面向 macOS arm64（Apple 芯片），所有命令在 **终端** 中执行，需先安装 Xcode Command Line Tools。仅支持 arm64，不提供 Intel 或 universal 版本；运行 Agent 同样需要自行配置模型服务。

### 1. 安装构建和后台依赖

```bash
python3 -m pip install --target .build/pythonlibs \
  scons pyinstaller fastapi uvicorn openai "pydantic>=2,<3" pillow requests pygltflib python-multipart
export PYTHONPATH="$PWD/.build/pythonlibs:$PWD"
```

依赖安装到本项目 `.build/pythonlibs`；后台打包脚本会从这个位置查找 PyInstaller。重新打开终端后需要重新设置 `PYTHONPATH`。

### 2. 编译编辑器

```bash
python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4
```

生成 `bin/godot.macos.editor.arm64`。`-j4` 表示并行编译 4 个任务；内存不足时可改为 `-j2`。

### 3. 打包 Agent 后台和完整应用

```bash
bash misc/vibe_agent/build_sidecar.sh
bash misc/vibe_agent/package_macos.sh
```

`build_sidecar.sh` 使用 PyInstaller 生成 arm64 onedir 后台（不生成嵌套 `.app`），`package_macos.sh` 将编辑器、后台、许可证和说明文档组装为 `VibeGodot.app` 并压缩。产物：

```text
.build/
├── VibeGodot-macOS-arm64.zip
└── VibeGodot-macOS-arm64/
    └── VibeGodot.app/
        └── Contents/
            ├── MacOS/VibeGodot
            └── Resources/
                ├── vibe_agent/godotvibe-agent/
                │   ├── godotvibe-agent
                │   └── _internal/
                ├── 使用说明.md
                └── 能力对应表.md
```

运行：

```bash
open .build/VibeGodot-macOS-arm64/VibeGodot.app
```

API Key 保存在 macOS 登录钥匙串的 `Godot/VibeAgent/api_key` 条目中，不写入项目、启动参数、环境变量或日志；接口地址、API 模型、视觉模型、Codex 模型和回答语言等非敏感项保存在编辑器设置（Editor Settings）中，API 与 Codex 模型键彼此独立。打包时会把经过校验、版本固定的 Codex app-server 发布件随 `.app` 一起分发，供 **Sign in with ChatGPT** 登录与模型列表使用；使用者仍需使用完整发布包，分发时需要保留 `.app` 内的全部文件，目标机器无需安装 Python。

> **未签名 / 未公证**：当前发布包不包含代码签名和公证，仅支持 arm64。从网络下载后 macOS Gatekeeper 可能阻止打开并标记 quarantine；确认来源可信后，可在“系统设置 → 隐私与安全性”中选择仍要打开，或执行 `xattr -dr com.apple.quarantine <VibeGodot.app>`。是否绕过或允许打开由用户自行决定。

## 开发验证

在上述 Python 依赖环境中执行：

```powershell
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
$env:GODOT_TEST_EXE="$PWD\bin\godot.windows.editor.x86_64.exe"
python -m unittest discover -s vibe_tools/tests -v
```

设置 `GODOT_TEST_EXE` 可启用真实 Godot 进程集成测试；未设置时对应测试会跳过。编排回归使用离线模型替身，不能据此宣称真实主模型或视觉服务调用已验证；外部模型链路需使用实际配置单独测试。

## 项目结构

- `editor/plugins/agent_editor_plugin.*`：原生 Agent 与 Tests 界面。
- `vibe_tools/editor_sidecar.py`：编辑器接口、任务事件与修改／测试／回修编排。
- `vibe_tools/editor_*.py`：隔离工作区、测试计划、行为执行、判定、合理性审查及语言设置。
- `vibe_tools/tests/`：Python 回归与 Godot 进程集成测试。
- `misc/vibe_agent/`：Windows 构建、打包脚本与使用文档。

## 上游与许可证

VibeGodot 基于 [Godot Engine](https://godotengine.org) 开发，保留 Godot 的原生 2D、3D 编辑能力。引擎源码采用 [MIT 许可证](LICENSE.txt)；贡献者及第三方版权信息见 [AUTHORS.md](AUTHORS.md)、[COPYRIGHT.txt](COPYRIGHT.txt) 和 [LOGO_LICENSE.txt](LOGO_LICENSE.txt)。随包依赖保留各自许可证。

本分支特有问题请在本仓库反馈；通用 Godot 资料可参考官方文档与上游项目。

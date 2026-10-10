# macOS arm64 原生 VibeGodot Agent 迁移设计

- 日期：2026-10-10
- 状态：设计定稿，等待实现（本文档不包含任何实现改动）
- 目标平台：macOS 15 及以上 / Apple Silicon（arm64）
- 上游基线：`0c92b253161d8db9c0b7a700e5cded701b26c7ba`
- 本轮范围：只落 API Key 凭据；只做 arm64；不涉及 OAuth、代码签名/公证、旧 Web 入口

本设计把现有仅支持 Windows 的原生 VibeGodot Agent（`editor/plugins/agent_editor_plugin.*` +
`vibe_tools/editor_sidecar.py`）迁移到 macOS arm64。核心原则是：**只改“平台相关的启动与密钥存储”，
不改协议、不改编排、不改 Windows 行为。**

---

## 1. 推荐方案摘要

1. **Sidecar 可执行文件位置**：macOS 使用 PyInstaller **onedir（无 `.app` 包装）**，产物目录放到
   `VibeGodot.app/Contents/Resources/vibe_agent/godotvibe-agent/`。编辑器优先用
   `OS::get_singleton()->get_bundle_resource_dir()` 定位 `Contents/Resources`，再回退到开发目录
   （`.build/sidecar-dist/...`）与开发虚拟环境（`.venv/bin/python` + `vibe_tools/editor_sidecar.py`）。
   Windows 分支原样保留。
2. **不要用嵌套 `.app` 作为 sidecar**：`OS_MacOS::create_process()` 在 `p_path` 是 bundle 时会走
   `NSWorkspace`（LaunchServices，异步、进程归属不同），会破坏“同步拿到子进程 PID + 握手校验”的设计。
   传纯 Mach-O 可执行文件才会走 `OS_Unix::create_process()`。这是选择 onedir 目录而非 `.app` 的硬性原因。
3. **凭据抽象**：`vibe_tools/editor_credentials.py` 保留唯一公开函数 `credential(name, value=None)`，
   内部按平台分派。Windows 继续用 Credential Manager（逻辑逐字不变）；macOS 用
   **Security.framework（ctypes 直接调用）** 读写登录钥匙串，不使用 `security` CLI，避免密钥出现在进程
   `argv`（Apple 帮助文本本身也警告 `-w` “insecure”）。两个平台都**没有明文回退**。
4. **握手/端口/token/父进程监控协议不变**：`protocol_version = 1`、`127.0.0.1` 随机端口、256 位令牌、
   `.godot/agent/instances/handshake-<pid>.json`、`POST /v1/shutdown` 正常关闭 + 超时强杀、
   `_watch_parent()` 看门狗全部保持。仅 `/v1/configure` 响应**增加可选的 `credential_store` 字段**。
5. **构建/打包新增 macOS 脚本**，Windows 的 `.ps1` 完全不动；新增
   `misc/vibe_agent/build_sidecar.sh` 与 `misc/vibe_agent/package_macos.sh`。

---

## 2. 当前 Windows 假设（迁移必须先消化的耦合点）

| # | 位置 | Windows 假设 | macOS 影响 |
| --- | --- | --- | --- |
| A1 | `editor/plugins/agent_editor_plugin.cpp:989-1002` | 安装目录固定 `vibe_agent/godotvibe-agent/godotvibe-agent.exe` | macOS 安装形态是 `.app`，sidecar 在 `Contents/Resources`，且没有 `.exe` |
| A2 | 同上 `:995` | 开发产物固定 `.build/sidecar-dist/godotvibe-agent/godotvibe-agent.exe` | macOS onedir 文件名无 `.exe` |
| A3 | 同上 `:999-1000` | 开发虚拟环境固定 `.venv/Scripts/pythonw.exe` | macOS 是 `.venv/bin/python`，没有 `Scripts/`、通常也没有 `pythonw` |
| A4 | 同上 `:1004` | 错误提示写死“缺少 godotvibe-agent.exe；…build_sidecar.ps1” | macOS 需要平台化提示与脚本名 |
| A5 | `editor/plugins/agent_editor_plugin.cpp:309` | 占位文案写死“已保存到 Windows 凭据管理器” | macOS 应为“macOS 钥匙串” |
| A6 | `vibe_tools/editor_credentials.py:1-42` | 只实现 `CredWriteW`/`CredReadW`，非 `nt` 直接抛错 | macOS 上任何模型请求都会失败 |
| A7 | `misc/vibe_agent/build_sidecar.ps1:23-39` | `--windowed`、`--add-data "src;dest"`、校验 `.exe` | macOS 的 `--windowed` 会生成 `.app`，分隔符是 `:` |
| A8 | `misc/vibe_agent/package_windows.ps1:52-57` | 拷贝 `VibeGodot.exe` 与 `vibe_agent/godotvibe-agent` | macOS 需要组装 `.app` 并把 sidecar 放进 `Contents/Resources` |
| A9 | `misc/vibe_agent/README.zh-CN.md`、`README.md`、`CAPABILITIES.md` | 全部面向 Windows 构建/使用 | 需要新增 macOS 章节（Windows 章节保留） |

已经跨平台、**无需改动**的部分（实现时要显式确认，避免误改）：

- `vibe_tools/editor_sidecar.py:697-715` `_parent_alive()` 已在非 Windows 分支使用
  `os.kill(pid, 0)`，macOS 可直接用。
- `vibe_tools/godot_runner.py:44-51` 的 `_auto_detect()` 已包含 `godot.macos.editor.arm64`；
  `:371-380` 的 `_web_export_templates_dir()` 已处理 `darwin`。
- `vibe_tools/editor_sidecar.py` 的 `socket.bind(("127.0.0.1", 0))`、`os.urandom(32).hex()`、
  握手文件写入、`atexit` 清理、`uvicorn` 启动均与平台无关。
- `editor/editor_node.cpp:9278-9283` 注册逻辑与平台无关（无头/恢复模式不启动 Agent）。
- `vibe_tools/editor_behavior.py:47-48` 等处的 `CREATE_NO_WINDOW` 仅在 `os.name == "nt"` 生效，
  macOS 传 0，无需改。

---

## 3. 目标架构与不变式

```
VibeGodot.app/
  Contents/
    Info.plist
    MacOS/VibeGodot                     ← 定制编辑器（arm64 Mach-O）
    Resources/
      vibe_agent/godotvibe-agent/        ← PyInstaller onedir（arm64 Mach-O，无 .app 包装）
        godotvibe-agent
        _internal/...
```

进程/信任边界与 Windows 完全一致：

- 编辑器实例 → 启动 1 个 sidecar 子进程（每个编辑器实例独立）。
- sidecar 只监听 `127.0.0.1` 随机端口，只持有本实例令牌。
- 编辑器用握手文件里的 `pid`、`port`、`token` 校验子进程身份。
- API Key 只存在操作系统密钥库，以及 sidecar 进程内存中。

**不变式（实现必须全部满足）：**

1. `protocol_version` 保持 `1`；`/v1/health`、`/v1/configure`、`/v1/chat/history` 等既有端点语义不变。
2. 握手校验保持：`protocol == 1`、`1024 <= port <= 65535`、`reported_pid == sidecar_pid`、
   `token.length() == 64`。
3. 正常关闭走 `POST /v1/shutdown`（Bearer 令牌），超时后再 `kill()`。
4. 父进程退出后 sidecar 看门狗自动退出。
5. 非敏感配置（`base_url`、`model`、`vision_model`、`response_language`）仍存 Editor Settings
   `vibe_agent/*`；API Key 绝不写入项目文件、启动参数、环境变量或日志。
6. Windows 分支、Windows 脚本、Windows 文案逐字不变。

---

## 4. Sidecar 路径解析（推荐方案）

### 4.1 候选顺序

以 `exe_dir = OS::get_singleton()->get_executable_path().get_base_dir()` 为基准，按顺序取第一个存在者：

1. **macOS 安装态（`.app`）**：
   `OS::get_singleton()->get_bundle_resource_dir().path_join("vibe_agent/godotvibe-agent/godotvibe-agent")`
   - `get_bundle_resource_dir()` 在 macOS 通过 `[NSBundle mainBundle] resourcePath` 返回
     `.../Contents/Resources`（`platform/macos/os_macos.mm:454-463`）。在非 bundle 的开发态它可能返回
     包含可执行文件的目录，因此取不到就自然回退。
   - 该 API 在 `core/os/os.h:318` 声明，非 macOS 返回空串，可安全调用而不需要 `#if`。
2. **Windows 安装态（保持原文）**：`exe_dir/vibe_agent/godotvibe-agent/godotvibe-agent.exe`。
3. **开发态 onedir**：`repository/.build/sidecar-dist/godotvibe-agent/godotvibe-agent`（Windows 加 `.exe`），
   其中 `repository = exe_dir.get_base_dir()`。
4. **开发态虚拟环境 + 脚本**：
   - macOS：`repository/.venv/bin/python`（不存在时再试 `repository/.venv/bin/python3`）
     + `repository/vibe_tools/editor_sidecar.py`。
   - Windows：`repository/.venv/Scripts/pythonw.exe` + 同上的脚本路径。

说明：开发态裸二进制位于 `<repo>/bin/godot.macos.editor.arm64`，因此
`exe_dir.get_base_dir()` 正好是仓库根，和 Windows 的 `bin\` 约定对称。

### 4.2 C++ 结构（设计伪代码，非最终实现）

```cpp
void AgentWorkspace::_start_sidecar() {
    const String exe_dir = OS::get_singleton()->get_executable_path().get_base_dir();
    const String repository = exe_dir.get_base_dir();
    String executable;
    String script;

    // 1) macOS .app bundle: Contents/Resources/vibe_agent/godotvibe-agent/godotvibe-agent
    const String bundle_resource_dir = OS::get_singleton()->get_bundle_resource_dir();
    if (!bundle_resource_dir.is_empty()) {
        const String bundled = bundle_resource_dir.path_join("vibe_agent/godotvibe-agent/godotvibe-agent");
        if (FileAccess::exists(bundled)) {
            executable = bundled;
        }
    }

#if defined(WINDOWS_ENABLED)
    if (executable.is_empty()) {
        // 与当前实现逐字一致的三段回退：安装目录 -> .build -> .venv
        ...
    }
#elif defined(MACOS_ENABLED)
    if (executable.is_empty()) {
        const String dev_onedir = repository.path_join(".build/sidecar-dist/godotvibe-agent/godotvibe-agent");
        const String dev_python = repository.path_join(".venv/bin/python");
        const String dev_python3 = repository.path_join(".venv/bin/python3");
        if (FileAccess::exists(dev_onedir)) {
            executable = dev_onedir;
        } else if (FileAccess::exists(dev_python) || FileAccess::exists(dev_python3)) {
            executable = FileAccess::exists(dev_python) ? dev_python : dev_python3;
            script = repository.path_join("vibe_tools/editor_sidecar.py");
        }
    }
#endif

    if (executable.is_empty() || (!script.is_empty() && !FileAccess::exists(script))) {
        _set_status(_u8("缺少 Agent 后台；请先运行 build_sidecar.sh（macOS）或 build_sidecar.ps1（Windows），"
                        "或配置仓库 .venv"),
                    true);
        return;
    }
    // 其余参数（--project/--parent-pid/--handshake/--godot-exe）与现有实现完全相同。
}
```

要点：

- 传参、`create_process(executable, arguments, &sidecar_pid, false)` 与现有实现一致；`--godot-exe`
  继续传 `OS::get_singleton()->get_executable_path()`。
- `.app` 场景下 `--godot-exe` 是 `.../Contents/MacOS/VibeGodot`，`GodotRunner` 可用它跑
  `--headless --editor --import` 与项目进程；验收测试不依赖导出模板。
- **禁止**把 sidecar 做成 `.app`：`OS_MacOS::create_process()`（`platform/macos/os_macos.mm:798-857`）
  对 bundle 会走 `NSWorkspace`，路径为纯 Mach-O 时才走 `OS_Unix::create_process()`，后者才能同步返回
  子进程 PID 供握手校验。
- 平台分支宏使用构建系统已有的 `WINDOWS_ENABLED` / `MACOS_ENABLED`
  （`platform/macos/detect.py:211` 追加 `MACOS_ENABLED`）。

### 4.3 错误提示与占位文案

- 失败提示按平台展示脚本名（macOS `build_sidecar.sh`，Windows `build_sidecar.ps1`），其余诊断路径不变。
- 设置弹窗占位符来源改为 sidecar 返回的 `credential_store`：
  - `"windows"` → `已保存到 Windows 凭据管理器`（与现状逐字相同）
  - `"macos"` → `已保存到 macOS 钥匙串`
  - 无字段（旧 sidecar）→ 回退到 Windows 文案，保证旧版兼容。

---

## 5. 凭据抽象（本轮仅 API Key）

### 5.1 模块接口（`vibe_tools/editor_credentials.py`）

保持导入方不变（`vibe_tools/editor_sidecar.py:38` 的 `from vibe_tools.editor_credentials import credential`）：

```python
SERVICE_PREFIX = "Godot/VibeAgent/"
ACCOUNT = "VibeAgent"

class CredentialError(RuntimeError):
    """密钥库不可用/拒绝访问；不包含密钥内容。"""

def credential(name: str, value: str | None = None) -> str: ...
def credential_backend() -> str: ...   # "windows" | "macos"
```

- `value is None`：读取；不存在返回 `""`；存在返回明文。
- `value is not None`：写入并返回 `value`。
- `name` 只允许 `api_key`（本轮），拼接为 `Godot/VibeAgent/api_key`。
- 非 Windows、非 macOS：抛 `CredentialError`（保持“无明文回退”）。
- `credential_backend()` 供 sidecar 在 `/v1/configure` 响应里回填 `credential_store`。

### 5.2 Windows（行为保持不变）

把现有实现原样抽成 `_windows_credential(name, value)`：

- 结构与字段、`target = "Godot/VibeAgent/" + name`、`UserName="VibeAgent"`、`Type=1`、
  `Persist=2`、`UTF-16LE` 编码、错误码 `1168`（`ERROR_NOT_FOUND`）→ 返回 `""`，全部不变。
- 仅把入口的 `os.name != "nt"` 早退判断上移到 `credential()` 的分派层；Windows 上执行路径等价。

### 5.3 macOS Keychain（推荐实现）

用 `ctypes` 直接调用 Security.framework，避免子进程与 `argv` 泄露：

- 库：`ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")`。
- service = `"Godot/VibeAgent/" + name`，account = `"VibeAgent"`，与 Windows 的 target 命名对齐。
- 值编码：UTF-8 原始字节（经 `SecKeychain*GenericPassword` 存取），与 Windows 的 UTF-16LE 不同，
  但对外行为一致（读出同一明文）。
- 使用的 API：
  - 写：`SecKeychainAddGenericPassword`；返回 `errSecDuplicateItem (-25299)` 时用
    `SecKeychainFindGenericPassword` 取到 item，再 `SecKeychainItemModifyContent` 覆盖（等价于
    `security -U`）。结束后 `SecKeychainItemFreeContent` / `CFRelease` 释放。
  - 读：`SecKeychainFindGenericPassword`；成功后拷贝缓冲区、`SecKeychainItemFreeContent` 释放。
- 状态码映射：
  - `errSecSuccess (0)`：成功。
  - `errSecItemNotFound (-25300)`：读取时返回 `""`（视为未配置）。
  - `errSecDuplicateItem (-25299)`：仅写路径用于转更新。
  - `errSecAuthFailed (-25293)`、`errSecInteractionNotAllowed (-25308)`、`errSecUserCanceled (-128)`
    等：抛 `CredentialError`，消息只带函数名与状态码，绝不带值。
- 不采用 `security` CLI：`security help add-generic-password` 明确提示“Use of the -p or -w options is
  insecure”，密码会短暂出现在 `argv`（同用户可 `ps` 看到）；也不引入第三方 `keyring` 依赖以降低
  PyInstaller 打包与供应链成本。

### 5.4 sidecar 的读写接线

- `GET /v1/configure`：读取 `credential("api_key")`，返回
  `{..., "has_api_key": bool, "credential_store": credential_backend(), "protocol_version": 1}`。
  - 读取抛 `CredentialError` 时不要 500：返回 `has_api_key=false` 并附
    `credential_error=<无密钥内容的简短原因>`，保证设置弹窗仍能打开。
- `POST /v1/configure`：`request.api_key` 非空时写入；写入失败返回 HTTP 500，`detail` 为中文可读原因
  （如“无法写入 macOS 钥匙串（状态码 -25308）”），编辑器沿用现有“后台请求失败（HTTP %d）”提示。
- 非敏感字段仍存 `Runtime.config` 并由编辑器持久化到 Editor Settings。
- `protocol_version` 保持 `1`，新增字段是可选项；旧编辑器忽略新字段不受影响。

---

## 6. macOS arm64 构建与打包

### 6.1 编辑器（arm64）

```bash
python -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4
```

产物：`bin/godot.macos.editor.arm64`。参考 `.github/workflows/macos_builds.yml` 的 macOS 依赖/
AccessKit/Vulkan 可选步骤；Metal 为默认渲染后端，Vulkan 可选，与本迁移无强耦合。**不产出
`universal` 二进制，不做 `lipo`。**

### 6.2 Sidecar（PyInstaller onedir，arm64）

新增 `misc/vibe_agent/build_sidecar.sh`，参数与 Windows 对齐，但：

- `--onedir --console`（等价于不加 `--windowed`）：macOS 上 `--windowed` 会生成 `.app` bundle，
  必须避免（见 4.2）。`--console` 产物是普通目录：
  `.build/sidecar-dist/godotvibe-agent/godotvibe-agent` + `_internal/`。
- `--target-arch arm64`（旧版 PyInstaller 写作 `--target-architecture`）。
- `--add-data "src:dest"` 用冒号分隔（Windows 是分号）。
- 依赖根 `.build/pythonlibs`，`PYTHONPATH=".build/pythonlibs:$repo"`。
- 其余 flag 与 Windows 保持一致：`--collect-submodules vibe_tools.templates`、
  `--collect-all openai --collect-all uvicorn`、排除 `pandas/openpyxl/lxml/numpy`、
  `uvicorn.*` hidden-import、prompts/api_cache 数据、入口 `vibe_tools/editor_sidecar.py`。
- 结束断言产物可执行（`-x`）。

`misc/vibe_agent/build_sidecar.ps1` 不改。

### 6.3 组装 `VibeGodot.app`

新增 `misc/vibe_agent/package_macos.sh`：

1. 以模板 `misc/dist/macos_tools.app` 为骨架拷成 `.build/VibeGodot-macOS-arm64/VibeGodot.app`
   （模板当前只有 `Contents/Info.plist`、`PkgInfo`、`Resources/`，需新建 `Contents/MacOS/`）。
2. `bin/godot.macos.editor.arm64` → `Contents/MacOS/VibeGodot`。
3. `.build/sidecar-dist/godotvibe-agent` → `Contents/Resources/vibe_agent/godotvibe-agent`。
4. 用 `/usr/libexec/PlistBuddy` 设置 `CFBundleExecutable=VibeGodot`、`CFBundleName=VibeGodot`、
   `CFBundleIdentifier=com.vibegodot.editor`（模板其余键保留）。
5. 拷贝 `LICENSE.txt`、`misc/vibe_agent/README.zh-CN.md`（→`使用说明.md`）、
   `misc/vibe_agent/CAPABILITIES.md`（→`能力对应表.md`）到输出根。
6. `ditto -c -k --sequesterRsrc --keepParent VibeGodot.app VibeGodot-macOS-arm64.zip` 生成可分发包。

**不做**代码签名、公证、`codesign --deep`、加固运行时/entitlements。直接从本机构建运行不受影响；
经浏览器下载会带 quarantine，Gatekeeper 可能拦截，属已知限制（用户可自行 `xattr -dr
com.apple.quarantine`），不在本轮解决。

`misc/vibe_agent/package_windows.ps1` 不改。

---

## 7. Windows 行为保持不变

- `_start_sidecar()` 的 Windows 分支保持安装目录 → `.build/sidecar-dist` → `.venv/Scripts/pythonw.exe`
  三段回退、`.exe` 文件名、错误文案、`create_process` 调用不变。
- 设置占位符在 Windows 仍为“已保存到 Windows 凭据管理器”（由 `credential_store == "windows"` 映射）。
- `editor_credentials.py` 的 `CredWriteW`/`CredReadW` 语义（target、UTF-16LE、Persist、1168）不变。
- `build_sidecar.ps1`、`package_windows.ps1` 不改。
- `/v1/configure` 仅新增可选字段，旧客户端不受影响。

---

## 8. 非目标（明确排除）

- **Intel x86_64 / universal2**：只出 arm64，不使用 `--target-arch universal2`，不 `lipo`。
- **Plus / ChatGPT OAuth 登录**：本轮只实现 API Key。后续阶段将把 OAuth 刷新令牌放在独立 service 名
  （如 `Godot/VibeAgent/oauth_refresh_token`）下复用同一 `credential()` 抽象；本轮不写相关代码。
- **代码签名 / 公证 / 加固运行时 / entitlements**：不做。
- **旧 Web 入口**：不改 `vibe_tools/web_server.py`、`vibe_tools/static/`、`vibe_tools/templates/`；
  它们只作为回归对照保留。
- **Windows Web/Windows 导出工具链**：`export_project_windows` 等保持现状；在 macOS 上导出 Windows
  目标需要 Windows 导出模板，属 Godot 原生行为，不在本迁移范围。验收测试直接运行编辑器二进制，
  不依赖导出模板。

---

## 9. 数据与进程流

### 9.1 启动

1. 编辑器非无头启动，`AgentWorkspace` 收到 `NOTIFICATION_READY` → `_start_sidecar()`。
2. 解析 sidecar 可执行文件（第 4 节候选顺序）。
3. 建 `.godot/agent/instances/`，生成 `handshake-<editor_pid>.json` 的路径（先删旧文件）。
4. `create_process(sidecar, [script?, --project, --parent-pid, --handshake, --godot-exe], &sidecar_pid, false)`。
5. sidecar：解析参数 → 校验 `project.godot` → 生成 `token = os.urandom(32).hex()` →
   建立随机本地监听端口 → 原子写握手文件（`protocol_version/port/pid/token`）→ 启动 uvicorn →
   启动 `_watch_parent` 看门狗。
6. 编辑器轮询到握手文件后 `_read_handshake()` 校验四项，通过后 `GET /v1/health`（Bearer token），
   再 `_push_configuration(false)`（不下发 Key）。

### 9.2 配置与 API Key

1. 用户在设置弹窗填写 base_url/model/vision_model/语言/API Key，点保存。
2. 编辑器 `POST /v1/configure`，`api_key` 仅在非空时携带；非空时 sidecar 写入系统密钥库。
3. sidecar 返回 `has_api_key` 与 `credential_store`；编辑器持久化非敏感字段到 Editor Settings，
   并把占位符更新为“已保存到 …凭据管理器/钥匙串”。
4. 每次模型调用前 sidecar 现读 `credential("api_key")`；缺失则相关请求返回错误，不静默回退。

### 9.3 退出

1. 编辑器 `_stop_sidecar()`：`POST /v1/shutdown`（Bearer）→ 等待退出（约 800 ms）→ 仍存活则 `kill()`。
2. sidecar `atexit` 与看门狗都会删除握手文件；父进程消失时看门狗令 `server.should_exit = True`。

---

## 10. 错误处理与回退策略

| 场景 | 行为 |
| --- | --- |
| 找不到 sidecar 可执行文件 | 状态栏按平台提示运行对应构建脚本或配置 `.venv`；不崩溃 |
| sidecar 启动后异常退出 | 现有 `is_process_running` 检测 → “后台异常退出；可点击顶部重启后台” |
| 握手文件缺失/损坏 | 保持现状：不连接，超时报“启动超时”，日志在 `.godot/agent/sidecar.log` |
| 握手字段校验失败 | 保持现状：拒绝（协议/端口/PID/token 四项） |
| 密钥库无该条目 | `credential()` 返回 `""`；`has_api_key=false`；占位符“输入 API Key” |
| 钥匙串被锁/拒绝/取消 | 读：`GET /v1/configure` 仍返回 200，附 `credential_error`，UI 可打开；写：HTTP 500 中文原因 |
| 非 macOS/Windows 平台 | `CredentialError`，不提供明文回退 |
| sidecar 崩溃或端口占用 | 每实例随机端口 + 独立 token，天然隔离；重启按钮重建 |
| `.app` 被 Gatekeeper 隔离 | 已知限制，文档提示；不做签名/公证 |

**回退层级**：macOS sidecar 路径四段回退（bundle → 旧 Windows 安装目录（不命中）→ 开发 onedir →
开发 venv）；凭据无回退（只有系统密钥库）。任何情况下都**不写明文密钥**。

---

## 11. 改动文件清单（未来实现的精确范围）

| 文件 | 类型 | 改动 | 影响 Windows |
| --- | --- | --- | --- |
| `editor/plugins/agent_editor_plugin.cpp` | 改 | `_start_sidecar()` 平台化路径解析与错误文案；占位符按 `credential_store` 选择 | 否（分支内逐字保留） |
| `editor/plugins/agent_editor_plugin.h` | 改 | 视需要新增 1 个私有辅助方法声明（如 `_credential_store_label()`） | 否 |
| `vibe_tools/editor_credentials.py` | 改 | 平台分派；抽出 `_windows_credential`；新增 `_macos_credential`、`credential_backend`、`CredentialError` | 否（语义等价） |
| `vibe_tools/editor_sidecar.py` | 改 | `/v1/configure` GET/POST 返回 `credential_store`；写入失败 HTTP 500；读取失败降级字段 | 否（仅新增字段） |
| `misc/vibe_agent/build_sidecar.sh` | 新增 | macOS PyInstaller onedir 构建 | 否 |
| `misc/vibe_agent/package_macos.sh` | 新增 | 组装 `VibeGodot.app` + zip | 否 |
| `vibe_tools/tests/test_editor_credentials.py` | 新增 | 分派/target 命名/密钥库状态码映射/写读参数构造 | 否 |
| `vibe_tools/tests/test_editor_sidecar.py` | 改 | 断言 `/v1/configure` 含 `credential_store`，错误映射 | 否 |
| `README.md` | 改 | 新增 macOS 构建/使用章节（Windows 章节保留） | 否 |
| `misc/vibe_agent/README.zh-CN.md` | 改 | 新增 macOS 使用说明（Windows 内容保留） | 否 |
| `misc/vibe_agent/CAPABILITIES.md` | 改 | 凭据/分发行补充 macOS Keychain 与 arm64 | 否 |

**明确不改**：`misc/vibe_agent/build_sidecar.ps1`、`misc/vibe_agent/package_windows.ps1`、
`vibe_tools/godot_runner.py`、`vibe_tools/editor_behavior.py`、`vibe_tools/editor_harness.py`、
`vibe_tools/web_server.py`、`vibe_tools/static/`、`vibe_tools/templates/`、`editor/editor_node.cpp`。

（本设计文档自身即本轮唯一新增文件：`docs/superpowers/specs/2026-10-10-macos-agent-arm64-design.md`。）

---

## 12. 测试策略

### 12.1 与主机无关的 Python 单元测试

- `credential_backend()`：mock `sys.platform`/`os.name`，断言 `windows`/`macos`，其余抛错。
- target/service 命名：断言 `Godot/VibeAgent/api_key`，Windows target 与 macOS service 一致。
- macOS 写路径：mock Security.framework 绑定，断言首次 `Add`、重复 `errSecDuplicateItem` 后走
  `Find` + `Modify`，并释放 item/content（无泄漏断言以调用计数为准）。
- macOS 读路径：`errSecItemNotFound` → `""`；`errSecAuthFailed` → `CredentialError`；
  成功时按 UTF-8 解码。
- 不存在明文回退：任何非 0/非 not-found 状态都必须抛错或返回空，绝不落盘。
- sidecar：`GET/POST /v1/configure` 的 `credential_store`、`has_api_key`、写入失败 500 的 detail；
  现有 `test_editor_sidecar.py`、`test_editor_workflow.py` 继续通过（它们 patch 了 `credential`）。

### 12.2 macOS 集成测试（`skipUnless(sys.platform == "darwin")`）

- 真实钥匙串往返：使用唯一一次性 service（如 `Godot/VibeAgent/test-<uuid>`）写入合成值、读回、
  `finally` 中删除；不接触真实 `api_key`，不打印值。
- 启动真实 sidecar 子进程（`sys.executable + vibe_tools/editor_sidecar.py`，临时项目），验证握手文件、
  端口范围、token 长度、`/v1/health`、`/v1/shutdown`、父进程退出看门狗。
- 路径解析：在临时目录伪造 `.app/Contents/Resources/vibe_agent/...` 结构，断言编辑器选择的路径
  （C++ 逻辑以人工/脚本验收为主，Python 侧只验证目录约定）。

### 12.3 Godot 进程集成

- `GODOT_TEST_EXE="$PWD/bin/godot.macos.editor.arm64" python -m unittest discover -s vibe_tools/tests -v`
  启用真实进程用例（导入、运行、截图、超时、取消、失败→回修→回退）。
- 模型/视觉使用离线替身；真实服务另行手动验证。

### 12.4 Windows 回归

- 在同一 Python 套件于 Windows 上运行，确认 `test_editor_credentials` 的 Windows 分支与
  `/v1/configure` 行为不变；在 Windows 跑 `build_sidecar.ps1` + `package_windows.ps1` 冒烟。

---

## 13. 验收命令

### 13.1 macOS arm64（实现后在本机执行）

```bash
# 依赖
python -m pip install --target .build/pythonlibs scons pyinstaller fastapi uvicorn openai \
  "pydantic>=2,<3" pillow requests pygltflib python-multipart
export PYTHONPATH="$PWD/.build/pythonlibs:$PWD"

# 1) 编辑器
python -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4
test -f bin/godot.macos.editor.arm64

# 2) sidecar
bash misc/vibe_agent/build_sidecar.sh
test -x .build/sidecar-dist/godotvibe-agent/godotvibe-agent

# 3) 打包
bash misc/vibe_agent/package_macos.sh
test -x ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/MacOS/VibeGodot"
test -x ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Resources/vibe_agent/godotvibe-agent/godotvibe-agent"

# 4) Python 回归 + Godot 集成
GODOT_TEST_EXE="$PWD/bin/godot.macos.editor.arm64" python -m unittest discover -s vibe_tools/tests -v
```

手动验收：

```bash
open .build/VibeGodot-macOS-arm64/VibeGodot.app
```

1. 顶部 Agent → 设置，填模型服务与 API Key，保存后占位符显示“已保存到 macOS 钥匙串”。
2. 仅验证条目存在（**不要打印值**）：
   `/usr/bin/security find-generic-password -s Godot/VibeAgent/api_key >/dev/null; echo $?`（0 表示存在）。
3. 发起一轮对话并触发一次 Tests；确认 `.godot/agent/sidecar.log` 无握手/密钥错误。
4. 正常退出编辑器，确认无残留 `godotvibe-agent` 进程。

### 13.2 Windows 回归（Windows 主机）

```powershell
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
python -m unittest discover -s vibe_tools/tests -v
.\misc\vibe_agent\build_sidecar.ps1
.\misc\vibe_agent\package_windows.ps1 -SkipBuild -SkipSidecarBuild -Zip
```

### 13.3 本轮文档提交的校验

```bash
git status --short
git diff --check -- docs/superpowers/specs/2026-10-10-macos-agent-arm64-design.md
test -s docs/superpowers/specs/2026-10-10-macos-agent-arm64-design.md
```

---

## 14. 无法在非 macOS 主机完成的验证

| 验证项 | 原因 | 可在哪完成 |
| --- | --- | --- |
| 编译/链接 `bin/godot.macos.editor.arm64` | 需要 macOS SDK、Xcode CLT、arm64 工具链 | macOS arm64 |
| PyInstaller 生成 Mach-O onedir（`--target-arch arm64`） | 交叉生成 Mach-O 不在本轮范围 | macOS arm64 |
| `SecKeychainAddGenericPassword` / `Find` / `Modify` 真实往返 | Security.framework 仅 macOS | macOS（集成测试） |
| `get_bundle_resource_dir()` 与 `.app` 路径解析 | `NSBundle` 仅 macOS | macOS |
| `create_process` 走 `OS_Unix` 而非 `NSWorkspace`（PID 正确） | 平台实现差异 | macOS |
| `.app` 组装、`PlistBuddy`、`ditto`、quarantine 行为 | macOS 专用工具 | macOS |
| Windows Credential Manager 语义与 PowerShell 打包 | 仅 Windows | Windows |
| 真实主模型/视觉服务链路 | 需用户自己的密钥与网络，从不在自动化测试中 | 人工，任一平台（不记录密钥） |

说明：本次设计编写主机为 macOS 15.3.1 arm64，因此实现完成后 13.1 的命令可在同一类环境执行；
但**本提交只包含设计文档**，上述运行时验证都还没有可测的实现改动，故当前均未执行。

---

## 15. 风险与后续阶段

- **钥匙串访问提示**：sidecar 未签名，重建后代码签名身份变化，读取既有条目时 macOS 可能弹窗要求授权
  （需选择“始终允许”）。这是签名/公证排除在外的直接后果；文档需说明。
- **PID 复用**：看门狗用 `os.kill(pid, 0)`，理论上父 PID 被复用会延迟退出；与现有 Linux 行为一致，
  概率极低，接受。
- **后续阶段（OAuth，非本轮）**：在 `credential()` 抽象上新增独立 service 名存放刷新令牌，登录流程作为
  独立 UI/端点加入；不改本轮的 API Key 路径与协议。

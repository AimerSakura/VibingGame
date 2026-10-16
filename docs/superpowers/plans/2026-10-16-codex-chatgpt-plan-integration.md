# macOS arm64 优先接入 Sign in with ChatGPT（Codex app-server）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 macOS arm64 上让 VibeGodot Agent 支持 Sign in with ChatGPT：用户无需 API Key，浏览器完成 ChatGPT 计划授权后，由 `codex app-server`（stdio JSONL）驱动当前 Godot 工程workspace；同时完整保留现有 API Key 模式与 Windows 行为。

**Architecture:** 在 sidecar 内新增独立的 `codex_auth`（OAuth/SIWC + 令牌保管）与 `codex_app_server`（JSON-RPC over stdio）两层，sidecar 依据 `auth_mode` 在既有 `SimpleLLMProvider`（API Key）与新的 Codex app-server 通道之间切换；`ACCESS_TOKEN` 只注入 app-server 子进程环境变量；令牌仅存 OS 密钥库，绝不落项目文件、argv 或日志。编辑器只新增“账号/模式”UI 与轮询，不改握手、端口、token、SSE 基础契约。

**Tech Stack:** Godot C++（`editor/plugins/agent_editor_plugin.*`）、Python 3、FastAPI/uvicorn、stdlib `http.server`/`urllib`/`secrets`/`hashlib`、`cryptography`（JWKS 验签）、Codex app-server（stdio JSONL）、macOS Security.framework（沿用既有 `editor_credentials`）、PyInstaller onedir、unittest。

---

## Global Constraints

- **本计划提交本身只新增一个文件**：`docs/superpowers/plans/2026-10-16-codex-chatgpt-plan-integration.md`。实现阶段才按“文件级改动范围”修改源码；本提交不碰任何源码、测试、构建文件或其它文档。
- **凭据铁律（实现与本计划共同遵守）**：不读取、不写入、不复制、不打印、不上传任何真实 `access_token` / `refresh_token` / `id_token` / API Key；示例、测试与日志一律使用占位符或测试替身；任何日志、异常消息、断言失败输出都不得包含令牌明文。实现阶段的人工验收也只做“条目存在性/长度”检查，不 `cat`/`echo` 令牌。
- **保护既有脏改动**：开始任何实现前先 `git status --short`；不得 `git reset`/`git checkout`/`git stash`/`git clean` 或覆盖非本任务创建的文件与分支。本计划的改动与既有改动若有冲突，停下报告，不擅自回退。
- **平台优先级**：macOS arm64 为第一优先级并端到端可验证；Windows 保持现状（API Key 模式、`.ps1` 脚本、文案与路径解析顺序逐字不变）。
- **协议不变式**：`protocol_version=1`；`127.0.0.1` 随机端口；256 位握手 token；`reported_pid==sidecar_pid`；`POST /v1/shutdown` + 超时 `kill()`；父进程看门狗；以上全部不改。
- **无明文回退**：任何凭据只允许存进 OS 密钥库（Windows Credential Manager / macOS Keychain）；不支持平台直接抛 `CredentialError`，绝不落盘为明文。
- **令牌注入面最小**：`ACCESS_TOKEN` 只出现在 app-server 子进程的 `env` 中；sidecar 自身 `os.environ` 不得写入该键；不得出现在 `argv`、URL、项目文件、握手文件、SSE、日志。
- **向后兼容**：`/v1/configure` 与 SSE 仅“新增可选字段/事件”，旧编辑器忽略新字段不受影响；`credential("api_key")` 语义与既有测试不变。
- **TDD**：每个任务先写失败测试，再写最小实现；每个任务产生一个独立、可单独验证、可单独回滚的 commit。
- **依赖克制**：除 `cryptography` 外不新增第三方运行时依赖；OAuth/PKCE/回调/HTTP 用 Python 标准库实现，避免扩大 PyInstaller 与供应链面。

---

## 0. 输入、基线与锚点

### 0.1 官方权威流程（实现必须严格遵守）

来源（OpenAI 官方文档，实施时如页面更新以最新为准）：

- Sign in with ChatGPT / Registration and sign-in：<https://developers.openai.com/siwc/token-sharing-open-source/sign-in>
- Accounts and sessions（刷新/轮换/撤销）：<https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions>
- Codex app-server（OAuth access token 驱动）：<https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server>
- Errors and recovery：<https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery>
- Token reference：<https://developers.openai.com/siwc/token-sharing-open-source/token-reference>
- Preview limitations：<https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations>
- Codex app-server 协议总览（JSON-RPC/thread/turn/item）：<https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md>

固化为常量的关键事实：

| 项 | 值 |
| --- | --- |
| 授权端点 | `https://auth.openai.com/api/accounts/authorize` |
| 令牌端点 | `https://auth.openai.com/api/accounts/oauth/token` |
| 发现文档 | `https://auth.openai.com/.well-known/openid-configuration`（提供 `jwks_uri`、`revocation_endpoint`） |
| 首次注册 `client_id` | `dynamic_agent_client`（仅是入口，绝不是要保存/复用的 client id） |
| 资源 | `https://api.openai.com/v1` |
| 身份 + 计划 scope | `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct` |
| PKCE | `code_challenge_method=S256` |
| 回调 | `http://127.0.0.1:<port>/auth/callback`（路径固定；端口可变；不得用 `localhost`） |
| access token | 1 小时（`expires_in: 3600`） |
| refresh token | 30 天，每次刷新轮换并返回新的 30 天 refresh token |
| 计划能力判据 | 授予 scope 含 `chatgpt.tokens.use.direct` |
| app-server 令牌传递 | 子进程环境变量 `ACCESS_TOKEN`（`env_key="ACCESS_TOKEN"`） |
| app-server 配置 | `model_provider="openai_chatgpt_plan"`，`wire_api="responses"`，`requires_openai_auth=false`，`supports_websockets=false` |
| app-server 生命周期 | `initialize` → `initialized` → `thread/start` → `turn/start`；流 `item/agentMessage/delta`；`turn/completed` 的 `status` 只有 `completed` 算成功 |

### 0.2 当前代码边界（本计划不读取，实现阶段才打开）

| 边界 | 职责（来自任务上下文，未在本次提交中读取源码） |
| --- | --- |
| `editor/plugins/agent_editor_plugin.cpp` / `.h` | Godot 设置/对话 UI、sidecar 生命周期、握手校验、SSE 读取与渲染 |
| `vibe_tools/editor_sidecar.py` | FastAPI sidecar：`Runtime`/配置、`SimpleLLMProvider`、文件/测试/回退工具、SSE、`/v1/configure`、取消/重启 |
| `vibe_tools/editor_credentials.py` | Windows Credential Manager 与 macOS Keychain 抽象（现有唯一公开入口 `credential(name, value=None)`，本轮扩展） |
| `misc/vibe_agent/package_macos.sh` | macOS arm64 `.app` 打包（sidecar 已由 PyInstaller onedir 产出） |
| `misc/vibe_agent/build_sidecar.sh` | macOS sidecar 构建（若存在；不存在则按既有命名对齐） |
| `misc/vibe_agent/*.ps1` | Windows 构建/打包（**不得修改**） |

> 说明：本计划提交时**不读取** `editor/`、`platform/`、`vibe_tools/`、`misc/`、`tests/` 等被禁止路径；因此任务里给的是符号名与语义契约，不写死行号。实现阶段先核对符号真名（含 `SimpleLLMProvider` 的方法名、SSE 事件 schema、`/v1/configure` 的 Pydantic 模型名），再落地代码；发现与本计划假设不一致时，只按“新增、向后兼容、不改既有语义”原则调整，并在实施报告中记录差异。

### 0.3 环境准备（macOS arm64 开发机）

```bash
# 1) Python 依赖（在既有 .build/pythonlibs 基础上追加 cryptography）
python3 -m pip install --target .build/pythonlibs \
  scons pyinstaller fastapi uvicorn openai "pydantic>=2,<3" pillow requests pygltflib \
  python-multipart "cryptography>=42"
export PYTHONPATH="$PWD/.build/pythonlibs:$PWD"

# 2) Codex CLI（提供 app-server 子命令；版本需支持 `codex app-server`）
codex --version
codex app-server --help | head -n 20
```

Expected：`import cryptography, fastapi, uvicorn, pydantic` 成功；`codex --version` 输出版本号；`codex app-server --help` 含 `--listen`。若本机无 `codex`，允许用 `CODEX_BIN=/abs/path/to/codex` 覆盖，或按 Task 8 获取固定版本二进制。

- [ ] 确认 `python3 -c "import cryptography, fastapi; print('deps ok')"` 输出 `deps ok`。
- [ ] 确认 `git status --short` 在开始实现前只包含本计划文件或干净；存在无关脏改动时记录但不触碰。

### 0.4 全局常量与不变量（所有任务共享）

```python
# vibe_tools/codex_auth.py 计划常量（实现时集中定义，禁止散落硬编码）
AUTHORIZE_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
AGENT_NAME_HINT = "VibeGodot"          # 同时作为 app-server clientInfo.name
CALLBACK_PATH = "/auth/callback"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_TIMEOUT_S = 300
REFRESH_SKEW_S = 120                   # 距过期不足 120s 即视为需要刷新
```

不可变量：

1. `protocol_version == 1`，握手四项校验不变；新增能力只走 `/v1/configure` 的可选字段与新增端点。
2. `credential("api_key")` 的读写行为与既有测试逐字不变；新能力另加 `secret(name)` / `delete_secret(name)`。
3. API Key 模式（`auth_mode == "api_key"`）仍走原 `SimpleLLMProvider` 与既有工具链，行为不变。
4. ChatGPT 模式（`auth_mode == "chatgpt"`）下，模型推理与工具编排由 `codex app-server` 承担，sidecar 只做协议桥接与令牌注入。
5. Windows 平台下 `auth_mode` 强制为 `api_key`（`auth_mode="chatgpt"` 请求返回 `409`/`unsupported`），UI 隐藏入口；`.ps1` 与 Windows 路径/文案不动。

---

## 1. 目标与非目标

### 1.1 目标

- **G1** macOS arm64 用户可用“Continue with ChatGPT”完成 Sign in with ChatGPT 首次注册，获得“签发的 `client_id`”，并持久化 `ext_agent_host_id`。
- **G2** 回调后保存 `client_id`、`id_token`、`access_token`、`refresh_token`、`scope`、过期信息；再次登录复用签发的 `client_id`（配合 `id_token_hint`/`login_hint`）。
- **G3** `refresh_token` 轮换刷新与撤销（登出）；刷新错误按官方码恢复。
- **G4** `ACCESS_TOKEN` 只注入 `codex app-server` 子进程；通过 stdio JSONL 完成 `initialize`/`initialized`/`thread/start`/`turn/start`，流式消费 `item/agentMessage/delta` 与 `turn/completed`。
- **G5** app-server 在“当前 Godot 工程 workspace”下运行（`cwd=project_root`、`workspaceWrite`），可见编辑器既有 SSE 事件映射与取消/重启。
- **G6** 保留 API Key 模式（`SimpleLLMProvider`）与 Windows 行为不变。
- **G7** macOS arm64 打包把固定版本的 `codex` 二进制随 `.app` 分发并做完整性校验。

### 1.2 非目标

- 不做 Intel/universal 构建；不做代码签名、公证、加固运行时/entitlements。
- 不改 `SimpleLLMProvider` 的既有模型协议、不改验证/回退/测试编排语义。
- 不做 Windows 的 ChatGPT 模式（本阶段只保证 Windows 回归不破）。
- 不做多用户/远程托管、团队 workspace 管理后台、计费切换（ChatGPT 计划与 API Key 之外的第三条计费路径）。
- 不实现 Responses API 直连（不经 app-server）；不实现 WebSocket（`supports_websockets=false`）。
- 不改 `misc/vibe_agent/*.ps1`、`vibe_tools/web_server.py`、`vibe_tools/static/`、`vibe_tools/templates/`。

---

## 2. 架构与数据流

### 2.1 组件与信任边界

```
VibeGodot.app (arm64)
  Contents/MacOS/VibeGodot              ← Godot 编辑器（C++，UI + SSE）
  Contents/Resources/vibe_agent/
    godotvibe-agent/                    ← PyInstaller onedir sidecar（FastAPI）
      godotvibe-agent
    codex/codex                          ← 随包分发的 app-server 二进制（固定版本）
  Contents/Resources/...                 ← 令牌不在包内

进程与信任边界：
  [编辑器] --HTTP/SSE(Bearer 握手 token)--> [sidecar]
  [sidecar] --stdio JSONL(JSON-RPC)--> [codex app-server] --HTTPS--> api.openai.com
  [sidecar] --OS 密钥库--> Keychain（令牌只在此处与内存）
  [sidecar] --HTTPS--> auth.openai.com（授权/令牌/撤销）
```

### 2.2 首次注册（dynamic registration）时序

1. 编辑器设置里选 `Sign in with ChatGPT` → `POST /v1/auth/chatgpt/start`。
2. sidecar：读取/生成并持久化 `ext_agent_host_id`（`urn:uuid:<uuid4>`，存 `Godot/VibeAgent/codex_host_id`）。
3. sidecar：生成 `state`、`nonce`、PKCE verifier；启动 `127.0.0.1:0` 回调监听；组装 authorize URL：
   `client_id=dynamic_agent_client`、`agent_name_hint=VibeGodot`、`ext_agent_host_id=...`、
   `response_type=code`、`redirect_uri=http://127.0.0.1:<port>/auth/callback`、`scope=<SCOPE>`、
   `resource=https://api.openai.com/v1`、`state`、`nonce`、`code_challenge_method=S256`、`code_challenge`。
4. sidecar 调 `webbrowser.open(auth_url)`（系统浏览器）；**不把含 hint 的完整 URL 写日志**。
5. 浏览器回调到 `/auth/callback`：校验 `state`；新注册回调应带 `code` + 签发的 `client_id`（可带 `scope`）。
   - 若 `error=access_denied`：停止本次尝试，不换 code。
   - 若新注册回调没有签发 `client_id`：视为注册未完成，报错。
6. sidecar 换 code：`POST TOKEN_ENDPOINT`（form-encoded：`grant_type=authorization_code`、签发的 `client_id`、
   `code`、`code_verifier`、同一 `redirect_uri`、`resource`；无 client secret）。
7. 用发现文档的 `jwks_uri` 验 `id_token` 签名，并校验 `iss`、`aud==client_id`、`exp`、`nonce`；用 `sub` 作为账号身份。
8. 检查 `scope` 是否含 `chatgpt.tokens.use.direct` → `plan_enabled=True/False`。
9. 原子写入账号令牌 bundle（只进 OS 密钥库）；回调页返回“可关闭浏览器”。
10. 编辑器轮询 `GET /v1/auth/chatgpt/status` → `completed`，显示账号标签；随后一轮对话走 Codex 通道。

### 2.3 再次登录与刷新时序

- **再次登录（重新授权）**：用保存的签发 `client_id`，省略 `agent_name_hint`；带同一 `ext_agent_host_id`；
  可带 `id_token_hint`（保留的 `id_token`）与 `login_hint`（已验证邮箱）；全新 `state`/`nonce`/PKCE。
  回调若给不同 `client_id`：拒绝，不覆盖已选账号注册。
- **刷新**：`POST TOKEN_ENDPOINT`（`grant_type=refresh_token`、签发 `client_id`、`refresh_token`、`resource`；
  省略 `scope`）。成功则**成组替换** `access_token`/`refresh_token`/`expires_in`/`scope`/`saved_at`；
  同一账号会话的刷新必须串行化，避免轮换竞态。
- **撤销（登出）**：从发现文档取 `revocation_endpoint`，`POST token=<refresh_token>&token_type_hint=refresh_token&client_id=<issued>`;
  空 `200` 视为成功；`5xx`/网络失败按退避重试；本地仍清除令牌并提示“远端撤销未确认”。

### 2.4 一轮对话数据流（ChatGPT 模式）

```
编辑器 POST /v1/agent/run {prompt}
  → sidecar: 选 provider（chatgpt）
  → 令牌检查：过期/临近过期 → 刷新（加锁）
  → 确保 app-server 存活（不存在/令牌已换 → 重启）
       spawn: codex app-server --listen stdio:// -c ... ; env["ACCESS_TOKEN"]=<access token>
       initialize(clientInfo.name="VibeGodot") → initialized
  → thread/start {cwd=project_root, model, approvalPolicy:"never",
                  sandboxPolicy:{type:"workspaceWrite", writableRoots:[project_root], networkAccess:false}}
     或 thread/resume {threadId=已保存}
  → turn/start {threadId, input:[{type:"text", text:prompt}]}
  ← item/agentMessage/delta*           → SSE 文本增量
  ← item/commandExecution/outputDelta* → SSE 工具输出增量
  ← item/fileChange/*                  → SSE 文件变更事件
  ← turn/completed(status)             → SSE 结束（completed 才算成功；failed/interrupted 报错）
```

- 取消：编辑器既有取消 → sidecar `turn/interrupt {threadId, turnId}`，等 `turn/completed(status="interrupted")`。
- 重启后台：`_restart_sidecar()` 语义不变（重启的是 sidecar）；sidecar 内部另管 app-server 生命周期：
  进程崩溃 → 记录 → 需要时用最新令牌重启 → `thread/resume`。
- 线程 ID 持久化在 `.godot/agent/codex/thread.json`（非敏感，`0600`，原子写），供重启后 `thread/resume`。

### 2.5 API Key 模式（保持不变）

`auth_mode == "api_key"` 时完全走既有路径：`SimpleLLMProvider` + 既有工具循环 + 既有 SSE 事件；
本计划对 sidecar 的改动只增加分支与可选字段，绝不改变既有分支内部逻辑。

---

## 3. 接口契约

### 3.1 Sidecar HTTP 新增/变更端点

| 方法 | 路径 | 请求 | 响应 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/v1/configure` | — | 既有字段 + `auth_mode`、`chatgpt_supported`、`chatgpt_account`、`codex_version`、`credential_store` | 只读；无令牌 |
| POST | `/v1/configure` | 既有字段 + 可选 `auth_mode`、`chatgpt_account_id` | 同 GET | `auth_mode=chatgpt` 且不支持 → `409` |
| POST | `/v1/auth/chatgpt/start` | `{}` | `{login_id, status:"authorizing"}` | 启动浏览器与回调监听；不返回令牌/含 hint URL |
| GET | `/v1/auth/chatgpt/status` | `?login_id=` | `{status:"pending\|completed\|failed\|cancelled", account?, error?}` | 编辑器轮询；`account` 只含标签/邮箱/计划位 |
| POST | `/v1/auth/chatgpt/cancel` | `{login_id}` | `{status:"cancelled"}` | 关闭监听、丢弃 state/PKCE |
| POST | `/v1/auth/chatgpt/logout` | `{account_id}` | `{status:"revoked"\|"revoke_unconfirmed"}` | 撤销 + 清本地 |
| GET | `/v1/auth/chatgpt/accounts` | — | `{accounts:[{account_id,label,email,plan_enabled}...]}` | 只返回元数据 |

- 所有新端点仍要求既有 Bearer 握手 token（复用现有鉴权依赖）。
- `error` 字段只放“非敏感原因码/文案”（如 `invalid_grant`、`state_mismatch`），不含令牌、`code`、`code_verifier`。
- `chatgpt_account` 形如 `{account_id,label,email,plan_enabled}`；`account_id` 为不可逆摘要（见 §5）。

### 3.2 内部 Python 接口（新增模块）

```python
# vibe_tools/codex_auth.py
class CodexAuthError(RuntimeError):
    """消息只含操作名 + 官方错误码，绝不含令牌/授权码/verifier。"""

@dataclass(frozen=True)
class CodexAccount:
    account_id: str; label: str; email: str; issuer: str; subject: str
    client_id: str; ext_agent_host_id: str
    id_token: str; access_token: str; refresh_token: str
    token_type: str; expires_in: int; earliest_refresh_at: int | None
    scopes: tuple[str, ...]; saved_at: str; plan_enabled: bool

def host_id() -> str: ...                       # 读取或生成并持久化 urn:uuid:...
def new_pkce() -> tuple[str, str]: ...          # (verifier, challenge)
def build_authorize_url(*, client_id, host_id, state, nonce, challenge,
                        redirect_uri, agent_name_hint=None,
                        id_token_hint=None, login_hint=None) -> str: ...
class LoopbackCallback:                          # 127.0.0.1:0 + /auth/callback
    def start(self) -> str: ...                  # 返回 redirect_uri
    def wait(self, timeout_s: int) -> dict[str, str]: ...   # 只回 code/state/error/scope/client_id
    def close(self) -> None: ...
def exchange_code(*, client_id, code, verifier, redirect_uri) -> dict: ...
def validate_id_token(id_token: str, *, jwks: dict, client_id: str,
                      nonce: str, issuer: str) -> dict: ...   # 返回 claims
def refresh(account: CodexAccount) -> CodexAccount: ...
def revoke(account: CodexAccount) -> bool: ...   # True=确认撤销
```

```python
# vibe_tools/codex_accounts.py  —— 账号注册表 + 安全存储（只走 editor_credentials.secret）
def account_id_for(*, issuer: str, subject: str, client_id: str) -> str: ...  # 不可逆短摘要
def save_account(account: CodexAccount) -> None: ...
def load_account(account_id: str) -> CodexAccount | None: ...
def list_accounts() -> list[dict]: ...           # 元数据，无令牌
def delete_account(account_id: str) -> None: ...
def active_account_id() -> str | None: ...       # 存非敏感指针（Editor Settings / 小状态文件）
def set_active_account(account_id: str | None) -> None: ...
```

```python
# vibe_tools/codex_app_server.py
class CodexAppServerError(RuntimeError): ...

class CodexAppServer:
    def __init__(self, *, codex_exe: str, cwd: str, access_token: str,
                 model: str, env: dict | None = None, log=print): ...
    def start(self) -> None: ...                 # spawn + initialize + initialized
    def start_thread(self) -> str: ...           # thread/start → thread.id
    def resume_thread(self, thread_id: str) -> str: ...
    def run_turn(self, thread_id: str, text: str) -> Iterator[TurnEvent]: ...
    def interrupt(self, thread_id: str, turn_id: str) -> None: ...
    def stop(self) -> None: ...                  # 关 stdin/stdout + terminate/kill
    def alive(self) -> bool: ...
```

```python
# vibe_tools/editor_credentials.py 扩展
def secret(name: str, value: str | None = None) -> str: ...   # 通用命名密钥
def delete_secret(name: str) -> None: ...
# 合法 name：api_key | codex_host_id | codex/<account_id>；其余 ValueError
# credential(name, value) 保持原签名与原语义；api_key 走同一后端
```

`TurnEvent` 为 `dataclass`，字段 `kind: str`（`"agent_delta" | "command_output" | "file_change" | "turn_completed" | "error"`）、
`text: str = ""`、`item_id: str | None = None`、`status: str | None = None`、`message: str | None = None`。

### 3.3 Codex app-server JSON-RPC 契约（stdio JSONL）

- 传输：子进程 stdin/stdout，一行一个 JSON 对象；不需要 `"jsonrpc":"2.0"` 头。
- 客户端请求带自增 `id`；响应按 `id` 关联；通知无 `id`；**服务端发起的请求**（审批）带 `id`，必须回 `{id, result}`。
- 启动配置（常量，不得改写其它 provider）：

```
codex app-server --listen stdio:// \
  -c 'model_provider="openai_chatgpt_plan"' \
  -c 'model_providers.openai_chatgpt_plan.name="ChatGPT plan"' \
  -c 'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"' \
  -c 'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"' \
  -c 'model_providers.openai_chatgpt_plan.wire_api="responses"' \
  -c 'model_providers.openai_chatgpt_plan.requires_openai_auth=false' \
  -c 'model_providers.openai_chatgpt_plan.supports_websockets=false'
```

- 首轮：
  `{"method":"initialize","id":1,"params":{"clientInfo":{"name":"VibeGodot","title":"VibeGodot","version":"<app version>"}}}`
  → 成功响应；随后 `{"method":"initialized"}`（通知，无 id）。
- `thread/start` params：`{"cwd": project_root, "model": <config>, "approvalPolicy": "never",
  "sandboxPolicy": {"type":"workspaceWrite","writableRoots":[project_root],"networkAccess":false}}`。
- `turn/start` params：`{"threadId": ..., "input":[{"type":"text","text": prompt}]}`。
- 消费通知：`turn/started`、`item/started`、`item/agentMessage/delta`（拼接同 `itemId`）、`item/commandExecution/outputDelta`、
  `item/completed`、`turn/completed`；错误通知 `error`（含 `codexErrorInfo`）。
- 审批：本阶段固定 `approvalPolicy:"never"`，因此不出现审批请求；若仍收到服务端请求，统一回 `{"decision":"decline"}` 并记日志（不含内容敏感信息），避免悬挂。**审批 UI 为后续阶段，非本计划目标。**
- 参数名以所装 `codex` 版本为准：实现前用 `codex app-server generate-json-schema --out /tmp/codex-schema` 核对
  `turn/start`、`thread/start`、`initialize` 的字段名；发现不一致时以生成 schema 为准并按 §0.2 记录差异。

### 3.4 编辑器侧状态与文案（C++）

- 设置弹窗新增“账号”区域：模式单选（`API Key` / `Sign in with ChatGPT`）。
- 选择 ChatGPT 且未登录：显示按钮 **Continue with ChatGPT**（按官方 UI/UX 指南文案与品牌）。
- 登录中：显示“等待浏览器完成授权…” + 取消；轮询 `GET /v1/auth/chatgpt/status`（间隔 1s，超时按回调超时）。
- 已登录：显示账号标签与 `plan_enabled`；提供“切换账号/退出登录”。
- 计划未启用（缺 `chatgpt.tokens.use.direct`）：保留登录态，提示“ChatGPT 计划用量未启用”，引导改用 API Key 或重新授权（**不**在普通登录时强制 `prompt=consent`）。
- 不支持平台（Windows 或未找到 codex）：隐藏 ChatGPT 选项，仅保留 API Key；文案与 Windows 现状逐字不变。

---

## 4. 状态机

### 4.1 账号认证状态机（sidecar 单账号会话）

```
unauthenticated
  --start--> authorizing ------------------ callback(state ok) --> exchanging --> validating
                 |  |                                                              |
                 |  |<-- cancel/timeout -----------------------------+             |
                 |  v                                                             v
                 | cancelled                                            authenticated(plan_enabled=True/False)
                 |                                                              |
                 |                            expiry/401 --refresh--> refreshing -+
                 |                                                              |
                 |                              invalid_grant/refresh_* --> reauth_required
                 v                                                              |
             failed(<code>)                                          logout/revoke --> revoking --> unauthenticated
```

约束：任何一次 `start` 都生成全新的 `state`/`nonce`/PKCE 与新的回调端口；旧尝试必须先 `cancel`；
同一时刻只允许一个 `authorizing`。`reauth_required` 只清除不可用令牌，保留 `client_id`/账号映射与 host id。

### 4.2 app-server / thread / turn 状态机

```
stopped --ensure--> starting(initialize) --ok--> ready
ready --turn/start--> running --turn/completed(completed)--> ready
running --interrupt--> interrupting --turn/completed(interrupted)--> ready
running|ready --process exit--> crashed --ensure--> starting --resume--> ready
starting --initialize timeout/exit--> failed(可重试)
```

`turn/completed` 的 `status=="failed"` 映射为 SSE 错误事件并保留 `codexErrorInfo`（不含令牌）；
`interrupted` 按取消处理，不算失败。

### 4.3 刷新锁与并发

- 进程内：`asyncio.Lock`（或线程锁）按 `account_id` 串行刷新。
- 进程间：同一主机可能有多个编辑器实例 → 刷新前后用 `~/.config/VibeGodot/codex/<account_id>.lock`
  （`fcntl.flock`，仅 macOS/Linux）串行化；锁内先重读密钥库，若已被别的进程刷新到更新 `saved_at` 则直接复用，避免 `refresh_token_reused`。
- app-server 生命周期与刷新互斥：刷新成功后若 app-server 持有旧令牌，先 `stop()` 再以新 `ACCESS_TOKEN` 重启并 `thread/resume`。

---

## 5. 账号与凭据存储安全

- **唯一持久化位置**：OS 密钥库。
  - 账号令牌 bundle：`secret("codex/<account_id>")`，`account_id = base64url(sha256(issuer + "\x00" + subject + "\x00" + client_id))[:22]`（不可逆、不含邮箱/`sub` 原文）。
  - host id：`secret("codex_host_id")`（opaque 标识，非认证凭据）。
  - 非敏感指针（active account id、上次登录邮箱标签）可存 Editor Settings `vibe_agent/chatgpt_account*`，**不得**存任何令牌。
- **bundle 内容**（仅存密钥库，序列化 JSON）：`issuer/subject/client_id/ext_agent_host_id/id_token/access_token/refresh_token/token_type/expires_in/earliest_refresh_at/scopes/saved_at/plan_enabled/email/label`。
- **不落盘面**：项目文件、`.godot/agent/`（除线程 ID 与日志）、启动参数、`argv`、环境变量（除 app-server 子进程 `ACCESS_TOKEN`）、SSE、日志、崩溃转储。
- **注入面**：`child_env = dict(os.environ); child_env["ACCESS_TOKEN"] = access_token; Popen(..., env=child_env)`；
  绝不 `os.environ["ACCESS_TOKEN"] = ...`；Popen 后不对子进程 env 做任何打印。
- **日志脱敏**：统一 `_redact(value)`（仅保留长度与前 4 位，或直接 `"***"`）；记录异常前调用；授权 URL 只记录去掉 query 的 origin+path。
- **原子写与非敏感状态**：`thread.json`/`active_account.json` 等用临时文件 + `os.replace` + `0o600`。
- **内存限制**：Python 字符串不可变，无法可靠擦除；实现上尽量缩小令牌生命周期与拷贝次数，并在文档中如实说明该限制。
- **合规**：不做遥测上传；不把令牌放入任何分析/支持日志；如用户怀疑泄露，引导在 ChatGPT 设置中断开。

---

## 6. macOS arm64 打包策略

1. **Codex 二进制获取**：优先 `CODEX_BIN` 指向本地已验证二进制；否则由 `misc/vibe_agent/fetch_codex.sh`
   从**固定版本**的官方发布下载 macOS aarch64 资产到 `.build/codex/codex`（`chmod 0755`），校验官方 SHA-256，
   失败即终止并给出哈希不匹配信息（不打印任何凭据）。固定版本记录在脚本顶部常量（`CODEX_VERSION`、`CODEX_SHA256`）。
2. **sidecar 解析顺序**（Python，避免新增 C++ 路径分支）：
   - 显式 `--codex-exe <path>`（编辑器/测试可传，最高优先）；
   - bundled：sidecar 可执行文件的 `Contents/Resources/vibe_agent/codex/codex`（由 `sys.executable` 反推）；
   - dev：仓库 `.build/codex/codex`；
   - `CODEX_BIN` 环境变量；
   - `shutil.which("codex")`。
   全部落空 → `chatgpt_supported=false`，UI 隐藏入口，API Key 模式照常。
3. **打包**：`misc/vibe_agent/package_macos.sh` 增加一步把 `.build/codex/codex` 拷到
   `VibeGodot.app/Contents/Resources/vibe_agent/codex/codex`，并断言 `test -x` 且
   `file`/`lipo -archs` 显示 `arm64`（不 `lipo` 合并，不做签名）。
4. **PyInstaller**：`build_sidecar.sh` 追加 `--collect-all cryptography`（如 hook 已覆盖则保持幂等），
   其余 flag 与现有保持一致；sidecar 产物仍是纯 Mach-O onedir（禁止嵌套 `.app`，见既有设计）。
5. **完整性**：打包脚本对 codex 做 SHA-256 复核；`.app` 与 zip 的最终校验只记录文件存在、架构与版本
   （`codex --version`），不记录令牌。
6. **Gatekeeper**：沿用既有“未签名/未公证 + quarantine 提示”文档口径；不改签名策略。

---

## 7. Windows 兼容策略

- **功能门控**：`chatgpt_supported` 在 Windows 恒为 `false`；`POST /v1/configure {auth_mode:"chatgpt"}`
  返回 `409`（`detail="ChatGPT sign-in is not supported on this platform yet"`）；`GET /v1/configure` 的
  `auth_mode` 恒为 `"api_key"`，忽略持久化的越权值。
- **代码可移植但默认休眠**：`codex_auth`/`codex_app_server`/`codex_accounts` 不 import 平台专有模块，
  依赖的 `fcntl` 用 `try/except ImportError` 保护；Windows 上相关测试用 `skipUnless(sys.platform!="win32")`
  或直接测“门控拒绝”。
- **不改 Windows 资产**：`misc/vibe_agent/build_sidecar.ps1`、`package_windows.ps1`、Windows 路径三段回退、
  错误文案、`Credential Manager` 语义、`.exe` 命名全部不动。
- **凭据扩展兼容**：`secret()`/`delete_secret()` 的 Windows 分支复用 `CredWriteW/CredReadW/CredDeleteW`；
  `credential("api_key")` 行为逐字不变。
- **回归**：在 Windows 上跑完整 Python 套件 + `build_sidecar.ps1` + `package_windows.ps1` 冒烟，确认无新增失败。

---

## 8. 错误恢复矩阵

| 场景 | 信号 | 行为 | 用户可见 |
| --- | --- | --- | --- |
| state 不匹配 | 回调 `state` ≠ 待定 | 丢弃本次尝试，不换 code | “授权校验失败，请重试” |
| 用户拒绝授权 | `error=access_denied` | 停止，不换 code | “已取消授权” |
| 新注册缺签发 client_id | 回调无 `client_id` | 视为注册未完成 | “注册未完成，请重试” |
| 回调 client_id 与选择不符 | 两端不一致 | 拒绝，不覆盖注册 | “账号不一致，已拒绝” |
| code 兑换 `invalid_grant` | 令牌端点 | 丢弃 code，用已保存 `client_id` 重新授权 | “授权已过期，请重新登录” |
| id_token 验签/iss/aud/exp/nonce 失败 | JWKS 校验 | 拒绝并清除本次临时令牌（保留旧账号） | “令牌校验失败” |
| 缺 `chatgpt.tokens.use.direct` | scope | 保留登录，`plan_enabled=false` | 提示改用 API Key / 重新授权 |
| 刷新不可用 | `invalid_grant`/`invalid_refresh_token`/`token_expired`/`refresh_token_expired`/`refresh_token_invalidated`/`refresh_token_reused` | 清不可用令牌，保留 client_id | “需要重新登录” |
| 刷新 `invalid_client` | 令牌端点 | 标记配置错误，不重试 | 报告并保留其它账号 |
| 刷新 `refresh_token_reused` | 令牌端点 | 视为并发/重放，重读密钥库；仍冲突则要求重登 | “登录状态失效” |
| 401/`subscription_sharing_invalid_user` | Responses | 刷新一次后重试一次；仍失败要求重登 | 错误码原文（无令牌） |
| 403/`subscription_sharing_user_not_eligible` / `chatpass_v2_*` | Responses | 不重试、不循环 OAuth | 解释政策限制 |
| 429/`subscription_sharing_usage_limit_exceeded` | Responses | 暂停 ChatGPT 计划请求 | 链接 ChatGPT 设置 → Usage |
| 503/`subscription_sharing_*_unavailable`、路由不可用 | Responses | 保留凭据，有限退避 | “稍后重试” |
| app-server 进程崩溃 | stdout EOF | 有界重启，成功则 `thread/resume` | 状态栏提示“已恢复” |
| JSONL 半行/坏 JSON | 解析 | 跳过并计数；连续超阈值 → 判崩溃 | 同上 |
| `initialize` 超时 | 计时器 | `stop()` 判失败，可重试 | “后台助手启动失败” |
| app-server 收到审批请求 | 服务端请求 | 统一 `decline` 并记日志 | 目前不会发生（`approvalPolicy:"never"`） |
| 远端撤销失败 | `revocation_endpoint` 5xx/网络 | 本地仍清除，标记未确认 | “已本地退出，远端未确认” |
| 不支持平台 | Windows/无 codex | 门控，不影响 API Key | 仅显示 API Key |

---

## 9. 文件级改动范围（精确清单）

新增：

| 文件 | 作用 |
| --- | --- |
| `vibe_tools/codex_auth.py` | OAuth/SIWC：host id、PKCE、回调、兑换、验签、刷新、撤销 |
| `vibe_tools/codex_accounts.py` | 账号注册表与安全存储（只调 `editor_credentials.secret`） |
| `vibe_tools/codex_app_server.py` | app-server 子进程与 JSON-RPC/JSONL 客户端 |
| `vibe_tools/codex_provider.py` | sidecar 的 ChatGPT provider 桥（事件映射到既有 SSE 契约） |
| `vibe_tools/tests/test_codex_auth.py` | PKCE/URL/回调/兑换/验签/刷新/撤销/脱敏测试 |
| `vibe_tools/tests/test_codex_accounts.py` | account_id 摘要、存储往返、无令牌日志测试 |
| `vibe_tools/tests/test_codex_app_server.py` | 假 app-server 的初始化/线程/turn/中断/崩溃/注入面测试 |
| `misc/vibe_agent/fetch_codex.sh` | 固定版本 codex 二进制下载与 SHA-256 校验 |

修改：

| 文件 | 改动 | Windows 影响 |
| --- | --- | --- |
| `vibe_tools/editor_credentials.py` | 新增 `secret()`/`delete_secret()`；`credential()` 语义不变 | 否（Windows 分支复用现有 Win32 API） |
| `vibe_tools/editor_sidecar.py` | `auth_mode` 分支、ChatGPT 端点、Codex 桥接线、能力探测 | 否（仅新增字段/端点；默认 `api_key`） |
| `editor/plugins/agent_editor_plugin.cpp` | 账号 UI、模式选择、轮询、文案 | 否（Windows 隐藏入口，原逻辑保留） |
| `editor/plugins/agent_editor_plugin.h` | 按需新增私有方法/成员声明 | 否 |
| `vibe_tools/tests/test_editor_credentials.py` | `secret()`/`delete_secret()` 用例 | 否 |
| `vibe_tools/tests/test_editor_sidecar.py` | 新字段/新端点/门控用例 | 否 |
| `vibe_tools/tests/test_agent_editor_plugin_source.py` | C++ 源码静态断言（可选新增测试） | 否 |
| `misc/vibe_agent/build_sidecar.sh` | 打包 `cryptography` | 否 |
| `misc/vibe_agent/package_macos.sh` | 随包分发 codex 并校验架构/哈希 | 否 |
| `README.md`、`misc/vibe_agent/README.zh-CN.md`、`misc/vibe_agent/CAPABILITIES.md` | 使用/能力/限制说明 | 否（Windows 章节保留） |
| `pyproject.toml` / `uv.lock` | 仅在确有依赖清单时追加 `cryptography` | 否 |

明确不改：`misc/vibe_agent/build_sidecar.ps1`、`misc/vibe_agent/package_windows.ps1`、
`vibe_tools/godot_runner.py`、`vibe_tools/editor_behavior.py`、`vibe_tools/editor_harness.py`、
`vibe_tools/web_server.py`、`vibe_tools/static/`、`vibe_tools/templates/`、`editor/editor_node.cpp`、
`platform/` 与构建系统文件（本计划不新增 C++ 路径分支）。

---

## Task 1: `editor_credentials` 增加通用命名密钥（保留 `api_key` 语义）

**Files:**
- Modify: `vibe_tools/editor_credentials.py`
- Test: `vibe_tools/tests/test_editor_credentials.py`

**Interfaces:**
- Produces：`secret(name: str, value: str | None = None) -> str`、`delete_secret(name: str) -> None`。
- 合法 `name`：`api_key`、`codex_host_id`、`codex/<account_id>`（`<account_id>` 允许 `[A-Za-z0-9_-]{8,64}`）；其余 `ValueError`。
- `credential(name, value)` 行为与签名不变；建议内部改为 `return secret(name, value)`（仅 `api_key` 通过校验）。
- 后端：macOS Keychain / Windows Credential Manager；不支持平台抛 `CredentialError`；无明文回退。

- [ ] **Step 1: 写失败测试。** 在既有 `test_editor_credentials.py` 追加：

```python
class SecretNameTest(unittest.TestCase):
    def test_accepts_supported_names(self):
        for name in ("api_key", "codex_host_id", "codex/AbC123_-xyz"):
            with patch.object(ec, "_macos_credential", return_value="v") as macos, \
                 patch.object(ec.sys, "platform", "darwin"):
                self.assertEqual(ec.secret(name), "v")

    def test_rejects_unknown_name(self):
        for bad in ("oauth_refresh_token", "codex/", "codex/../x", "api_key/../x"):
            with self.assertRaises(ValueError):
                ec.secret(bad)

    def test_delete_routes_to_backend(self):
        with patch.object(ec, "_macos_delete", return_value=None) as dele, \
             patch.object(ec.sys, "platform", "darwin"):
            ec.delete_secret("codex/AbC123_-xyz")
            dele.assert_called_once_with("codex/AbC123_-xyz")

    def test_missing_delete_is_ok(self):
        # 删除不存在条目必须幂等（sign-out 可重复）
        with patch.object(ec, "_macos_delete", side_effect=ec.CredentialError("-25300")):
            with self.assertRaises(ec.CredentialError):
                ec.delete_secret("codex/AbC123_-xyz")
```

> 实现时按既有测试文件的实际 import/风格微调；`_macos_delete`/`_windows_delete` 为新增可注入点。

- [ ] **Step 2: 运行并确认失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_credentials -v`

Expected: `AttributeError: module 'vibe_tools.editor_credentials' has no attribute 'secret'`（或 `delete_secret`）。

- [ ] **Step 3: 最小实现。** 新增名称正则与 `secret`/`delete_secret`；`credential()` 转调 `secret()`；
  macOS 增加 `_macos_delete`（`SecKeychainFindGenericPassword` + `SecKeychainItemDelete`，`errSecItemNotFound` 视为成功）；
  Windows 增加 `_windows_delete`（`CredDeleteW`，`ERROR_NOT_FOUND(1168)` 视为成功）。所有错误消息只含函数名与状态码。

- [ ] **Step 4: 运行并通过。**

Run: 同 Step 2 命令 + `python3 -m unittest vibe_tools.tests.test_editor_sidecar -v`

Expected: 两个套件 `OK`；`api_key` 既有断言不回归。

- [ ] **Step 5: 静态检查与提交。**

```bash
git diff --check -- vibe_tools/editor_credentials.py vibe_tools/tests/test_editor_credentials.py
git add vibe_tools/editor_credentials.py vibe_tools/tests/test_editor_credentials.py
git commit -m "feat(credentials): add named secret and delete APIs"
```

---

## Task 2: `codex_auth` 基础件——host id、PKCE、回调监听、authorize URL

**Files:**
- Create: `vibe_tools/codex_auth.py`
- Test: `vibe_tools/tests/test_codex_auth.py`

**Interfaces:**
- Produces：§0.4 常量、`host_id()`、`new_pkce()`、`build_authorize_url()`、`LoopbackCallback`、`CodexAuthError`。
- `host_id()`：优先 `secret("codex_host_id")`；为空则生成 `urn:uuid:<uuid4>`、`secret(...)` 写入并返回；并发下用进程内锁 + 二次读取。

- [ ] **Step 1: 写失败测试（含 RFC 7636 向量与 URL 断言）。**

```python
from vibe_tools import codex_auth as ca

class PkceTest(unittest.TestCase):
    def test_s256_against_rfc7636_vector(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(ca.pkce_challenge(verifier),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

class AuthorizeUrlTest(unittest.TestCase):
    def _qs(self, url):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

    def test_new_registration_params(self):
        url = ca.build_authorize_url(
            client_id=ca.DYNAMIC_CLIENT_ID, host_id="urn:uuid:abc",
            state="S", nonce="N", challenge="C",
            redirect_uri="http://127.0.0.1:1455/auth/callback",
            agent_name_hint=ca.AGENT_NAME_HINT)
        q = self._qs(url)
        self.assertEqual(q["client_id"], "dynamic_agent_client")
        self.assertEqual(q["agent_name_hint"], "VibeGodot")
        self.assertEqual(q["ext_agent_host_id"], "urn:uuid:abc")
        self.assertEqual(q["scope"], ca.SCOPE)
        self.assertEqual(q["resource"], ca.RESOURCE)
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertNotIn("localhost", url)
        self.assertEqual(urllib.parse.urlparse(url).path, "/api/accounts/authorize")

    def test_reauth_omits_agent_name_hint_and_keeps_host(self):
        q = self._qs(ca.build_authorize_url(
            client_id="oaiapp_x", host_id="urn:uuid:abc", state="S", nonce="N",
            challenge="C", redirect_uri="http://127.0.0.1:2/auth/callback",
            id_token_hint="IDT", login_hint="u@example.com"))
        self.assertNotIn("agent_name_hint", q)
        self.assertEqual(q["id_token_hint"], "IDT")
        self.assertEqual(q["ext_agent_host_id"], "urn:uuid:abc")

class HostIdTest(unittest.TestCase):
    def test_generates_urn_uuid_once_and_reuses(self):
        store = {}
        with patch.object(ca, "secret", side_effect=lambda n, v=None: store.get(n) if v is None else store.__setitem__(n, v) or v):
            first = ca.host_id(); second = ca.host_id()
        self.assertTrue(first.startswith("urn:uuid:"))
        self.assertEqual(first, second)
```

- [ ] **Step 2: 运行并确认失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_codex_auth -v`

Expected: `ModuleNotFoundError: No module named 'vibe_tools.codex_auth'`。

- [ ] **Step 3: 最小实现。** 纯标准库实现 `pkce_challenge`（`base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=")`）、
  `new_pkce`、`build_authorize_url`（`urllib.parse.urlencode`）、`LoopbackCallback`
  （`http.server.ThreadingHTTPServer` 绑 `127.0.0.1:0`，只接受 `GET /auth/callback`，把 query 解析后 `queue` 回传，
  单次使用、带 `close()` 与超时；响应页为静态极简 HTML）。

- [ ] **Step 4: 运行并通过。** 同 Step 2 命令，Expected `OK`。

- [ ] **Step 5: 静态检查与提交。**

```bash
git diff --check -- vibe_tools/codex_auth.py vibe_tools/tests/test_codex_auth.py
git add vibe_tools/codex_auth.py vibe_tools/tests/test_codex_auth.py
git commit -m "feat(codex): add siwc primitives (host id, pkce, loopback, authorize url)"
```

---

## Task 3: 回调校验、code 兑换、id_token 验签与 scope 判定

**Files:**
- Modify: `vibe_tools/codex_auth.py`
- Test: `vibe_tools/tests/test_codex_auth.py`

**Interfaces:**
- Produces：`validate_callback(query, *, expected_state, issued_client_id_expected: bool) -> dict`、
  `exchange_code(...) -> dict`、`validate_id_token(...) -> dict`、`discover()`（带内存缓存）。
- 兑换请求 form-encoded，含 `grant_type=authorization_code`、`client_id`（签发）、`code`、`code_verifier`、`redirect_uri`、`resource`。

- [ ] **Step 1: 写失败测试。** 覆盖：
  - `validate_callback`：state 不符抛 `CodexAuthError("state_mismatch")`；`error=access_denied` 抛 `CodexAuthError("access_denied")`；
    新注册缺 `client_id` 抛 `CodexAuthError("missing_client_id")`；成功返回含 `code` 与签发 `client_id`。
  - `exchange_code`：用 `unittest.mock.patch` 替换 HTTP 发送，断言 body 为 form-encoded 且字段齐全、`resource` 固定、`code_verifier` 为 verifier、无 `client_secret`。
  - `validate_id_token`：测试内生成 RSA 密钥对与 JWKS，签一个正确 token → 通过；逐个破坏签名/`iss`/`aud`/`exp`/`nonce` → 分别抛错。
    验签用 `cryptography`：只接受 `RS256`/`ES256`，拒绝 `none`/`HS256`。
  - `plan_enabled`：scope 含/不含 `chatgpt.tokens.use.direct` 两种结果。

```python
class IdTokenTest(unittest.TestCase):
    def test_rejects_alg_none(self):
        with self.assertRaises(ca.CodexAuthError):
            ca.validate_id_token("eyJhbGciOiJub25lIn0.e30.", jwks={"keys": []},
                                 client_id="c", nonce="n", issuer="https://auth.openai.com")
```

- [ ] **Step 2: 运行并确认失败。** Expected：`AttributeError: ... validate_callback`。

- [ ] **Step 3: 最小实现。** `urllib.request` 发 POST；`discover()` 取 `jwks_uri`/`revocation_endpoint` 并内存缓存；
  `validate_id_token` 手写 JWT 解析（`base64url` 解 header/payload，按 `kid` 找 JWK，`cryptography` 验签，再做 `iss/aud/exp/nonce` 校验）。
  所有异常消息只含错误类别，绝不带 token。

- [ ] **Step 4: 运行并通过。** 同 Task 2 命令，Expected `OK`。

- [ ] **Step 5: 提交。**

```bash
git diff --check -- vibe_tools/codex_auth.py vibe_tools/tests/test_codex_auth.py
git add vibe_tools/codex_auth.py vibe_tools/tests/test_codex_auth.py
git commit -m "feat(codex): validate callback, exchange code, verify id token"
```

---

## Task 4: 刷新轮换、撤销、账号注册表与安全存储

**Files:**
- Modify: `vibe_tools/codex_auth.py`
- Create: `vibe_tools/codex_accounts.py`
- Test: `vibe_tools/tests/test_codex_auth.py`、`vibe_tools/tests/test_codex_accounts.py`

**Interfaces:**
- Produces：`refresh(account) -> CodexAccount`、`revoke(account) -> bool`、`CodexAccount`（dataclass）、
  `account_id_for(...)`、`save_account/load_account/list_accounts/delete_account/active_account_id/set_active_account`。
- `refresh` 必须成组替换 access/refresh/expiry/scope/saved_at；`refresh_token_reused` 等映射为 `CodexAuthError`。

- [ ] **Step 1: 写失败测试。**
  - `refresh`：断言请求缺 `scope`、带签发 `client_id` 与 `resource`；成功响应后新 refresh token 覆盖旧的（轮换）；
    错误码列表逐一断言抛 `CodexAuthError` 且 `str(exc)` 不含 refresh token 值。
  - `revoke`：断言 `token_type_hint=refresh_token`、`client_id`；`200` → `True`；`5xx` → 有界重试后 `False`。
  - `account_id_for`：同输入稳定、不同 subject/client 不同；返回串不含 email。
  - 存储往返：mock `editor_credentials.secret`，`save_account` 后 `load_account` 等值；`list_accounts` 不含 token 字段。
  - 日志脱敏：`assertLogs` 捕获，断言 `access_token`/`refresh_token`/`id_token` 明文不在任何日志行。

```python
class NoSecretInLogsTest(unittest.TestCase):
    def test_refresh_does_not_log_secrets(self):
        acct = make_account(access_token="AT-SECRET", refresh_token="RT-SECRET")
        with self.assertLogs("vibe_tools.codex_auth", level="DEBUG") as cm:
            with patch.object(ca, "_post_form", return_value=ok_token_response()):
                ca.refresh(acct)
        joined = "\n".join(cm.output)
        self.assertNotIn("AT-SECRET", joined)
        self.assertNotIn("RT-SECRET", joined)
```

- [ ] **Step 2: 运行并确认失败。** Expected：`ImportError`/`AttributeError`。

- [ ] **Step 3: 最小实现。** `refresh`/`revoke` 走 `_post_form`；`CodexAccount.from_token_response` 统一构造；
  序列化 bundle 为紧凑 JSON 后 `secret("codex/"+account_id, json)`；`list_accounts` 只返回元数据；
  `active_account_id` 用非敏感状态文件（`0o600`、原子写）。

- [ ] **Step 4: 运行并通过。** 两个测试文件 Expected `OK`。

- [ ] **Step 5: 提交。**

```bash
git diff --check -- vibe_tools/codex_auth.py vibe_tools/codex_accounts.py \
  vibe_tools/tests/test_codex_auth.py vibe_tools/tests/test_codex_accounts.py
git add vibe_tools/codex_auth.py vibe_tools/codex_accounts.py \
  vibe_tools/tests/test_codex_auth.py vibe_tools/tests/test_codex_accounts.py
git commit -m "feat(codex): add token refresh, revocation and account store"
```

---

## Task 5: `codex_app_server` JSON-RPC/JSONL 客户端

**Files:**
- Create: `vibe_tools/codex_app_server.py`
- Test: `vibe_tools/tests/test_codex_app_server.py`

**Interfaces:**
- Produces：`CodexAppServer`（§3.2）；`TurnEvent`；`CodexAppServerError`；`ACCESS_TOKEN` 注入与脱敏日志。

- [ ] **Step 1: 写失败测试。** 用一个测试用假 app-server（`tests/fixtures/fake_codex_app_server.py`，纯 stdlib）
  按脚本读 stdin、写 JSONL。断言：
  - 启动参数含 `app-server --listen stdio://` 与 7 个 `-c` 配置；`ACCESS_TOKEN` 在子进程 env，`os.environ` 与 argv 均无。
  - `initialize` 先于 `initialized` 先于 `thread/start`；`initialize` 的 `clientInfo.name=="VibeGodot"`。
  - 事件顺序：`agentMessage/delta` 拼接、`turn/completed(completed)` 终止；`failed`/`interrupted` 映射 `TurnEvent`。
  - `interrupt` 发送 `turn/interrupt`；坏 JSON 行被跳过且不影响后续；`initialize` 无响应 → 超时 `CodexAppServerError`。
  - 子进程退出后 `alive()` 为假，`stop()` 幂等；日志无 `ACCESS_TOKEN` 值。

```python
class InjectionTest(unittest.TestCase):
    def test_access_token_only_in_child_env(self):
        token = "AT-ONLY-CHILD"
        srv = CodexAppServer(codex_exe=fake, cwd=tmp, access_token=token,
                             model="gpt-5.1-codex", log=self.logs.append)
        srv.start()
        self.assertNotIn("ACCESS_TOKEN", os.environ)
        self.assertIn("ACCESS_TOKEN", srv._child_env)
        self.assertEqual(srv._child_env["ACCESS_TOKEN"], token)
        self.assertNotIn(token, " ".join(srv._argv))
        self.assertNotIn(token, "\n".join(self.logs))
        srv.stop()
```

- [ ] **Step 2: 运行并确认失败。** Expected：`ModuleNotFoundError`。

- [ ] **Step 3: 最小实现。** `subprocess.Popen(..., text=True, bufsize=1, stdin=PIPE, stdout=PIPE, stderr=PIPE)`；
  后台 reader 线程逐行 `json.loads`，按 `id` 唤醒等待者，其余投递到通知回调与 `queue`；
  请求带自增 id + 超时 `threading.Event`；`run_turn` 生成器消费队列直到 `turn/completed`；
  收到服务端审批请求回 `decline`；`stop()` 关 stdin、`terminate`、超时 `kill`。

- [ ] **Step 4: 运行并通过。** Expected `OK`。

- [ ] **Step 5: 提交。**

```bash
git diff --check -- vibe_tools/codex_app_server.py vibe_tools/tests/test_codex_app_server.py \
  vibe_tools/tests/fixtures/fake_codex_app_server.py
git add vibe_tools/codex_app_server.py vibe_tools/tests/test_codex_app_server.py \
  vibe_tools/tests/fixtures/fake_codex_app_server.py
git commit -m "feat(codex): add app-server stdio jsonl client"
```

---

## Task 6: sidecar 集成——provider 选择、认证端点、事件映射

**Files:**
- Create: `vibe_tools/codex_provider.py`
- Modify: `vibe_tools/editor_sidecar.py`
- Test: `vibe_tools/tests/test_editor_sidecar.py`

**Interfaces:**
- Produces：`Runtime.auth_mode`、`chatgpt_supported` 探测、§3.1 端点、既有 SSE 事件映射（增量字段）。
- 保持：`SimpleLLMProvider` 分支逐字不变；`protocol_version=1`；Bearer 鉴权依赖不变。

- [ ] **Step 1: 写失败测试（FastAPI TestClient 风格与既有测试一致）。**
  - `GET /v1/configure` 新增字段；`auth_mode` 默认 `api_key`；`chatgpt_supported` 在无 codex 时为 `false`。
  - `POST /v1/configure {auth_mode:"chatgpt"}` 在 `chatgpt_supported=false` 时 `409`；`api_key` 模式仍 `200`。
  - `POST /v1/auth/chatgpt/start`：mock `codex_auth`/`webbrowser.open`，断言不返回 URL/令牌，返回 `login_id`。
  - `GET /v1/auth/chatgpt/status`：`pending → completed(account)`；`logout` 调 `revoke` 且不泄露令牌。
  - provider 分支：`auth_mode=chatgpt` 时 `/v1/agent/run` 走假 `CodexAppServer`，`item/agentMessage/delta` → SSE 文本增量，
    `turn/completed` → 正常结束；`auth_mode=api_key` 时仍走 `SimpleLLMProvider` 替身。
  - 脱敏：捕获日志断言无 `ACCESS_TOKEN`/refresh token 明文。

- [ ] **Step 2: 运行并确认失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_sidecar -v`

Expected：新用例失败（404/断言失败）。

- [ ] **Step 3: 最小实现。** 在 `editor_sidecar.py`：
  - 解析 `auth_mode`（默认 `api_key`；不支持平台强制 `api_key`）。
  - 新增 Pydantic 模型与路由；后台 `asyncio` 任务跑 OAuth 流程与状态机；`webbrowser.open` 在流程内调用。
  - 抽出 provider 工厂：`api_key → SimpleLLMProvider`；`chatgpt → CodexProvider`（`codex_provider.py`，封装 §2.4 流程与 §3.3 事件映射）。
  - app-server 生命周期挂在 `Runtime`，sidecar 关闭时 `stop()`；`/v1/shutdown` 语义不变。
  - 线程 ID 存 `.godot/agent/codex/thread.json`（`0o600`、原子写）。

- [ ] **Step 4: 运行并通过。**

Run: 同 Step 2 命令 + `python3 -m unittest vibe_tools.tests.test_editor_workflow -v`

Expected：`OK`；API Key 路径无回归。

- [ ] **Step 5: 提交。**

```bash
git diff --check -- vibe_tools/editor_sidecar.py vibe_tools/codex_provider.py \
  vibe_tools/tests/test_editor_sidecar.py
git add vibe_tools/editor_sidecar.py vibe_tools/codex_provider.py \
  vibe_tools/tests/test_editor_sidecar.py
git commit -m "feat(sidecar): add chatgpt auth endpoints and codex provider bridge"
```

---

## Task 7: 编辑器账号 UI（C++）

**Files:**
- Modify: `editor/plugins/agent_editor_plugin.cpp`
- Modify: `editor/plugins/agent_editor_plugin.h`
- Test: `vibe_tools/tests/test_agent_editor_plugin_source.py`（新增静态断言；C++ 无单测框架）

**Interfaces:**
- Consumes：`/v1/configure` 的 `auth_mode/chatgpt_supported/chatgpt_account`；`/v1/auth/chatgpt/*`。
- Produces：模式单选、**Continue with ChatGPT** 按钮、登录中轮询、已登录账号标签、退出登录；不支持平台隐藏入口。
- 保持：Windows 文案、sidecar 启动三段回退、握手校验、SSE 基础解析不变。

- [ ] **Step 1: 写失败测试（源码静态断言）。** 新增 Python 测试读取 `.cpp/.h`，断言存在：
  - 端点字符串 `"/v1/auth/chatgpt/start"`、`"/v1/auth/chatgpt/status"`、`"/v1/auth/chatgpt/logout"`；
  - 按钮/文案 `"Continue with ChatGPT"`；
  - 不支持平台时 ChatGPT 选项被隐藏的分支关键字（例如 `chatgpt_supported`）；
  - 既有 Windows 占位文案 `"已保存到 Windows 凭据管理器"` 仍存在（回归保护）。

- [ ] **Step 2: 运行并确认失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_agent_editor_plugin_source -v`

Expected：断言失败（字符串缺失）。

- [ ] **Step 3: 最小实现。** 设置弹窗增加账号区与按钮；`HTTPClient` 调用新端点；用既有定时器/`_process` 轮询状态；
  文案走 `_u8(...)`；Windows 分支行为不变。所有新增字符串使用 `TTR`/既有本地化风格。

- [ ] **Step 4: 运行并通过 + 编译门禁（macOS arm64）。**

```bash
PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_agent_editor_plugin_source -v
python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4
test -f bin/godot.macos.editor.arm64
```

Expected：测试 `OK`；编译退出码 0，产物存在。

- [ ] **Step 5: 提交。**

```bash
git diff --check -- editor/plugins/agent_editor_plugin.cpp editor/plugins/agent_editor_plugin.h \
  vibe_tools/tests/test_agent_editor_plugin_source.py
git add editor/plugins/agent_editor_plugin.cpp editor/plugins/agent_editor_plugin.h \
  vibe_tools/tests/test_agent_editor_plugin_source.py
git commit -m "feat(editor): add sign in with chatgpt account ui"
```

---

## Task 8: macOS arm64 打包随附 codex 二进制

**Files:**
- Create: `misc/vibe_agent/fetch_codex.sh`
- Modify: `misc/vibe_agent/build_sidecar.sh`（若存在）
- Modify: `misc/vibe_agent/package_macos.sh`
- Test: 脚本 `bash -n` + 产物断言

**Interfaces:**
- `fetch_codex.sh`：`CODEX_VERSION`、`CODEX_SHA256` 固定；`CODEX_BIN` 覆盖；下载/校验/`chmod 0755` 到 `.build/codex/codex`。
- sidecar 解析顺序见 §6.2；`package_macos.sh` 拷贝到 `Contents/Resources/vibe_agent/codex/codex`。
- 不修改 `*.ps1`。

- [ ] **Step 1: 写失败断言（在实现前运行，确认当前失败）。**

```bash
bash -n misc/vibe_agent/fetch_codex.sh
grep -q 'vibe_agent/codex/codex' misc/vibe_agent/package_macos.sh
```

Expected：第一条 `No such file`；第二条 grep 非 0。

- [ ] **Step 2: 最小实现。** 新增脚本并接入 `package_macos.sh`；`build_sidecar.sh` 追加 `--collect-all cryptography`。

- [ ] **Step 3: 运行并通过（macOS arm64）。**

```bash
bash -n misc/vibe_agent/fetch_codex.sh
bash misc/vibe_agent/fetch_codex.sh
test -x .build/codex/codex
file .build/codex/codex | grep -q 'arm64'
bash misc/vibe_agent/package_macos.sh
test -x ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Resources/vibe_agent/codex/codex"
".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Resources/vibe_agent/codex/codex" --version
```

Expected：全部退出码 0；`file` 显示 `Mach-O ... arm64`；`--version` 输出固定版本。

- [ ] **Step 4: 提交。**

```bash
git diff --check -- misc/vibe_agent/fetch_codex.sh misc/vibe_agent/build_sidecar.sh \
  misc/vibe_agent/package_macos.sh
git add misc/vibe_agent/fetch_codex.sh misc/vibe_agent/build_sidecar.sh \
  misc/vibe_agent/package_macos.sh
git commit -m "build(macos): bundle pinned codex app-server binary"
```

---

## Task 9: Windows 兼容门控与回归

**Files:**
- Modify: `vibe_tools/editor_sidecar.py`（门控分支，已在 Task 6 预留）
- Modify: `vibe_tools/tests/test_editor_sidecar.py`（Windows 门控用例）
- 不改任何 `*.ps1`

**Interfaces:**
- `chatgpt_supported` 在 Windows 恒 `false`；`POST /v1/configure {auth_mode:"chatgpt"}` → `409`；
  `auth_mode` 读值恒为 `"api_key"`。

- [ ] **Step 1: 写失败/门控测试。** 用 `patch.object(platform, "system", ...)`/`patch.object(os, "name", "nt")` 模拟 Windows：
  断言 `chatgpt_supported=false`、`409`、`auth_mode=="api_key"`、`/v1/auth/chatgpt/start` 返回 `501/unsupported`。

- [ ] **Step 2: 运行并确认预期（先失败后通过）。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_sidecar -v`

- [ ] **Step 3: 最小实现门控**（若 Task 6 已满足，则此处只补测试）。

- [ ] **Step 4: 提交。**

```bash
git diff --check -- vibe_tools/editor_sidecar.py vibe_tools/tests/test_editor_sidecar.py
git add vibe_tools/editor_sidecar.py vibe_tools/tests/test_editor_sidecar.py
git commit -m "test(windows): gate chatgpt sign-in behind platform capability"
```

---

## Task 10: 文档与能力表

**Files:**
- Modify: `README.md`
- Modify: `misc/vibe_agent/README.zh-CN.md`
- Modify: `misc/vibe_agent/CAPABILITIES.md`

- [ ] **Step 1: 写失败断言。**

```bash
grep -q 'Sign in with ChatGPT' README.md
grep -q 'Continue with ChatGPT' misc/vibe_agent/README.zh-CN.md
grep -q 'ChatGPT' misc/vibe_agent/CAPABILITIES.md
grep -c 'Windows' misc/vibe_agent/README.zh-CN.md
```

Expected：前三条失败；第四条 ≥ 1（Windows 内容仍在）。

- [ ] **Step 2: 最小实现。** 文档说明：macOS arm64 优先；两种模式；授权后令牌只进 Keychain；
  登出撤销；账号切换；不支持平台提示；未签名/未公证 + quarantine 限制；**不出现任何真实令牌或授权 URL**。

- [ ] **Step 3: 运行并通过。** 同 Step 1 断言全部通过。

- [ ] **Step 4: 提交。**

```bash
git diff --check -- README.md misc/vibe_agent/README.zh-CN.md misc/vibe_agent/CAPABILITIES.md
git add README.md misc/vibe_agent/README.zh-CN.md misc/vibe_agent/CAPABILITIES.md
git commit -m "docs: document sign in with chatgpt usage and limits"
```

---

## Task 11: 集成验收（无文件改动，作为最终门禁）

**Files:** 无（只运行命令；本任务不产生 commit）。

- [ ] **Step 1: 全量 Python 单元测试（含离线替身）。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest discover -s vibe_tools/tests -v`

Expected：`OK`；`test_codex_*`、`test_editor_credentials`、`test_editor_sidecar`、`test_editor_workflow` 全绿。

- [ ] **Step 2: Godot 进程集成回归。**

Run: `GODOT_TEST_EXE="$PWD/bin/godot.macos.editor.arm64" PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest discover -s vibe_tools/tests -v`

Expected：`OK`（模型/视觉用离线替身，不触网）。

- [ ] **Step 3: 假 app-server 端到端（无需真实令牌）。**

Run: 用 `VIBE_CODEX_APP_SERVER`（测试注入点）指向 `tests/fixtures/fake_codex_app_server.py`，启动真实 sidecar，走
`/v1/configure` 设 `auth_mode=chatgpt`（测试用假凭据注入）→ `/v1/agent/run` → 断言 SSE 文本增量与正常结束。

Expected：SSE 收到拼接后的完整文本与结束事件；进程与日志中无令牌明文。

- [ ] **Step 4: 真实登录人工验收（macOS arm64，只有此步触网，且不记录任何令牌）。**
  1. `open .build/VibeGodot-macOS-arm64/VibeGodot.app`。
  2. 设置 → 选 ChatGPT → 点 **Continue with ChatGPT** → 浏览器完成授权。
  3. 确认 UI 显示账号与计划状态；发起一轮对话，观察流式输出与文件/命令工具事件。
  4. 取消一轮对话；重启后台后确认可继续（`thread/resume`）。
  5. 退出登录，确认远端撤销（`revoke` 返回成功）且 UI 回到未登录。
  6. 仅验证 Keychain 条目存在（**绝不打印值**）：
     `/usr/bin/security find-generic-password -s 'Godot/VibeAgent/codex_host_id' >/dev/null; echo $?` 期望 `0`。
  7. `grep -iE 'access_token|refresh_token|id_token' .godot/agent/sidecar.log` 期望无令牌明文（仅可能出现被脱敏的字段名）。

- [ ] **Step 5: Windows 回归（Windows 主机）。**

Run（PowerShell）:

```powershell
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
python -m unittest discover -s vibe_tools/tests -v
.\misc\vibe_agent\build_sidecar.ps1
.\misc\vibe_agent\package_windows.ps1 -SkipBuild -SkipSidecarBuild -Zip
```

Expected：套件 `OK`；`chatgpt_supported=false` 门控用例通过；`.ps1` 未被本计划修改。

- [ ] **Step 6: 记录结论。** 无 commit；把 Step 1–5 实际输出、未执行项及原因写入实施报告。

---

## 15. 最终变更文件清单

| 文件 | 类型 | 所属任务 | 说明 |
| --- | --- | --- | --- |
| `vibe_tools/codex_auth.py` | 新增 | 2、3、4 | SIWC/PKCE/回调/兑换/验签/刷新/撤销 |
| `vibe_tools/codex_accounts.py` | 新增 | 4 | 账号注册表与安全存储 |
| `vibe_tools/codex_app_server.py` | 新增 | 5 | app-server stdio JSONL 客户端 |
| `vibe_tools/codex_provider.py` | 新增 | 6 | 事件映射与 turn 生命周期桥 |
| `vibe_tools/editor_credentials.py` | 改 | 1 | `secret()`/`delete_secret()`；`api_key` 语义不变 |
| `vibe_tools/editor_sidecar.py` | 改 | 6、9 | provider 选择、认证端点、门控 |
| `vibe_tools/tests/test_codex_auth.py` | 新增 | 2、3、4 | PKCE/URL/回调/兑换/验签/刷新/撤销/脱敏 |
| `vibe_tools/tests/test_codex_accounts.py` | 新增 | 4 | 摘要/存储/无令牌日志 |
| `vibe_tools/tests/test_codex_app_server.py` | 新增 | 5 | 初始化/线程/turn/中断/崩溃/注入面 |
| `vibe_tools/tests/fixtures/fake_codex_app_server.py` | 新增 | 5、11 | 离线假 app-server |
| `vibe_tools/tests/test_editor_credentials.py` | 改 | 1 | `secret()`/`delete_secret()` |
| `vibe_tools/tests/test_editor_sidecar.py` | 改 | 6、9 | 新字段/端点/门控 |
| `vibe_tools/tests/test_agent_editor_plugin_source.py` | 新增 | 7 | C++ 源码静态断言 |
| `editor/plugins/agent_editor_plugin.cpp` | 改 | 7 | 账号 UI 与轮询 |
| `editor/plugins/agent_editor_plugin.h` | 改（按需） | 7 | 新增声明 |
| `misc/vibe_agent/fetch_codex.sh` | 新增 | 8 | 固定版本 codex 下载/校验 |
| `misc/vibe_agent/build_sidecar.sh` | 改 | 8 | 打包 `cryptography` |
| `misc/vibe_agent/package_macos.sh` | 改 | 8 | 随包 codex + 架构断言 |
| `README.md`、`misc/vibe_agent/README.zh-CN.md`、`misc/vibe_agent/CAPABILITIES.md` | 改 | 10 | 使用/能力/限制 |
| `docs/superpowers/plans/2026-10-16-codex-chatgpt-plan-integration.md` | 新增 | 本计划提交 | 仅本文件；实现阶段不再改动 |

**明确不改**：`misc/vibe_agent/*.ps1`、`vibe_tools/godot_runner.py`、`vibe_tools/editor_behavior.py`、
`vibe_tools/editor_harness.py`、`vibe_tools/web_server.py`、`vibe_tools/static/`、`vibe_tools/templates/`、
`editor/editor_node.cpp`、`platform/**`、`SConstruct`、`modules/**`、`thirdparty/**`。

---

## 16. 验收矩阵

| # | 验收项 | 命令 | 期望 | 平台 | 任务 |
| --- | --- | --- | --- | --- | --- |
| A1 | 计划文件本身 | `git diff --check`；`test -s docs/superpowers/plans/2026-10-16-codex-chatgpt-plan-integration.md` | 无输出；退出码 0 | 任意 | 本提交 |
| A2 | 凭据 API | `python3 -m unittest vibe_tools.tests.test_editor_credentials -v` | `OK`，`api_key` 不回归 | 任意 | 1 |
| A3 | OAuth 基础件 | `python3 -m unittest vibe_tools.tests.test_codex_auth -v` | `OK`，含 RFC 7636 向量 | 任意 | 2、3、4 |
| A4 | 账号存储 | `python3 -m unittest vibe_tools.tests.test_codex_accounts -v` | `OK`，无令牌日志 | 任意 | 4 |
| A5 | app-server 客户端 | `python3 -m unittest vibe_tools.tests.test_codex_app_server -v` | `OK`，注入面受限、崩溃恢复 | 任意 | 5 |
| A6 | sidecar 集成 | `python3 -m unittest vibe_tools.tests.test_editor_sidecar -v` | `OK`，新端点/映射/门控 | 任意 | 6、9 |
| A7 | workflow 回归 | `python3 -m unittest vibe_tools.tests.test_editor_workflow -v` | `OK` | 任意 | 6 |
| A8 | 编辑器源码断言 | `python3 -m unittest vibe_tools.tests.test_agent_editor_plugin_source -v` | `OK` | 任意 | 7 |
| A9 | 编辑器编译 | `python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4` | 退出码 0，产物存在 | macOS arm64 | 7 |
| A10 | codex 获取与架构 | `bash misc/vibe_agent/fetch_codex.sh`；`file .build/codex/codex` | `Mach-O ... arm64`、哈希匹配 | macOS arm64 | 8 |
| A11 | `.app` 随包 | `test -x .../Resources/vibe_agent/codex/codex`；`codex --version` | 存在且可执行 | macOS arm64 | 8 |
| A12 | 全量 Python | `python3 -m unittest discover -s vibe_tools/tests -v` | `OK` | 任意 | 11 |
| A13 | Godot 集成 | `GODOT_TEST_EXE=... python3 -m unittest discover ...` | `OK`（离线替身） | macOS arm64 | 11 |
| A14 | 假 app-server E2E | Task 11 Step 3 脚本 | SSE 完整、无令牌明文 | 任意 | 11 |
| A15 | 真实登录人工验收 | Task 11 Step 4 | 授权/对话/取消/重启/登出/日志无令牌 | macOS arm64 | 11 |
| A16 | Windows 回归 | Task 11 Step 5 PowerShell | 套件 `OK`、`.ps1` 未改 | Windows | 9、11 |
| A17 | 文档断言 | Task 10 Step 1 脚本 | 全部通过，Windows 章节保留 | 任意 | 10 |

---

## 17. 边界、错误路径与平台限制汇总

- **令牌生命周期**：access 1h；refresh 30 天且**每次刷新轮换**；刷新前 `REFRESH_SKEW_S=120` 预刷新；
  刷新成功必须成组替换；同账号刷新串行（进程内锁 + 跨进程文件锁）；`refresh_token_reused` 视为竞态，重读后仍冲突才要求重登。
- **回调**：`127.0.0.1` 随机端口、路径固定 `/auth/callback`、单次使用、总超时 300s；`state` 强校验；
  新注册必须有签发 `client_id`，再次登录回调如带不同 `client_id` 一律拒绝。
- **id_token**：`RS256`/`ES256`，`iss=https://auth.openai.com`，`aud==client_id`，校验 `exp` 与 `nonce`；
  只接受 JWKS 中匹配 `kid` 的键。
- **计划能力**：scope 缺 `chatgpt.tokens.use.direct` 时保留身份、禁用计划用量，不静默换计费；普通登录不强制 consent。
- **app-server**：`approvalPolicy:"never"` + `workspaceWrite`（本阶段不做审批 UI）；`turn/completed.status` 只有 `completed` 成功；
  崩溃有界重启并 `thread/resume`；配置参数以所装 `codex` 版本的生成 schema 为准。
- **Windows**：功能门控为 `false`，API Key 与 `.ps1` 完全不变。
- **非目标**：Intel/universal、签名/公证、Windows ChatGPT 模式、Responses 直连、WebSocket、托管/多租户。

### 17.1 非 macOS 主机无法完成项

| 验证项 | 原因 | 可在哪完成 |
| --- | --- | --- |
| 编译 `bin/godot.macos.editor.arm64` | 需 macOS SDK/Xcode CLT/arm64 工具链 | macOS arm64 |
| Keychain 真实往返与 `security find-generic-password` | Security.framework 仅 macOS | macOS |
| `.app` 组装、`PlistBuddy`、`ditto`、quarantine | macOS 专用工具 | macOS arm64 |
| codex macOS aarch64 资产与 `Mach-O arm64` 断言 | 需 macOS 资产/`file` | macOS arm64 |
| 真实浏览器授权与 ChatGPT 计划推理 | 需用户 ChatGPT 账号与网络；自动化不触网 | 人工，macOS arm64（不记录令牌） |
| Windows 门控与 PowerShell 打包回归 | 仅 Windows | Windows |

### 17.2 安全声明（实现与本计划共同约束）

- 自动化测试从不使用真实令牌；真实登录只在 Task 11 Step 4 的人工步骤发生，且只检查“存在性/无明文/无匹配”，
  不打印、不复制、不上传任何令牌；测试替身与日志一律脱敏。
- 本计划文档本身不包含任何真实凭据、令牌、cookie、授权码或密钥。

---

## 18. 风险与后续阶段

- **未签名应用的 Keychain 授权弹窗**：sidecar/编辑器重建后签名身份变化，读取既有条目时 macOS 可能要求授权；
  属既有“不做签名/公证”限制的直接后果，文档需说明（选择“始终允许”）。
- **app-server 版本漂移**：协议字段随 `codex` 版本演进；本计划固定版本并在实现前用 `generate-json-schema` 核对；
  升级版本时须重跑 Task 5/11 并更新 `CODEX_VERSION`/`CODEX_SHA256`。
- **刷新竞态**：多编辑器实例共享账号时依赖跨进程锁；若锁不可用（如不支持 `fcntl`），退化为串行重读 + 失败重登。
- **内存擦除限制**：Python 字符串不可变，令牌无法保证即时擦除；通过最小生命周期与禁日志缓解，并在文档中如实说明。
- **后续阶段（非本计划）**：审批 UI（`item/*/requestApproval` 映射到编辑器对话框）、Windows ChatGPT 模式、
  代码签名/公证、`id_token_hint` 的加密缓存、用量/速率限制面板（`account/rateLimits/*`）。

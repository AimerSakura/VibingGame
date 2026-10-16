# API Key 与 ChatGPT/Codex 模式模型配置隔离设计（实际边界）

> 状态：已实现。本文记录本次落地后的真实契约与边界，不含占位符。
> 对应提交：`feat(agent): isolate API and ChatGPT model settings`。

## 1. 背景与问题

原设置界面在 API Key 与 ChatGPT/Codex 两种认证模式之间复用同一组
`base_url` / `model` / `vision_model`：

- ChatGPT/Codex 会话把 API Key 模式的 `model`（旧默认
  `claude-sonnet-4-20250514`）直接交给 `codex app-server` 的
  `thread/start`，属于跨模式默认值兜底。
- 两种模式互相覆盖：保存 API Key 配置会重置 Codex 配置，反之亦然。
- ChatGPT/Codex 模式没有可用的模型清单，也没有图片输入通道；图片只走
  API Key 的 `image_url` 路径。

本次实现把两种模式的模型配置、模型来源、图片通道彻底拆开，并对旧配置做
单向迁移。

## 2. 模式与模型来源

| 模式 | 平台 | 模型来源 | 视觉能力 | 回退规则 |
| --- | --- | --- | --- | --- |
| API Key | Windows、macOS、Linux | 兼容 OpenAI 的 `client.models.list()`（`SimpleLLMProvider.list_model_catalog()`） | 主模型 `supports_image` 未知（`null`）；视觉请求使用单独配置的 `vision_model` | `models.list` 不可用/为空时返回 `manual=true` 与 `model_list_unavailable`，界面保留“手动输入模型 ID” |
| ChatGPT / Codex | Windows x86_64、macOS arm64 | 随包 `codex app-server` 的 `model/list`（`CodexProvider.list_models()`） | 由该模型 `inputModalities` 是否含 `image` 决定 | 列表失败返回固定错误码；不使用任何 `claude` / `gemini` 默认字符串，不把 API 模式模型传给 Codex |

Linux 及其他平台保持 API Key-only：`chatgpt_supported=false`，ChatGPT
选项禁用，`/v1/auth/chatgpt/*` 返回 409。

## 3. 模型列表 JSON 契约

端点：`GET /v1/models`（与其他 sidecar 端点一致，需要
`Authorization: Bearer <session token>`）。

顶层字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `mode` | `"api_key"` \| `"chatgpt"` | 当前生效模式 |
| `source` | 同上 | 与 `mode` 相同，显式标注来源 |
| `models` | `ModelEntry[]` | 当前模式的候选模型 |
| `manual` | `bool` | `true` 表示界面必须保留手动输入回退（仅 API 模式会出现） |
| `error` | `string \| null` | 失败时的固定安全错误码，成功为 `null` |
| `selected` | `string` | 当前该模式已保存的模型 id |
| `protocol_version` | `int` | 固定为 `1` |

`ModelEntry` 字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | `string` | 用于请求的模型 id，必需、有界 |
| `label` | `string` | 展示名 |
| `display_name` | `string` | 展示名（与 `label` 相同） |
| `supports_image` | `bool \| null` | Codex 由 `inputModalities` 推导；API 模式恒为 `null`（未知） |
| `is_default` | `bool` | 是否为服务端默认模型（Codex `isDefault`；API 模式恒 `false`） |
| `hidden` | `bool` | 是否隐藏（Codex），列表默认不返回隐藏项 |
| `supported_reasoning_efforts` | `string[]` | Codex 推理档位（最多 16 项） |
| `selected` | `bool` | 是否等于 `selected` |

固定错误码（全部为无凭据、可读的安全值）：

- `api_key_missing`：API 模式尚未保存 API Key。
- `model_list_unavailable`：API 接口不支持或未返回模型列表。
- `chatgpt_account_missing`：ChatGPT 模式尚未登录账号。
- `chatgpt_models_unavailable`：Codex `model/list` 调用失败（含不支持）。
- `chatgpt_model_missing`：ChatGPT 模式运行 Agent 时未选择 Codex 模型。
- `codex_image_unsupported`：所选 Codex 模型不含 `image` input modality。
- `codex_image_path_invalid`：`localImage` 路径非法或越界。

## 4. Codex app-server 协议

`CodexAppServer` 新增只读方法：

- `list_models(*, include_hidden=False) -> list[CodexModel]`：发送
  `model/list`（参数 `includeHidden`，可带分页 `cursor`），解析
  `{data: [...], nextCursor}`，`nextCursor` 存在时按上限（10 页）继续。
  仅返回非隐藏项（除非 `include_hidden=True`）。**不**创建 thread、**不**
  启动 turn、**不**写 `thread.json`。
- `CodexModel`（`dataclass(frozen=True)`）：`id`、`display_name`、
  `supports_image`、`is_default`、`hidden`、`supported_reasoning_efforts`。
  解析规则：`id` 取 `id` 或 `model`；展示名取 `displayName`/`display_name`；
  `supports_image` 仅在 `inputModalities` 含 `image` 时为真，缺失一律为假；
  推理档位兼容字符串与 `{effort: ...}` 两种形状。

`run_turn(thread_id, text, images=None)` 的 `turn/start` UserInput 为：

```json
[
  {"type": "text", "text": "..."},
  {"type": "localImage", "path": "/abs/stage/.godot/agent/attachments/x.png"}
]
```

文本调用保持兼容：不传 `images` 时输入仍是单个 `text` 项。

## 5. 图片输入与路径边界

- ChatGPT/Codex 模式的参考图片只能经 `turn/start` 的 `localImage` 传输，绝不
  回退到 API Key 的 `image_url` 通道。验收 Harness 的截图视觉判断在 ChatGPT
  模式下也返回不可用（`vision=None`），而不是偷偷使用 API Key。
- sidecar 在创建 `Workspace` 后，把附件复制到
  `<stage>/.godot/agent/attachments/<name>`（目录 0700、文件 0600）。该目录位于
  `Workspace.IGNORED` 的 `.godot` 树下，`Workspace.changes()` / `commit` 不会把
  它当作项目改动；`Workspace.close()` 会随 stage 一起删除副本。
- `CodexProvider._safe_images` 只接受绝对路径、必须存在、必须是常规文件，且
  `resolve()` 后必须仍在 workspace stage 内，否则抛
  `codex_image_path_invalid`，且不会启动 app-server。
- 图片 turn 前 `CodexProvider` 调用一次 `model/list` 校验所选模型声明了
  `image` input modality；未声明或模型不在列表中一律抛
  `codex_image_unsupported`（fail closed）。
- 界面侧：模型列表返回的 `supports_image=false` 会禁用“添加参考图片”、
  拖放与剪贴板入口；服务器仍是最终强制点。

## 6. 配置分离与旧配置迁移

sidecar 配置项：

```python
{
  "response_language": "zh-CN",
  "base_url": "", "model": "claude-sonnet-4-20250514", "vision_model": "gemini-2.5-flash",
  "codex_model": "",
}
```

- `POST /v1/configure` 只合并请求中显式出现的字段（Pydantic
  `model_fields_set` + `exclude_none`）。保存 API Key 模式不会重置
  `codex_model`，保存 ChatGPT 模式不会重置 `model` / `vision_model`。
- 编辑器 Editor Settings 键：旧 `vibe_agent/model` 与
  `vibe_agent/vision_model` **只作为 API Key 模式的初始值**；新增
  `vibe_agent/codex_model` 保存 Codex 模型，二者互不覆盖。切换模式后各自
  的值保留。
- `codex app-server` 的线程/模型只取 `codex_model`；`model/list` 的返回值
  决定可选项与默认项，任何 API 模式字符串都不会注入 Codex。

## 7. 安全不变式

- `ACCESS_TOKEN` 仍只注入 app-server 子进程环境；模型列表与图片路径流程不
  新增任何令牌读取、日志、SSE 或 argv 暴露面。
- 模型 id 在进入界面/请求前经过有界字符集过滤（字母、数字与
  `-_.:/@+`）；错误只返回固定码，绝不回显 sidecar 原始异常文本。
- 附件副本位于项目 `.godot` 忽略树内，且路径经项目目录与 stage 目录双重
  边界校验，不会逃逸到项目外或进入提交。
- 文档、日志与测试只使用占位符，不含真实 API Key、OAuth token、Cookie 或
  账号令牌。

## 8. 平台与兼容性

- macOS arm64、Windows x86_64：完整 ChatGPT/Codex 能力（登录、模型列表、
  图片输入）。
- Linux 及未随包 Codex 的平台：仅 API Key，行为与旧版一致；`/v1/models`
  仍工作并提供手动回退。
- 旧 SSE 事件、`/v1/configure` 旧字段、文本 `run_turn` 调用保持向后兼容。

## 9. 验证

- `vibe_tools/tests/test_codex_app_server.py`：`model/list` 解析、分页、
  隐藏项、`localImage` UserInput、文本调用不回归。
- `vibe_tools/tests/test_codex_provider.py`：`list_models` 不建线程/不跑
  turn、错误码安全、图片路径边界、无 image modality 拒绝。
- `vibe_tools/tests/test_editor_sidecar.py`：配置隔离、`/v1/models` 契约、
  附件 stage 边界与不提交、图片错误可读文案。
- `vibe_tools/tests/test_llm_transfer.py`：API 模型目录归一化与
  `models.list()` 回退。

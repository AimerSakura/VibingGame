# Agent 模型模式隔离实施计划（实际执行记录）

> 本计划记录**已执行**的步骤，全部条目已勾选。设计边界见
> `docs/superpowers/specs/2026-10-16-model-mode-isolation-design.md`。

**目标：** 把 API Key 与 ChatGPT/Codex 两种认证模式的模型配置、模型来源和
图片输入通道彻底拆开；新增 `/v1/models` 模型列表契约与 Codex
`model/list` 桥接；ChatGPT/Codex 图片经 `turn/start` 的 `localImage` 传输
并受 stage 路径边界约束；旧 `vibe_agent/model`、`vibe_agent/vision_model`
只作为 API Key 模式初始值。

**技术栈：** Godot C++（`editor/plugins/agent_editor_plugin.*`）、Python 3、
FastAPI/Pydantic v2、Codex app-server（stdio JSONL）、unittest。

**基线：** `d5f01d974de033c72fe7ddc2894fbf60be76e4b8`。

## 全局约束

- 只改任务允许的路径；不碰 `.build`、`.git`、`bin`、`.godot`、`thirdparty`、
  `platform`、`scene`、`servers`、`tests`（根目录）、`editor`（除插件两文件）、
  凭据/账户文件与打包脚本。
- 不读取、不写入、不打印任何真实 API Key、OAuth token、Cookie 或账号令牌；
  测试与文档只用占位符。
- 先写失败测试再实现；每个验证命令的结果记录在提交说明中。
- 保持旧 SSE/`/v1/configure`/文本 turn 调用向后兼容。

## 任务清单

### 1. TDD 失败测试
- [x] 新建 `vibe_tools/tests/test_llm_transfer.py`：API 模型目录归一化、
  `models.list()` 空/异常回退、`list_models()` 旧行为不回归。
- [x] 扩展 `vibe_tools/tests/test_codex_app_server.py`：`model/list` 解析、
  分页、隐藏项、`inputModalities` 判定、`localImage` UserInput、文本调用。
- [x] 扩展 `vibe_tools/tests/test_codex_provider.py`：`list_models` 无线程/
  无 turn、固定错误码、图片路径边界、text-only 模型拒绝图片、无图片不查
  模型。
- [x] 扩展 `vibe_tools/tests/test_editor_sidecar.py`：配置互不覆盖、
  `/v1/models` 契约与手动回退、附件 stage 目录且不提交、图片错误可读文案、
  缺 Codex 模型报错。
- [x] 运行失败测试并记录原因（导入缺失 / 404 / 配置被覆盖）。

### 2. Python 实现
- [x] `llm_transfer.py`：新增 `normalize_api_model_entries()` 与
  `SimpleLLMProvider.list_model_catalog()`；保留 `list_models()`。
- [x] `codex_app_server.py`：新增 `CodexModel`、`list_models()`、
  `_models_page()` / `_model_from()` / `_input_modalities()` /
  `_reasoning_efforts()`；`run_turn(..., images=None)` 与 `_user_input()`。
- [x] `codex_provider.py`：新增 `list_models()`（隔离临时 cwd，不写
  `thread.json`、不跑 turn）、`_safe_images()`、`_require_image_support()`；
  扩展 `run_agent(..., images=None)`；新增 `ERR_MODELS_UNAVAILABLE`、
  `ERR_IMAGE_UNSUPPORTED`、`ERR_IMAGE_PATH_INVALID`、`ERR_MODEL_MISSING`。
- [x] `editor_sidecar.py`：`ConfigureRequest` 增加可选 `codex_model` 并按
  `model_fields_set` 合并；`_codex_provider()` 只读 `codex_model`；
  `_provider(visual=True)` 在 ChatGPT 模式返回 `None`；新增
  `_attachment_items()` / `_encode_images()` / `_stage_attachment_paths()`；
  `_run_workflow()` 按模式传 base64 或 stage 图片路径；新增
  `/v1/models` 与 `_model_catalog_payload()`；补充固定错误码文案。

### 3. C++ 编辑器
- [x] `agent_editor_plugin.h`：新增 `REQUEST_MODELS`、`api_section`、
  `api_model_choices`、`codex_model`、`codex_model_status`、
  `attach_button`、能力/请求标志位与相应方法声明。
- [x] `agent_editor_plugin.cpp`：
  - [x] API Key 字段组与 ChatGPT/Codex 字段组按模式切换可见性。
  - [x] Codex 模型用 `OptionButton`，从 `/v1/models` 填充，不设默认字符串。
  - [x] API 模型增加候选列表，接口不支持时保留可编辑的手动输入框。
  - [x] `_push_configuration()` / `_save_settings()` 提交并持久化
    `codex_model`（键 `vibe_agent/codex_model`），不写入旧 API 键。
  - [x] `supports_image` 为假时禁用附件入口；`model/list` 失败给安全提示。
  - [x] 登录/登出/切换模式/重启 sidecar 时重置模型列表缓存标志。

### 4. 文档
- [x] 新增 `docs/superpowers/specs/2026-10-16-model-mode-isolation-design.md`。
- [x] 新增本计划文档。
- [x] 更新 `README.md`：两种模式的模型来源、视觉能力、失败回退。
- [x] 更新 `misc/vibe_agent/README.zh-CN.md` 与
  `misc/vibe_agent/CAPABILITIES.md`。

### 5. 验证（实际结果见提交说明）
- [x] `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest
      vibe_tools.tests.test_codex_app_server vibe_tools.tests.test_codex_provider
      vibe_tools.tests.test_editor_sidecar vibe_tools.tests.test_llm_transfer`
- [x] `GODOT_EXE=/usr/bin/true PYTHONPATH="$PWD/.build/pythonlibs:$PWD"
      python3 -m unittest discover -s vibe_tools/tests`
- [x] `git diff --check`
- [x] `git status --short --branch`
- [x] `python3 -m py_compile`（全部改动 Python 文件）
- [x] C++：`clang++ -fsyntax-only` 因缺少生成头文件
  （`gdextension_interface.gen.h`）无法进行；改用括号/花括号平衡与人工
  逐段复核，并记录该环境限制。

### 6. 提交
- [x] 确认工作区仅含允许路径改动，单独提交：
  `feat(agent): isolate API and ChatGPT model settings`。

## 风险与回退

- 旧 Codex 版本若不支持 `model/list`，ChatGPT 模式模型列表返回
  `chatgpt_models_unavailable`，界面提示重新登录；文本 turn 不受影响。
- API 服务若不支持 `models.list`，界面保留手动输入，不阻塞保存。
- 文本 turn 调用签名保持兼容（`images` 为可选关键字参数），旧测试与调用
  不需修改即可通过。

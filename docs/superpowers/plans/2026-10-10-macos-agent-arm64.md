# macOS arm64 原生 VibeGodot Agent 迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将原生 VibeGodot Agent 从 Windows-only 迁移到 macOS arm64，保留 Windows 行为和现有握手/任务/验收流程。

**Architecture:** 通过平台分支选择 sidecar 路径；macOS 用 Security.framework Keychain 保存 API Key；新增 arm64 sidecar/editor 构建和 .app 打包脚本；不改 Agent 协议和 Plus/OAuth。

**Tech Stack:** Godot C++/Objective-C++ platform APIs, Python 3, FastAPI/uvicorn, PyInstaller onedir, macOS arm64, unittest, Bash.

## Global Constraints

- 只支持 macOS arm64；构建目标 macOS 11.0+，不做 Intel/universal。
- Windows Credential Manager、Windows sidecar 脚本和 Windows Agent 行为保持不变。
- macOS Keychain 使用 Security.framework ctypes；不使用 security CLI，不落明文，不写入日志/启动参数/环境变量。
- 保持 protocol_version=1、随机 loopback 端口、Bearer token、握手 PID/token 校验、父进程看门狗、取消/重启/回滚/Tests 流程。
- 本计划不实现 Plus/ChatGPT OAuth、代码签名/公证、旧 web_server 或 Windows 导出能力迁移。
- 实施者必须先写失败测试，再写生产代码；保护既有脏改动；每个可独立交付的任务单独 commit。

---

## 0. 输入、基线与环境准备

- 设计来源：`docs/superpowers/specs/2026-10-10-macos-agent-arm64-design.md`（本计划严格实现它，不修改它）。
- 本计划执行时只允许新增本文件；实现阶段按“最终变更文件清单”改动。
- 参考当前源码锚点（实现时可核对）：
  - `editor/plugins/agent_editor_plugin.cpp:989-1035` `_start_sidecar()`；`:274-282` `_push_configuration()`；`:307-311` 设置占位符。
  - `editor/plugins/agent_editor_plugin.h:87-116` 私有成员与 `_start_sidecar/_stop_sidecar/_restart_sidecar/_read_handshake`。
  - `vibe_tools/editor_credentials.py:1-42`（当前 Windows-only）。
  - `vibe_tools/editor_sidecar.py:38` 导入；`:53-72` `Runtime`/`ConfigureRequest`；`:554-566` `/v1/configure`；`:316`、`:464` 模型调用前读取 key。
  - `vibe_tools/tests/test_editor_sidecar.py`（现有 142 行，setUp 在 `:32` patch `credential`）。
  - `misc/vibe_agent/build_sidecar.ps1`、`misc/vibe_agent/package_windows.ps1`（保持不改）。
  - `platform/macos/detect.py:81-85` arm64 `-mmacosx-version-min=11.0`；`:211` `MACOS_ENABLED`。
  - `platform/macos/os_macos.mm:454-463` `get_bundle_resource_dir()`；`:798-857` `create_process()`（bundle 走 `NSWorkspace`，纯 Mach-O 走 `OS_Unix`）。
  - `core/os/os.cpp:318-320` 非 macOS 默认 `get_bundle_resource_dir()` 返回 `"."`。
  - `misc/dist/macos_tools.app/Contents/Info.plist` 模板骨架。

### 0.1 依赖安装（macOS arm64 构建机）

Run:

```bash
python3 -m pip install --target .build/pythonlibs \
  scons pyinstaller fastapi uvicorn openai "pydantic>=2,<3" pillow requests pygltflib python-multipart
export PYTHONPATH="$PWD/.build/pythonlibs:$PWD"
```

Expected: `pip` 退出码 0；`.build/pythonlibs/PyInstaller` 存在。

- [ ] 确认 `python3 -c "import PyInstaller, fastapi, uvicorn, pydantic; print('deps ok')"` 输出 `deps ok`。
- [ ] 确认 `git status --short` 在开始实现前只包含本计划文件或干净。

### 0.2 平台分支宏与不可变量（所有任务共同约束）

- 平台判定：Python 层 `os.name == "nt"`（Windows）、`sys.platform == "darwin"`（macOS）；C++ 层 `#if defined(WINDOWS_ENABLED)` / `#elif defined(MACOS_ENABLED)`。
- 不变量：`protocol_version == 1`；握手校验 `protocol==1 && 1024<=port<=65535 && reported_pid==sidecar_pid && token.length()==64`；关闭走 `POST /v1/shutdown` 后超时 `kill()`；父进程看门狗 `_watch_parent()`；`credential()` 无明文回退。
- 并发/取消/重试/重启：
  - `/v1/configure` 在 `Runtime.run_lock.locked()` 时返回 HTTP 409（任务期间不允许改配置）。
  - Keychain/凭据调用是同步短操作，不加锁、不自动重试（避免重复弹出钥匙串授权）；失败即抛错，由调用方映射。
  - 重启按钮 `_restart_sidecar()` 先 `_stop_sidecar()`（`/v1/shutdown` + 超时 `kill()`）再 `_start_sidecar()`，每次重启重新做路径解析与握手，不缓存旧路径。
  - 取消任务走既有 `/v1/agent/cancel`，与本迁移无关，不新增行为。

---

## Task 1: 凭据模块平台分派与 Windows 语义抽取

**Files:**
- Modify: `vibe_tools/editor_credentials.py`
- Test: `vibe_tools/tests/test_editor_credentials.py`（Create）

**Interfaces:**
- Consumes: `ctypes`, `ctypes.wintypes`, `os`, `sys`。
- Produces（供 Task 2/3 与 `editor_sidecar.py` 使用）：
  - `CredentialError(RuntimeError)`：密钥库不可用/拒绝访问；`str(exc)` 只含操作名与状态码，绝不含密钥。
  - `SERVICE_PREFIX: str = "Godot/VibeAgent/"`；`ACCOUNT: str = "VibeAgent"`。
  - `credential(name: str, value: str | None = None) -> str`：`name != "api_key"` 抛 `ValueError`；`value is None` 读、缺失返回 `""`；否则写并返回 `value`；非 Windows/macOS 抛 `CredentialError`。
  - `credential_backend() -> str`：`"windows"` / `"macos"`；其余抛 `CredentialError`。
  - 私有 `_windows_credential(name, value)`（语义等价现状）、`_macos_credential(name, value)`（本任务为占位，Task 2 实现）。
  - 可注入点（供跨平台测试）：`_load_windows_api()`、`_windows_last_error()`、`_windows_error()`。

- [ ] **Step 1: 写失败测试。** 创建 `vibe_tools/tests/test_editor_credentials.py`，内容如下（先只放 Task 1 用例）：

```python
from __future__ import annotations

import ctypes
import unittest
from unittest.mock import MagicMock, patch

from vibe_tools import editor_credentials as ec


class DispatchTest(unittest.TestCase):
    def test_backend_windows(self):
        with patch.object(ec.os, "name", "nt"):
            self.assertEqual(ec.credential_backend(), "windows")

    def test_backend_macos(self):
        with patch.object(ec.sys, "platform", "darwin"):
            self.assertEqual(ec.credential_backend(), "macos")

    def test_backend_unsupported(self):
        with patch.object(ec.os, "name", "posix"), patch.object(ec.sys, "platform", "linux"):
            with self.assertRaises(ec.CredentialError):
                ec.credential_backend()

    def test_credential_unsupported_has_no_plaintext_fallback(self):
        with patch.object(ec.os, "name", "posix"), patch.object(ec.sys, "platform", "linux"):
            with self.assertRaises(ec.CredentialError):
                ec.credential("api_key", "sk-秘密")
            with self.assertRaises(ec.CredentialError):
                ec.credential("api_key")

    def test_invalid_name_rejected(self):
        with patch.object(ec.os, "name", "nt"):
            with self.assertRaises(ValueError):
                ec.credential("oauth_refresh_token")

    def test_credential_routes_to_macos(self):
        with patch.object(ec.sys, "platform", "darwin"), \
             patch.object(ec, "_macos_credential", return_value="macos-value") as macos:
            self.assertEqual(ec.credential("api_key"), "macos-value")
            macos.assert_called_once_with("api_key", None)


class WindowsCredentialTest(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.CredWriteW.return_value = 1
        self.api.CredReadW.return_value = 1
        self.patches = [
            patch.object(ec, "_load_windows_api", return_value=self.api),
            patch.object(ec, "_windows_last_error", return_value=0),
            patch.object(ec.os, "name", "nt"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_write_uses_service_naming_and_utf16le(self):
        self.assertEqual(ec.credential("api_key", "sk-秘密"), "sk-秘密")
        item = self.api.CredWriteW.call_args.args[0]._obj
        self.assertEqual(item.TargetName, "Godot/VibeAgent/api_key")
        self.assertEqual(item.UserName, "VibeAgent")
        self.assertEqual(item.Type, 1)
        self.assertEqual(item.Persist, 2)
        blob = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        self.assertEqual(blob.decode("utf-16-le"), "sk-秘密")

    def test_read_not_found_returns_empty(self):
        self.api.CredReadW.return_value = 0
        with patch.object(ec, "_windows_last_error", return_value=1168):
            self.assertEqual(ec.credential("api_key"), "")

    def test_read_auth_failure_raises_without_secret(self):
        self.api.CredReadW.return_value = 0
        with patch.object(ec, "_windows_last_error", return_value=5), \
             patch.object(ec, "_windows_error", return_value=OSError("拒绝访问")):
            with self.assertRaises(OSError):
                ec.credential("api_key")

    def test_read_success_decodes_and_frees(self):
        blob = "sk-读回".encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)

        def fake_read(target, type_, flags, out):
            item = ec.Credential()
            item.CredentialBlobSize = len(blob)
            item.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            out._obj.contents = item
            return 1

        self.api.CredReadW.side_effect = fake_read
        self.assertEqual(ec.credential("api_key"), "sk-读回")
        self.api.CredFree.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认预期失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_credentials -v`

Expected: FAIL/ERROR。具体：`ImportError: cannot import name 'editor_credentials'` 不会出现（模块存在），失败点是 `AttributeError: module 'vibe_tools.editor_credentials' has no attribute 'CredentialError'`（首个用例 setUp 阶段）。

- [ ] **Step 3: 最小实现。** 把 `vibe_tools/editor_credentials.py` 全量替换为：

```python
"""Windows Credential Manager / macOS Keychain storage. No plaintext fallback."""
import ctypes
import os
import sys
from ctypes import wintypes

SERVICE_PREFIX = "Godot/VibeAgent/"
ACCOUNT = "VibeAgent"


class CredentialError(RuntimeError):
    """密钥库不可用或拒绝访问。消息中不包含密钥内容。"""


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _load_windows_api():
    return ctypes.WinDLL("Advapi32.dll", use_last_error=True)


def _windows_last_error() -> int:
    return ctypes.get_last_error()


def _windows_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _windows_credential(name: str, value: str | None = None) -> str:
    api = _load_windows_api()
    target = SERVICE_PREFIX + name
    if value is not None:
        blob = value.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        item = Credential(Type=1, TargetName=target, CredentialBlobSize=len(blob),
                          CredentialBlob=buffer, Persist=2, UserName=ACCOUNT)
        api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        if not api.CredWriteW(ctypes.byref(item), 0):
            raise _windows_error()
        return value
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if _windows_last_error() == 1168:  # ERROR_NOT_FOUND
            return ""
        raise _windows_error()
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob,
                                pointer.contents.CredentialBlobSize).decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def _macos_credential(name: str, value: str | None = None) -> str:
    raise CredentialError("macOS 钥匙串支持尚未启用")


def credential_backend() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise CredentialError("不支持的平台：仅支持 Windows 凭据管理器与 macOS 钥匙串")


def credential(name: str, value: str | None = None) -> str:
    if name != "api_key":
        raise ValueError("不支持的凭据名称：" + name)
    if os.name == "nt":
        return _windows_credential(name, value)
    if sys.platform == "darwin":
        return _macos_credential(name, value)
    raise CredentialError("不支持的平台：仅支持 Windows 凭据管理器与 macOS 钥匙串")
```

- [ ] **Step 4: 运行测试并确认通过。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_credentials -v`

Expected: `Ran 10 tests ... OK`（0 failures、0 errors）。

- [ ] **Step 5: 回归 Windows 导入路径与调用方。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -c "import vibe_tools.editor_sidecar, vibe_tools.editor_credentials as e; print(e.credential_backend())"`

Expected: 在 macOS 上打印 `macos`；`editor_sidecar` 导入无异常。`test_editor_sidecar`/`test_editor_workflow` 仍可导入（完整回归在 Task 3 后统一跑）。

- [ ] **Step 6: 提交。**

```bash
git add vibe_tools/editor_credentials.py vibe_tools/tests/test_editor_credentials.py
git commit -m "refactor(credentials): platform dispatch preserving windows semantics"
```

---

## Task 2: macOS Security.framework Keychain 读写实现

**Files:**
- Modify: `vibe_tools/editor_credentials.py`
- Test: `vibe_tools/tests/test_editor_credentials.py`（追加 macOS 用例）

**Interfaces:**
- Consumes: Task 1 的 `credential()` 分派、`CredentialError`、`SERVICE_PREFIX`、`ACCOUNT`。
- Produces：
  - `_load_macos_security() -> ctypes.CDLL`（Security.framework）。
  - `_load_macos_corefoundation() -> ctypes.CDLL`（CoreFoundation.framework，用于 `CFRelease`）。
  - 状态码常量：`ERR_SEC_SUCCESS=0`、`ERR_SEC_ITEM_NOT_FOUND=-25300`、`ERR_SEC_DUPLICATE_ITEM=-25299`。
  - `_macos_credential(name, value)` 真实读写：UTF-8 原始字节；写重复条目时 `Find` + `ItemModifyContent` 覆盖；读缺失返回 `""`；其他非 0 状态抛 `CredentialError`；`SecKeychainItemFreeContent`/`CFRelease` 必释放。

- [ ] **Step 1: 写失败测试。** 在 `vibe_tools/tests/test_editor_credentials.py` 追加：

```python
class MacOSCredentialTest(unittest.TestCase):
    def setUp(self):
        self.security = MagicMock()
        self.corefoundation = MagicMock()
        self.patches = [
            patch.object(ec.sys, "platform", "darwin"),
            patch.object(ec.os, "name", "posix"),
            patch.object(ec, "_load_macos_security", return_value=self.security),
            patch.object(ec, "_load_macos_corefoundation", return_value=self.corefoundation),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_write_success_calls_add_with_service_and_utf8(self):
        def fake_add(keychain, slen, service, alen, account, plen, data, out):
            self.assertEqual(service, b"Godot/VibeAgent/api_key")
            self.assertEqual(account, b"VibeAgent")
            out._obj.value = 0x1234
            return ec.ERR_SEC_SUCCESS

        self.security.SecKeychainAddGenericPassword.side_effect = fake_add
        self.assertEqual(ec.credential("api_key", "sk-秘密"), "sk-秘密")
        self.corefoundation.CFRelease.assert_called_once()
        self.assertEqual(self.corefoundation.CFRelease.call_args.args[0].value, 0x1234)

    def test_write_duplicate_updates_then_releases(self):
        self.security.SecKeychainAddGenericPassword.return_value = ec.ERR_SEC_DUPLICATE_ITEM

        def fake_find(keychain, slen, service, alen, account, plen, pdata, out):
            out._obj.value = 0x5678
            return ec.ERR_SEC_SUCCESS

        self.security.SecKeychainFindGenericPassword.side_effect = fake_find
        self.security.SecKeychainItemModifyContent.return_value = ec.ERR_SEC_SUCCESS
        self.assertEqual(ec.credential("api_key", "sk-new"), "sk-new")
        self.security.SecKeychainItemModifyContent.assert_called_once()
        self.corefoundation.CFRelease.assert_called_once()
        self.assertEqual(self.corefoundation.CFRelease.call_args.args[0].value, 0x5678)

    def test_read_not_found_returns_empty(self):
        self.security.SecKeychainFindGenericPassword.return_value = ec.ERR_SEC_ITEM_NOT_FOUND
        self.assertEqual(ec.credential("api_key"), "")
        self.security.SecKeychainItemFreeContent.assert_not_called()

    def test_read_auth_failed_raises_without_secret(self):
        self.security.SecKeychainFindGenericPassword.return_value = -25293
        with self.assertRaises(ec.CredentialError) as ctx:
            ec.credential("api_key")
        self.assertIn("-25293", str(ctx.exception))
        self.security.SecKeychainItemFreeContent.assert_not_called()

    def test_read_success_decodes_utf8_and_frees(self):
        payload = "sk-读回".encode("utf-8")
        buffer = ctypes.create_string_buffer(payload)

        def fake_find(keychain, slen, service, alen, account, plen, pdata, out):
            plen._obj.value = len(payload)
            pdata._obj.value = ctypes.addressof(buffer)
            out._obj.value = 0x9abc
            return ec.ERR_SEC_SUCCESS

        self.security.SecKeychainFindGenericPassword.side_effect = fake_find
        self.assertEqual(ec.credential("api_key"), "sk-读回")
        self.security.SecKeychainItemFreeContent.assert_called_once()
        self.corefoundation.CFRelease.assert_called_once()

    def test_write_failure_raises_without_secret(self):
        self.security.SecKeychainAddGenericPassword.return_value = -25308
        with self.assertRaises(ec.CredentialError) as ctx:
            ec.credential("api_key", "sk-秘密")
        self.assertNotIn("sk-秘密", str(ctx.exception))

    def test_no_plaintext_fallback_on_unknown_status(self):
        self.security.SecKeychainFindGenericPassword.return_value = -1
        with self.assertRaises(ec.CredentialError):
            ec.credential("api_key")
```

- [ ] **Step 2: 运行测试并确认预期失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_credentials.MacOSCredentialTest -v`

Expected: ERROR，`AttributeError: module 'vibe_tools.editor_credentials' has no attribute 'ERR_SEC_SUCCESS'`（或 `_load_macos_security`）。

- [ ] **Step 3: 最小实现。** 在 `vibe_tools/editor_credentials.py` 中、`_macos_credential` 之前插入常量与加载器，并用真实实现替换 `_macos_credential`：

```python
ERR_SEC_SUCCESS = 0
ERR_SEC_ITEM_NOT_FOUND = -25300
ERR_SEC_DUPLICATE_ITEM = -25299


def _load_macos_security():
    return ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")


def _load_macos_corefoundation():
    return ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _macos_credential(name: str, value: str | None = None) -> str:
    security = _load_macos_security()
    corefoundation = _load_macos_corefoundation()
    service = SERVICE_PREFIX + name
    service_bytes = service.encode("utf-8")
    account_bytes = ACCOUNT.encode("utf-8")
    if value is not None:
        data = value.encode("utf-8")
        item_ref = ctypes.c_void_p()
        status = security.SecKeychainAddGenericPassword(
            None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
            len(data), data, ctypes.byref(item_ref))
        if status == ERR_SEC_DUPLICATE_ITEM:
            found_item = ctypes.c_void_p()
            status = security.SecKeychainFindGenericPassword(
                None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                None, None, ctypes.byref(found_item))
            if status != ERR_SEC_SUCCESS:
                raise CredentialError(
                    "无法更新 macOS 钥匙串（SecKeychainFindGenericPassword 状态码 %d）" % status)
            try:
                status = security.SecKeychainItemModifyContent(found_item, None, len(data), data)
                if status != ERR_SEC_SUCCESS:
                    raise CredentialError(
                        "无法更新 macOS 钥匙串（SecKeychainItemModifyContent 状态码 %d）" % status)
            finally:
                corefoundation.CFRelease(found_item)
        elif status != ERR_SEC_SUCCESS:
            raise CredentialError(
                "无法写入 macOS 钥匙串（SecKeychainAddGenericPassword 状态码 %d）" % status)
        elif item_ref:
            corefoundation.CFRelease(item_ref)
        return value
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    item_ref = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
        ctypes.byref(password_length), ctypes.byref(password_data), ctypes.byref(item_ref))
    if status == ERR_SEC_ITEM_NOT_FOUND:
        return ""
    if status != ERR_SEC_SUCCESS:
        raise CredentialError(
            "无法读取 macOS 钥匙串（SecKeychainFindGenericPassword 状态码 %d）" % status)
    try:
        return ctypes.string_at(password_data, password_length.value).decode("utf-8")
    finally:
        security.SecKeychainItemFreeContent(None, password_data)
        if item_ref:
            corefoundation.CFRelease(item_ref)
```

- [ ] **Step 4: 运行测试并确认通过。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_credentials -v`

Expected: `Ran 17 tests ... OK`。

- [ ] **Step 5: 在真实 macOS 上做一次性往返（不触碰真实 api_key）。**

Run:

```bash
PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 - <<'PY'
import ctypes
import uuid

from vibe_tools import editor_credentials as ec

prefix = "Godot/VibeAgent/test-" + uuid.uuid4().hex + "/"
marker = "sk-roundtrip-" + uuid.uuid4().hex
ec.SERVICE_PREFIX = prefix  # 仅进程内覆盖，隔离真实 Godot/VibeAgent/api_key

# 真实调用 _macos_credential 写读，不落盘、不打印值。
assert ec.credential("api_key", marker) == marker
assert ec.credential("api_key") == marker

# 删除本次一次性条目（只删 test 前缀，不触碰真实条目）。
security = ec._load_macos_security()
cf = ec._load_macos_corefoundation()
service = (prefix + "api_key").encode("utf-8")
account = ec.ACCOUNT.encode("utf-8")
plen = ctypes.c_uint32()
pdata = ctypes.c_void_p()
item = ctypes.c_void_p()
status = security.SecKeychainFindGenericPassword(
    None, len(service), service, len(account), account,
    ctypes.byref(plen), ctypes.byref(pdata), ctypes.byref(item))
assert status == 0, status
security.SecKeychainItemFreeContent(None, pdata)
if item:
    security.SecKeychainItemDelete(item)
    cf.CFRelease(item)
print("macos keychain roundtrip ok")
PY
```

Expected: 输出 `macos keychain roundtrip ok`，退出码 0；不打印 `marker`。若交互授权弹窗出现，选择允许；失败则记录状态码，不得改为明文回退。

- [ ] **Step 6: 提交。**

```bash
git add vibe_tools/editor_credentials.py vibe_tools/tests/test_editor_credentials.py
git commit -m "feat(credentials): macos keychain via security framework"
```

---

## Task 3: sidecar `/v1/configure` 增加 `credential_store` 契约

**Files:**
- Modify: `vibe_tools/editor_sidecar.py`
- Test: `vibe_tools/tests/test_editor_sidecar.py`

**Interfaces:**
- Consumes: `vibe_tools.editor_credentials` 的 `credential`、`credential_backend`、`CredentialError`；`Runtime.config`、`Runtime.run_lock`、`PROTOCOL_VERSION`；`ConfigureRequest`。
- Produces：
  - `GET /v1/configure` 响应新增 `credential_store`（`"windows"`/`"macos"`）；读取失败时 `has_api_key=false` 且附 `credential_error`（无密钥内容），HTTP 仍 200；老字段 `response_language/base_url/model/vision_model/has_api_key/protocol_version` 全部保留。
  - `POST /v1/configure`：`api_key` 非空时写入；写入失败返回 HTTP 500，`detail` 为 `CredentialError` 中文原因；响应同样带 `credential_store`；`api_key` 为空时**不写**只读；非敏感字段仍写入 `Runtime.config`。
  - 不变：`protocol_version == 1`；任务运行中返回 409。

- [ ] **Step 1: 写失败测试。** 在 `vibe_tools/tests/test_editor_sidecar.py` 顶部 import 区追加：

```python
from vibe_tools import editor_sidecar as sidecar_module
from vibe_tools.editor_credentials import CredentialError
```

在 `setUp` 的 `self.credentials` 之后追加：

```python
        self.backend = patch.object(sidecar_module, "credential_backend", return_value="macos")
        self.backend.start()
```

在 `tearDown` 的 `self.credentials.stop()` 前追加：

```python
        self.backend.stop()
```

在类内追加以下用例：

```python
    def test_get_configure_reports_credential_store_and_legacy_fields(self):
        response = self.client.get("/v1/configure", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["credential_store"], "macos")
        self.assertIn("has_api_key", body)
        self.assertEqual(body["protocol_version"], 1)
        for key in ("response_language", "base_url", "model", "vision_model"):
            self.assertIn(key, body)

    def test_get_configure_degrades_when_keychain_read_fails(self):
        with patch.object(sidecar_module, "credential",
                          side_effect=CredentialError("无法读取 macOS 钥匙串（状态码 -25308）")):
            response = self.client.get("/v1/configure", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_api_key"])
        self.assertIn("-25308", response.json()["credential_error"])

    def test_post_configure_writes_key_only_when_present(self):
        with patch.object(sidecar_module, "credential", return_value="") as cred:
            response = self.client.post("/v1/configure", headers=self.headers,
                                        json={"api_key": "sk-test", "base_url": "http://x"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["credential_store"], "macos")
        cred.assert_any_call("api_key", "sk-test")

    def test_post_configure_without_key_only_reads(self):
        with patch.object(sidecar_module, "credential", return_value="") as cred:
            response = self.client.post("/v1/configure", headers=self.headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        cred.assert_called_once_with("api_key")

    def test_post_configure_write_failure_returns_500_without_key(self):
        with patch.object(sidecar_module, "credential",
                          side_effect=CredentialError("无法写入 macOS 钥匙串（状态码 -25308）")):
            response = self.client.post("/v1/configure", headers=self.headers,
                                        json={"api_key": "sk-秘密"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("钥匙串", response.json()["detail"])
        self.assertNotIn("sk-秘密", response.text)

    def test_configure_rejects_change_while_task_running(self):
        Runtime.run_lock.acquire()
        try:
            response = self.client.post("/v1/configure", headers=self.headers,
                                        json={"base_url": "http://x"})
        finally:
            Runtime.run_lock.release()
        self.assertEqual(response.status_code, 409)
```

- [ ] **Step 2: 运行测试并确认预期失败。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_sidecar -v`

Expected: ERROR/FAIL。`AttributeError: module 'vibe_tools.editor_sidecar' has no attribute 'credential_backend'`（setUp patch 失败）。

- [ ] **Step 3: 最小实现。**
  1. 修改导入行 `vibe_tools/editor_sidecar.py:38`：

```python
from vibe_tools.editor_credentials import CredentialError, credential, credential_backend
```

  2. 用下面的实现替换 `:554-566` 的 `get_configuration` 与 `configure`：

```python
def _configure_payload(has_api_key: bool) -> dict[str, Any]:
    payload = {**Runtime.config, "has_api_key": has_api_key, "protocol_version": PROTOCOL_VERSION}
    try:
        payload["credential_store"] = credential_backend()
    except CredentialError as error:
        payload["credential_error"] = str(error)
    return payload


@app.get("/v1/configure")
def get_configuration() -> dict[str, Any]:
    try:
        has_api_key = bool(credential("api_key"))
    except CredentialError as error:
        payload = _configure_payload(False)
        payload["credential_error"] = str(error)
        return payload
    return _configure_payload(has_api_key)


@app.post("/v1/configure")
def configure(request: ConfigureRequest) -> dict[str, Any]:
    if Runtime.run_lock.locked():
        raise HTTPException(409, "任务期间不能修改模型配置")
    if request.api_key:
        try:
            credential("api_key", request.api_key)
        except CredentialError as error:
            raise HTTPException(500, str(error)) from error
    Runtime.config = request.model_dump(exclude={"api_key"})
    try:
        has_api_key = bool(credential("api_key"))
    except CredentialError as error:
        payload = _configure_payload(False)
        payload["credential_error"] = str(error)
        return payload
    return _configure_payload(has_api_key)
```

- [ ] **Step 4: 运行测试并确认通过。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_sidecar -v`

Expected: 全部通过，含新增 6 个用例；`protocol_version` 仍为 1。

- [ ] **Step 5: 回归既有测试（含 workflow）。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest vibe_tools.tests.test_editor_sidecar vibe_tools.tests.test_editor_workflow -v`

Expected: 全部通过；`test_editor_workflow` 仍 patch `sidecar.credential`，行为不变。

- [ ] **Step 6: 提交。**

```bash
git add vibe_tools/editor_sidecar.py vibe_tools/tests/test_editor_sidecar.py
git commit -m "feat(sidecar): expose credential_store on configure endpoints"
```

---

## Task 4: 编辑器 `_start_sidecar()` 平台化路径解析

**Files:**
- Modify: `editor/plugins/agent_editor_plugin.cpp`
- Modify（仅当需要新辅助方法时）: `editor/plugins/agent_editor_plugin.h`

**Interfaces:**
- Consumes: `OS::get_singleton()->get_executable_path()`、`get_bundle_resource_dir()`（仅 macOS 分支）、`FileAccess::exists()`、`create_process()`。
- Produces：
  - Windows 分支（`#if defined(WINDOWS_ENABLED)`）逐字保持：`exe_dir/vibe_agent/godotvibe-agent/godotvibe-agent.exe` → `repository/.build/sidecar-dist/godotvibe-agent/godotvibe-agent.exe` → `repository/.venv/Scripts/pythonw.exe` + `vibe_tools/editor_sidecar.py`。
  - macOS 分支（`#elif defined(MACOS_ENABLED)`）：`get_bundle_resource_dir()/vibe_agent/godotvibe-agent/godotvibe-agent` → `repository/.build/sidecar-dist/godotvibe-agent/godotvibe-agent` → `repository/.venv/bin/python`（缺失再试 `.venv/bin/python3`）+ `vibe_tools/editor_sidecar.py`。
  - 失败提示按平台给出脚本名：macOS `build_sidecar.sh`，Windows `build_sidecar.ps1`。
  - 不变：`create_process(executable, arguments, &sidecar_pid, false)`、`--project/--parent-pid/--handshake/--godot-exe` 参数与握手校验。

- [ ] **Step 1: 写失败静态测试（C++ 无单元测试框架，用可执行静态断言代替）。**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
src = Path("editor/plugins/agent_editor_plugin.cpp").read_text(encoding="utf-8")
start = src.index("void AgentWorkspace::_start_sidecar()")
end = src.index("void AgentWorkspace::_read_handshake()")
body = src[start:end]
assert "#if defined(WINDOWS_ENABLED)" in body, "missing WINDOWS_ENABLED branch"
assert "#elif defined(MACOS_ENABLED)" in body, "missing MACOS_ENABLED branch"
assert "get_bundle_resource_dir()" in body, "missing bundle resource lookup"
windows_part = body.split("#elif defined(MACOS_ENABLED)")[0]
assert "get_bundle_resource_dir" not in windows_part, "windows branch must not call bundle API"
assert 'repository.path_join(".build/sidecar-dist/godotvibe-agent/godotvibe-agent")' in body
assert 'repository.path_join(".venv/bin/python")' in body
assert 'repository.path_join(".venv/bin/python3")' in body
assert "build_sidecar.sh" in body and "build_sidecar.ps1" in body
assert "godotvibe-agent.exe" in windows_part
print("sidecar path assertions OK")
PY
```

Expected: FAIL，`AssertionError: missing WINDOWS_ENABLED branch`。

- [ ] **Step 2: 最小实现。** 将 `editor/plugins/agent_editor_plugin.cpp:989-1006` 替换为：

```cpp
void AgentWorkspace::_start_sidecar() {
	String executable_dir = OS::get_singleton()->get_executable_path().get_base_dir();
	String repository = executable_dir.get_base_dir();
	String executable;
	String script;
	String missing_message;

#if defined(WINDOWS_ENABLED)
	executable = executable_dir.path_join("vibe_agent/godotvibe-agent/godotvibe-agent.exe");
	if (!FileAccess::exists(executable)) {
		String development_sidecar = repository.path_join(".build/sidecar-dist/godotvibe-agent/godotvibe-agent.exe");
		if (FileAccess::exists(development_sidecar)) {
			executable = development_sidecar;
		} else {
			executable = repository.path_join(".venv/Scripts/pythonw.exe");
			script = repository.path_join("vibe_tools/editor_sidecar.py");
		}
	}
	missing_message = _u8("缺少 godotvibe-agent.exe；请先运行 build_sidecar.ps1，或配置仓库 .venv");
#elif defined(MACOS_ENABLED)
	// get_bundle_resource_dir() 只在 macOS 分支内调用：非 macOS 默认实现返回 "."。
	const String bundle_resource_dir = OS::get_singleton()->get_bundle_resource_dir();
	if (!bundle_resource_dir.is_empty()) {
		const String bundled = bundle_resource_dir.path_join("vibe_agent/godotvibe-agent/godotvibe-agent");
		if (FileAccess::exists(bundled)) {
			executable = bundled;
		}
	}
	if (executable.is_empty()) {
		const String development_sidecar = repository.path_join(".build/sidecar-dist/godotvibe-agent/godotvibe-agent");
		const String development_python = repository.path_join(".venv/bin/python");
		const String development_python3 = repository.path_join(".venv/bin/python3");
		if (FileAccess::exists(development_sidecar)) {
			executable = development_sidecar;
		} else if (FileAccess::exists(development_python) || FileAccess::exists(development_python3)) {
			executable = FileAccess::exists(development_python) ? development_python : development_python3;
			script = repository.path_join("vibe_tools/editor_sidecar.py");
		}
	}
	missing_message = _u8("缺少 godotvibe-agent；请先运行 build_sidecar.sh，或配置仓库 .venv");
#else
	missing_message = _u8("当前平台不支持 Agent 后台");
#endif

	if (executable.is_empty() || (!script.is_empty() && !FileAccess::exists(script))) {
		_set_status(missing_message, true);
		return;
	}
	String project = ProjectSettings::get_singleton()->get_resource_path();
	String instance_dir = project.path_join(".godot/agent/instances");
	DirAccess::make_dir_recursive_absolute(instance_dir);
	handshake_file = instance_dir.path_join("handshake-" + String::num_int64(OS::get_singleton()->get_process_id()) + ".json");
	DirAccess::remove_absolute(handshake_file);

	List<String> arguments;
	if (!script.is_empty()) {
		arguments.push_back(script);
	}
	arguments.push_back("--project");
	arguments.push_back(project);
	arguments.push_back("--parent-pid");
	arguments.push_back(String::num_int64(OS::get_singleton()->get_process_id()));
	arguments.push_back("--handshake");
	arguments.push_back(handshake_file);
	arguments.push_back("--godot-exe");
	arguments.push_back(OS::get_singleton()->get_executable_path());
	Error error = OS::get_singleton()->create_process(executable, arguments, &sidecar_pid, false);
	if (error != OK) {
		_set_status(vformat(_u8("无法启动 Agent 后台（%d）"), error), true);
		return;
	}
	startup_elapsed = 0.0;
	backend_ready = false;
	backend_url.clear();
	backend_token.clear();
	_set_status(_u8("正在启动 Agent 后台…"));
}
```

说明：`missing_message` 变量保证 Windows 文案逐字不变；三个 `#else` 分支仅用于非 Windows/macOS 构建，不影响目标平台。

- [ ] **Step 3: 运行静态测试并确认通过。**

Run: 同 Step 1 的 `python3 - <<'PY' ... PY`。

Expected: `sidecar path assertions OK`。

- [ ] **Step 4: 编译验证（macOS arm64 主机）。**

Run:

```bash
python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4
test -f bin/godot.macos.editor.arm64 && echo "editor binary OK"
```

Expected: 编译退出码 0，输出 `editor binary OK`。若仅做快速语法检查，可先 `python3 -m SCons platform=macos target=editor arch=arm64 -j4`；但验收以 `production=yes` 为准。

- [ ] **Step 5: 运行时握手/PID/重启/停止检查（构建后，人工执行）。**

Run:

```bash
rm -f /tmp/vibe-probe.log
./bin/godot.macos.editor.arm64 --path "$PWD" --editor >/tmp/vibe-probe.log 2>&1 &
editor_pid=$!
sleep 8
ls .godot/agent/instances/handshake-*.json
pgrep -fl godotvibe-agent || true
kill -TERM "$editor_pid"; wait "$editor_pid" 2>/dev/null || true
sleep 2
pgrep -fl godotvibe-agent && echo "LEFTOVER PROCESS" || echo "no leftover sidecar"
grep -i "握手\|handshake\|token" .godot/agent/sidecar.log || true
```

Expected: 出现 1 个 `handshake-<pid>.json`；`pgrep` 仅 1 个 `godotvibe-agent`；编辑器退出后 `no leftover sidecar`。重启按钮验证：在应用内点击顶部“重启后台”，再次 `pgrep -fl godotvibe-agent` 应仍是 1 个进程且 PID 变化（旧进程被 `/v1/shutdown` 或 `kill()` 收走）。

- [ ] **Step 6: 提交。**

```bash
git add editor/plugins/agent_editor_plugin.cpp editor/plugins/agent_editor_plugin.h
git commit -m "feat(editor): resolve macos arm64 sidecar paths behind MACOS_ENABLED"
```

---

## Task 5: 编辑器设置占位符按 `credential_store` 显示

**Files:**
- Modify: `editor/plugins/agent_editor_plugin.cpp`
- Modify（可选，若抽私有辅助）: `editor/plugins/agent_editor_plugin.h`

**Interfaces:**
- Consumes: `REQUEST_CONFIGURE` 响应里的 `has_api_key` 与 `credential_store`。
- Produces：占位符映射 `"macos"` → `已保存到 macOS 钥匙串`；`"windows"`/缺字段 → `已保存到 Windows 凭据管理器`（旧 sidecar 兼容）；`has_api_key==false` → `输入 API Key`。
- 不变：`api_key->clear()`、随后请求 `/v1/chat/history`。

- [ ] **Step 1: 写失败静态测试。**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
src = Path("editor/plugins/agent_editor_plugin.cpp").read_text(encoding="utf-8")
assert "credential_store" in src, "missing credential_store handling"
assert "已保存到 macOS 钥匙串" in src, "missing macos placeholder"
assert "已保存到 Windows 凭据管理器" in src, "missing windows fallback placeholder"
assert 'response.get("has_api_key", false)' in src
print("placeholder assertions OK")
PY
```

Expected: FAIL，`AssertionError: missing credential_store handling`。

- [ ] **Step 2: 最小实现。** 将 `editor/plugins/agent_editor_plugin.cpp:307-311` 的 `REQUEST_CONFIGURE` 分支替换为：

```cpp
		case REQUEST_CONFIGURE: {
			api_key->clear();
			String credential_store = response.get("credential_store", "windows");
			String saved_label = credential_store == "macos"
					? _u8("已保存到 macOS 钥匙串")
					: _u8("已保存到 Windows 凭据管理器");
			api_key->set_placeholder(response.get("has_api_key", false) ? saved_label : _u8("输入 API Key"));
			_request("/v1/chat/history", HTTPClient::METHOD_GET, Dictionary(), REQUEST_HISTORY);
		} break;
```

- [ ] **Step 3: 运行静态测试并确认通过。**

Run: 同 Step 1 的 `python3 - <<'PY' ... PY`。

Expected: `placeholder assertions OK`。

- [ ] **Step 4: 编译并回归。**

Run:

```bash
python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4
test -f bin/godot.macos.editor.arm64 && echo "editor binary OK"
```

Expected: 退出码 0，`editor binary OK`。

- [ ] **Step 5: 提交。**

```bash
git add editor/plugins/agent_editor_plugin.cpp editor/plugins/agent_editor_plugin.h
git commit -m "feat(editor): show macos keychain placeholder from credential_store"
```

---

## Task 6: macOS sidecar 构建脚本 `build_sidecar.sh`

**Files:**
- Create: `misc/vibe_agent/build_sidecar.sh`
- 不改: `misc/vibe_agent/build_sidecar.ps1`

**Interfaces:**
- Consumes: `VIBE_BUILD_PYTHON`（可选）、`.build/pythonlibs`、`vibe_tools/editor_sidecar.py`。
- Produces：`.build/sidecar-dist/godotvibe-agent/godotvibe-agent`（可执行 Mach-O）+ `_internal/`；失败非 0 退出；不生成 `.app`。

- [ ] **Step 1: 写失败测试。** Run:

```bash
test -f misc/vibe_agent/build_sidecar.sh && bash -n misc/vibe_agent/build_sidecar.sh
```

Expected: 非 0 退出（文件不存在）。

- [ ] **Step 2: 最小实现。** 创建 `misc/vibe_agent/build_sidecar.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_exe="${VIBE_BUILD_PYTHON:-python3}"
library_root="$repo_root/.build/pythonlibs"

if [[ ! -d "$library_root/PyInstaller" ]]; then
	echo "PyInstaller is missing from .build/pythonlibs. Install build dependencies first." >&2
	exit 1
fi

export PYTHONPATH="$library_root:$repo_root"
dist="$repo_root/.build/sidecar-dist"
work="$repo_root/.build/sidecar-work"
spec="$repo_root/.build/sidecar-spec"
mkdir -p "$dist" "$work" "$spec"

help_text="$("$python_exe" -m PyInstaller --help 2>&1)"
if grep -qE -- '--target-arch([ =]|$)' <<<"$help_text"; then
	arch_flag=(--target-arch arm64)
else
	arch_flag=(--target-architecture arm64)
fi

"$python_exe" -m PyInstaller \
	--noconfirm --clean --onedir --console \
	--name godotvibe-agent \
	--distpath "$dist" --workpath "$work" --specpath "$spec" \
	--paths "$repo_root" --paths "$library_root" \
	"${arch_flag[@]}" \
	--add-data "$repo_root/vibe_tools/prompts:vibe_tools/prompts" \
	--add-data "$repo_root/vibe_tools/api_cache:vibe_tools/api_cache" \
	--collect-submodules vibe_tools.templates \
	--collect-all openai --collect-all uvicorn \
	--exclude-module pandas --exclude-module openpyxl \
	--exclude-module lxml --exclude-module numpy \
	--hidden-import uvicorn.logging \
	--hidden-import uvicorn.loops.auto \
	--hidden-import uvicorn.protocols.http.auto \
	--hidden-import uvicorn.protocols.websockets.auto \
	--hidden-import uvicorn.lifespan.on \
	"$repo_root/vibe_tools/editor_sidecar.py"

sidecar="$dist/godotvibe-agent/godotvibe-agent"
if [[ ! -x "$sidecar" ]]; then
	echo "Sidecar executable was not created: $sidecar" >&2
	exit 1
fi
echo "Sidecar created: $sidecar"
```

- [ ] **Step 3: 运行静态断言并确认通过。**

Run:

```bash
set -e
bash -n misc/vibe_agent/build_sidecar.sh
grep -q -- '--onedir --console' misc/vibe_agent/build_sidecar.sh
grep -q -- '--target-arch' misc/vibe_agent/build_sidecar.sh
grep -q 'vibe_tools/prompts:vibe_tools/prompts' misc/vibe_agent/build_sidecar.sh
grep -q 'vibe_tools/api_cache:vibe_tools/api_cache' misc/vibe_agent/build_sidecar.sh
! grep -q -e '--windowed' misc/vibe_agent/build_sidecar.sh
! grep -q 'vibe_tools/prompts;vibe_tools/prompts' misc/vibe_agent/build_sidecar.sh
echo "build_sidecar.sh assertions OK"
```

Expected: 输出 `build_sidecar.sh assertions OK`。`! grep` 找不到匹配返回 0。

- [ ] **Step 4: 真实构建（macOS arm64）。**

Run:

```bash
bash misc/vibe_agent/build_sidecar.sh
test -x .build/sidecar-dist/godotvibe-agent/godotvibe-agent && echo "sidecar executable OK"
test -d .build/sidecar-dist/godotvibe-agent/_internal && echo "onedir OK"
test ! -e .build/sidecar-dist/godotvibe-agent/godotvibe-agent.app && echo "no nested app OK"
file .build/sidecar-dist/godotvibe-agent/godotvibe-agent
```

Expected: `sidecar executable OK`、`onedir OK`、`no nested app OK`；`file` 输出包含 `Mach-O 64-bit executable arm64`。

- [ ] **Step 5: 冒烟运行 sidecar 入口。**

Run:

```bash
.build/sidecar-dist/godotvibe-agent/godotvibe-agent --help >/tmp/vibe-sidecar-help.log 2>&1; echo "exit=$?"
```

Expected: `exit=0` 或打印参数说明；不得因缺 `_internal` 报 `ModuleNotFoundError`。

- [ ] **Step 6: 提交。**

```bash
git add misc/vibe_agent/build_sidecar.sh
git commit -m "build(macos): add arm64 pyinstaller onedir sidecar script"
```

---

## Task 7: macOS 打包脚本 `package_macos.sh`

**Files:**
- Create: `misc/vibe_agent/package_macos.sh`
- 不改: `misc/vibe_agent/package_windows.ps1`

**Interfaces:**
- Consumes: `bin/godot.macos.editor.arm64`、`.build/sidecar-dist/godotvibe-agent`、`misc/dist/macos_tools.app` 模板、`LICENSE.txt`、`misc/vibe_agent/README.zh-CN.md`、`misc/vibe_agent/CAPABILITIES.md`。
- Produces：`.build/VibeGodot-macOS-arm64/VibeGodot.app`（`Contents/MacOS/VibeGodot`、`Contents/Resources/vibe_agent/godotvibe-agent/`、定制 `Info.plist`）+ `.build/VibeGodot-macOS-arm64.zip`；不含签名/公证。

- [ ] **Step 1: 写失败测试。** Run:

```bash
test -f misc/vibe_agent/package_macos.sh && bash -n misc/vibe_agent/package_macos.sh
```

Expected: 非 0 退出（文件不存在）。

- [ ] **Step 2: 最小实现。** 创建 `misc/vibe_agent/package_macos.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output="$repo_root/.build/VibeGodot-macOS-arm64"
app="$output/VibeGodot.app"
engine="$repo_root/bin/godot.macos.editor.arm64"
sidecar="$repo_root/.build/sidecar-dist/godotvibe-agent"
template="$repo_root/misc/dist/macos_tools.app"
archive="$repo_root/.build/VibeGodot-macOS-arm64.zip"

if [[ ! -f "$engine" ]]; then
	echo "Editor binary not found: $engine" >&2
	exit 1
fi
if [[ ! -x "$sidecar/godotvibe-agent" ]]; then
	echo "Agent sidecar not found. Run misc/vibe_agent/build_sidecar.sh first." >&2
	exit 1
fi
case "$output" in
	"$repo_root"/.build/*) ;;
	*) echo "For safety, output must be under $repo_root/.build" >&2; exit 1 ;;
esac

rm -rf "$output"
mkdir -p "$output"
cp -R "$template" "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/vibe_agent"
cp "$engine" "$app/Contents/MacOS/VibeGodot"
chmod +x "$app/Contents/MacOS/VibeGodot"
rm -rf "$app/Contents/Resources/vibe_agent/godotvibe-agent"
cp -R "$sidecar" "$app/Contents/Resources/vibe_agent/godotvibe-agent"

/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable VibeGodot" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName VibeGodot" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.vibegodot.editor" "$app/Contents/Info.plist"

cp "$repo_root/LICENSE.txt" "$output/GODOT_LICENSE.txt"
cp "$repo_root/misc/vibe_agent/README.zh-CN.md" "$output/使用说明.md"
cp "$repo_root/misc/vibe_agent/CAPABILITIES.md" "$output/能力对应表.md"

rm -f "$archive"
ditto -c -k --sequesterRsrc --keepParent "$app" "$archive"
echo "Package created: $archive"
```

- [ ] **Step 3: 运行静态断言并确认通过。**

Run:

```bash
set -e
bash -n misc/vibe_agent/package_macos.sh
grep -q 'Contents/MacOS/VibeGodot' misc/vibe_agent/package_macos.sh
grep -q 'Contents/Resources/vibe_agent/godotvibe-agent' misc/vibe_agent/package_macos.sh
grep -q 'PlistBuddy' misc/vibe_agent/package_macos.sh
grep -q 'ditto -c -k --sequesterRsrc --keepParent' misc/vibe_agent/package_macos.sh
grep -q 'misc/dist/macos_tools.app' misc/vibe_agent/package_macos.sh
echo "package_macos.sh assertions OK"
```

Expected: 输出 `package_macos.sh assertions OK`。

- [ ] **Step 4: 真实打包（macOS arm64，需先完成 Task 6）。**

Run:

```bash
bash misc/vibe_agent/package_macos.sh
test -x ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/MacOS/VibeGodot" && echo "app executable OK"
test -x ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Resources/vibe_agent/godotvibe-agent/godotvibe-agent" && echo "bundled sidecar OK"
test -f ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Info.plist" && echo "info plist OK"
test -f .build/VibeGodot-macOS-arm64.zip && echo "zip OK"
/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" ".build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/Info.plist"
```

Expected: `app executable OK`、`bundled sidecar OK`、`info plist OK`、`zip OK`；分别打印 `VibeGodot` 与 `com.vibegodot.editor`。

- [ ] **Step 5: 提交。**

```bash
git add misc/vibe_agent/package_macos.sh
git commit -m "build(macos): package arm64 app bundle with sidecar"
```

---

## Task 8: 文档更新（README / 使用说明 / 能力表）

**Files:**
- Modify: `README.md`
- Modify: `misc/vibe_agent/README.zh-CN.md`
- Modify: `misc/vibe_agent/CAPABILITIES.md`

**Interfaces:**
- Consumes: Task 4–7 的实际路径、脚本名、Keychain 行为、未签名/未公证限制。
- Produces：新增 macOS arm64 构建/使用章节；Windows 章节逐字保留；明确 API Key 存 macOS 钥匙串；明确未签名/未公证与 Gatekeeper/quarantine 限制。

- [ ] **Step 1: 写失败测试。** Run:

```bash
set -e
grep -q 'macOS' README.md
grep -q 'build_sidecar.sh' README.md
grep -q 'macOS 钥匙串' misc/vibe_agent/README.zh-CN.md
grep -q 'macOS' misc/vibe_agent/CAPABILITIES.md
grep -qi 'arm64' misc/vibe_agent/CAPABILITIES.md
grep -q 'Windows 构建' README.md
grep -q 'Windows 使用说明' misc/vibe_agent/README.zh-CN.md
echo "doc assertions OK"
```

Expected: 第一处 `grep -q 'macOS' README.md` 失败（当前 README 无 macOS 章节）。

- [ ] **Step 2: 最小实现。**
  1. `README.md`：在“Windows 构建”之后新增 `## macOS arm64 构建` 章节，包含：
     - 依赖安装（0.1 的 `pip install` 命令）、`python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4`；
     - `bash misc/vibe_agent/build_sidecar.sh`、`bash misc/vibe_agent/package_macos.sh`；
     - 产物 `.build/VibeGodot-macOS-arm64/VibeGodot.app` 与 `.build/VibeGodot-macOS-arm64.zip`；
     - `open .build/VibeGodot-macOS-arm64/VibeGodot.app`；
     - 明确 API Key 存 macOS 登录钥匙串、非敏感项存 Editor Settings；
     - 明确仅 arm64、未签名/未公证、下载后可能被 Gatekeeper 拦截（`xattr -dr com.apple.quarantine` 为可选用户操作）。
     - 顶部“目前提供 Windows 编辑器…”句子改为“Windows 与 macOS arm64”，并将“更多说明”链接保留。
  2. `misc/vibe_agent/README.zh-CN.md`：标题与正文区分平台，新增 macOS 段落：
     - 运行 `.app`，第一个设置项 API Key 存 macOS 钥匙串；
     - sidecar 为 `Contents/Resources/vibe_agent/godotvibe-agent/godotvibe-agent`（无 `.exe`、无嵌套 `.app`）；
     - 构建命令指向 `build_sidecar.sh`/`package_macos.sh`；
     - 保留全部 Windows 段落原文。
  3. `misc/vibe_agent/CAPABILITIES.md`：
     - `API Key` 行改为 `Windows Credential Manager / macOS 登录钥匙串`；
     - `分发` 行补充 `macOS arm64 .app（PyInstaller onedir sidecar）`；
     - 末尾新增限制说明：仅 arm64、未签名/未公证。

- [ ] **Step 3: 运行静态断言并确认通过。**

Run: 同 Step 1 的 grep 块。

Expected: `doc assertions OK`。

- [ ] **Step 4: 检查无渲染破损与无 Windows 文案丢失。**

Run:

```bash
git diff --check -- README.md misc/vibe_agent/README.zh-CN.md misc/vibe_agent/CAPABILITIES.md
grep -c 'Windows' misc/vibe_agent/README.zh-CN.md
```

Expected: `git diff --check` 无输出；`grep -c` ≥ 1（Windows 内容仍在）。

- [ ] **Step 5: 提交。**

```bash
git add README.md misc/vibe_agent/README.zh-CN.md misc/vibe_agent/CAPABILITIES.md
git commit -m "docs: document macos arm64 build and keychain usage"
```

---

## Task 9: 集成验收（无文件改动，作为最终门禁）

**Files:** 无（只运行命令；本任务不产生 commit）。

**Interfaces:**
- Consumes: Task 1–8 全部产物。
- Produces: 验收结论与残留限制记录（写入实施报告，不提交代码）。

- [ ] **Step 1: Python 全量单元测试（macOS arm64）。**

Run: `PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest discover -s vibe_tools/tests -v`

Expected: `OK`；其中 `test_editor_credentials` 全绿、`test_editor_sidecar` 全绿、`test_editor_workflow` 全绿。

- [ ] **Step 2: 启用真实 Godot 进程集成。**

Run: `GODOT_TEST_EXE="$PWD/bin/godot.macos.editor.arm64" PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 -m unittest discover -s vibe_tools/tests -v`

Expected: `OK`；覆盖导入、运行、截图、超时、取消、失败→回修→回退；模型/视觉用离线替身。

- [ ] **Step 3: 真实 sidecar health/handshake/shutdown（无需编辑器 GUI）。**

Run:

```bash
tmp_project="$(mktemp -d)"
printf 'config_version=5\n' > "$tmp_project/project.godot"
mkdir -p "$tmp_project/.godot/agent/instances"
handshake="$tmp_project/.godot/agent/instances/handshake-$$.json"
pids_before="$(pgrep -f godotvibe-agent | wc -l | tr -d ' ')"
PYTHONPATH="$PWD/.build/pythonlibs:$PWD" python3 vibe_tools/editor_sidecar.py \
  --project "$tmp_project" --parent-pid "$$" --handshake "$handshake" \
  --godot-exe "$PWD/bin/godot.macos.editor.arm64" >"$tmp_project/sidecar.log" 2>&1 &
sidecar_pid=$!
for _ in $(seq 1 40); do [[ -s "$handshake" ]] && break; sleep 0.25; done
cat "$handshake"
token="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$handshake")"
port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$handshake")"
curl -s -H "Authorization: Bearer $token" "http://127.0.0.1:$port/v1/health"
echo
curl -s -H "Authorization: Bearer $token" -X POST "http://127.0.0.1:$port/v1/shutdown"
sleep 1
kill -0 "$sidecar_pid" 2>/dev/null && echo "SIDECAR STILL ALIVE" || echo "sidecar clean shutdown"
rm -rf "$tmp_project"
```

Expected: 握手 JSON 含 `protocol_version=1`、`port`（1024–65535）、`pid`、`token`（长度 64）；`/v1/health` 返回 `ok`；shutdown 后 `sidecar clean shutdown`；不残留进程。

- [ ] **Step 4: 父进程看门狗与退出无残留。**

Run:

```bash
pgrep -fl godotvibe-agent && echo "FOUND LEFTOVER" || echo "no leftover sidecar process"
```

Expected: `no leftover sidecar process`。

- [ ] **Step 5: `VibeGodot.app` 手动 Agent/Tests 验收。**

Run: `open .build/VibeGodot-macOS-arm64/VibeGodot.app`

人工步骤：
1. 顶部 Agent → 设置，填写地址/模型/视觉模型/API Key，保存后占位符显示“已保存到 macOS 钥匙串”。
2. 仅验证条目存在（**不要打印值**）：`/usr/bin/security find-generic-password -s Godot/VibeAgent/api_key >/dev/null; echo $?`，期望 `0`。
3. 发起一轮对话并触发一次 Tests；检查 `.godot/agent/sidecar.log` 无握手/密钥错误。
4. 点击“重启后台”，确认 App 内连接恢复且系统进程表中该 sidecar 仅 1 个。
5. 正常退出编辑器，确认无残留 `godotvibe-agent`。

- [ ] **Step 6: Windows 回归（在 Windows 主机执行，非本计划主机）。**

Run（PowerShell）:

```powershell
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
python -m unittest discover -s vibe_tools/tests -v
.\misc\vibe_agent\build_sidecar.ps1
.\misc\vibe_agent\package_windows.ps1 -SkipBuild -SkipSidecarBuild -Zip
```

Expected: Python 套件 `OK`（Windows 分支语义不变）；PowerShell 脚本产物存在；`build_sidecar.ps1`/`package_windows.ps1` 未被本计划修改。

- [ ] **Step 7: 记录结论。** 无 commit；把 Step 1–6 的实际输出、未执行项及原因写入实施报告。

---

## 4. 最终变更文件清单

| 文件 | 类型 | 所属任务 | 说明 |
| --- | --- | --- | --- |
| `vibe_tools/editor_credentials.py` | 改 | Task 1、2 | 平台分派、`CredentialError`、`credential_backend`、macOS Security.framework |
| `vibe_tools/tests/test_editor_credentials.py` | 新增 | Task 1、2 | 分派/命名/状态码/释放/无明文回退测试 |
| `vibe_tools/editor_sidecar.py` | 改 | Task 3 | `/v1/configure` GET/POST 增加 `credential_store`、错误映射 |
| `vibe_tools/tests/test_editor_sidecar.py` | 改 | Task 3 | 新字段、读取降级、写入失败 500、409 |
| `editor/plugins/agent_editor_plugin.cpp` | 改 | Task 4、5 | macOS 分支路径解析、平台错误文案、Keychain 占位符 |
| `editor/plugins/agent_editor_plugin.h` | 改（按需） | Task 4、5 | 仅当抽出辅助方法时新增声明；默认不改 |
| `misc/vibe_agent/build_sidecar.sh` | 新增 | Task 6 | PyInstaller arm64 onedir（无 `.app`、冒号 `--add-data`） |
| `misc/vibe_agent/package_macos.sh` | 新增 | Task 7 | 组装 `.app`、PlistBuddy、ditto zip |
| `README.md` | 改 | Task 8 | macOS arm64 构建/使用章节 |
| `misc/vibe_agent/README.zh-CN.md` | 改 | Task 8 | macOS 使用说明（保留 Windows） |
| `misc/vibe_agent/CAPABILITIES.md` | 改 | Task 8 | Keychain、arm64、未签名限制 |
| `docs/superpowers/plans/2026-10-10-macos-agent-arm64.md` | 新增 | 本计划提交 | 仅本文件；实现阶段不再改动 |

**明确不改**：`docs/superpowers/specs/2026-10-10-macos-agent-arm64-design.md`、`misc/vibe_agent/build_sidecar.ps1`、`misc/vibe_agent/package_windows.ps1`、`vibe_tools/godot_runner.py`、`vibe_tools/editor_behavior.py`、`vibe_tools/editor_harness.py`、`vibe_tools/web_server.py`、`vibe_tools/static/`、`vibe_tools/templates/`、`editor/editor_node.cpp`。

---

## 5. 验收矩阵

| # | 验收项 | 命令 | 期望结果 | 平台 | 任务 |
| --- | --- | --- | --- | --- | --- |
| A1 | 凭据单元测试 | `python3 -m unittest vibe_tools.tests.test_editor_credentials -v` | `Ran 17 tests ... OK` | 任意 | 1、2 |
| A2 | sidecar 单元测试 | `python3 -m unittest vibe_tools.tests.test_editor_sidecar -v` | `OK`，含 `credential_store` 断言 | 任意 | 3 |
| A3 | workflow 回归 | `python3 -m unittest vibe_tools.tests.test_editor_workflow -v` | `OK` | 任意 | 3 |
| A4 | C++ 路径静态断言 | Step 1 的 python 断言块 | `sidecar path assertions OK` | 任意 | 4 |
| A5 | 占位符静态断言 | Step 1 的 python 断言块 | `placeholder assertions OK` | 任意 | 5 |
| A6 | 编辑器编译 | `python3 -m SCons platform=macos target=editor arch=arm64 production=yes debug_symbols=no -j4` | 退出码 0，生成 `bin/godot.macos.editor.arm64` | macOS arm64 | 4、5 |
| A7 | 构建脚本语法 | `bash -n misc/vibe_agent/build_sidecar.sh` | 退出码 0 | 任意 | 6 |
| A8 | sidecar 产物 | `test -x .build/sidecar-dist/godotvibe-agent/godotvibe-agent && file ...` | 可执行，`Mach-O ... arm64`，无嵌套 `.app` | macOS arm64 | 6 |
| A9 | 打包脚本语法 | `bash -n misc/vibe_agent/package_macos.sh` | 退出码 0 | 任意 | 7 |
| A10 | `.app` 产物 | `test -x .build/VibeGodot-macOS-arm64/VibeGodot.app/Contents/MacOS/VibeGodot` 等 | 全部存在，Plist 为 `VibeGodot`/`com.vibegodot.editor` | macOS arm64 | 7 |
| A11 | 文档断言 | Task 8 Step 1 的 grep 块 | `doc assertions OK`，Windows 章节保留 | 任意 | 8 |
| A12 | Python 全量 | `python3 -m unittest discover -s vibe_tools/tests -v` | `OK` | 任意 | 9 |
| A13 | Godot 进程集成 | `GODOT_TEST_EXE=... python3 -m unittest discover ...` | `OK` | macOS arm64 | 9 |
| A14 | 真实 sidecar 握手/health/shutdown | Task 9 Step 3 脚本 | 握手字段合法、health `ok`、`sidecar clean shutdown` | macOS arm64 | 9 |
| A15 | 无残留进程 | `pgrep -fl godotvibe-agent` | 无输出 | macOS arm64 | 9 |
| A16 | Windows 回归 | Task 9 Step 6 PowerShell | 套件 `OK`，两边脚本产物存在 | Windows | 9 |
| A17 | 计划文件本身 | `git diff --check -- docs/superpowers/plans/2026-10-10-macos-agent-arm64.md`；`test -s ...` | 无输出；退出码 0 | 任意 | 本提交 |

---

## 6. 边界、错误路径与平台限制汇总

- 凭据层错误路径：`errSecItemNotFound (-25300)` → 读返回 `""`；`errSecDuplicateItem (-25299)` → 仅在写路径触发 `Find`+`ModifyContent`；`errSecAuthFailed (-25293)`/`errSecInteractionNotAllowed (-25308)`/`errSecUserCanceled (-128)`/未知状态 → `CredentialError`，消息只带操作名与状态码；非 Windows/macOS → `CredentialError`；任何路径都无明文落盘回退。
- 资源释放：读成功必须 `SecKeychainItemFreeContent`（`finally`）+ `CFRelease(item_ref)`；写成功 `CFRelease(item_ref)`；重复写 `CFRelease(found_item)`（`finally`）。
- sidecar 契约：GET 读取失败 HTTP 200 + `has_api_key=false` + `credential_error`；POST 写入失败 HTTP 500 + `detail=str(CredentialError)`；`api_key` 为空只读不写；任务运行中 POST 返回 409。
- 启动路径：Windows 三段回退与 `.exe` 命名、错误文案逐字不变，且不调用 `get_bundle_resource_dir()`；macOS 只在 `MACOS_ENABLED` 分支调用，三段回退 bundle → 开发 onedir → 开发 venv；失败不崩溃、仅状态栏提示。
- 握手/进程：`protocol_version=1`、随机 loopback 端口、256 位 token、`reported_pid==sidecar_pid`、正常 `POST /v1/shutdown` + 超时 `kill()`、父进程看门狗、重启重建全部保持；sidecar 必须是纯 Mach-O，禁止嵌套 `.app`。
- 并发：每个编辑器实例独立 sidecar（独立端口/token/握手文件）；`run_lock` 串行化任务与配置；凭据同步访问不并发加锁、不自动重试。
- 非目标：Intel/universal、OAuth、签名/公证/entitlements、旧 web_server、Windows 导出工具链。

### 6.1 非 macOS 主机无法完成项

| 验证项 | 原因 | 可在哪完成 |
| --- | --- | --- |
| 编译 `bin/godot.macos.editor.arm64` | 需要 macOS SDK/Xcode CLT/arm64 工具链 | macOS arm64 |
| PyInstaller 生成 arm64 Mach-O onedir | 需在 macOS arm64 上运行 PyInstaller | macOS arm64 |
| Security.framework Keychain 真实往返 | 仅 macOS 提供 | macOS |
| `get_bundle_resource_dir()`/`.app` 路径与 `OS_Unix::create_process` PID 语义 | 仅 macOS | macOS |
| `PlistBuddy`/`ditto`/Gatekeeper quarantine | macOS 专用 | macOS |
| Windows Credential Manager 与 PowerShell 打包回归 | 仅 Windows | Windows |
| 真实主模型/视觉服务链路 | 需用户自有密钥与网络；自动化测试不涉及 | 人工，任一平台（不记录密钥） |

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

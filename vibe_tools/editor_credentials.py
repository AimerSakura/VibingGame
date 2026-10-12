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


ERR_SEC_SUCCESS = 0
ERR_SEC_ITEM_NOT_FOUND = -25300
ERR_SEC_DUPLICATE_ITEM = -25299


def _load_macos_security():
    return ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")


def _load_macos_corefoundation():
    return ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _configure_macos_functions(security, corefoundation) -> None:
    """Declare argtypes/restype so ctypes marshals pointers correctly on arm64.

    Untyped ctypes calls truncate ``SecKeychainRef``/``unsigned char *`` to a
    32-bit int, which silently corrupts pointers when a value exceeds 2**31.
    """
    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p)]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyContent.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    security.SecKeychainItemModifyContent.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    corefoundation.CFRelease.argtypes = [ctypes.c_void_p]
    corefoundation.CFRelease.restype = None


def _macos_credential(name: str, value: str | None = None) -> str:
    security = _load_macos_security()
    corefoundation = _load_macos_corefoundation()
    _configure_macos_functions(security, corefoundation)
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

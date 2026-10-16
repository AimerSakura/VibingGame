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


class SecretNameTest(unittest.TestCase):
    def test_accepts_supported_names(self):
        for name in ("api_key", "codex_host_id", "codex/AbC123_-xyz"):
            with patch.object(ec, "_macos_credential", return_value="v"), \
                 patch.object(ec.sys, "platform", "darwin"):
                self.assertEqual(ec.secret(name), "v")

    def test_rejects_unknown_name(self):
        for bad in ("oauth_refresh_token", "codex/", "codex/../x", "api_key/../x",
                    "codex/short", "codex/" + "a" * 65, "codex/空格名字占位"):
            with self.assertRaises(ValueError):
                ec.secret(bad)

    def test_delete_routes_to_backend(self):
        with patch.object(ec, "_macos_delete", return_value=None) as dele, \
             patch.object(ec.sys, "platform", "darwin"):
            ec.delete_secret("codex/AbC123_-xyz")
            dele.assert_called_once_with("codex/AbC123_-xyz")

    def test_delete_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            ec.delete_secret("codex/../x")

    def test_delete_propagates_credential_error(self):
        with patch.object(ec, "_macos_delete", side_effect=ec.CredentialError("-25300")):
            with self.assertRaises(ec.CredentialError):
                ec.delete_secret("codex/AbC123_-xyz")

    def test_credential_still_restricted_to_api_key(self):
        with patch.object(ec, "_macos_credential", return_value="v"), \
             patch.object(ec.sys, "platform", "darwin"):
            with self.assertRaises(ValueError):
                ec.credential("codex_host_id")

    def test_secret_unsupported_platform_raises(self):
        with patch.object(ec.os, "name", "posix"), patch.object(ec.sys, "platform", "linux"):
            with self.assertRaises(ec.CredentialError):
                ec.secret("codex_host_id")
            with self.assertRaises(ec.CredentialError):
                ec.delete_secret("codex_host_id")


class WindowsDeleteTest(unittest.TestCase):
    def test_delete_routes_not_found_is_ok(self):
        api = MagicMock()
        api.CredDeleteW.return_value = 0
        with patch.object(ec, "_load_windows_api", return_value=api), \
             patch.object(ec, "_windows_last_error", return_value=1168), \
             patch.object(ec.os, "name", "nt"):
            ec.delete_secret("codex/AbC123_-xyz")
        api.CredDeleteW.assert_called_once_with("Godot/VibeAgent/codex/AbC123_-xyz", 1, 0)

    def test_delete_failure_raises_without_secret(self):
        api = MagicMock()
        api.CredDeleteW.return_value = 0
        with patch.object(ec, "_load_windows_api", return_value=api), \
             patch.object(ec, "_windows_last_error", return_value=5), \
             patch.object(ec, "_windows_error", return_value=OSError("拒绝访问")), \
             patch.object(ec.os, "name", "nt"):
            with self.assertRaises(OSError):
                ec.delete_secret("codex/AbC123_-xyz")


class MacOSDeleteTest(unittest.TestCase):
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

    def test_not_found_is_idempotent(self):
        self.security.SecKeychainFindGenericPassword.return_value = ec.ERR_SEC_ITEM_NOT_FOUND
        ec.delete_secret("codex/AbC123_-xyz")
        self.security.SecKeychainItemDelete.assert_not_called()

    def test_delete_calls_delete_and_releases(self):
        def fake_find(keychain, slen, service, alen, account, plen, pdata, out):
            out._obj.value = 0x4321
            return ec.ERR_SEC_SUCCESS

        self.security.SecKeychainFindGenericPassword.side_effect = fake_find
        self.security.SecKeychainItemDelete.return_value = ec.ERR_SEC_SUCCESS
        ec.delete_secret("codex/AbC123_-xyz")
        self.security.SecKeychainItemDelete.assert_called_once()
        self.corefoundation.CFRelease.assert_called_once()

    def test_delete_failure_raises_without_secret(self):
        self.security.SecKeychainFindGenericPassword.return_value = ec.ERR_SEC_SUCCESS
        self.security.SecKeychainItemDelete.return_value = -25308
        with self.assertRaises(ec.CredentialError) as ctx:
            ec.delete_secret("codex/AbC123_-xyz")
        self.assertIn("-25308", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

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

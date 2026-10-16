from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from vibe_tools import codex_accounts as accounts
from vibe_tools.codex_auth import DIRECT_SCOPE, CodexAccount


def make_account(**overrides) -> CodexAccount:
    values = dict(
        account_id="AbC123_-xyz",
        label="测试账号",
        email="user@example.com",
        issuer="https://auth.openai.com",
        subject="subject-1",
        client_id="issued-client",
        ext_agent_host_id="urn:uuid:host",
        id_token="PLACEHOLDER-ID",
        access_token="PLACEHOLDER-ACCESS",
        refresh_token="PLACEHOLDER-REFRESH",
        token_type="Bearer",
        expires_in=3600,
        earliest_refresh_at=1234,
        scopes=(DIRECT_SCOPE,),
        saved_at="2026-10-16T00:00:00+00:00",
        plan_enabled=True,
    )
    values.update(overrides)
    return CodexAccount(**values)


class AccountIdTest(unittest.TestCase):
    def test_is_stable_and_url_safe(self):
        first = accounts.account_id_for(issuer="https://auth.openai.com", subject="sub-1", client_id="c1")
        second = accounts.account_id_for(issuer="https://auth.openai.com", subject="sub-1", client_id="c1")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9_-]{8,64}$")

    def test_distinguishes_subject_and_client(self):
        base = accounts.account_id_for(issuer="i", subject="s1", client_id="c1")
        self.assertNotEqual(base, accounts.account_id_for(issuer="i", subject="s2", client_id="c1"))
        self.assertNotEqual(base, accounts.account_id_for(issuer="i", subject="s1", client_id="c2"))
        self.assertNotEqual(base, accounts.account_id_for(issuer="i2", subject="s1", client_id="c1"))

    def test_does_not_embed_email(self):
        account_id = accounts.account_id_for(issuer="i", subject="s1", client_id="c1")
        self.assertNotIn("user@example.com", account_id)
        self.assertNotIn("subject-1", account_id)


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.secrets: dict[str, str] = {}
        patches = [
            patch.object(accounts, "_state_dir", return_value=self._tmp.name),
            patch.object(accounts.editor_credentials, "secret", side_effect=self._secret),
            patch.object(accounts.editor_credentials, "delete_secret", side_effect=self._delete),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _secret(self, name, value=None):
        if value is None:
            return self.secrets.get(name, "")
        self.secrets[name] = value
        return value

    def _delete(self, name):
        self.secrets.pop(name, None)
        return None

    def _registry_raw(self) -> str:
        path = os.path.join(self._tmp.name, "accounts.json")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_save_load_round_trip(self):
        account = make_account()
        accounts.save_account(account)
        self.assertIn("codex/AbC123_-xyz", self.secrets)
        loaded = accounts.load_account("AbC123_-xyz")
        self.assertEqual(loaded, account)

    def test_load_missing_returns_none(self):
        self.assertIsNone(accounts.load_account("AbC123_-xyz"))

    def test_list_accounts_returns_metadata_without_tokens(self):
        accounts.save_account(make_account())
        metadata = accounts.list_accounts()
        self.assertEqual(len(metadata), 1)
        entry = metadata[0]
        self.assertEqual(entry["account_id"], "AbC123_-xyz")
        self.assertEqual(entry["label"], "测试账号")
        self.assertEqual(entry["email"], "user@example.com")
        self.assertTrue(entry["plan_enabled"])
        for forbidden in ("access_token", "refresh_token", "id_token"):
            self.assertNotIn(forbidden, entry)

    def test_registry_file_never_stores_tokens(self):
        account = make_account(
            access_token="ACCESS-TOKEN-PLACEHOLDER",
            refresh_token="REFRESH-TOKEN-PLACEHOLDER",
            id_token="ID-TOKEN-PLACEHOLDER",
        )
        accounts.save_account(account)
        raw = self._registry_raw()
        self.assertNotIn("ACCESS-TOKEN-PLACEHOLDER", raw)
        self.assertNotIn("REFRESH-TOKEN-PLACEHOLDER", raw)
        self.assertNotIn("ID-TOKEN-PLACEHOLDER", raw)
        self.assertNotIn("subject-1", raw)

    def test_delete_account_removes_secret_and_metadata(self):
        accounts.save_account(make_account())
        accounts.delete_account("AbC123_-xyz")
        self.assertNotIn("codex/AbC123_-xyz", self.secrets)
        self.assertEqual(accounts.list_accounts(), [])
        self.assertIsNone(accounts.active_account_id())

    def test_active_account_pointer(self):
        self.assertIsNone(accounts.active_account_id())
        accounts.set_active_account("AbC123_-xyz")
        self.assertEqual(accounts.active_account_id(), "AbC123_-xyz")
        accounts.set_active_account(None)
        self.assertIsNone(accounts.active_account_id())

    def test_first_saved_account_becomes_active(self):
        accounts.save_account(make_account())
        self.assertEqual(accounts.active_account_id(), "AbC123_-xyz")

    def test_saved_bundle_is_valid_json(self):
        accounts.save_account(make_account())
        bundle = json.loads(self.secrets["codex/AbC123_-xyz"])
        self.assertEqual(bundle["account_id"], "AbC123_-xyz")
        self.assertEqual(bundle["scopes"], [DIRECT_SCOPE])


if __name__ == "__main__":
    unittest.main()

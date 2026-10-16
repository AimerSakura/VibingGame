"""Tests for the OpenAI-compatible provider's model catalog contract.

The API-Key mode build of ``SimpleLLMProvider`` lists models through the
OpenAI-compatible ``models.list()`` endpoint.  The normalized catalog shape is
shared with the editor/sidecar contract, so it is exercised here without any
network access.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vibe_tools.llm_transfer import SimpleLLMProvider, normalize_api_model_entries


class _FakeModelsEndpoint:
    def __init__(self, items=None, error: Exception | None = None) -> None:
        self._items = items or []
        self._error = error
        self.calls = 0

    def list(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=list(self._items))


class _FakeClient:
    def __init__(self, models: _FakeModelsEndpoint) -> None:
        self.models = models


def _provider(models: _FakeModelsEndpoint) -> SimpleLLMProvider:
    with patch.object(SimpleLLMProvider, "_init_client", return_value=_FakeClient(models)):
        return SimpleLLMProvider(model="gpt-4o-mini", api_key="test-key", base_url="http://x")


class NormalizeApiModelEntriesTest(unittest.TestCase):
    def test_entries_have_the_shared_catalog_shape(self):
        entries = normalize_api_model_entries(
            [SimpleNamespace(id="b-model"), SimpleNamespace(id="a-model")]
        )
        self.assertEqual([entry["id"] for entry in entries], ["a-model", "b-model"])
        for entry in entries:
            self.assertEqual(set(entry), {"id", "label", "display_name", "supports_image", "is_default"})
            self.assertEqual(entry["label"], entry["id"])
            self.assertEqual(entry["display_name"], entry["id"])
            # The /v1/models endpoint does not advertise input modalities.
            self.assertIsNone(entry["supports_image"])
            self.assertFalse(entry["is_default"])

    def test_dictionaries_and_duplicates_are_normalized(self):
        entries = normalize_api_model_entries(
            [{"id": "same"}, {"id": "same"}, {"id": "other"}, {"id": ""}, {}, None]
        )
        self.assertEqual([entry["id"] for entry in entries], ["other", "same"])

    def test_unknown_payloads_return_empty_list(self):
        self.assertEqual(normalize_api_model_entries(None), [])
        self.assertEqual(normalize_api_model_entries("not-a-list"), [])
        self.assertEqual(normalize_api_model_entries([SimpleNamespace(id=123)]), [])

    def test_overlong_ids_are_dropped(self):
        self.assertEqual(normalize_api_model_entries([{"id": "x" * 500}]), [])


class ListModelCatalogTest(unittest.TestCase):
    def test_list_model_catalog_uses_models_list(self):
        endpoint = _FakeModelsEndpoint([SimpleNamespace(id="gemini-2.5-pro")])
        provider = _provider(endpoint)
        catalog = provider.list_model_catalog()
        self.assertEqual(endpoint.calls, 1)
        self.assertEqual([entry["id"] for entry in catalog], ["gemini-2.5-pro"])

    def test_list_model_catalog_returns_empty_and_never_raises(self):
        endpoint = _FakeModelsEndpoint(error=RuntimeError("boom"))
        provider = _provider(endpoint)
        self.assertEqual(provider.list_model_catalog(), [])
        self.assertEqual(provider.list_models(), [])

    def test_list_models_legacy_shape_is_unchanged(self):
        endpoint = _FakeModelsEndpoint([SimpleNamespace(id="b"), SimpleNamespace(id="a")])
        provider = _provider(endpoint)
        self.assertEqual(provider.list_models(), ["b", "a"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

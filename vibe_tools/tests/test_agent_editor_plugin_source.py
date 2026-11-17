import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[2]
    / "editor"
    / "plugins"
    / "agent_editor_plugin.cpp"
).read_text(encoding="utf-8")


def switch_case_block(case_name: str, next_case: str) -> str:
    start = SOURCE.index(f"case {case_name}:")
    end = SOURCE.index(f"case {next_case}:", start)
    return SOURCE[start:end]


class AgentEditorStartupContractTest(unittest.TestCase):
    def test_persists_and_restores_auth_mode(self):
        self.assertIn('settings->set_setting("vibe_agent/auth_mode"', SOURCE)
        self.assertRegex(SOURCE, r'EDITOR_DEF\("vibe_agent/auth_mode"\s*,')

    def test_health_reads_config_state_before_pushing_configuration(self):
        health = switch_case_block("REQUEST_HEALTH", "REQUEST_CONFIGURE")
        config_state = switch_case_block("REQUEST_CONFIG_STATE", "REQUEST_MODELS")

        self.assertIn("_refresh_config_state();", health)
        self.assertNotIn("_push_configuration(false);", health)
        self.assertIn("_push_configuration(false);", config_state)

    def test_migrates_existing_chatgpt_account_when_no_mode_preference_exists(self):
        config_state = switch_case_block("REQUEST_CONFIG_STATE", "REQUEST_MODELS")

        self.assertIn("!auth_mode_preference_loaded", config_state)
        self.assertIn("chatgpt_account_id.is_empty()", config_state)
        self.assertIn('auth_mode->select(1);', config_state)

    def test_checks_auth_mode_preference_before_registering_default(self):
        constructor_start = SOURCE.index("AgentWorkspace::AgentWorkspace()")
        constructor = SOURCE[constructor_start:]

        self.assertIn('bool auth_mode_preference_loaded = false;',
                      Path(__file__).resolve().parents[2]
                      .joinpath("editor", "plugins", "agent_editor_plugin.h")
                      .read_text(encoding="utf-8"))
        self.assertRegex(
            constructor,
            r'auth_mode_preference_loaded = settings && settings->has_setting\("vibe_agent/auth_mode"\);'
            r'\s*String saved_auth_mode = EDITOR_DEF\("vibe_agent/auth_mode",',
        )

    def test_login_and_logout_persist_the_selected_auth_mode(self):
        method_start = SOURCE.index("void AgentWorkspace::_handle_chatgpt_status_response")
        method_end = SOURCE.index("void AgentWorkspace::_handle_chatgpt_request_error", method_start)
        login = SOURCE[method_start:method_end]
        logout = switch_case_block("REQUEST_CHATGPT_LOGOUT", "REQUEST_HISTORY")

        self.assertIn("_store_auth_mode_preference();", login)
        self.assertIn("_store_auth_mode_preference();", logout)

    def test_model_failure_releases_request_and_schedules_one_retry(self):
        failure_start = SOURCE.index('if (completed == REQUEST_MODELS)')
        failure_end = SOURCE.index('String detail = response.has("detail"', failure_start)
        failure = SOURCE[failure_start:failure_end]
        settings_start = SOURCE.index("void AgentWorkspace::_show_settings()")
        settings_end = SOURCE.index("void AgentWorkspace::_on_auth_mode_selected", settings_start)
        settings = SOURCE[settings_start:settings_end]

        self.assertIn("_mark_model_request_failed();", failure)
        self.assertIn("void AgentWorkspace::_mark_model_request_failed()", SOURCE)
        self.assertIn("models_requested = false;", SOURCE)
        self.assertIn("models_refresh_pending = true;", SOURCE)
        self.assertIn("models_retry_count = 0;", settings)

    def test_model_retry_waits_and_clears_stale_error_status(self):
        pending_start = SOURCE.index("void AgentWorkspace::_request_pending_models()")
        pending_end = SOURCE.index("void AgentWorkspace::_update_codex_image_capability", pending_start)
        pending = SOURCE[pending_start:pending_end]
        failure_start = SOURCE.index("void AgentWorkspace::_mark_model_request_failed()")
        failure_end = SOURCE.index("void AgentWorkspace::_request_pending_models()", failure_start)
        failure = SOURCE[failure_start:failure_end]
        settings_start = SOURCE.index("void AgentWorkspace::_show_settings()")
        settings_end = SOURCE.index("void AgentWorkspace::_on_auth_mode_selected", settings_start)
        settings = SOURCE[settings_start:settings_end]

        self.assertIn("models_retry_delay > 0.0", pending)
        self.assertIn("models_retry_count < 3", failure)
        self.assertIn("models_retry_delay =", failure)
        self.assertIn("codex_model_status->set_text(String());", settings)

    def test_model_http_failure_preserves_cached_models_and_clears_error(self):
        failure_start = SOURCE.index("if (completed == REQUEST_MODELS)")
        failure_end = SOURCE.index('String detail = response.has("detail"', failure_start)
        failure = SOURCE[failure_start:failure_end]

        # HTTP 请求失败时不会走到 _apply_model_catalog，上一次成功的 codex_model
        # 缓存仍然可用：不能清空它，并且要清掉“无法获取”的误导性文案；
        # 只有没有任何缓存模型时才显示现有失败提示。
        self.assertNotIn("codex_model->clear();", failure)
        self.assertIn("_mark_model_request_failed();", failure)
        self.assertRegex(
            failure,
            r'if \(codex_model && codex_model->get_item_count\(\) > 0\) \{\s*'
            r'codex_model_status->set_text\(String\(\)\);\s*'
            r'\} else \{\s*'
            r'codex_model_status->set_text\(_u8\("无法获取 Codex 模型列表',
        )


if __name__ == "__main__":
    unittest.main()

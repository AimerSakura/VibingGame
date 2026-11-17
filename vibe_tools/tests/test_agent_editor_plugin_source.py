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

    def test_completed_login_configures_chatgpt_before_requesting_models(self):
        header = (
            Path(__file__).resolve().parents[2]
            / "editor"
            / "plugins"
            / "agent_editor_plugin.h"
        ).read_text(encoding="utf-8")
        self.assertIn("void _finish_chatgpt_login(bool p_request_models = true);", header)

        status_start = SOURCE.index("void AgentWorkspace::_handle_chatgpt_status_response")
        completed_start = SOURCE.index('if (state == "completed"', status_start)
        completed_end = SOURCE.index('if (state == "failed"', completed_start)
        completed = SOURCE[completed_start:completed_end]

        # 登录成功时 sidecar 还没有收到 auth_mode=chatgpt：这里只能更新 UI，
        # 不能提前请求模型，否则请求会以 API Key 模式发出并占住共享请求通道，
        # 导致随后的 /v1/configure 无法提交，模型目录也就一直不会刷新。
        # 必须先 _push_configuration(false)，等 REQUEST_CONFIGURE 成功后再补拉目录。
        self.assertIn("_finish_chatgpt_login(false);", completed)
        self.assertIn("_update_chatgpt_ui(false);", completed)
        success_start = completed.index("_finish_chatgpt_login(false);")
        success = completed[success_start:]
        # 账号校验失败分支保持原有行为（可请求模型）；成功路径不得再用默认参数。
        self.assertNotIn("_finish_chatgpt_login();", success)
        self.assertLess(
            success_start,
            completed.index("_push_configuration(false);"),
        )

        finish_start = SOURCE.index("void AgentWorkspace::_finish_chatgpt_login(bool p_request_models)")
        finish_end = SOURCE.index("void AgentWorkspace::_process_chatgpt_poll", finish_start)
        finish = SOURCE[finish_start:finish_end]
        self.assertIn("_update_chatgpt_ui(p_request_models);", finish)

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

    def test_model_catalog_error_preserves_cached_models_before_replacing_list(self):
        method_start = SOURCE.index("void AgentWorkspace::_apply_model_catalog")
        method_end = SOURCE.index("void AgentWorkspace::_on_api_model_choice_selected", method_start)
        catalog = SOURCE[method_start:method_end]
        clear_index = catalog.index("codex_model->clear();")
        error_index = catalog.index("if (!error.is_empty())")
        error_branch = catalog[error_index:clear_index]

        self.assertLess(error_index, clear_index)
        self.assertIn("_mark_model_request_failed();", error_branch)
        self.assertIn("codex_model->get_item_count() > 0", error_branch)
        self.assertIn("codex_model_status->set_text(String());", error_branch)

    def test_json_null_catalog_error_does_not_block_successful_catalog(self):
        helper_start = SOURCE.index("static String _agent_auth_text")
        helper_end = SOURCE.index("static String _agent_model_id_text", helper_start)
        helper = SOURCE[helper_start:helper_end]
        catalog_start = SOURCE.index("void AgentWorkspace::_apply_model_catalog")
        catalog_end = SOURCE.index("void AgentWorkspace::_on_api_model_choice_selected", catalog_start)
        catalog = SOURCE[catalog_start:catalog_end]

        # 成功的 ChatGPT 目录响应会带 "error": null。Godot 把 NIL 转成字符串时
        # 得到 "<null>"，若把它当成非空错误，目录会在填充 codex_model 之前返回，
        # 下拉框保持为空并显示“无法获取”提示。NIL 必须解码为空字符串。
        self.assertIn('_agent_auth_text(p_response.get("error", ""), 64)', catalog)
        self.assertRegex(
            helper,
            r'(?s)if \(p_value\.get_type\(\) == Variant::NIL\) \{.*?return String\(\);.*?\}',
        )


if __name__ == "__main__":
    unittest.main()

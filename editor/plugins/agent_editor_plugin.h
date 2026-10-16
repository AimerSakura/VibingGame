/**************************************************************************/
/*  agent_editor_plugin.h                                                 */
/**************************************************************************/

#pragma once

#include "core/io/http_client.h"
#include "core/os/os.h"
#include "editor/plugins/editor_plugin.h"
#include "scene/gui/box_container.h"

class Button;
class AcceptDialog;
class EditorFileDialog;
class HTTPRequest;
class Label;
class LineEdit;
class OptionButton;
class RichTextLabel;
class TabContainer;
class TextEdit;
class Tree;
class TextureRect;

class AgentWorkspace : public VBoxContainer {
	GDCLASS(AgentWorkspace, VBoxContainer);
	friend class AgentEditorPlugin;

	enum RequestKind {
		REQUEST_NONE,
		REQUEST_HEALTH,
		REQUEST_CONFIGURE,
		REQUEST_HISTORY,
		REQUEST_CLEAR,
		REQUEST_CANCEL,
		REQUEST_ATTACHMENTS,
		REQUEST_ASSETS,
		REQUEST_HARNESS_PLAN,
		REQUEST_HARNESS_SAVE,
		REQUEST_ROLLBACK,
		REQUEST_CONFIG_STATE,
		REQUEST_MODELS,
		REQUEST_CHATGPT_START,
		REQUEST_CHATGPT_STATUS,
		REQUEST_CHATGPT_CANCEL,
		REQUEST_CHATGPT_LOGOUT,
	};

	enum StreamKind {
		STREAM_NONE,
		STREAM_AGENT,
		STREAM_HARNESS,
	};

	HTTPRequest *http = nullptr;
	HTTPRequest *editor_state_http = nullptr;
	Ref<HTTPClient> stream_client;
	PackedByteArray stream_bytes;
	String stream_path;
	String stream_body;
	StreamKind stream_kind = STREAM_NONE;
	bool stream_request_sent = false;
	double stream_idle = 0.0;

	TabContainer *tabs = nullptr;
	RichTextLabel *transcript = nullptr;
	TextEdit *prompt = nullptr;
	Label *status = nullptr;
	Label *attachments_label = nullptr;
	LineEdit *api_key = nullptr;
	LineEdit *base_url = nullptr;
	LineEdit *model = nullptr;
	LineEdit *vision_model = nullptr;
	// API Key 模式字段独立分组，切到 ChatGPT 模式时整组隐藏，绝不与 Codex 模型混用。
	VBoxContainer *api_section = nullptr;
	// API 模型候选列表；接口不支持 models.list 时隐藏，保留上面的手动输入框。
	OptionButton *api_model_choices = nullptr;
	OptionButton *response_language = nullptr;
	OptionButton *auth_mode = nullptr;
	VBoxContainer *chatgpt_section = nullptr;
	// Codex 模型只能来自 sidecar /v1/models（app-server model/list），不使用任何默认字符串兜底。
	OptionButton *codex_model = nullptr;
	Label *codex_model_status = nullptr;
	// 选中的 Codex 模型是否声明 image input modality；未确认时按不支持处理。
	bool codex_image_supported = true;
	bool models_requested = false;
	bool api_models_requested = false;
	Button *attach_button = nullptr;
	Label *chatgpt_status = nullptr;
	Label *chatgpt_account_info = nullptr;
	Button *chatgpt_login_button = nullptr;
	Button *chatgpt_cancel_button = nullptr;
	Button *chatgpt_logout_button = nullptr;
	AcceptDialog *settings_dialog = nullptr;
	Button *send_button = nullptr;
	Button *cancel_button = nullptr;
	Button *retry_button = nullptr;
	Tree *test_tree = nullptr;
	TextEdit *test_plan_editor = nullptr;
	RichTextLabel *test_output = nullptr;
	RichTextLabel *test_details = nullptr;
	BoxContainer *test_evidence = nullptr;
	TextureRect *test_screenshot = nullptr;
	Label *test_phase = nullptr;
	Button *test_stop = nullptr;
	Vector<Button *> test_edit_buttons;
	Dictionary test_plan;
	double test_refresh_elapsed = 0.0;
	EditorFileDialog *attachment_dialog = nullptr;
	EditorFileDialog *asset_dialog = nullptr;

	OS::ProcessID sidecar_pid = 0;
	String handshake_file;
	String backend_url;
	String backend_token;
	String current_run_id;
	String last_agent_run_id;
	String last_prompt;
	// ChatGPT 认证由 sidecar 托管：编辑器只保存短期 login_id/account_id 与展示用 metadata。
	String chatgpt_login_id;
	// 取消登录时，status 轮询可能正占用共享 HTTPRequest；这里排队一次待发送的 cancel，
	// 等当前请求结束、request_kind 归位后再补发，避免并发复用同一 HTTPRequest。
	bool chatgpt_cancel_pending = false;
	String chatgpt_cancel_login_id;
	String chatgpt_account_id;
	String chatgpt_account_label;
	String chatgpt_account_email;
	String chatgpt_codex_version;
	bool chatgpt_plan_enabled = true;
	bool chatgpt_supported = false;
	bool chatgpt_login_active = false;
	bool config_state_loaded = false;
	double chatgpt_poll_elapsed = 0.0;
	double chatgpt_login_elapsed = 0.0;
	double config_state_retry = 0.0;
	PackedStringArray attachment_ids;
	RequestKind request_kind = REQUEST_NONE;
	bool backend_ready = false;
	bool task_running = false;
	bool workspace_visible = false;
	double startup_elapsed = 0.0;

	static String _u8(const char *p_text);
	void _set_status(const String &p_text, bool p_error = false);
	void _append_inline_markdown(const String &p_text, int p_depth = 0);
	void _open_markdown_link(const Variant &p_meta);
	void _append_markdown(const String &p_text);
	void _append_message(const String &p_author, const String &p_text);
	PackedStringArray _get_dirty_files() const;
	void _scan_changes();

	void _start_sidecar();
	void _stop_sidecar();
	void _restart_sidecar();
	void _read_handshake();
	void _request(const String &p_path, HTTPClient::Method p_method, const Dictionary &p_body, RequestKind p_kind);
	void _request_completed(int p_result, int p_code, const PackedStringArray &p_headers, const PackedByteArray &p_body);
	void _push_configuration(bool p_include_key);

	void _start_stream(const String &p_path, const Dictionary &p_body, StreamKind p_kind);
	void _process_stream(double p_delta);
	void _consume_stream_events();
	void _handle_sse(const String &p_event);
	void _finish_stream(bool p_error, const String &p_message = String());

	void _send();
	void _retry();
	void _cancel();
	void _clear_conversation();
	void _save_settings();
	void _show_settings();
	void _quick_prompt(const String &p_text);
	void _rollback_last_task();

	bool _is_chatgpt_request(RequestKind p_kind) const;
	bool _chatgpt_mode_active() const;
	bool _auth_mode_selected() const;
	String _codex_model_id() const;
	void _request_models();
	void _apply_model_catalog(const Dictionary &p_response);
	void _on_api_model_choice_selected(int p_index);
	void _on_codex_model_selected(int p_index);
	void _update_codex_image_capability();
	void _apply_config_state(const Dictionary &p_response);
	bool _apply_chatgpt_account(const Dictionary &p_account);
	void _update_chatgpt_ui();
	void _refresh_config_state();
	void _start_chatgpt_login();
	void _cancel_chatgpt_login();
	void _flush_pending_chatgpt_cancel();
	void _logout_chatgpt();
	void _process_chatgpt_poll(double p_delta);
	void _handle_chatgpt_status_response(const Dictionary &p_response);
	void _handle_chatgpt_request_error(RequestKind p_kind, int p_code);
	void _finish_chatgpt_login();
	void _on_auth_mode_selected(int p_index);

	void _choose_attachments();
	void _choose_assets();
	void _attachments_selected(const PackedStringArray &p_paths);
	void _assets_selected(const PackedStringArray &p_paths);
	void _paste_clipboard_image();
	void _files_dropped(const PackedStringArray &p_files);

	void _load_harness_plan();
	void _save_harness_plan();
	void _run_selected_test();
	void _run_all_tests();
	void _open_selected_artifact();
	void _render_harness_plan(const Dictionary &p_plan);
	void _show_test_details();
	void _regenerate_tests();
	void _toggle_test_json(bool p_visible);
	void _set_test_busy(bool p_busy);

protected:
	void _notification(int p_what);

public:
	AgentWorkspace();
	~AgentWorkspace();
};

class AgentEditorPlugin : public EditorPlugin {
	GDCLASS(AgentEditorPlugin, EditorPlugin);

	AgentWorkspace *workspace = nullptr;

public:
	virtual String get_plugin_name() const override { return "Agent"; }
	virtual bool has_main_screen() const override { return true; }
	virtual void edit(Object *p_object) override {}
	virtual bool handles(Object *p_object) const override { return false; }
	virtual void make_visible(bool p_visible) override;
	virtual void selected_notify() override;

	AgentEditorPlugin();
};

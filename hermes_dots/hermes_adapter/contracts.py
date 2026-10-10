"""Recorded v2026.9.24 contracts, checked against git show at the integration pin."""

HERMES_REF = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
HOOK_PAYLOADS = {
    "pre_tool_call": ("tool_name", "args", "session_id", "task_id", "turn_id", "tool_call_id"),
    "subagent_start": ("parent_session_id", "child_session_id", "child_subagent_id", "child_role", "child_goal"),
    "subagent_stop": ("parent_session_id", "child_session_id", "child_role", "child_summary", "child_status", "tool_call_history", "duration_ms"),
    "transform_tool_result": ("tool_name", "args", "result", "task_id", "session_id", "duration_ms", "status"),
    "post_tool_call": ("tool_name", "args", "result", "session_id", "task_id", "status"),
    "post_llm_call": ("session_id", "task_id", "turn_id", "user_message", "assistant_response", "conversation_history", "model", "platform"),
    "pre_gateway_dispatch": ("event", "gateway", "session_store"),
    "on_session_end": ("session_id", "task_id", "turn_id", "completed", "failed", "interrupted", "turn_exit_reason", "model", "platform"),
}
CRON_CREATE_FIELDS = ("prompt", "schedule", "name", "repeat", "deliver", "origin", "skill", "skills",
                      "model", "provider", "base_url", "script", "context_from", "enabled_toolsets", "workdir",
                      "no_agent", "attach_to_session", "monitor_script", "monitor_url", "reasoning_effort",
                      "failure_deliver", "paused", "paused_reason", "pinned")

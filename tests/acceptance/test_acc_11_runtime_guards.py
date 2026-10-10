"""§13.11: native hooks enforce paths, approvals, helpers, redaction."""

import json

import pytest

from .invariants import assert_invariants


def test_real_runtime_hooks_block_unauthorized_tools(scenario, tmp_path):
    s = scenario
    hooks = s.runtime.hooks
    denied = tmp_path / "private"
    s.settings.confirm_change(s.settings.propose_change("extra_denied_paths", [str(denied)]))
    for name, args in (("read_file", {"path": "~/.hermes/config.yaml"}),
                       ("write_file", {"path": str(denied / "file")}),
                       ("terminal", {"command": "cat $HOME/.hermes/config.yaml"}),
                       ("terminal", {"command": f"cat {denied / 'file'}"}),
                       ("slack_post", {"target": "Acme", "content": "Ready"})):
        assert hooks.pre_tool_call(name, args, session_id="primary")["action"] == "block"
    card = s.working()
    hooks.sessions["primary"] = (card.id, 1)
    args = {"target": "Acme", "content": "Ready"}
    content = json.dumps(args, sort_keys=True)
    approval = s.engine.approvals(card.id).grant("post", "Acme", content, 1)
    assert hooks.pre_tool_call("slack_post", {**args, "content": "Changed"}, session_id="primary")["action"] == "block"
    assert hooks.pre_tool_call("slack_post", args, session_id="primary") is None
    s.approved_effects.append((card.id, approval, "post", "Acme", content, 1))
    assert hooks.pre_tool_call("slack_post", args, session_id="primary")["action"] == "block"
    hooks.subagent_start("primary", "helper")
    hooks.subagent_stop("primary", "helper")
    with pytest.raises(PermissionError):
        s.runtime.brain_write({"text": "Helper lesson"}, session_id="helper")
    assert hooks.pre_tool_call("dots_brain_write", {"text": "Helper lesson"}, session_id="helper")["action"] == "block"
    token = "xoxb-" + "invented-12345"
    result = hooks.transform_tool_result("read_file", {}, {"text": token})
    assert token not in result and "[REDACTED]" in result
    hooks.post_tool_call(tool_name="read_file", args={}, result=token)
    hooks.post_llm_call(user_message=token, assistant_response="Acme")
    assert token not in str(s.logs())
    assert_invariants(s)

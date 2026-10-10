"""Hermes v2026.9.24 registration: tools, guards, memory and Slack callbacks."""

import asyncio
import os
from pathlib import Path

from hermes_dots.slack.render import HOOKS as SLACK_HOOKS
from hermes_dots.inbox.service import HOOKS as INBOX_HOOKS
from .hooks import HOOKS as RUNTIME_HOOKS
from .runtime import DotsRuntime, json_result

HOOK_MODULES = list(dict.fromkeys([*SLACK_HOOKS, *INBOX_HOOKS, *RUNTIME_HOOKS]))
TOOLS = {
    "dots_brain_search": ("Search the brain and return citations.", {"query": {"type": "string"}}),
    "dots_brain_write": ("Request a serialized notebook write as Orca.", {"text": {"type": "string"}, "task_id": {"type": "string"}}),
    "dots_task": ("Create a card or operate a confirmed task.", {"operation": {"type": "string"}}),
    "dots_settings": ("List settings or propose a change for owner confirmation.", {"text": {"type": "string"}}),
    "dots_schedule": ("Create, list, pause or remove task-linked Hermes schedules.", {"operation": {"type": "string"}}),
    "dots_helper": ("Delegate a task to a temporary Hermes helper.", {"task_id": {"type": "string"}, "goal": {"type": "string"}}),
    "dots_tick": ("Process local work and practice mail in the primary session.", {}),
}


def register(ctx):
    from .memory import BrainMemoryProvider
    runtime = DotsRuntime(ctx=ctx)
    ctx.on_unload(runtime.close)
    ctx.register_memory_provider(BrainMemoryProvider())
    for name in RUNTIME_HOOKS:
        ctx.register_hook(name, getattr(runtime.hooks, name))

    def handle(name, args, session_id):
        if runtime.hooks.helper(session_id) and name != "dots_brain_search":
            raise PermissionError("Helpers may search but cannot operate owner services")
        if name == "dots_brain_search": return runtime.brain.search(args["query"][:1000], limit=5)
        if name == "dots_brain_write": return runtime.brain_write(args, session_id=session_id)
        if name == "dots_task": return runtime.task(args, session_id=session_id)
        if name == "dots_settings": return runtime.settings_tool(args)
        if name == "dots_helper":
            return runtime.helpers.launch(runtime.engine, args["task_id"], args["goal"], parent_session_id=session_id)
        if name == "dots_tick":
            result = runtime.tick()
            if not runtime.engine.model_blocked(): result["mail"] = runtime.mail.tick()
            return result
        operation = args["operation"]
        if operation == "list": return runtime.engine.schedules()
        task = runtime.ledger.get_task(args["task_id"])
        runtime.engine._require_confirmed(task.id)
        if operation == "create":
            return runtime.scheduler.create(runtime.engine, task.id, args["instruction"], args["time"],
                                            args["timezone"], args.get("end_date"))
        if operation not in {"pause", "remove"}: raise ValueError("Unknown schedule operation")
        resource = next((r for r in runtime.engine.resources(task.id, "schedule") if r["id"] == args["job_id"]), None)
        if resource is None: raise PermissionError("Schedule does not belong to this task")
        runtime.engine._resource_action(resource, operation, "paused" if operation == "pause" else "removed")
        return {"job_id": resource["id"], "operation": operation}

    for name, (description, properties) in TOOLS.items():
        def handler(args, _name=name, **kwargs):
            # Runtime identity comes from Hermes, never model-generated tool arguments.
            session_id = kwargs.get("session_id") or kwargs.get("task_id", "")
            return json_result(handle(_name, args, session_id))
        schema = {"name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "additionalProperties": True}}
        ctx.register_tool(name=name, toolset="hermes_dots", schema=schema, handler=handler)

    for action_id in list(dict.fromkeys([*SLACK_HOOKS, *INBOX_HOOKS])):
        async def action_handler(ack, body, action, _id=action_id):
            await ack()
            def dispatch():
                route = runtime.bridge.receive({**body, "action": {**action, "action_id": _id}})
                if route.kind == "ignored": return route
                try:
                    result = runtime.route(route) if route.kind != "question" else {"reply": route.text}
                except (ValueError, PermissionError) as error:
                    runtime.hooks.event("slack_action_rejected", {"error": type(error).__name__})
                    result = {"reply": "Please use the current card or request a fresh proposal."}
                transport = getattr(runtime, "transport", None)
                if transport and isinstance(result, dict) and result.get("reply"):
                    transport.reply(result["reply"], thread_ts=(body.get("message") or {}).get("ts") or body.get("message_ts"))
                return result
            return await asyncio.to_thread(dispatch)
        ctx.register_slack_action_handler(action_id, action_handler)

    def platform_handler(native, adapter):
        from .slack import SlackRuntime
        transport = SlackRuntime(runtime, ctx, native)
        transport.attach()

    ctx.register_platform_handler("slack", platform_handler)

    profile = Path(os.environ["HERMES_HOME"])
    script = profile / "scripts/dots-tick.sh"
    if script.exists(): runtime.scheduler.ensure_tick(str(script), runtime.root / "workspace")
    return runtime

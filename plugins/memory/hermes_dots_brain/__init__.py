"""MemoryProvider entry point installed into HERMES_HOME/plugins/hermes_dots_brain."""

from hermes_dots.hermes_adapter.memory import BrainMemoryProvider


def register(ctx):
    ctx.register_memory_provider(BrainMemoryProvider())

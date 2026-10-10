"""Deterministic structured model responses; never contacts a provider."""

from copy import deepcopy


class ModelLimitError(RuntimeError):
    pass


class FakeModel:
    def __init__(self, responses=None, *, mode="normal"):
        self.responses = {}
        self.mode = mode
        self.calls = 0
        self.tokens = 0
        self.requests = []
        for key, response in (responses or {}).items():
            self.register(key, response)

    def register(self, key, response, *, tokens=10):
        if tokens < 0:
            raise ValueError("tokens must be nonnegative")
        self.responses[key] = (deepcopy(response), tokens)

    def complete_structured(self, schema, prompt, *, key=None):
        self.calls += 1
        self.requests.append(deepcopy({"schema": schema, "prompt": prompt, "key": key}))
        if self.mode == "limit":
            raise ModelLimitError("fake model usage limit reached")
        if self.mode == "malformed":
            self.tokens += 1
            return "{malformed JSON"
        if self.mode != "normal":
            raise ValueError(f"unknown model mode: {self.mode}")
        response, tokens = self.responses[key]
        self.tokens += tokens
        return deepcopy(response)

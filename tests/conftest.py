import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winagent.backends.fake import FakeBackend  # noqa: E402
from winagent.config import Config  # noqa: E402


@pytest.fixture
def config() -> Config:
    # verify_on_completion is OFF in the shared fixture so scripted-LLM tests keep their exact call
    # counts; tests that exercise the completion verification set it to True explicitly.
    return Config(api_base_url="http://fake.local/v1", api_key="k", model="fake-model", action_delay=0.0,
                  screenshot_max_width=800, confirm_dangerous_actions=True, verify_on_completion=False)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(width=1600, height=900)


class ScriptedLLM:
    """Stand-in for LLMClient that replays scripted responses."""

    def __init__(self, responses, native=True):
        self.responses = list(responses)
        self.native = native
        self.calls = []
        self.supports_tools = None
        self.supports_vision = None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def reset_cancel(self):
        self._cancelled = False

    def chat(self, messages, tools=None, tool_choice=None, response_json=False, temperature=None, max_tokens=None):
        from winagent.llm import ChatResponse, LLMError

        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools,
                           "temperature": temperature, "max_tokens": max_tokens})
        if not self.responses:
            raise LLMError("script exhausted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        if isinstance(item, ChatResponse):
            return item
        if isinstance(item, str):
            message = {"role": "assistant", "content": item}
        else:
            message = item
        return ChatResponse(message=message, finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 5},
                            model="fake-model", raw={}, latency=0.01)


def native_tool_message(*calls, content=None):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)
        ],
    }


@pytest.fixture
def scripted_llm():
    return ScriptedLLM

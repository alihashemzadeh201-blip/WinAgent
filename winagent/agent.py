"""The agent loop.

``Agent.run(task)`` sends the conversation to the model, executes the tool
calls it returns, feeds the results (including screenshots) back, and repeats
until the model calls ``task_complete``, replies with plain text, asks the
user something, hits ``max_steps`` or is stopped.

The class is UI-agnostic: progress is reported through :class:`AgentEvents`
callbacks so the same core drives the Qt GUI and the command line.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .backends.base import DesktopBackend, EmergencyStop
from .config import Config
from .llm import LLMCancelled, LLMClient, LLMError
from .prompts import build_system_prompt
from .protocol import (
    AssistantTurn,
    ToolCall,
    assistant_message_json,
    assistant_message_native,
    image_part,
    parse_json_protocol,
    parse_native_response,
    text_part,
    tool_result_message_native,
    tool_results_message_json,
)
from .screenshot import Screenshot
from .screenguard import ScreenGuard
from .tools import ToolExecutor, ToolResult
from .tools.definitions import openai_tool_schemas

log = logging.getLogger(__name__)


@dataclass
class AgentEvents:
    """Callbacks the UI can hook into (all optional, called from the worker thread)."""

    on_status: Callable[[str], None] = lambda msg: None
    on_thought: Callable[[str], None] = lambda text: None
    on_assistant_text: Callable[[str], None] = lambda text: None
    on_tool_start: Callable[[ToolCall], None] = lambda call: None
    on_tool_end: Callable[[ToolResult], None] = lambda result: None
    on_screenshot: Callable[[Screenshot], None] = lambda shot: None
    on_step: Callable[[int, int], None] = lambda step, max_steps: None
    on_error: Callable[[str], None] = lambda msg: None
    on_done: Callable[["RunOutcome"], None] = lambda outcome: None
    ask_user: Callable[[str, list[str]], Optional[str]] = lambda q, opts: None
    confirm: Callable[[ToolCall, str], bool] = lambda call, reason: False


@dataclass
class RunOutcome:
    status: str                # completed | answered | stopped | error | max_steps | waiting_user
    message: str = ""
    steps: int = 0
    tool_calls: int = 0
    duration: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)


class Agent:
    def __init__(self, config: Config, backend: DesktopBackend, llm: Optional[LLMClient] = None,
                 events: Optional[AgentEvents] = None, guard: Optional[ScreenGuard] = None):
        self.config = config
        self.backend = backend
        self.llm = llm or LLMClient(config)
        self.events = events or AgentEvents()
        self.stop_event = threading.Event()
        # ``guard`` keeps the host UI out of screenshots / from under the mouse (the GUI passes a QtScreenGuard)
        self.executor = ToolExecutor(backend, config, confirm=self._confirm, stop_event=self.stop_event, guard=guard)
        self.history: list[dict[str, Any]] = []   # chat history WITHOUT the system prompt
        self._image_slots: list[int] = []          # indexes of history messages that carry images
        self.protocol = self._initial_protocol()
        self.vision = bool(config.vision_enabled)
        self._lock = threading.Lock()
        self.running = False

    # --------------------------------------------------------------- control
    def _initial_protocol(self) -> str:
        p = (self.config.tool_protocol or "auto").lower()
        return "json" if p == "json" else "native"

    def stop(self) -> None:
        self.stop_event.set()
        self.llm.cancel()

    def reset(self) -> None:
        with self._lock:
            self.history.clear()
            self._image_slots.clear()
            self.executor.last_screenshot = None

    def _confirm(self, call: ToolCall, reason: str) -> bool:
        try:
            return bool(self.events.confirm(call, reason))
        except Exception:  # pragma: no cover
            log.exception("confirm callback failed")
            return False

    # ------------------------------------------------------------ messaging
    def _system_message(self) -> dict[str, Any]:
        try:
            info = self.backend.system_info()
        except Exception as exc:  # pragma: no cover
            info = {"note": f"system info unavailable: {exc}"}
        prompt = build_system_prompt(protocol=self.protocol, vision=self.vision, system_info=info,
                                     language=self.config.response_language, extra=self.config.extra_system_prompt)
        return {"role": "system", "content": prompt}

    def _messages(self) -> list[dict[str, Any]]:
        return [self._system_message(), *self.history]

    def _append(self, msg: dict[str, Any], has_image: bool = False) -> None:
        self.history.append(msg)
        if has_image:
            self._image_slots.append(len(self.history) - 1)
        self._trim()

    def _trim(self) -> None:
        # 1. strip old images (keep the last N)
        keep = max(1, self.config.max_images_in_context)
        while len(self._image_slots) > keep:
            idx = self._image_slots.pop(0)
            if idx < len(self.history):
                self.history[idx] = _strip_images(self.history[idx])
        # 2. cap total messages (keep the first user message for context)
        limit = max(10, self.config.max_history_messages)
        if len(self.history) > limit:
            overflow = len(self.history) - limit
            first = self.history[0]
            cut = self.history[1 + overflow:]
            # never start with an orphan tool message
            while cut and cut[0].get("role") == "tool":
                cut.pop(0)
            self.history = [first, *cut]
            self._image_slots = [i for i in range(len(self.history)) if _has_image(self.history[i])]

    def _user_message(self, text: str, shot: Optional[Screenshot] = None) -> dict[str, Any]:
        if shot is not None and self.vision:
            return {"role": "user", "content": [text_part(text), image_part(shot.data_url())]}
        return {"role": "user", "content": text}

    # ------------------------------------------------------------------ run
    def run(self, task: str, *, initial_screenshot: bool = True) -> RunOutcome:
        with self._lock:
            return self._run(task, initial_screenshot=initial_screenshot)

    def _run(self, task: str, *, initial_screenshot: bool) -> RunOutcome:
        start = time.time()
        self.stop_event.clear()
        self.llm.reset_cancel()
        self.running = True
        steps = 0
        n_calls = 0
        outcome = RunOutcome("error")
        try:
            shot = None
            if initial_screenshot and self.vision:
                try:
                    self.events.on_status("Taking screenshot…")
                    shot = self.executor.take_screenshot()
                    self.events.on_screenshot(shot)
                except Exception as exc:
                    log.warning("initial screenshot failed: %s", exc)
            text = task
            if shot is not None:
                text = f"{task}\n\n[Current screen attached. {shot.describe()}]"
            self._append(self._user_message(text, shot), has_image=shot is not None)

            while True:
                if self.stop_event.is_set():
                    outcome = RunOutcome("stopped", "Stopped by user.")
                    break
                if steps >= self.config.max_steps:
                    outcome = RunOutcome("max_steps", f"Reached the limit of {self.config.max_steps} steps. Send a new message to continue.")
                    break
                steps += 1
                self.events.on_step(steps, self.config.max_steps)
                self.events.on_status(f"Thinking… (step {steps}/{self.config.max_steps})")

                turn = self._call_model()
                if turn.thought:
                    self.events.on_thought(turn.thought)

                if not turn.tool_calls:
                    # plain answer -> conversation turn is over
                    reply = turn.text or "(empty response)"
                    self._append(assistant_message_native(turn) if self.protocol == "native" else assistant_message_json(turn))
                    self.events.on_assistant_text(reply)
                    outcome = RunOutcome("answered", reply)
                    break

                if turn.text:
                    self.events.on_assistant_text(turn.text)
                self._append(assistant_message_native(turn) if self.protocol == "native" else assistant_message_json(turn))

                results: list[ToolResult] = []
                finished: Optional[ToolResult] = None
                waiting: Optional[ToolResult] = None
                for call in turn.tool_calls:
                    if self.stop_event.is_set():
                        results.append(ToolResult(call, False, error="Stopped by user."))
                        continue
                    if finished is not None or waiting is not None:
                        results.append(ToolResult(call, False, error="Skipped: a previous tool call ended the turn."))
                        continue
                    n_calls += 1
                    self.events.on_tool_start(call)
                    self.events.on_status(f"Running {call.name}…")
                    result = self.executor.execute(call)
                    self.events.on_tool_end(result)
                    if result.screenshot is not None:
                        self.events.on_screenshot(result.screenshot)
                    results.append(result)
                    if result.task_complete:
                        finished = result
                    elif result.ask_user is not None:
                        waiting = result

                if waiting is not None:
                    q = waiting.ask_user["question"]
                    opts = waiting.ask_user.get("options") or []
                    self.events.on_assistant_text(q)
                    self.events.on_status("Waiting for your answer…")
                    answer = self.events.ask_user(q, opts)
                    if answer is None:
                        waiting.data = {"message": "The user did not answer (cancelled)."}
                        self._append_results(results)
                        outcome = RunOutcome("stopped" if self.stop_event.is_set() else "waiting_user", q)
                        break
                    waiting.data = {"user_answer": answer}
                    self._append_results(results)
                    continue

                self._append_results(results)

                if finished is not None:
                    summary = finished.data.get("summary", "Done.")
                    self.events.on_assistant_text(summary)
                    outcome = RunOutcome("completed" if finished.data.get("success", True) else "answered", summary)
                    break
                if self.stop_event.is_set():
                    outcome = RunOutcome("stopped", "Stopped by user.")
                    break
        except EmergencyStop as exc:
            outcome = RunOutcome("stopped", str(exc) or "Emergency stop.")
        except LLMCancelled:
            outcome = RunOutcome("stopped", "Stopped by user.")
        except LLMError as exc:
            log.error("LLM error: %s", exc)
            self.events.on_error(str(exc))
            outcome = RunOutcome("error", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("agent crashed")
            self.events.on_error(f"{type(exc).__name__}: {exc}")
            outcome = RunOutcome("error", f"{type(exc).__name__}: {exc}")
        finally:
            self.running = False
        outcome.steps = steps
        outcome.tool_calls = n_calls
        outcome.duration = time.time() - start
        outcome.usage = {"prompt_tokens": self.llm.total_prompt_tokens, "completion_tokens": self.llm.total_completion_tokens}
        self.events.on_done(outcome)
        return outcome

    # ------------------------------------------------------------ internals
    def _append_results(self, results: list[ToolResult]) -> None:
        """Add tool results (and the newest screenshot) to the history."""
        latest_shot: Optional[Screenshot] = None
        for r in results:
            if r.screenshot is not None:
                latest_shot = r.screenshot
        if self.protocol == "native":
            for r in results:
                self._append(tool_result_message_native(r.call, r.to_text()))
            if latest_shot is not None and self.vision:
                self._append(self._user_message(f"[Screenshot after the actions above. {latest_shot.describe()}]", latest_shot), has_image=True)
        else:
            msg = tool_results_message_json([(r.call, r.to_text()) for r in results])
            if latest_shot is not None and self.vision:
                msg["content"] = [text_part(msg["content"] + f"\n[Screenshot after the actions above. {latest_shot.describe()}]"),
                                  image_part(latest_shot.data_url())]
                self._append(msg, has_image=True)
            else:
                self._append(msg)

    def _call_model(self) -> AssistantTurn:
        """Call the model with the current protocol, downgrading features on failure."""
        for _attempt in range(4):
            if self.stop_event.is_set():
                raise LLMCancelled("stopped")
            messages = self._messages()
            try:
                if self.protocol == "native":
                    resp = self.llm.chat(messages, tools=openai_tool_schemas())
                    turn = parse_native_response(resp.message)
                else:
                    resp = self.llm.chat(messages, response_json=False)
                    content = resp.message.get("content") or ""
                    if isinstance(content, list):
                        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                    turn = parse_json_protocol(content)
                    if resp.message.get("tool_calls"):  # server did tools anyway
                        native = parse_native_response(resp.message)
                        if native.tool_calls:
                            turn = native
                turn.finish_reason = resp.finish_reason
                turn.usage = resp.usage
                return turn
            except LLMError as exc:
                if isinstance(exc, LLMCancelled):
                    raise
                if self.protocol == "native" and self.llm.supports_tools is False and self.config.tool_protocol in ("auto", "native"):
                    self.events.on_status("Model has no native tool calling – switching to JSON protocol.")
                    self._switch_to_json()
                    continue
                if self.vision and self.llm.supports_vision is False:
                    self.events.on_status("Model does not accept images – continuing without vision.")
                    self._disable_vision()
                    continue
                raise
        raise LLMError("Could not negotiate a working protocol with the model.")

    def _switch_to_json(self) -> None:
        self.protocol = "json"
        # convert native assistant/tool messages in history into the JSON protocol
        converted: list[dict[str, Any]] = []
        pending: list[tuple[ToolCall, str]] = []
        for msg in self.history:
            role = msg.get("role")
            if role == "tool":
                pending.append((ToolCall(name=msg.get("name", "tool"), arguments={}, id=msg.get("tool_call_id", "")), msg.get("content", "")))
                continue
            if pending:
                converted.append(tool_results_message_json(pending))
                pending = []
            if role == "assistant" and msg.get("tool_calls"):
                turn = parse_native_response(msg)
                converted.append(assistant_message_json(turn))
            else:
                converted.append(msg)
        if pending:
            converted.append(tool_results_message_json(pending))
        self.history = converted
        self._image_slots = [i for i in range(len(self.history)) if _has_image(self.history[i])]

    def _disable_vision(self) -> None:
        self.vision = False
        self.history = [_strip_images(m) for m in self.history]
        self._image_slots = []


def _has_image(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)


def _strip_images(msg: dict[str, Any]) -> dict[str, Any]:
    content = msg.get("content")
    if not isinstance(content, list):
        return msg
    texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    new = dict(msg)
    new["content"] = "\n".join(t for t in texts if t) + "\n[older screenshot removed to save context]"
    return new

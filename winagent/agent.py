"""The agent loop.

``Agent.run(task)`` sends the conversation to the model, executes the tool
calls it returns, feeds the results (including screenshots) back, and repeats
until the model calls ``task_complete``, returns a structured chat answer, asks the
user something, hits ``max_steps`` or is stopped.

The class is UI-agnostic: progress is reported through :class:`AgentEvents`
callbacks so the same core drives the Qt GUI and the command line.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .backends.base import DesktopBackend, EmergencyStop
from .config import Config
from .history import progress_message, request_summary, result_record
from .llm import LLMCancelled, LLMClient, LLMError, LLMResponseError
from .prompts import TASK_IN_PROGRESS_PROMPT, build_system_prompt
from .protocol import (
    AssistantTurn,
    ProtocolError,
    ToolCall,
    assistant_message_json,
    assistant_message_native,
    image_part,
    looks_structured,
    parse_json_protocol,
    parse_native_response,
    response_text,
    text_part,
    tool_result_message_native,
    tool_results_message_json,
)
from .screenshot import Screenshot
from .screenguard import ScreenGuard
from .skills import Skill, load_skills, skills_section
from .tools import ToolExecutor, ToolResult
from .tools.definitions import openai_tool_schemas

log = logging.getLogger(__name__)

# frame id as embedded by Screenshot.describe() ("frame <12 hex chars>")
_FRAME_ID_RE = re.compile(r"frame ([0-9a-f]{12})")


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
        self._model_frame: Optional[Screenshot] = None  # last FULL image actually included in a model request
        self._image_slots: list[int] = []          # indexes of history messages that carry images
        self._image_frames: list[tuple[int, str]] = []  # (message index, frame_id) of attached images
        self._stall_sig: Any = None               # bucketed action signature of the last round
        self._stall_count = 0                     # consecutive rounds repeating that state with no progress
        self._stall_tool: Optional[str] = None    # single tool name of the last round (any target)
        self._stall_tool_count = 0                # consecutive no-progress rounds using that tool
        self._stall_warned = False                # warning already injected for the current stall
        self.protocol = self._initial_protocol()
        self.vision = bool(config.vision_enabled)
        # Raw execution history is retained for inspection until the next request. Only a factual,
        # protocol-neutral handoff is carried across requests (also when the GUI rebuilds the agent).
        self._followup_history: Optional[list[dict[str, Any]]] = None
        self._past_context: list[dict[str, Any]] = []
        self._request_index = 0
        self._has_progress_note = False
        self._task_records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.running = False
        # Installed skills (user-provided procedure documents) are loaded once and rendered into
        # the system prompt of every request. A broken/missing skills dir must never break a run.
        try:
            self.skills: list[Skill] = load_skills()
        except Exception:
            log.exception("skill loading failed; continuing without skills")
            self.skills = []
        self._skills_section = skills_section(self.skills)

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
            self._image_frames.clear()
            self.executor.last_screenshot = None
            self._model_frame = None
            self._followup_history = None
            self._past_context = []
            self._request_index = 0
            self._has_progress_note = False
            self._task_records = []
            self._reset_stall()

    def _reset_stall(self) -> None:
        self._stall_sig = None
        self._stall_count = 0
        self._stall_tool = None
        self._stall_tool_count = 0
        self._stall_warned = False

    def followup_history(self) -> list[dict[str, Any]]:
        """Copy the conversation handoff, never old live tool calls, when starting/rebuilding a task."""
        return copy.deepcopy(self._followup_history if self._followup_history is not None else self.history)

    def _confirm(self, call: ToolCall, reason: str) -> bool:
        try:
            return bool(self.events.confirm(call, reason))
        except Exception:  # pragma: no cover
            log.exception("confirm callback failed")
            return False

    # ------------------------------------------------------------ messaging
    def _system_message(self, coordinate_space: Optional[str] = None) -> dict[str, Any]:
        try:
            info = self.backend.system_info()
        except Exception as exc:  # pragma: no cover
            info = {"note": f"system info unavailable: {exc}"}
        # Surface the agent's own layout policy to the model alongside the machine facts.
        info["preferred_keyboard_layout"] = self.config.preferred_keyboard_layout
        info["auto_fix_keyboard_layout"] = self.config.auto_fix_keyboard_layout
        # Skills can be installed/edited at any time (e.g. from the settings UI); the section is
        # rebuilt per request so a freshly saved skill takes effect without rebuilding the agent.
        try:
            self.skills = load_skills()
            self._skills_section = skills_section(self.skills)
        except Exception:  # pragma: no cover - a broken skills dir must not break a run
            log.exception("skill loading failed; keeping the previous skills section")
        prompt = build_system_prompt(protocol=self.protocol, vision=self.vision, system_info=info,
                                     language=self.config.response_language, extra=self.config.extra_system_prompt,
                                     coordinate_space=coordinate_space or self.config.coordinate_space,
                                     skills=self._skills_section)
        return {"role": "system", "content": prompt}

    def _messages(self, coordinate_space: Optional[str] = None) -> list[dict[str, Any]]:
        return [self._system_message(coordinate_space), *self.history]

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
        # 2. Evict complete old request pairs first. The CURRENT user's request must never be cut.
        limit = max(10, self.config.max_history_messages)
        while len(self.history) > limit and self._request_index > 0:
            count = min(2, self._request_index)
            del self.history[:count]
            self._request_index -= count
        # 3. Trim whole execution rounds, not individual native calls/results. Keep the latest
        # round and retained image rounds: dropping their image could leave _model_frame pointing
        # at an unseen frame while an older screenshot is still in the request. These small sets
        # (or one large atomic batch) may exceed the soft message limit.
        while len(self.history) > limit:
            start = self._request_index + 1 + int(self._has_progress_note)
            boundaries = [i for i in range(start, len(self.history)) if self.history[i].get("role") == "assistant"]
            removable = next(((left, right) for left, right in zip(boundaries, boundaries[1:], strict=False)
                              if not any(_has_image(msg) for msg in self.history[left:right])), None)
            if removable is None:
                break
            left, right = removable
            del self.history[left:right]
            if not self._has_progress_note:
                self.history.insert(self._request_index + 1, progress_message(self._task_records))
                self._has_progress_note = True
        if self._has_progress_note:
            self.history[self._request_index + 1] = progress_message(self._task_records)
        self._image_slots = [i for i, msg in enumerate(self.history) if _has_image(msg)]
        self._resync_image_frames()

    def _resync_image_frames(self) -> None:
        """Rebuild the (index, frame_id) table of images currently attached in the history.

        The frame id is part of every screenshot description text, so an image whose message
        survived trimming is known by id; once its message is gone, the same frame may be
        attached again if the screen needs to be shown.
        """
        frames: list[tuple[int, str]] = []
        for i, msg in enumerate(self.history):
            if _has_image(msg):
                match = _FRAME_ID_RE.search(_message_text(msg))
                if match:
                    frames.append((i, match.group(1)))
        self._image_frames = frames

    def _attached_frame_ids(self) -> set[str]:
        return {fid for _, fid in self._image_frames}

    def _user_message(self, text: str, shot: Optional[Screenshot] = None) -> tuple[dict[str, Any], bool]:
        """Build a user message, skipping a duplicate image already present in the context."""
        if shot is not None and self.vision:
            if not shot.is_region and shot.frame_id in self._attached_frame_ids():
                note = (f"\n[No new image attached: the screen is unchanged and full frame {shot.frame_id} "
                        "is already in the context, so the identical image was not duplicated.]")
                return {"role": "user", "content": text_part(text + note)}, False
            image = image_part(shot.data_url())
            if not shot.is_region:
                self._model_frame = shot
            return {"role": "user", "content": [text_part(text), image]}, True
        return {"role": "user", "content": text}, False

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
        self._past_context = self.followup_history()
        self.history = copy.deepcopy(self._past_context)
        self._request_index = len(self.history)
        self._has_progress_note = False
        self._task_records = []
        self._reset_stall()
        self._verified_this_task = False   # completion verification is offered ONCE per task
        self._image_slots = [i for i, msg in enumerate(self.history) if _has_image(msg)]
        self._resync_image_frames()
        # No image from a previous request is a current coordinate frame. A failed/disabled
        # initial capture must not silently reuse the old desktop or its completion state.
        self._model_frame = None
        self.executor.last_screenshot = None
        try:
            shot = None
            capture_error = ""
            if initial_screenshot and self.vision:
                try:
                    self.events.on_status("Taking screenshot…")
                    shot = self.executor.take_screenshot()
                    self.events.on_screenshot(shot)
                except Exception as exc:
                    capture_error = str(exc)
                    log.warning("initial screenshot failed: %s", exc)
            text = f"[Current user request]\n{task}"
            if shot is not None:
                text += f"\n\n[Current screen attached. {shot.describe()}]"
            elif capture_error:
                text += ("\n\n[No current screenshot is available. Do not use coordinates from a previous request. "
                         f"Request a fresh screenshot or use non-pointer tools. Capture error: {capture_error[:500]}]")
            # Check (and, when enabled, correct) the input language BEFORE any action of this task.
            layout_note = self._initial_layout_note()
            if layout_note:
                text += f"\n{layout_note}"
            user_msg, has_image = self._user_message(text, shot)
            self._append(user_msg, has_image=has_image)

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

                # Snapshot BEFORE HTTP/confirmation callbacks: no later capture may change this response's coordinates.
                frame = self._model_frame if self.vision else self.executor.last_screenshot
                coordinate_space = frame.coordinate_space if frame else self.config.coordinate_space
                turn = self._call_model(coordinate_space=coordinate_space, task_in_progress=n_calls > 0)
                if turn.thought:
                    self.events.on_thought(turn.thought)

                if not turn.tool_calls:
                    # Only an explicit message envelope (or provider refusal) may finish a chat turn.
                    reply = turn.text  # _call_model has already rejected empty/malformed output
                    # Keep the envelope in history so native chat examples do not teach bare-text completions.
                    self._append(assistant_message_json(turn))
                    self.events.on_assistant_text(reply)
                    outcome = RunOutcome("answered", reply)
                    break

                if turn.text:
                    self.events.on_assistant_text(turn.text)
                self._append(assistant_message_native(turn) if self.protocol == "native" else assistant_message_json(turn))

                results: list[ToolResult] = []
                finished: Optional[ToolResult] = None
                waiting: Optional[ToolResult] = None
                refresh_required = False
                for call in turn.tool_calls:
                    if self.stop_event.is_set():
                        results.append(ToolResult(call, False, error="Stopped by user."))
                        continue
                    if refresh_required:
                        results.append(ToolResult(call, False, error="Skipped: display geometry changed. "
                                                  "This action was NOT executed. Replan from the new full screenshot."))
                        continue
                    if finished is not None or waiting is not None:
                        results.append(ToolResult(call, False, error="Skipped: a previous tool call ended the turn."))
                        continue
                    n_calls += 1
                    self.events.on_tool_start(call)
                    self.events.on_status(f"Running {call.name}…")
                    try:
                        result = self.executor.execute(call, coordinate_frame=frame, coordinate_space=coordinate_space)
                    except EmergencyStop as exc:
                        self.stop()
                        result = ToolResult(call, False, error=str(exc) or "Emergency stop.")
                    if result.needs_new_screenshot:
                        refresh_required = True
                        self.events.on_status("Display changed – refreshing screenshot before any more actions.")
                        if not self.stop_event.is_set():
                            try:
                                result.screenshot = self.executor.take_screenshot(all_screens=frame.all_screens if frame else False)
                            except Exception as exc:
                                result.data["screenshot_error"] = str(exc)
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
                self._update_stall(turn, results)

                if finished is not None:
                    summary = finished.data.get("summary", "Done.")
                    success = bool(finished.data.get("success", True))
                    # Self-check: before accepting a SUCCESSFUL completion of real tool work, the
                    # model gets ONE round to verify the result against a fresh screenshot (it may
                    # confirm with task_complete again or continue working). An honest
                    # success=false report is never re-verified – it is the truthful outcome.
                    if self.config.verify_on_completion and success and n_calls > 0 and not self._verified_this_task:
                        self._verified_this_task = True
                        self._append_verification_prompt(summary)
                        continue
                    self.events.on_assistant_text(summary)
                    outcome = RunOutcome("completed" if success else "answered", summary)
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
        # Close the assistant turn after terminal tool results and preserve a compact, factual
        # handoff for a follow-up. Do not execute anything, call the model, or pretend errors succeeded.
        closing = request_summary(outcome.message, outcome.status, self._task_records)
        if n_calls or outcome.status not in ("completed", "answered"):
            self._append(closing)
        self._followup_history = [*self._past_context, {"role": "user", "content": task}, closing]
        keep = max(2, (max(10, self.config.max_history_messages) - 2) // 2 * 2)
        self._followup_history = self._followup_history[-keep:]
        log.info("Request ended: status=%s actions=%d; retained %d prior request summaries for follow-up.",
                 outcome.status, n_calls, len(self._followup_history) // 2)
        self.events.on_done(outcome)
        return outcome

    # ------------------------------------------------------------ internals
    def _initial_layout_note(self) -> str:
        """Verify the input language before doing anything; correct it when the setting is on.

        The check runs for EVERY task, before the first action. When the layout is already the
        preferred one the check is silent (no noise in the context); when it is wrong, the layout
        is fixed first (if enabled) and a short [Input language check ...] line is appended to the
        task's first message so the model knows what happened.
        """
        preferred = (self.config.preferred_keyboard_layout or "").strip()
        if not preferred or preferred.lower() in ("any", "auto", "off"):
            return ""
        try:
            active = self.backend.active_keyboard_layout()
        except Exception:  # pragma: no cover - backend without layout support
            return ""
        if not active or active == "unknown" or active.lower() == preferred.lower():
            return ""
        note = self.executor._ensure_keyboard_layout() or "mismatch detected"
        return f"[Input language check before any action: {note}]"

    def _append_results(self, results: list[ToolResult]) -> None:
        """Add tool results (and the newest screenshot) to the history."""
        latest_shot: Optional[Screenshot] = None
        for r in results:
            record = result_record(r)
            if record is not None:
                self._task_records.append(record)
            if r.screenshot is not None:
                latest_shot = r.screenshot
        if latest_shot is not None and self.vision and not latest_shot.is_region:
            self._model_frame = latest_shot
        if self.protocol == "native":
            # Publish the whole matching result batch before trimming; partial batches can leave
            # orphan results or assistant calls without replies at the next HTTP request.
            self.history.extend(tool_result_message_native(r.call, r.to_text()) for r in results)
            if latest_shot is not None and self.vision:
                shot_msg, has_image = self._user_message(f"[Screenshot after the actions above. {latest_shot.describe()}]", latest_shot)
                self.history.append(shot_msg)
                if has_image:
                    self._image_slots.append(len(self.history) - 1)
            self._trim()
        else:
            msg = tool_results_message_json([(r.call, r.to_text()) for r in results])
            if latest_shot is not None and self.vision:
                base_text = msg["content"] + f"\n[Screenshot after the actions above. {latest_shot.describe()}]"
                if not latest_shot.is_region and latest_shot.frame_id in self._attached_frame_ids():
                    base_text += (f" [No new image: the screen is unchanged and full frame {latest_shot.frame_id} "
                                  "is already in the context, so the identical image was not duplicated.]")
                    msg["content"] = text_part(base_text)
                    self._append(msg, has_image=False)
                else:
                    msg["content"] = [text_part(base_text), image_part(latest_shot.data_url())]
                    self._append(msg, has_image=True)
            else:
                self._append(msg)

    def _update_stall(self, turn: AssistantTurn, results: list[ToolResult]) -> None:
        """Detect repeated actions with no visible effect and tell the model to change course.

        A round "stalls" when it repeats the previous round's action signature while the screen is
        unchanged (per duplicate detection) or every call errored. Pointer coordinates are bucketed
        into 16-unit cells so a few pixels of jitter still count as the SAME stuck action (the model
        often "retries" a dead click at slightly different pixels), and a second counter watches the
        same single tool used with ANY target. After a few consecutive stalls we inject a one-time
        warning: change approach, or skip the step when it is genuinely impossible.
        """
        calls = turn.tool_calls
        if not calls:
            self._reset_stall()
            return
        sig = _stall_signature(calls)
        unchanged = any(r.data.get("screen_unchanged") for r in results)
        failed = bool(results) and all(not r.ok for r in results) and any(r.error for r in results)
        no_progress = bool(unchanged or failed)
        if self._stall_sig == sig and no_progress:
            self._stall_count += 1
        else:
            self._stall_sig, self._stall_count = sig, 1
        if no_progress and len(calls) == 1:
            if self._stall_tool == calls[0].name:
                self._stall_tool_count += 1
            else:
                self._stall_tool, self._stall_tool_count = calls[0].name, 1
        else:
            self._stall_tool, self._stall_tool_count = None, 0
        if not no_progress:
            self._stall_warned = False
        if (self._stall_count >= 3 or self._stall_tool_count >= 4) and not self._stall_warned:
            self._stall_warned = True
            names = ", ".join(dict.fromkeys(c.name for c in calls))
            count = max(self._stall_count, self._stall_tool_count)
            self.history.append({"role": "user", "content": (
                f"[Stall detected: {names} has now been attempted {count} times in a row with NO visible "
                "change on the screen (or failing every time). Repeating it will not work – do NOT call "
                "the same action again. If you are clicking a control that might be disabled/greyed "
                "(e.g. a disabled combo box), it will never respond: verify with get_window_controls "
                "('enabled') or a zoomed screenshot, then use a different route (the `menu` tool, a "
                "keyboard shortcut, run_command, open_app) or SKIP this sub-step and continue with the "
                "rest of the task. Finish with task_complete and an honest summary (success=false if "
                "the goal was not reached), or use ask_user if the user must intervene.]")})
            self.events.on_status("Repeated action without visible change – asking the model to change approach.")

    def _append_verification_prompt(self, summary: str) -> None:
        """Ask the model to verify a claimed completion before it is accepted (once per task).

        A fresh full screenshot is attached. The model either confirms with task_complete (which is
        then accepted without another verification round) or continues working; the step budget
        bounds the whole thing, so verification can never loop.
        """
        self.events.on_status("Verifying the result before accepting completion…")
        shot: Optional[Screenshot] = None
        if self.vision:
            try:
                shot = self.executor.take_screenshot()
                self.events.on_screenshot(shot)
            except Exception as exc:  # verification must not kill an otherwise-finished task
                log.warning("verification screenshot failed: %s", exc)
        text = (
            "[Completion check – the agent just reported this task as done: "
            f"“{summary[:400]}”. Before accepting it, VERIFY that the user's original request is "
            "actually satisfied: compare the CURRENT screen (new screenshot attached) and the tool "
            "results above against the request. "
            "If anything is missing or wrong, do NOT accept it – continue with the next action(s) to "
            "finish the job properly. "
            "If it is genuinely satisfied, confirm by calling task_complete again with the same "
            "truthful summary. Do not repeat actions that already succeeded.]"
        )
        if shot is not None:
            text += f"\n\n[Verification screenshot. {shot.describe()}]"
        msg, has_image = self._user_message(text, shot)
        self._append(msg, has_image=has_image)

    def _call_model(self, *, coordinate_space: Optional[str] = None, task_in_progress: bool = False) -> AssistantTurn:
        """Negotiate capabilities and recover bad output BEFORE committing any tool calls to history.

        Transport retries live in LLMClient. Response retries have a separate, finite budget; the
        repair prompt is request-local so invalid native calls never leave orphan tool messages.
        Previously successful rounds are preserved and are never re-executed by this retry loop.
        """
        coordinate_space = coordinate_space or self.config.coordinate_space
        retries = 0
        repair_reason = ""
        last_content = ""          # raw text of the most recently rejected response
        last_turn: Optional[AssistantTurn] = None  # its parsed form, when parsing itself succeeded
        while True:
            if self.stop_event.is_set():
                raise LLMCancelled("stopped")
            messages = self._messages(coordinate_space)
            if task_in_progress:
                messages[0]["content"] += "\n" + TASK_IN_PROGRESS_PROMPT
            if repair_reason:
                # `retries` is 1 on the first repair, so attempt==1 keeps the original message and
                # escalation starts on the SECOND retry (the model needs a different nudge, not a repeat).
                messages.append({"role": "user", "content": self._response_repair_prompt(
                    repair_reason, task_in_progress=task_in_progress, attempt=retries)})
            # A low-temperature model that failed once will usually emit the SAME invalid output when
            # asked again verbatim. Each retry therefore also raises the temperature slightly so the
            # loop can escape the identical-output attractor (capped at 1.5).
            retry_temperature = None if retries == 0 else min(self.config.temperature + 0.25 * retries, 1.5)
            try:
                if self.protocol == "native":
                    resp = self.llm.chat(messages, tools=openai_tool_schemas(coordinate_space), temperature=retry_temperature)
                else:
                    resp = self.llm.chat(messages, temperature=retry_temperature)
                if self.stop_event.is_set():
                    raise LLMCancelled("stopped")
                log.info("Model response: requested=%s reported=%s coordinate_space=%s finish=%s",
                         self.config.model, resp.model, coordinate_space, resp.finish_reason)
                if not isinstance(resp.message, dict):
                    raise LLMResponseError("Assistant message must be an object.")
                if resp.finish_reason == "content_filter":
                    raise LLMError("The model response was blocked by the provider's content filter.")
                if isinstance(resp.message.get("refusal"), str) and resp.message["refusal"].strip():
                    return parse_native_response(resp.message)  # a genuine refusal is not a format fault
                if resp.finish_reason in ("length", "max_tokens", "max_output_tokens"):
                    raise LLMResponseError("The response was truncated by the output-token limit. Reply more concisely.")
                if self.protocol == "native" or resp.message.get("tool_calls") or resp.message.get("function_call") is not None:
                    turn = parse_native_response(resp.message, allow_plain_text=False)
                else:
                    content = response_text(resp.message.get("content"))
                    last_content = content if isinstance(content, str) else ""
                    turn = parse_json_protocol(last_content)
                if turn.parse_error:
                    last_turn = None
                    raise LLMResponseError(turn.parse_error)
                last_turn = turn
                if task_in_progress and not turn.tool_calls:
                    raise LLMResponseError("Tool work is in progress: a message alone cannot end the task. "
                                           "Continue with tools, ask_user, or explicitly call task_complete.")
                if resp.finish_reason in ("tool_calls", "function_call") and not turn.tool_calls:
                    raise LLMResponseError("The response announces tool calls but contains none.")
                if not turn.tool_calls and not turn.text.strip():
                    raise LLMResponseError("The response has no actions or answer.")
                turn.finish_reason = resp.finish_reason
                turn.usage = resp.usage
                return turn
            except (LLMResponseError, ProtocolError) as exc:
                if self.stop_event.is_set():
                    raise LLMCancelled("stopped") from exc
                if retries >= self.config.max_response_retries:
                    # A JSON-protocol model that keeps answering in prose (or with a {"message": ...}
                    # envelope) while work is in progress will never learn the action format. The
                    # expectation for a stuck/impossible step is to be SKIPPED, so end the request
                    # with the model's own honest words instead of a protocol crash.
                    graceful = self._graceful_finish_turn(task_in_progress, last_turn, last_content)
                    if graceful is not None:
                        log.warning("Model kept returning non-action output after %d attempts; finishing the "
                                    "task with its text answer: %.200s", retries + 1, (last_content or graceful.text or ""))
                        self.events.on_status("The model answered with text instead of actions – finishing with its answer.")
                        graceful.finish_reason = graceful.finish_reason or "stop"
                        return graceful
                    raise LLMError(f"Model response is still invalid after {retries + 1} attempts: {exc} "
                                   "No actions from the rejected response were executed. "
                                   "Check the model/tool protocol or increase Max tokens for truncated output.") from exc
                retries += 1
                repair_reason = str(exc)
                log.warning("Invalid model output; requesting correction (%d/%d).", retries, self.config.max_response_retries)
                self.events.on_status(f"Invalid model response – requesting correction ({retries}/{self.config.max_response_retries}).")
            except LLMError as exc:
                if isinstance(exc, LLMCancelled):
                    raise
                # These are one-way state changes, separate from the response-repair budget.
                if self.protocol == "native" and self.llm.supports_tools is False and self.config.tool_protocol in ("auto", "native"):
                    self.events.on_status("Model has no native tool calling – switching to JSON protocol.")
                    self._switch_to_json()
                    continue
                if self.vision and self.llm.supports_vision is False:
                    self.events.on_status("Model does not accept images – continuing without vision.")
                    self._disable_vision()
                    continue
                raise

    def _response_repair_prompt(self, reason: str, *, task_in_progress: bool = False, attempt: int = 1) -> str:
        protocol = (
            'Return native tool_calls with complete JSON-object arguments for actions. '
            if self.protocol == "native" else
            'For actions return ONE complete JSON object, e.g. {"actions":[{"tool":"screenshot","args":{}}]} '
            '(shape example only; choose actual tools and required arguments). '
        )
        terminal = (
            "Tool work has started. Do not end with plain text or a message object: continue with the next tool, "
            "use ask_user, or call task_complete with a non-empty, truthful summary. "
            "Use success=false if unable to complete or declining, rather than inventing success. "
            + ("If the remaining work is impossible (e.g. a disabled control), finish NOW with exactly one JSON "
               'object, nothing else: {"actions":[{"tool":"task_complete","args":'
               '{"summary":"<honest summary in the user language>","success":false}}]} '
               if self.protocol == "json" else "")
            if task_in_progress else
            'For a complete answer needing no tools, return ONE JSON content object {"message":"your full answer"}, '
            'not bare text. '
        )
        # Escalation: repeating the same repair text to a deterministic model reproduces the same bad
        # output. Later retries say more, and say it differently.
        escalation = ""
        if attempt >= 2:
            escalation = ("Your previous attempt failed for the same reason. Keep the response MINIMAL: output the "
                          "valid response and nothing else – no screen descriptions, no quotes of earlier text, "
                          "no reasoning, no apologies. ")
        if attempt >= 3:
            if self.protocol == "json":
                escalation += ('A valid actions response has EXACTLY this shape (example content): '
                               '{"actions":[{"tool":"screenshot","args":{}}]}. '
                               "Send that shape with your actual next action, or a task_complete action when done. ")
            else:
                escalation += ("Use ONLY tool_calls (empty text content). If you cannot continue with a tool, "
                               "call the task_complete tool with a non-empty summary. ")
        return (f"Your previous response was invalid: {reason[:500]}\n"
                "It was discarded; NO actions from that response were executed. Send a COMPLETE replacement, "
                "not a continuation. Do not simply quote or wrap the broken fragment as an answer/summary. "
                "Reconsider the CURRENT user request, not an earlier completed task, using the screenshot and tool results already provided; "
                "do NOT repeat earlier successful actions. Do not echo screenshot metadata or return only reasoning. "
                "Preserve any refusal or inability honestly. Keep the response concise and in the original user's language. "
                + protocol + terminal + escalation)

    def _graceful_finish_turn(self, task_in_progress: bool, last_turn: Optional[AssistantTurn],
                              last_content: str) -> Optional[AssistantTurn]:
        """Convert an exhausted JSON-protocol retry loop into the model's own final words.

        Only when tool work is in progress and the model's last response was GENUINE TEXT (plain
        prose, or an explicit {"message": ...} envelope with no actions) – i.e. the model chose to
        talk instead of act, which is usually the honest "this step is impossible, skipping it"
        answer after a stall. Structured garbage (truncated JSON, broken arguments) is never
        accepted here: that stays an error, because it contains no trustworthy final words.
        """
        if not task_in_progress or self.protocol != "json":
            return None
        if last_turn is not None and not last_turn.tool_calls and not last_turn.parse_error and last_turn.text.strip():
            return last_turn  # the model explicitly wrapped its answer as {"message": "..."}
        content = (last_content or "").strip()
        if not content or looks_structured(content):
            return None
        return AssistantTurn(text=content, raw_content=last_content)

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
        self._resync_image_frames()

    def _disable_vision(self) -> None:
        self.vision = False
        self.history = [_strip_images(m) for m in self.history]
        self._image_slots = []
        self._image_frames = []


# Pointer argument keys that _stall_signature buckets (16-unit cells): the model often "retries" a
# dead click a few pixels off; that must still count as the SAME stuck action, not a new one.
_STALL_POINTERS = {
    "mouse_move": ("x", "y"),
    "click": ("x", "y"),
    "double_click": ("x", "y"),
    "right_click": ("x", "y"),
    "drag": ("x1", "y1", "x2", "y2"),
    "scroll": ("x", "y"),
}
_STALL_BUCKET = 16


def _stall_bucket(value: Any) -> Optional[int]:
    try:
        return int(round(float(value))) // _STALL_BUCKET
    except (TypeError, ValueError):
        return None


def _stall_signature(calls: list[ToolCall]) -> tuple:
    """Round's action identity with jitter-proof pointer coordinates."""
    sigs = []
    for c in calls:
        args = c.arguments or {}
        keys = _STALL_POINTERS.get(c.name)
        if keys is not None:
            buckets = tuple(_stall_bucket(args.get(k)) for k in keys)
            extra: dict[str, Any] = {}
            if c.name == "scroll":
                try:
                    amount = int(args.get("amount") or 0)
                except (TypeError, ValueError):
                    amount = 0
                extra["amount_sign"] = (amount > 0) - (amount < 0)
                extra["direction"] = str(args.get("direction") or "vertical").lower()
            elif c.name in ("click", "double_click", "right_click"):
                extra["button"] = str(args.get("button") or "left").lower()
                extra["clicks"] = args.get("clicks", 1)
            sigs.append((c.name, buckets, json.dumps(extra, sort_keys=True)))
        else:
            sigs.append((c.name, (), json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)))
    return tuple(sorted(sigs))


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


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

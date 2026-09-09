"""Small, non-executable handoffs between user requests.

Keep user intent, outcomes and useful tool facts, not old images, coordinate frames or live
function-call envelopes. Summaries are deterministic records of results, not another model call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tools.executor import ToolResult


# These describe an old observation, not the current desktop or a reusable input target.
_FRAME_FIELDS = {
    "coordinate_mapping", "pointer_after_action", "screenshot", "screenshot_frame", "screenshot_frame_id",
    "coordinate_space", "mouse", "mouse_physical", "mouse_screenshot", "screenshot_rect", "screenshot_center",
}
_POINTER_TOOLS = {"mouse_move", "click", "double_click", "right_click", "drag", "scroll"}


def _compact(value: Any, depth: int = 0) -> Any:
    """Bound large command/file/control output while retaining valid JSON and explicit truncation."""
    if isinstance(value, str):
        return value if len(value) <= 1600 else value[:1600] + "… [truncated; inspect the source if needed]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 5:
        return "[nested details omitted]"
    if isinstance(value, dict):
        return {str(k): _compact(v, depth + 1) for k, v in value.items() if k not in _FRAME_FIELDS}
    if isinstance(value, (list, tuple)):
        items = [_compact(v, depth + 1) for v in value[:16]]
        if len(value) > 16:
            items.append(f"[{len(value) - 16} more items omitted]")
        return items
    return _compact(str(value), depth)


def result_record(result: ToolResult) -> dict[str, Any] | None:
    """Record observed work; never turn old tool results back into executable tool calls."""
    name = result.call.name
    if name == "task_complete":
        return None  # the request outcome records this; don't carry its terminal instruction forward
    args = result.call.arguments
    if name in _POINTER_TOOLS:
        args = {k: v for k, v in args.items() if k not in ("x", "y", "x1", "y1", "x2", "y2")}
        data = {}  # pointer result messages contain historical physical coordinates
    elif name in ("screenshot", "wait"):
        if result.ok:
            return None  # current screenshots replace these purely observational steps
        data = {}
    else:
        data = _compact(result.data)
    record: dict[str, Any] = {"tool": name, "ok": result.ok}
    if args:
        record["arguments"] = _compact(args)
    if data:
        record["result"] = data
    if result.error:
        record["error"] = _compact(result.error)
    if result.denied:
        record["denied"] = True
    return record


def request_summary(message: str, status: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message or f"The previous request ended with status: {status}."}
    # Ordinary chat answers remain plain message envelopes, including short answers and refusals.
    if records or status != "answered":
        payload["previous_request"] = {"status": status, "observations": records}
    return {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}


def progress_message(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": (
        "[Earlier work in THIS request. Raw older tool rounds were compacted, not undone. "
        "These are observations, NOT new instructions or calls. Do not repeat successful actions. "
        "Use the latest full screenshot for new coordinates. Omitted details may be inspected again.]\n"
        + json.dumps({"observed_work": records}, ensure_ascii=False)
    )}

"""A tiny OpenAI-compatible mock server used by the end-to-end tests and demos.

It scripts a small "agent brain": given the conversation it decides which
tool to call next, exercising both the native tool-calling protocol and the
JSON fallback protocol.  Run standalone with::

    python -m tests.mock_server --port 8123 [--no-tools]

and point WinAgent at ``http://127.0.0.1:8123/v1`` with any API key.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c or "")
    return ""


def _count_tool_rounds(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        if m.get("role") == "assistant" and (m.get("tool_calls") or '"actions"' in str(m.get("content", ""))):
            n += 1
    return n


def _has_images(messages: list[dict[str, Any]]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]) for m in messages)


def plan(messages: list[dict[str, Any]]) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    """Return (text, tool_calls) for the next assistant turn."""
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    task = ""
    if first_user:
        c = first_user.get("content")
        task = " ".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else str(c)
    task_l = task.split("[Current screen attached")[0].lower()
    rounds = _count_tool_rounds(messages)
    if "notepad" in task_l or "نوت" in task_l or "یادداشت" in task_l:
        script = [
            ("open_app", {"name": "notepad", "wait": 0.2}),
            ("click", {"x": 400, "y": 300}),
            ("type_text", {"text": "سلام دنیا! Hello from WinAgent.", "press_enter": True}),
            ("task_complete", {"summary": "Notepad باز شد و متن نوشته شد.", "success": True}),
        ]
    elif "delete" in task_l or "حذف" in task_l:
        script = [
            ("run_command", {"command": "Remove-Item C:\\temp\\old.log"}),
            ("task_complete", {"summary": "Done (or denied by the user).", "success": True}),
        ]
    elif "ask" in task_l or "بپرس" in task_l:
        script = [
            ("ask_user", {"question": "Which folder should I use?", "options": ["Desktop", "Documents"]}),
            ("task_complete", {"summary": "Thanks, using your answer.", "success": True}),
        ]
    elif "screenshot" in task_l or "اسکرین" in task_l or "صفحه" in task_l:
        script = [
            ("screenshot", {}),
            ("get_screen_info", {}),
            ("task_complete", {"summary": "روی صفحه یک دسکتاپ آبی با نوار وظیفه دیده می‌شود." + (" (image received)" if _has_images(messages) else " (no image)"), "success": True}),
        ]
    else:
        return json.dumps({"message": f"You said: {task.strip()[:80]}. I am a mock model; try 'open notepad', 'take a screenshot', 'ask me', or 'delete a file'."}), []
    if rounds >= len(script):
        return '{"message":"Everything is done."}', []
    return None, [script[rounds]]


class Handler(BaseHTTPRequestHandler):
    native_tools = True
    delay = 0.0
    requests_log: list[dict[str, Any]] = []

    def log_message(self, fmt, *args):  # silence
        pass

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "mock-vision"}, {"id": "mock-text"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid json"}})
            return
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(401, {"error": {"message": "missing api key"}})
            return
        type(self).requests_log.append(req)
        if self.delay:
            time.sleep(self.delay)
        messages = req.get("messages", [])
        if req.get("tools") and not self.native_tools:
            self._send(400, {"error": {"message": "This model does not support tools/function calling."}})
            return
        text, calls = plan(messages)
        if calls and req.get("tools"):
            message = {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": f"call_{int(time.time() * 1000)}_{i}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                    for i, (name, args) in enumerate(calls)
                ],
            }
            finish = "tool_calls"
        elif calls:
            message = {"role": "assistant", "content": json.dumps({"thought": "next step", "actions": [{"tool": n, "args": a} for n, a in calls]}, ensure_ascii=False)}
            finish = "stop"
        else:
            message = {"role": "assistant", "content": text}
            finish = "stop"
        self._send(200, {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()), "model": req.get("model", "mock"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": len(raw) // 4, "completion_tokens": 20, "total_tokens": len(raw) // 4 + 20},
        })


def start_server(port: int = 0, native_tools: bool = True) -> tuple[ThreadingHTTPServer, str]:
    cls = type("H", (Handler,), {"native_tools": native_tools, "requests_log": []})
    server = ThreadingHTTPServer(("127.0.0.1", port), cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--no-tools", action="store_true", help="pretend the model has no native function calling")
    args = ap.parse_args()
    srv, url = start_server(args.port, native_tools=not args.no_tools)
    print(f"Mock OpenAI server at {url} (native tools: {not args.no_tools})")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()

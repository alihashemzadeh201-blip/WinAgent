"""Minimal OpenAI-compatible chat-completions client.

Works with OpenAI, Azure-style proxies, OpenRouter, Groq, Together, DeepSeek,
Ollama (``http://localhost:11434/v1``), LM Studio, vLLM, llama.cpp server,
LiteLLM, etc.  Only ``requests`` is used, so there is no SDK version drift.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .config import Config

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class LLMCancelled(LLMError):
    pass


@dataclass
class ChatResponse:
    message: dict[str, Any]
    finish_reason: str
    usage: dict[str, Any]
    model: str
    raw: dict[str, Any]
    latency: float


class LLMClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self._cancel = threading.Event()
        # capabilities discovered at run time
        self.supports_tools: Optional[bool] = None
        self.supports_vision: Optional[bool] = None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    # ----------------------------------------------------------------- utils
    def cancel(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "WinAgent/0.1 (+https://github.com/alihashemzadeh201-blip/WinAgent)"}
        key = (self.config.api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["api-key"] = key  # Azure OpenAI style
        headers.update(self.config.extra_headers or {})
        return headers

    def _timeout(self) -> tuple[float, float]:
        return (15.0, float(self.config.request_timeout))

    # ------------------------------------------------------------------ calls
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str | dict[str, Any]] = None,
        response_json: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        mt = self.config.max_tokens if max_tokens is None else max_tokens
        if mt and mt > 0:
            payload["max_tokens"] = mt
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if response_json:
            payload["response_format"] = {"type": "json_object"}

        url = self.config.chat_completions_url()
        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self.config.max_retries:
            if self._cancel.is_set():
                raise LLMCancelled("Request cancelled by user.")
            attempt += 1
            start = time.time()
            try:
                resp = self.session.post(url, headers=self._headers(), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         timeout=self._timeout())
            except requests.Timeout as exc:
                last_exc = LLMError(f"Request timed out after {self.config.request_timeout:.0f}s.")
                log.warning("LLM timeout (attempt %d): %s", attempt, exc)
                continue
            except requests.ConnectionError as exc:
                last_exc = LLMError(f"Cannot connect to {url}: {exc}")
                log.warning("LLM connection error (attempt %d): %s", attempt, exc)
                time.sleep(min(2 ** attempt, 8))
                continue
            latency = time.time() - start

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise LLMError(f"Non-JSON response from server: {resp.text[:300]}", resp.status_code, resp.text) from exc
                return self._parse(data, latency)

            body = resp.text[:2000]
            err_msg = _extract_error(body) or body
            # Feature negotiation: drop unsupported parameters and retry immediately.
            if resp.status_code in (400, 404, 422):
                lowered = err_msg.lower()
                if "tools" in payload and any(k in lowered for k in ("tool", "function")):
                    log.info("Server rejected tools (%s); falling back to JSON protocol.", err_msg[:120])
                    self.supports_tools = False
                    raise LLMError(f"Server does not support tool calling: {err_msg}", resp.status_code, body)
                if "response_format" in payload and "response_format" in lowered:
                    payload.pop("response_format", None)
                    attempt -= 1
                    continue
                if _mentions_image(lowered):
                    self.supports_vision = False
                    raise LLMError(f"Model does not accept images: {err_msg}", resp.status_code, body)
                if "max_tokens" in lowered and "max_completion_tokens" in lowered and "max_tokens" in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    attempt -= 1
                    continue
                if "temperature" in lowered and "temperature" in payload:
                    payload.pop("temperature")
                    attempt -= 1
                    continue
            if resp.status_code in (401, 403):
                raise LLMError(f"Authentication failed ({resp.status_code}). Check the API key. {err_msg}", resp.status_code, body)
            if resp.status_code == 404:
                raise LLMError(f"Endpoint or model not found (404) at {url}: {err_msg}", resp.status_code, body)
            if resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504):
                wait = _retry_after(resp) or min(2 ** attempt, 20)
                last_exc = LLMError(f"Server error {resp.status_code}: {err_msg}", resp.status_code, body)
                log.warning("LLM %s (attempt %d), retrying in %.1fs", resp.status_code, attempt, wait)
                for _ in range(int(wait * 10)):
                    if self._cancel.is_set():
                        raise LLMCancelled("Request cancelled by user.")
                    time.sleep(0.1)
                continue
            raise LLMError(f"HTTP {resp.status_code}: {err_msg}", resp.status_code, body)
        raise last_exc or LLMError("LLM request failed.")

    def _parse(self, data: dict[str, Any], latency: float) -> ChatResponse:
        choices = data.get("choices") or []
        if not choices:
            if "error" in data:
                raise LLMError(f"Server error: {_extract_error(json.dumps(data))}", body=json.dumps(data)[:2000])
            raise LLMError(f"Response has no choices: {json.dumps(data)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}
        if not message and "text" in choice:  # legacy completions style
            message = {"role": "assistant", "content": choice["text"]}
        usage = data.get("usage") or {}
        self.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.total_completion_tokens += int(usage.get("completion_tokens") or 0)
        if message.get("tool_calls"):
            self.supports_tools = True
        return ChatResponse(message=message, finish_reason=str(choice.get("finish_reason") or ""), usage=usage,
                            model=str(data.get("model") or self.config.model), raw=data, latency=latency)

    # ------------------------------------------------------------- discovery
    def list_models(self) -> list[str]:
        resp = self.session.get(self.config.models_url(), headers=self._headers(), timeout=(10, 30))
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {_extract_error(resp.text) or resp.text[:300]}", resp.status_code, resp.text)
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        names: list[str] = []
        for it in items or []:
            if isinstance(it, dict) and it.get("id"):
                names.append(str(it["id"]))
            elif isinstance(it, str):
                names.append(it)
        return sorted(set(names))

    def test_connection(self) -> str:
        """Send a tiny request and return a human readable status line."""
        start = time.time()
        resp = self.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=16, temperature=0.0)
        content = resp.message.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return f"OK – model '{resp.model}' answered in {time.time() - start:.1f}s: {content.strip()[:60]!r}"


def _extract_error(body: str) -> str:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict):
        err = data.get("error") or data.get("detail") or data.get("message")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return str(err)
    return ""


def _mentions_image(text: str) -> bool:
    return any(k in text for k in ("image_url", "image input", "does not support image", "vision", "multimodal", "invalid content type"))


def _retry_after(resp: requests.Response) -> Optional[float]:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return min(float(val), 60.0)
    except ValueError:
        return None

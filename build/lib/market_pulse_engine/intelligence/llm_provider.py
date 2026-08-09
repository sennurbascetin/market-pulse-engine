"""Pluggable LLM backend for the intelligence layer.

The project brief specifies ``gpt-5-mini``. In a standalone Python service that
requires an OpenAI API key, so the engine resolves a provider at runtime rather
than assuming one:

===============  ==========================================================
``openai``       ``gpt-5-mini`` via the OpenAI SDK (needs ``OPENAI_API_KEY``)
``heuristic``    Offline analyst — no network, no key, no cost
===============  ==========================================================

``MPE_LLM_PROVIDER=auto`` (the default) picks the first available of the above.
The offline analyst is not a stub: it produces the same structured sentiment
records and the same narrative shape, so the pipeline, the dashboard and the
Platinum tables behave identically with or without credentials. Adding a key
upgrades the prose without changing a line of downstream code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ..config import CONFIG
from ..logging_setup import get_logger

log = get_logger("intelligence.provider")


class LLMError(RuntimeError):
    """Raised when a remote provider call fails; callers fall back offline."""


@dataclass
class LLMResponse:
    """A single completion plus the accounting the run log records."""

    text: str
    tokens_used: int = 0
    model: str = ""
    provider: str = ""

    def as_json(self) -> dict[str, Any]:
        """Parse the completion as a JSON object, tolerating fenced output."""
        payload = self.text.strip()
        if payload.startswith("```"):
            payload = payload.split("```")[1]
            payload = payload.removeprefix("json").strip()
        # Some models pad JSON with a sentence of preamble; recover the object.
        if not payload.startswith("{"):
            start, end = payload.find("{"), payload.rfind("}")
            if start == -1 or end <= start:
                raise LLMError(f"response was not JSON: {self.text[:200]}")
            payload = payload[start : end + 1]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LLMError(f"malformed JSON from {self.provider}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError("expected a JSON object")
        return parsed


class BaseProvider:
    """Interface implemented by every backend."""

    name: str = "base"
    model: str = ""
    offline: bool = False

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResponse:
        raise NotImplementedError


class OfflineProvider(BaseProvider):
    """Sentinel for the built-in heuristic analyst.

    It never performs a completion — :mod:`.heuristic` supplies the analysis
    directly — but it satisfies the same interface so callers need no branching
    beyond checking :attr:`offline`.
    """

    name = "heuristic"
    model = "rule-based-analyst"
    offline = True

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResponse:
        raise LLMError("the offline analyst does not issue completions")


class OpenAIProvider(BaseProvider):
    """OpenAI Chat Completions backend (``gpt-5-mini`` by default)."""

    name = "openai"

    def __init__(self, model: str | None = None) -> None:
        from openai import OpenAI  # imported lazily so the SDK stays optional

        self.model = model or CONFIG.llm.openai_model
        self._client = OpenAI(timeout=CONFIG.llm.request_timeout)

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            request["response_format"] = {"type": "json_object"}

        try:
            completion = self._client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - SDK raises many error types
            raise LLMError(f"openai call failed: {exc}") from exc

        usage = getattr(completion, "usage", None)
        return LLMResponse(
            text=completion.choices[0].message.content or "",
            tokens_used=getattr(usage, "total_tokens", 0) or 0,
            model=self.model,
            provider=self.name,
        )


def _try_openai() -> BaseProvider | None:
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        return OpenAIProvider()
    except Exception as exc:  # noqa: BLE001 - SDK missing or misconfigured
        log.warning("openai unavailable", extra={"error": str(exc)})
        return None


def get_provider(preference: str | None = None) -> BaseProvider:
    """Resolve the analyst backend, degrading to the offline one if needed."""
    choice = (preference or CONFIG.llm.provider).lower()

    if choice == "heuristic":
        return OfflineProvider()
    if choice == "openai":
        return _try_openai() or OfflineProvider()

    provider = _try_openai() or OfflineProvider()
    log.info("analyst backend selected", extra={"provider": provider.name, "model": provider.model})
    return provider

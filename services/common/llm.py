"""The only LLM client in the repo.

It talks to a llama.cpp server on the internal network, over the OpenAI-shaped
chat-completions route. There is no cloud SDK, no API key and no external
endpoint anywhere in this project -- `demos/01_offline.sh` greps for exactly
that.

Two failure classes, and the difference between them is the whole retry policy:

  Unavailable -- connection refused, timeout, 5xx. The model is down or busy.
                 This is temporary, costs the caller no attempt, and is retried
                 for as long as it takes.
  Invalid     -- a reply arrived but is not usable: not JSON, missing the
                 field, empty. This one counts, because retrying it forever
                 would be a loop, not patience.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)


class LlmUnavailable(Exception):
    """Temporary. Retry indefinitely; consumes no attempt."""


class LlmInvalidOutput(Exception):
    """The model answered, but not usably. Counts against the attempt cap."""


class LlmClient:
    def __init__(self, base_url: str | None = None, *, timeout: float | None = None):
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.timeout = timeout or config.LLM_TIMEOUT_S
        self.session = requests.Session()

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        max_tokens: int = 220,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """One call, constrained to `schema` by the server's grammar.

        The grammar makes malformed JSON rare; the validation below makes it
        survivable when it happens anyway.
        """
        body = {
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": schema},
            },
        }
        try:
            response = self.session.post(
                f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LlmUnavailable(str(exc)) from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise LlmUnavailable(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise LlmInvalidOutput(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LlmInvalidOutput(f"unexpected response shape: {exc}") from exc

        if not content or not content.strip():
            raise LlmInvalidOutput("empty completion")
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise LlmInvalidOutput(f"not JSON: {content[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LlmInvalidOutput("completion is not a JSON object")
        return parsed

    def healthy(self) -> bool:
        try:
            return self.session.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except requests.RequestException:
            return False

"""LLM client abstraction (OpenRouter-compatible, optional).

Phase 4 uses a real LLM when configured (`.env`), but every path must
degrade gracefully to deterministic rules so tests never need network.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


class LLMClient(Protocol):
    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str: ...


class OpenRouterLLMClient:
    """Minimal OpenAI-compatible chat completions client."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        resp = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected LLM response: {data}") from None


def load_env_file(path: str | Path = ".env") -> None:
    """Minimal .env loader (no extra dependency). Never overwrites existing env."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def llm_client_from_env(env_path: str | Path = ".env") -> LLMClient | None:
    """Build an OpenRouterLLMClient if LLM_API_KEY is configured, else None."""
    load_env_file(env_path)
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None
    return OpenRouterLLMClient(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )

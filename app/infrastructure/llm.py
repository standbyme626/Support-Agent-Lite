"""LLM provider registry (pi-ai inspired): multi-provider, env-driven,
with automatic fallback chains. Every downstream path must still degrade
gracefully to deterministic rules when no provider is configured.

Providers are resolved from environment variables:

    LLM_PROVIDER=openrouter            # primary (runtime default)
    LLM_FALLBACKS=bailian              # comma-separated chain (optional)
    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL           # openrouter (legacy names kept)
    BAILIAN_API_KEY / BAILIAN_BASE_URL / BAILIAN_MODEL

Generic rule per provider <name>: <NAME>_API_KEY / <NAME>_BASE_URL /
<NAME>_MODEL override the built-in defaults.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Protocol

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/api/v1".replace("/api/api/", "/api/")  # guard vs typo
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

_BUILTIN: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": DEFAULT_MODEL,
        "key_env": "LLM_API_KEY",
    },
    "bailian": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "",
        "key_env": "BAILIAN_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
}


class LLMClient(Protocol):
    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str: ...


class ProviderError(RuntimeError):
    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"[{provider}] {detail}")
        self.provider = provider


class OpenAICompatibleClient:
    """One provider endpoint speaking the OpenAI chat-completions shape."""

    def __init__(
        self,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self.provider = provider
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self.last_usage: dict[str, int] = {}

    @property
    def model(self) -> str:
        return self._model

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = httpx.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self._timeout,
                )
                if resp.status_code == 429:  # rate limited: brief backoff, retry once
                    if attempt < self._max_retries:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                self.last_usage = {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                }
                return str(data["choices"][0]["message"]["content"]).strip()
            except Exception as exc:  # noqa: BLE001 - normalize to ProviderError
                last_error = exc
                if attempt < self._max_retries:
                    time.sleep(1.5)
        raise ProviderError(self.provider, repr(last_error))


# Backwards-compatible alias (older code/tests referenced this name).
OpenRouterLLMClient = OpenAICompatibleClient


class FallbackLLMClient:
    """Try providers in order; the first success wins. All fail -> raise."""

    def __init__(self, clients: list[LLMClient], *, provider_names: list[str] | None = None) -> None:
        self._clients = clients
        self.provider_names = provider_names or [getattr(c, "provider", f"c{i}") for i, c in enumerate(clients)]

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        errors: list[str] = []
        for client in self._clients:
            try:
                return client.complete(system=system, user=user, temperature=temperature)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
        raise RuntimeError("all providers failed: " + " | ".join(errors))

    @property
    def last_successful_provider(self) -> str | None:
        for c in reversed(self._clients):
            u = getattr(c, "last_usage", None)
            if u:
                return getattr(c, "provider", "?")
        return None


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


def _resolve(provider: str) -> OpenAICompatibleClient | None:
    """Build one provider client if its credential is present."""
    up = provider.upper().replace("-", "_")
    builtin = _BUILTIN.get(provider, {})
    key = os.environ.get(f"{up}_API_KEY") or (
        os.environ.get(builtin["key_env"]) if builtin.get("key_env") else ""
    )
    if not key:
        return None
    base_url = os.environ.get(f"{up}_BASE_URL") or builtin.get("base_url", "")
    model = os.environ.get(f"{up}_MODEL") or builtin.get("model", "")
    if not base_url or not model:
        return None
    return OpenAICompatibleClient(provider=provider, api_key=key, base_url=base_url, model=model)


def llm_client_from_env(env_path: str | Path = ".env") -> LLMClient | None:
    """Primary provider = LLM_PROVIDER (default openrouter); optional
    fallback chain via LLM_FALLBACKS (comma-separated provider names).
    Returns None when nothing is configured (tests stay offline)."""
    load_env_file(env_path)
    primary_name = os.environ.get("LLM_PROVIDER", "openrouter")
    chain_names = [primary_name] + [
        n.strip() for n in os.environ.get("LLM_FALLBACKS", "").split(",") if n.strip()
    ]
    clients: list[OpenAICompatibleClient] = []
    seen: set[str] = set()
    for name in chain_names:
        if not name or name in seen:
            continue
        seen.add(name)
        client = _resolve(name)
        if client is not None:
            clients.append(client)
    if not clients:
        return None
    if len(clients) == 1:
        return clients[0]
    return FallbackLLMClient(clients, provider_names=[c.provider for c in clients])

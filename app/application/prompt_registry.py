"""PromptRegistry: versioned prompt templates with safe rendering.

Templates are plain markdown with YAML-ish front-matter:

    ---
    prompt_key: agent_decision
    prompt_version: v1
    scenario: intake
    expected_schema: application/json
    ---
    <body with {variables} and {{escaped literal braces}}>

Safety guarantees (V2.1):
- deterministic loading + caching by key (``<key>.v1.md``)
- every ``{name}`` must have a value: missing variables raise
  ``PromptRenderError`` (no silent "None"/KeyError leakage into the prompt)
- literal braces in the body must be escaped as ``{{`` / ``}}``; stray
  unescaped braces raise (JSON examples in prompts use double braces)
- front-matter meta (prompt_key / prompt_version / expected_schema) is
  accessible for tracing and version pinning
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_ROOT = Path(__file__).resolve().parent / "prompts"

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_META_LINE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$")


class PromptNotFound(KeyError):
    pass


class PromptRenderError(ValueError):
    pass


@dataclass(frozen=True)
class PromptMeta:
    prompt_key: str
    prompt_version: str
    scenario: str = ""
    expected_schema: str = "application/json"


@dataclass(frozen=True)
class SkillMeta:
    """B4-v2 structured skill definition (seven meta fields)."""

    key: str
    version: int
    applies_to: tuple[str, ...]
    not_applies_to: tuple[str, ...]
    requires: tuple[str, ...]
    failure_mode: str
    output_format: str


class PromptRegistry:
    """Loads `<key>.v1.md` templates from the prompts dir on demand."""

    def __init__(self, prompts_dir: str | Path = PROMPTS_ROOT) -> None:
        self._dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

    def _load(self, key: str) -> str:
        if key not in self._cache:
            path = self._dir / f"{key}.v1.md"
            if not path.exists():
                raise PromptNotFound(key)
            self._cache[key] = path.read_text(encoding="utf-8")
        return self._cache[key]

    def meta(self, key: str) -> PromptMeta:
        """Front-matter metadata for a prompt (prompt_key/version/schema)."""
        raw = self._load(key)
        match = _FRONT_MATTER_RE.match(raw)
        meta: dict[str, str] = {}
        if match:
            for line in match.group(1).splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line_match = _META_LINE_RE.match(line)
                if line_match:
                    meta[line_match.group(1)] = line_match.group(2).strip()
        return PromptMeta(
            prompt_key=meta.get("prompt_key", key),
            prompt_version=meta.get("prompt_version", "v1"),
            scenario=meta.get("scenario", ""),
            expected_schema=meta.get("expected_schema", "application/json"),
        )

    def render(self, key: str, variables: dict[str, object]) -> str:
        """Render a prompt body with validated variables.

        ``{name}`` -> value (missing raises). ``{{`` / ``}}`` -> literal
        braces (JSON examples). Stray single braces raise.
        """
        body = self._load(key)
        body = _FRONT_MATTER_RE.sub("", body).strip()
        body = body.replace("{{", "\x01").replace("}}", "\x02")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise PromptRenderError(f"missing variable for prompt {key}: {name}")
            return str(variables[name])

        rendered = _VAR_RE.sub(replace, body)
        if "{" in rendered or "}" in rendered:
            raise PromptRenderError(
                f"unbalanced braces in prompt {key}; escape literal braces as {{" "{{}}"
            )
        return rendered.replace("\x01", "{").replace("\x02", "}")

    # --- B4-v2 skill registry -------------------------------------------------

    _SKILLS_SUBDIR = "skills"

    def _skill_path(self, key: str) -> Path:
        return self._dir / self._SKILLS_SUBDIR / f"{key}.v1.md"

    def skills(self) -> list[SkillMeta]:
        """All registered skill metas (sorted by key). Missing dir -> []."""
        d = self._dir / self._SKILLS_SUBDIR
        if not d.exists():
            return []
        out: list[SkillMeta] = []
        for path in sorted(d.glob("*.v1.md")):
            try:
                out.append(self._parse_skill_meta(path))
            except Exception:  # noqa: BLE001 - a broken skill never breaks others
                continue
        return out

    def get(self, key: str) -> SkillMeta:
        for m in self.skills():
            if m.key == key:
                return m
        raise PromptNotFound(f"skill:{key}")

    def select(self, intent: str | None) -> SkillMeta | None:
        """Route an intent to at most one skill.

        applies_to must contain the intent; any not_applies_to hit (the
        negative sample) vetoes. No match -> None (main prompt only).
        """
        if not intent:
            return None
        intent_l = str(intent).strip().lower()
        for m in self.skills():
            if intent_l in m.not_applies_to:
                continue
            if intent_l in m.applies_to:
                return m
        return None

    def _sections(self, key: str) -> tuple[str, str]:
        raw = self._load_skill(key)
        body = _FRONT_MATTER_RE.sub("", raw).strip()
        parts = body.split("## summary", 1)
        rest = parts[1] if len(parts) > 1 else ""
        sec = rest.split("## full", 1)
        summary = sec[0].strip()
        full = sec[1].strip() if len(sec) > 1 else ""
        return summary, full

    def get_summary(self, key: str) -> str:
        return self._sections(key)[0]

    def get_full(self, key: str) -> str:
        return self._sections(key)[1]

    def digest(self) -> str:
        """One line per skill for the slimmed main prompt's skill menu."""
        lines = []
        for m in self.skills():
            try:
                one_line = self.get_summary(m.key).splitlines()[0][:60]
            except Exception:  # noqa: BLE001
                continue
            lines.append(f"- {m.key}: {one_line}")
        return "\n".join(lines)

    def _load_skill(self, key: str) -> str:
        path = self._skill_path(key)
        if not path.exists():
            raise PromptNotFound(f"skill:{key}")
        cache_key = f"skill:{key}"
        if cache_key not in self._cache:
            self._cache[cache_key] = path.read_text(encoding="utf-8")
        return self._cache[cache_key]

    def _parse_skill_meta(self, path: Path) -> SkillMeta:
        match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
        meta: dict[str, str] = {}
        if match:
            for line in match.group(1).splitlines():
                lm = _META_LINE_RE.match(line.strip())
                if lm:
                    meta[lm.group(1)] = lm.group(2).strip()

        def _list(name: str) -> tuple[str, ...]:
            raw = meta.get(name, "").strip()
            if not raw or raw == "-":
                return ()
            return tuple(x.strip().lower() for x in raw.split(",") if x.strip())

        return SkillMeta(
            key=meta.get("key", path.stem.split(".")[0]),
            version=int(meta.get("version", "1")),
            applies_to=_list("applies_to"),
            not_applies_to=_list("not_applies_to"),
            requires=_list("requires"),
            failure_mode=meta.get("failure_mode", "fallback_main"),
            output_format=meta.get("output_format", ""),
        )

    @staticmethod
    def extract_json(raw: str) -> dict:
        """Best-effort JSON extraction from an LLM reply (may include fences).

        Raises ValueError when no JSON object can be found — callers fall
        back to deterministic rules (never guess a dangerous value).
        """
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?|```$", "", cleaned).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object in LLM output: {raw[:120]!r}")
        parsed = json.loads(cleaned[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM output is not a JSON object: {raw[:120]!r}")
        return parsed


_default_registry: PromptRegistry | None = None


def get_registry() -> PromptRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = PromptRegistry()
    return _default_registry

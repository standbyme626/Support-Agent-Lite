"""Semantic intent routing layer (embedding-based, V2.2).

Companion to IntentRouter: the keyword router stays the deterministic
fast path; this layer catches the long tail of natural Chinese phrasing
that never contains the curated keywords (e.g. "我的银行账户被冻结了"
contains no support keyword).

Design (aligned with production practice):
- Anchors: per-intent example utterances (CLINC150 English + zh_golden
  Chinese), embedded once offline by scripts/build_intent_anchors.py.
- Route: max cosine over all anchor vectors; above threshold the best
  intent wins, below threshold -> other (low-confidence fallback, never
  force-pick).
- Degradation: if anchors/credentials are unavailable the router returns
  None and callers fall back to rules-only (keyword path already handles
  that).
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from app.infrastructure.vector_store import EmbeddingError, SiliconFlowEmbedding

DEFAULT_THRESHOLD = 0.60
MIN_SEMANTIC_LEN = 6


@dataclass(frozen=True)
class SemanticDecision:
    intent: str
    confidence: float
    is_low_confidence: bool
    reason: str


class SemanticIntentRouter:
    """Embedding-based intent router with anchor prototypes."""

    def __init__(
        self,
        anchors_dir: str | Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        embedding: SiliconFlowEmbedding | None = None,
    ) -> None:
        self._threshold = threshold
        self._embedding = embedding or SiliconFlowEmbedding(
            timeout=10.0, retries=1, batch_size=1
        )
        self._anchors_dir = Path(
            anchors_dir or os.environ.get("INTENT_ANCHORS_DIR", "")
            or (Path(__file__).resolve().parent.parent.parent / "runtime" / "intent_anchors")
        )
        self._intents: list[str] = []
        self._anchor_texts: list[list[str]] = []
        self._anchor_vectors: list[list[list[float]]] = []
        self._anchor_np: list[object] | None = None

    @property
    def available(self) -> bool:
        return bool(self._intents)

    def load(self) -> bool:
        """Load persisted anchors; returns False (non-raising) on any failure."""
        anchors_p = self._anchors_dir / "anchors.json"
        vectors_p = self._anchors_dir / "vectors.json"
        if not anchors_p.exists() or not vectors_p.exists():
            return False
        try:
            payload = json.loads(anchors_p.read_text(encoding="utf-8"))
            anchors: dict[str, list[str]] = payload["anchors"]
            order = payload["order"]
            vectors = json.loads(vectors_p.read_text(encoding="utf-8"))
            self._intents = [i for i in order if anchors.get(i)]
            self._anchor_texts = [anchors[i] for i in self._intents]
            self._anchor_vectors = []
            idx = 0
            for texts in self._anchor_texts:
                n = len(texts)
                self._anchor_vectors.append(vectors[idx : idx + n])
                idx += n
            self._anchor_np = _try_numpy(self._anchor_vectors)
            return True
        except (KeyError, ValueError, TypeError):
            return False

    def route(self, message: str) -> SemanticDecision | None:
        """Return best-intent decision, or None when the layer is unavailable."""
        if not self._intents:
            return None
        text = message.strip()
        if not text:
            return SemanticDecision("other", 0.0, True, "empty-message")
        if len(text) < MIN_SEMANTIC_LEN:
            # Short messages are handled by the rule layer (chitchat
            # phrase matching, ticket refs); embedding anchors are built
            # from 8-30 char sentences and misjudge short utterances
            # (e.g. "处理好了" as chitchat).
            return SemanticDecision("other", 0.0, True, f"too-short:{len(text)}")
        try:
            (query_vec,) = self._embedding.embed([text])
        except EmbeddingError:
            return None

        best_intent = "other"
        best_score = 0.0
        if self._anchor_np is not None:
            import numpy as np

            q = np.asarray(query_vec, dtype=np.float32)
            q_norm = float(np.linalg.norm(q)) or 1.0
            for intent, block in zip(self._intents, self._anchor_np):
                dots = block @ q
                score = float(dots.max()) / q_norm  # blocks are pre-normalized
                if score > best_score:
                    best_score = score
                    best_intent = intent
        else:
            for intent, vecs in zip(self._intents, self._anchor_vectors):
                for v in vecs:
                    score = _cosine(query_vec, v)
                    if score > best_score:
                        best_score = score
                        best_intent = intent

        if best_score < self._threshold:
            return SemanticDecision(
                "other", round(best_score, 3), True, f"below-threshold:{self._threshold:.2f}"
            )
        return SemanticDecision(best_intent, round(best_score, 3), False, "anchor-match")


def _cosine(a: list[float], b: list[float]) -> float:
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (da * db)


def _try_numpy(anchor_vectors: list[list[list[float]]]) -> list[object] | None:
    """Pre-normalized per-intent numpy blocks; None when numpy is missing."""
    try:
        import numpy as np

        blocks = []
        for vecs in anchor_vectors:
            block = np.asarray(vecs, dtype=np.float32)
            norms = np.linalg.norm(block, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            blocks.append(block / norms)
        return blocks
    except ImportError:
        return None


class CascadeIntentRouter:
    """Rules fast-path + semantic main path + other fallback.

    Wraps IntentRouter (deterministic keywords) and SemanticIntentRouter
    (embedding anchors) behind the same route() interface so callers
    (workflow / graph pipeline) are unchanged.

    Order:
    1. Rule layer: only high-confidence keyword hits (confidence >=
       `rule_conf`) return directly — experiments showed loose keyword
       hits (conf < 0.8) are wrong ~30-45% of the time and degrade the
       cascade below embedding-only.
    2. Embedding layer: max anchor cosine; above `semantic_threshold`
       the best intent wins.
    3. Fallback: low confidence routes to "other" (never force-pick).

    Degradation: when anchors/credentials are unavailable the semantic
    layer returns None and routing falls back to the pure rule decision,
    matching pre-V2.2 behavior.
    """

    def __init__(
        self,
        rule_router: object | None = None,
        semantic_router: SemanticIntentRouter | None = None,
        rule_conf: float = 0.7,
        semantic_threshold: float = 0.62,
    ) -> None:
        from app.application.intent_router import IntentRouter

        self._rule = rule_router or IntentRouter()
        self._semantic = semantic_router or SemanticIntentRouter(threshold=semantic_threshold)
        if semantic_router is None:
            self._semantic.load()  # silent: unavailable -> rule-only degradation
        self._rule_conf = rule_conf
        self._semantic_threshold = semantic_threshold
        # 非对称意图阈值(E2E 实测修复 1B):support 宁错建单不漏单(错建单有
        # HITL 兜底),chitchat 宁不聊(闲聊无业务损失)。progress/faq 居中。
        self._intent_thresholds: dict[str, float] = {
            "support": 0.55,
            "progress_query": 0.58,
            "faq": 0.60,
            "chitchat": 0.68,
            "other": 0.62,
        }

    def route(self, message: str) -> SemanticDecision:
        rule = self._rule.route(message)

        # Deterministic keyword layer is the primary path: any rule signal
        # above the rule threshold wins outright. Weak rule hits (>=0.58
        # threshold but below rule_conf) also win — the curated keywords
        # are strong business signals ("怎么样了" is progress, never
        # chitchat); the embedding layer must not override them.
        if rule.intent != "other" and not rule.is_low_confidence:
            if rule.confidence >= self._rule_conf:
                return SemanticDecision(rule.intent, rule.confidence, False, "rule-fastpath")
            return SemanticDecision(rule.intent, rule.confidence, True, "rule-low-confidence")

        # No rule signal: the embedding layer owns the long tail.
        semantic = self._semantic.route(message)
        if semantic is not None:
            intent_thr = self._intent_thresholds.get(semantic.intent, self._semantic_threshold)
            if semantic.confidence >= intent_thr:
                return semantic
            return SemanticDecision("other", semantic.confidence, True, "semantic-fallback")

        # Embedding layer unavailable: full degradation to the pure rule
        # decision (pre-V2.2 behavior).
        return SemanticDecision(rule.intent, rule.confidence, rule.is_low_confidence, rule.reason)
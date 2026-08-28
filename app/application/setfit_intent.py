"""SetFit 本地意图分类层(批 3,E2E 修复 1C 根治)。

替换 SiliconFlow API 锚点语义层:确定性模型、1.6ms/条、零 API 依赖,
office eval 81.8% 超 API 基线(78.6%)。规则层 + 业务保底护栏不动。

接口与 SemanticIntentRouter 对齐(route -> SemanticDecision|None),Cascade
IntentRouter 直接注入。模型与训练见 scripts/train_setfit_intent.py。

- 懒加载:首次 route() 才 import setfit + 载模型(启动不背 2 分钟加载);
- 概率拒答(形态 C):max proba < PROB_THRESHOLD -> other 低置信,
  直击 zh_golden other 桶弱项;
- 线程安全:单进程内全局锁(predict 非线程安全);
- reason 复用 "anchor-match"/"below-threshold" 语义,workflow 续接逻辑不变。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

LABELS = ["support", "faq", "progress_query", "chitchat", "other"]
MIN_SEMANTIC_LEN = 6
PROB_THRESHOLD = float(os.environ.get("SETFIT_PROB_THRESHOLD", "0.35"))


@dataclass(frozen=True)
class SetFitDecision:
    intent: str
    confidence: float
    is_low_confidence: bool
    reason: str


class SetFitIntentRouter:
    """SetFit 微调 bge-small-zh 意图分类器(生产语义层)。"""

    def __init__(
        self,
        model_dir: str | Path | None = None,
        prob_threshold: float = PROB_THRESHOLD,
    ) -> None:
        self._model_dir = Path(
            model_dir or os.environ.get("SETFIT_MODEL_DIR", "")
            or (Path(__file__).resolve().parent.parent.parent / "runtime" / "setfit-intent")
        )
        self._prob_threshold = prob_threshold
        self._model = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        """Lazy-load the SetFit model; False (non-raising) when unavailable."""
        if self._model is not None:
            return True
        if not (self._model_dir / "model.safetensors").exists():
            return False
        try:
            from setfit import SetFitModel

            self._model = SetFitModel.from_pretrained(str(self._model_dir))
            return True
        except Exception as exc:  # noqa: BLE001 - 不可用即降级规则层
            print(f"[setfit] load failed, degrade to rule+API layer: {exc!r}")
            self._model = None
            return False

    def route(self, message: str) -> SetFitDecision | None:
        """Return best-intent decision, or None when the model is unavailable."""
        if not self.load():
            return None
        text = message.strip()
        if not text:
            return SetFitDecision("other", 0.0, True, "empty-message")
        if len(text) < MIN_SEMANTIC_LEN:
            return SetFitDecision("other", 0.0, True, f"too-short:{len(text)}")
        with self._lock:
            proba = self._model.predict_proba([text])[0]
        best_idx = int(proba.argmax())
        best_score = float(proba[best_idx])
        if best_score < self._prob_threshold:
            return SetFitDecision(
                "other", round(best_score, 3), True, f"below-threshold:{self._prob_threshold:.2f}"
            )
        return SetFitDecision(LABELS[best_idx], round(best_score, 3), False, "anchor-match")
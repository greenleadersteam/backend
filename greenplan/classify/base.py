"""Classifier abstraction: entity -> semantic category, algorithmic or ML.

RuleBasedClassifier (regex on layer/block name) is the Phase 1 implementation.
The ABC exists so a future ML-based classifier (fuzzy/embedding match against
a labeled corpus of layer names, no LLM at runtime) can sit behind the same
interface and be chained via CompositeClassifier without touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ClassificationResult(BaseModel):
    category: str
    subtype: str | None = None
    rule_id: str
    status: str
    confidence: float
    expand_blocks: bool = False


class Classifier(ABC):
    @abstractmethod
    def classify(self, entity) -> ClassificationResult | None:
        """Return a classification for a DXF entity, or None if no match."""
        raise NotImplementedError

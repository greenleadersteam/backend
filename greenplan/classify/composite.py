"""Chain classifiers: try a primary (typically rule-based), fall back to a
secondary (e.g. a future ML classifier) if the primary finds no match.

`fallback` is left unset by default in Phase 1 -- there is no ML classifier
implementation yet, only the seam to plug one in later without touching
callers of Classifier.classify().
"""

from __future__ import annotations

from greenplan.classify.base import ClassificationResult, Classifier


class CompositeClassifier(Classifier):
    def __init__(self, primary: Classifier, fallback: Classifier | None = None):
        self.primary = primary
        self.fallback = fallback

    def classify(self, entity) -> ClassificationResult | None:
        result = self.primary.classify(entity)
        if result is not None:
            return result
        if self.fallback is not None:
            return self.fallback.classify(entity)
        return None

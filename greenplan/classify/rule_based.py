"""Regex rule-pack classifier, generalized from the original
backend/dxf_layers.py classify_entity(): matches the entity's own layer
name first, then (for INSERT entities only) the referenced block name,
since a lot of semantic labeling in these drawings lives in block names
(e.g. "ПОДПОРНАЯ СТЕНКА_0.4м") rather than generic layer names.
"""

from __future__ import annotations

from greenplan.classify.base import ClassificationResult, Classifier
from greenplan.rules.schema import RulePack

STATUS_CONFIDENCE = {
    "auto": 0.9,
    "auto_partial": 0.7,
    "proxy_low_confidence": 0.4,
    "no_default_source": 0.1,
}


class RuleBasedClassifier(Classifier):
    def __init__(self, rule_pack: RulePack):
        self.rule_pack = rule_pack

    def classify(self, entity) -> ClassificationResult | None:
        layer = entity.dxf.layer
        compiled = self.rule_pack.match_layer(layer)
        matched_on = layer
        if compiled is None and entity.dxftype() == "INSERT":
            compiled = self.rule_pack.match_block(entity.dxf.name)
            matched_on = entity.dxf.name
        if compiled is None:
            return None

        subtype = None
        if compiled.subtype_regexes:
            subtype = compiled.subtype_regexes[-1][0]  # last entry is the catch-all default
            for key, regex in compiled.subtype_regexes:
                if regex.search(matched_on):
                    subtype = key
                    break

        return ClassificationResult(
            category=compiled.rule.key,
            subtype=subtype,
            rule_id=compiled.rule.id,
            status=compiled.rule.status,
            confidence=STATUS_CONFIDENCE.get(compiled.rule.status, 0.5),
            expand_blocks=compiled.rule.expand_blocks,
        )

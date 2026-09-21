"""Per-rule feature counts, plus an unmatched-layers breakdown.

The unmatched-layers report is the feedback loop for growing rule packs as
new pilot objects with unfamiliar naming conventions are tried: it surfaces
exactly which layers (by entity count, so the biggest gaps sort first) no
rule currently covers.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import BaseModel

from greenplan.rules.schema import RulePack


class RuleCoverage(BaseModel):
    rule_id: str
    key: str
    name_ru: str
    status: str
    count: int


class UnmatchedLayer(BaseModel):
    layer: str
    entity_count: int
    dxftypes: list[str]
    sample_source_file: str


class CoverageReport(BaseModel):
    rules: list[RuleCoverage]
    unmatched_layers: list[UnmatchedLayer]

    def to_dict(self) -> dict:
        return self.model_dump()


class CoverageBuilder:
    """Accumulates matched-rule counts and unmatched-layer stats while the
    entity loop runs, then finalizes into a CoverageReport.
    """

    def __init__(self, rule_pack: RulePack):
        self.rule_pack = rule_pack
        self._matched_counts: Counter[str] = Counter()
        self._unmatched_entity_counts: Counter[str] = Counter()
        self._unmatched_dxftypes: dict[str, set[str]] = defaultdict(set)
        self._unmatched_sample_source: dict[str, str] = {}

    def record_matched(self, rule_id: str) -> None:
        self._matched_counts[rule_id] += 1

    def record_unmatched(self, layer: str, dxftype: str, source_file: str) -> None:
        self._unmatched_entity_counts[layer] += 1
        self._unmatched_dxftypes[layer].add(dxftype)
        self._unmatched_sample_source.setdefault(layer, source_file)

    def build(self) -> CoverageReport:
        rules = [
            RuleCoverage(
                rule_id=compiled.rule.id,
                key=compiled.rule.key,
                name_ru=compiled.rule.name_ru,
                status=compiled.rule.status,
                count=self._matched_counts.get(compiled.rule.id, 0),
            )
            for compiled in self.rule_pack.rules
        ]
        unmatched = [
            UnmatchedLayer(
                layer=layer,
                entity_count=count,
                dxftypes=sorted(self._unmatched_dxftypes[layer]),
                sample_source_file=self._unmatched_sample_source[layer],
            )
            for layer, count in self._unmatched_entity_counts.most_common()
        ]
        return CoverageReport(rules=rules, unmatched_layers=unmatched)

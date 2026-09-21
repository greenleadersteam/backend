"""Declarative rule-pack schema: layer/block name regex -> semantic category.

A rule pack is a YAML file (see `default.yaml`) validated and compiled here.
This is the extensibility seam for new data providers: a new naming
convention is a new or edited YAML file, not a code change.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class SubtypeRule(BaseModel):
    key: str
    pattern: str


class Rule(BaseModel):
    """One row of the rule pack, as loaded from YAML (uncompiled)."""

    id: str
    key: str
    name_ru: str
    group: int
    status: str  # auto | auto_partial | proxy_low_confidence | no_default_source
    layer_pattern: str
    # Falls back to layer_pattern for INSERT block-name matching if unset.
    block_pattern: str | None = None
    expand_blocks: bool = False
    subtypes: list[SubtypeRule] = []


class CompiledRule(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    rule: Rule
    layer_regex: re.Pattern
    block_regex: re.Pattern
    subtype_regexes: list[tuple[str, re.Pattern]]


class RulePack(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str
    rules: list[CompiledRule]

    @classmethod
    def load(cls, path: Path | str) -> "RulePack":
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        rules = [Rule.model_validate(item) for item in raw]
        return cls(source=str(path), rules=[_compile(r) for r in rules])

    def match_layer(self, layer_name: str) -> CompiledRule | None:
        for compiled in self.rules:
            if compiled.layer_regex.search(layer_name):
                return compiled
        return None

    def match_block(self, block_name: str) -> CompiledRule | None:
        for compiled in self.rules:
            if compiled.block_regex.search(block_name):
                return compiled
        return None


def _compile(rule: Rule) -> CompiledRule:
    layer_regex = re.compile(rule.layer_pattern, re.IGNORECASE)
    block_regex = re.compile(rule.block_pattern or rule.layer_pattern, re.IGNORECASE)
    subtype_regexes = [(s.key, re.compile(s.pattern, re.IGNORECASE)) for s in rule.subtypes]
    return CompiledRule(
        rule=rule,
        layer_regex=layer_regex,
        block_regex=block_regex,
        subtype_regexes=subtype_regexes,
    )

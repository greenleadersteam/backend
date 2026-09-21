from types import SimpleNamespace

from greenplan.classify.composite import CompositeClassifier
from greenplan.classify.rule_based import RuleBasedClassifier
from greenplan.pipeline import load_default_rule_pack


def make_entity(layer: str, dxftype: str = "LWPOLYLINE", block_name: str | None = None):
    dxf = SimpleNamespace(layer=layer, name=block_name)
    return SimpleNamespace(dxf=dxf, dxftype=lambda: dxftype)


def test_matches_on_layer_name():
    classifier = RuleBasedClassifier(load_default_rule_pack())
    result = classifier.classify(make_entity("Газопровод"))
    assert result is not None
    assert result.category == "underground_utilities"
    assert result.subtype == "gas"
    assert result.status == "auto"


def test_insert_falls_back_to_block_name_when_layer_does_not_match():
    classifier = RuleBasedClassifier(load_default_rule_pack())
    entity = make_entity(layer="0", dxftype="INSERT", block_name="ПОДПОРНАЯ СТЕНКА_0.4м")
    result = classifier.classify(entity)
    assert result is not None
    assert result.category == "retaining_walls_slopes"


def test_non_insert_entity_does_not_check_block_name():
    classifier = RuleBasedClassifier(load_default_rule_pack())
    entity = make_entity(layer="0", dxftype="LINE", block_name="Здания")
    assert classifier.classify(entity) is None


def test_no_match_returns_none():
    classifier = RuleBasedClassifier(load_default_rule_pack())
    assert classifier.classify(make_entity("Совершенно неизвестный слой")) is None


def test_composite_classifier_falls_back():
    class AlwaysMatch:
        def classify(self, entity):
            return "fallback-result"

    class NeverMatch:
        def classify(self, entity):
            return None

    composite = CompositeClassifier(primary=NeverMatch(), fallback=AlwaysMatch())
    assert composite.classify(make_entity("anything")) == "fallback-result"

    composite_no_fallback = CompositeClassifier(primary=NeverMatch())
    assert composite_no_fallback.classify(make_entity("anything")) is None

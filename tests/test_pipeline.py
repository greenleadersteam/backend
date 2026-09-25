import ezdxf

from greenplan.classify.rule_based import RuleBasedClassifier
from greenplan.coverage import CoverageBuilder
from greenplan.pipeline import _classify_recursive, load_default_rule_pack


def _classifier_and_coverage():
    pack = load_default_rule_pack()
    return RuleBasedClassifier(pack), CoverageBuilder(pack)


def test_matched_top_level_entity_yields_itself_without_exploding():
    doc = ezdxf.new()
    msp = doc.modelspace()
    entity = msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "Газопровод"})

    classifier, coverage = _classifier_and_coverage()
    results = list(_classify_recursive(entity, classifier, coverage, "root.dxf"))

    assert len(results) == 1
    matched_entity, result = results[0]
    assert matched_entity is entity
    assert result.category == "underground_utilities"
    assert coverage.build().unmatched_layers == []


def test_unmatched_non_insert_entity_records_unmatched_and_yields_nothing():
    doc = ezdxf.new()
    msp = doc.modelspace()
    entity = msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "Совершенно неизвестный слой"})

    classifier, coverage = _classifier_and_coverage()
    results = list(_classify_recursive(entity, classifier, coverage, "root.dxf"))

    assert results == []
    unmatched = coverage.build().unmatched_layers
    assert len(unmatched) == 1
    assert unmatched[0].layer == "Совершенно неизвестный слой"


def test_unmatched_bound_insert_recurses_into_children_and_classifies_each():
    """The core fix: an INSERT referencing a *bound* (non-xref) block whose own
    layer/block name matches nothing -- e.g. a merged "сети" utility-network
    block -- must not hide its children. Each child should be classified (or
    recorded as unmatched) individually by its own layer, not lumped under the
    outer container's name.
    """
    doc = ezdxf.new()
    msp = doc.modelspace()
    block = doc.blocks.new(name="сети")
    block.add_lwpolyline([(0, 0), (1, 0), (1, 1)], dxfattribs={"layer": "Газопровод"})
    block.add_line((0, 0), (1, 1), dxfattribs={"layer": "Совершенно неизвестный слой"})
    insert = msp.add_blockref("сети", (10, 20), dxfattribs={"layer": "0"})

    classifier, coverage = _classifier_and_coverage()
    results = list(_classify_recursive(insert, classifier, coverage, "root.dxf"))

    assert len(results) == 1
    matched_entity, result = results[0]
    assert matched_entity is not insert  # a child of the exploded block, not the INSERT itself
    assert matched_entity.dxf.layer == "Газопровод"
    assert result.category == "underground_utilities"

    unmatched = coverage.build().unmatched_layers
    assert len(unmatched) == 1
    assert unmatched[0].layer == "Совершенно неизвестный слой"  # the child's own layer...
    assert unmatched[0].layer != "0"  # ...not the outer INSERT's layer/block name


def test_unmatched_insert_with_empty_block_records_unmatched_using_its_own_layer():
    doc = ezdxf.new()
    msp = doc.modelspace()
    doc.blocks.new(name="пустой_блок")  # zero entities inside
    insert = msp.add_blockref("пустой_блок", (0, 0), dxfattribs={"layer": "0"})

    classifier, coverage = _classifier_and_coverage()
    results = list(_classify_recursive(insert, classifier, coverage, "root.dxf"))

    assert results == []
    unmatched = coverage.build().unmatched_layers
    assert len(unmatched) == 1
    assert unmatched[0].layer == "0"


def test_nested_bound_blocks_recurse_through_multiple_levels():
    """A bound block can itself contain another bound-block INSERT (not just leaf
    geometry) -- the recursion should reach all the way down to real geometry.
    """
    doc = ezdxf.new()
    msp = doc.modelspace()
    inner = doc.blocks.new(name="inner_block")
    inner.add_line((0, 0), (1, 1), dxfattribs={"layer": "Здания"})
    outer = doc.blocks.new(name="outer_block")
    outer.add_blockref("inner_block", (0, 0), dxfattribs={"layer": "0"})
    insert = msp.add_blockref("outer_block", (5, 5), dxfattribs={"layer": "0"})

    classifier, coverage = _classifier_and_coverage()
    results = list(_classify_recursive(insert, classifier, coverage, "root.dxf"))

    assert len(results) == 1
    matched_entity, result = results[0]
    assert result.category == "buildings"
    assert matched_entity.dxf.layer == "Здания"

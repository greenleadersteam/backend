from greenplan.pipeline import load_default_norms


def test_default_norms_load():
    norms = load_default_norms()
    assert len(norms.rules) == 10


def test_find_and_distance_for():
    norms = load_default_norms()
    rule = norms.find("underground_utilities", "gas")
    assert rule is not None
    assert rule.tree_m == 1.5
    assert rule.shrub_m == 1.5
    assert "743-ПП" in rule.citation

    assert norms.distance_for("underground_utilities", "gas", "tree") == 1.5
    assert norms.distance_for("underground_utilities", "heat", "shrub") == 1.0


def test_subtype_none_matches_road_edge():
    norms = load_default_norms()
    rule = norms.find("road_edge", None)
    assert rule is not None
    assert rule.tree_m == 0.7
    assert rule.shrub_m == 0.5


def test_unknown_category_returns_none():
    norms = load_default_norms()
    assert norms.find("buildings", None) is None
    assert norms.distance_for("buildings", None, "tree") is None

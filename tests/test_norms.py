from greenplan.pipeline import load_default_norms


def test_default_norms_load():
    norms = load_default_norms()
    assert len(norms.rules) == 32
    keys = [(r.obstacle_category, r.obstacle_subtype) for r in norms.rules]
    assert len(keys) == len(set(keys))


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
    assert rule.tree_m == 2.0
    assert rule.shrub_m == 1.0


def test_ranges_use_upper_bound():
    norms = load_default_norms()
    assert norms.distance_for("road_edge", "arterial_citywide", "tree") == 7.0
    assert norms.distance_for("road_edge", "driveway", "tree") == 2.0
    assert norms.distance_for("green_existing", "existing_tree", "tree") == 6.0


def test_school_buildings_have_larger_setback():
    norms = load_default_norms()
    assert norms.distance_for("buildings", None, "tree") == 5.0
    assert norms.distance_for("buildings", "school_kindergarten", "tree") == 10.0


def test_unknown_category_returns_none():
    norms = load_default_norms()
    assert norms.find("contours", None) is None
    assert norms.distance_for("contours", None, "tree") is None

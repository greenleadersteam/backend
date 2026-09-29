from greenplan.pipeline import load_default_norms


def test_default_norms_load():
    norms = load_default_norms()
    assert len(norms.rules) == 39
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


def test_rule_ids_are_unique_and_norms_come_in_pairs():
    norms = load_default_norms()
    assert len({r.id for r in norms.rules}) == len(norms.rules)
    records = norms.norms()
    assert len(records) == 2 * len(norms.rules)
    assert len({n.id for n in records}) == len(records)
    assert {n.id for n in records} >= {"743-pp-gas-tree", "743-pp-gas-shrub", "743-pp-existing-tree-tree"}


def test_service_defaults_have_no_clause_and_dont_cite_the_act():
    for norm in load_default_norms().norms():
        if norm.basis == "service_default":
            assert norm.clause is None, norm.id
            assert "743-ПП" not in norm.citation, norm.id
            assert norm.citation.startswith("Значение сервиса"), norm.id


def test_shrub_dash_rows_are_service_defaults():
    norms = load_default_norms()
    for sub in ("gas", "water", "drainage", "sewer"):
        tree = norms.find("underground_utilities", sub).norm("tree")
        shrub = norms.find("underground_utilities", sub).norm("shrub")
        assert (tree.basis, shrub.basis) == ("regulation", "service_default")
        assert tree.clause.startswith("п. 3.6.3, табл. 3.6.1")
    heat = norms.find("underground_utilities", "heat")
    assert heat.norm("shrub").basis == "regulation"
    assert norms.find("underground_utilities", "other_utility").norm("tree").act is None
    assert norms.find("green_existing", "existing_tree").norm("tree").basis == "service_default"
    assert norms.find("poles_masts", None).norm("shrub").basis == "service_default"


def test_crown_note_in_table_3_6_1_tree_texts():
    rule = load_default_norms().find("buildings", None)
    assert "кроны не более 5 м" in rule.norm("tree").text
    assert "кроны" not in rule.norm("shrub").text


def test_default_id_and_text_fallbacks():
    from greenplan.norms.schema import SetbackRule

    rule = SetbackRule(obstacle_category="x", tree_m=1.0, shrub_m=2.0, citation="c")
    norm = rule.norm("shrub")
    assert (norm.id, norm.distance_m, norm.text, norm.basis) == ("x-any-shrub", 2.0, "c", "regulation")

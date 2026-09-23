from greenplan.pipeline import load_default_rule_pack


def test_default_rule_pack_loads():
    pack = load_default_rule_pack()
    assert len(pack.rules) == 12
    ids = {c.rule.id for c in pack.rules}
    assert ids == {"1", "3", "4", "5", "6", "7", "8", "11", "12", "19", "20", "90"}


def test_layer_matching_first_rule_wins():
    pack = load_default_rule_pack()
    assert pack.match_layer("Газопровод").rule.key == "underground_utilities"
    assert pack.match_layer("Здания").rule.key == "buildings"
    assert pack.match_layer("Части зданий").rule.key == "buildings"
    assert pack.match_layer("Опоры освещения").rule.key == "poles_masts"
    assert pack.match_layer("Совершенно неизвестный слой") is None


def test_bound_xref_layer_names_still_match_despite_prefix_noise():
    """A provider that binds an xref instead of leaving it external renames the
    child layer to "<original>|Category" or "<original>$0$Category" -- rules 4,
    19 and 20 used to be full-string-anchored and silently stopped matching
    these (confirmed on real objects), unlike every other rule in the pack.
    """
    pack = load_default_rule_pack()
    assert pack.match_layer("XREF_ГЕО_Измайловская пл|ТОПО_Части зданий").rule.key == "buildings"
    assert pack.match_layer("output[1-8]_3_ДЖКХ-24_02797tp$0$Здания").rule.key == "buildings"
    assert (
        pack.match_layer("XREF_ГЕО_Измайловская пл|КРАСНЫЕ_ЛИНИИ_Красные линии").rule.key
        == "red_lines"
    )
    assert pack.match_layer("output[1]_tp|Горизонтали").rule.key == "contours"


def test_geodetic_points_rule_matches_plain_and_bound_layer_names():
    pack = load_default_rule_pack()
    assert pack.match_layer("Геодезические пункты").rule.key == "geodetic_points"
    assert (
        pack.match_layer("output[1-8]_3_ДЖКХ-24_02797tp$0$Геодезические пункты").rule.key
        == "geodetic_points"
    )
    assert (
        pack.match_layer("XREF_ИГДИ_Камчатская улица|Геодезические пункты").rule.key
        == "geodetic_points"
    )


def test_underground_utility_subtypes_first_match_wins():
    pack = load_default_rule_pack()
    compiled = pack.match_layer("Газопровод")
    matched = [key for key, rx in compiled.subtype_regexes if rx.search("Газопровод")]
    assert matched[0] == "gas"

    compiled = pack.match_layer("Кабель электрический")
    matched = [key for key, rx in compiled.subtype_regexes if rx.search("Кабель электрический")]
    assert matched[0] == "power_cable"


def test_block_pattern_falls_back_to_layer_pattern():
    pack = load_default_rule_pack()
    compiled = pack.match_layer("Здания")
    assert compiled.block_regex.search("Части зданий")

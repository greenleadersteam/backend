from greenplan.pipeline import load_default_rule_pack


def test_default_rule_pack_loads():
    pack = load_default_rule_pack()
    assert len(pack.rules) == 11
    ids = {c.rule.id for c in pack.rules}
    assert ids == {"1", "3", "4", "5", "6", "7", "8", "11", "12", "19", "20"}


def test_layer_matching_first_rule_wins():
    pack = load_default_rule_pack()
    assert pack.match_layer("Газопровод").rule.key == "underground_utilities"
    assert pack.match_layer("Здания").rule.key == "buildings"
    assert pack.match_layer("Части зданий").rule.key == "buildings"
    assert pack.match_layer("Опоры освещения").rule.key == "poles_masts"
    assert pack.match_layer("Совершенно неизвестный слой") is None


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

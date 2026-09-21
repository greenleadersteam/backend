import pytest
from shapely.geometry import LineString, Polygon

from greenplan.zoning.topology import close_line_soup, resolve_nesting


def test_close_line_soup_bridges_small_gap_between_fragments():
    # A 20x20 square outline split into two open arcs with a 0.5m gap at
    # each seam -- mirrors the real object where a boundary was split across
    # two LWPOLYLINE entities whose shared endpoints didn't exactly match.
    arc_a = LineString([(0, 0), (20, 0), (20, 20)])
    arc_b = LineString([(20.5, 20.3), (0.3, 20.2), (0.2, 0.3)])  # gap ~0.3-0.5m at both seams

    polys = close_line_soup([arc_a, arc_b], tolerance=1.0)
    assert len(polys) == 1
    assert polys[0].area == pytest.approx(400.0, rel=0.05)


def test_close_line_soup_fails_to_bridge_gap_larger_than_tolerance():
    arc_a = LineString([(0, 0), (20, 0), (20, 20)])
    arc_b = LineString([(25, 25), (0.3, 20.2), (0.2, 0.3)])  # ~7m gap at one seam

    polys = close_line_soup([arc_a, arc_b], tolerance=1.0)
    assert polys == []


def test_close_line_soup_empty_input():
    assert close_line_soup([]) == []


def test_resolve_nesting_single_polygon_returned_as_is():
    square = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert resolve_nesting([square]) is square


def test_resolve_nesting_empty_list_returns_none():
    assert resolve_nesting([]) is None


def test_resolve_nesting_nested_polygon_becomes_a_hole():
    outer = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    inner = Polygon([(5, 5), (10, 5), (10, 10), (5, 10)])  # 25 m^2, fully inside outer

    result = resolve_nesting([outer, inner])
    assert result.area == pytest.approx(400.0 - 25.0, rel=1e-6)
    assert result.geom_type == "Polygon"
    assert len(result.interiors) == 1


def test_resolve_nesting_near_duplicate_polygons_are_not_treated_as_hole():
    # Two near-identical copies of the same boundary (e.g. the same site
    # outline digitized twice across duplicated sibling delivery folders).
    # near_dup is fully inside outer (would satisfy plain containment), but
    # at 96% of outer's area it's far too close in size to be a real hole --
    # treating it as one would carve out a degenerate sliver-thin annulus.
    outer = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    near_dup = Polygon([(1, 1), (99, 1), (99, 99), (1, 99)])

    result = resolve_nesting([outer, near_dup])
    assert result.area == pytest.approx(10000.0, rel=1e-6)
    assert result.geom_type == "Polygon"
    assert len(result.interiors) == 0


def test_resolve_nesting_island_inside_hole_is_solid_again():
    outer = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
    hole = Polygon([(10, 10), (20, 10), (20, 20), (10, 20)])  # 100 m^2 hole
    island = Polygon([(13, 13), (17, 13), (17, 17), (13, 17)])  # 16 m^2 island inside the hole

    result = resolve_nesting([outer, hole, island])
    assert result.area == pytest.approx(900.0 - 100.0 + 16.0, rel=1e-6)


def test_resolve_nesting_disjoint_polygons_are_just_unioned():
    a = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = Polygon([(20, 20), (30, 20), (30, 30), (20, 30)])

    result = resolve_nesting([a, b])
    assert result.area == pytest.approx(200.0, rel=1e-6)


def test_resolve_nesting_multipolygon_input_is_flattened():
    from shapely.geometry import MultiPolygon

    outer = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    inner = Polygon([(5, 5), (10, 5), (10, 10), (5, 10)])
    multi = MultiPolygon([outer, inner])

    result = resolve_nesting([multi])
    assert result.area == pytest.approx(400.0 - 25.0, rel=1e-6)

import ezdxf

from greenplan.geometry import hatch_to_polygon, is_effectively_closed


def _make_hatch(rings):
    doc = ezdxf.new()
    msp = doc.modelspace()
    hatch = msp.add_hatch()
    for ring in rings:
        hatch.paths.add_polyline_path(ring, is_closed=True)
    return hatch


def test_hatch_to_polygon_single_ring():
    ring = [(0, 0), (10, 0), (10, 10), (0, 10)]
    poly = hatch_to_polygon(_make_hatch([ring]))
    assert poly is not None
    assert poly.area == 100


def test_hatch_to_polygon_with_hole():
    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    inner = [(2, 2), (4, 2), (4, 4), (2, 4)]
    poly = hatch_to_polygon(_make_hatch([outer, inner]))
    assert poly is not None
    assert poly.area == 100 - 4


def test_hatch_to_polygon_no_paths_returns_none():
    poly = hatch_to_polygon(_make_hatch([]))
    assert poly is None


def test_is_effectively_closed_true_for_tiny_gap():
    pts = [(0, 0), (10, 0), (10, 10), (0, 10), (0.0001, 0.0001)]
    assert is_effectively_closed(pts)


def test_is_effectively_closed_false_for_open_ring():
    pts = [(0, 0), (10, 0), (10, 10), (0, 10), (5, 5)]
    assert not is_effectively_closed(pts)


def test_is_effectively_closed_false_for_too_few_points():
    assert not is_effectively_closed([(0, 0), (1, 1)])

import ezdxf
from ezdxf.lldxf.validator import is_binary_dxf_file
from shapely.geometry import Point

from greenplan.io.dxf_sink import append_planting_layer
from greenplan.model import PlantingPoint


def _one_point() -> list[PlantingPoint]:
    return [PlantingPoint(id="TREE-00001", geometry=Point(1.0, 2.0), plant_type="tree", rule_id="R1")]


def test_append_planting_layer_preserves_ascii_root(tmp_path):
    root = tmp_path / "root.dxf"
    ezdxf.new("R2018").saveas(str(root))  # default fmt="asc"

    out = tmp_path / "planting.dxf"
    append_planting_layer(root, _one_point(), out)

    assert not is_binary_dxf_file(str(out))
    doc = ezdxf.readfile(str(out))
    circles = [e for e in doc.modelspace() if e.dxftype() == "CIRCLE"]
    assert len(circles) == 1
    assert circles[0].dxf.layer == "GREENING_PROPOSED"


def test_append_planting_layer_preserves_binary_root(tmp_path):
    root = tmp_path / "root.dxf"
    ezdxf.new("R2018").saveas(str(root), fmt="bin")

    out = tmp_path / "planting.dxf"
    append_planting_layer(root, _one_point(), out)

    assert is_binary_dxf_file(str(out))
    doc = ezdxf.readfile(str(out))
    circles = [e for e in doc.modelspace() if e.dxftype() == "CIRCLE"]
    assert len(circles) == 1


def test_append_planting_layer_binary_root_with_proxy_graphic(tmp_path):
    """Regression test for a real ezdxf 1.4.4 bug: export_proxy_graphic()
    always hex-encodes proxy-graphic bytes for group code 310, which crashes
    BinaryTagWriter (it needs raw bytes for binary-data group codes) --
    confirmed on a real pilot object whose IMAGE entities carry proxy
    graphics, making every binary-format save fail without the workaround in
    dxf_sink._patch_proxy_graphic_export().
    """
    doc = ezdxf.new("R2018")
    circle = doc.modelspace().add_circle((0, 0), 1)
    circle.proxy_graphic = b"\x01\x02\x03" * 50
    root = tmp_path / "root.dxf"
    doc.saveas(str(root), fmt="bin")

    out = tmp_path / "planting.dxf"
    append_planting_layer(root, _one_point(), out)  # must not raise

    assert is_binary_dxf_file(str(out))
    reread = ezdxf.readfile(str(out))
    assert len(reread.modelspace()) == 2

"""DXF write-back: append the planting result to the root drawing on a
single new layer, without touching any existing entity or layer.

Ported from the prototype's full7_assemble.py, confirmed pattern: open the
root file for reading only (ezdxf.readfile), add entities to the in-memory
document, then doc.saveas() to a *different* path -- the source file on disk
is never opened for writing, so "original layers not modified" is structural,
not just a promise.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf.lldxf.validator import is_binary_dxf_file

from greenplan.model import PlantingPoint

TREE_RADIUS_M = 1.5
SHRUB_RADIUS_M = 0.35
DEFAULT_LAYER = "GREENING_PROPOSED"
XDATA_APPID = "GREENPLAN"


def _patch_proxy_graphic_export() -> None:
    """Work around an ezdxf 1.4.4 bug: ``export_proxy_graphic()`` always
    hex-encodes the proxy-graphic bytes for group code 310, which is only
    correct for the ASCII writer -- ``BinaryTagWriter.write_tag2()`` routes
    group 310 to ``_write_binary_chunks()``, which requires raw ``bytes`` and
    crashes with ``TypeError: a bytes-like object is required, not 'str'``.
    Any entity carrying proxy graphics (confirmed on real pilot data: IMAGE
    entities in "04_10001957_Старый Гай_ГЧ.dxf") makes *every* binary-format
    save fail. No newer ezdxf release fixes this (1.4.4 is current). Patching
    is safe for ASCII output too: the isinstance check below falls through to
    the original hex-string behavior unchanged.
    """
    import ezdxf.entities.dxfgfx as dxfgfx
    import ezdxf.proxygraphic as proxygraphic
    from ezdxf.lldxf import const
    from ezdxf.lldxf.tagwriter import BinaryTagWriter

    original = proxygraphic.export_proxy_graphic

    def patched(data: bytes, tagwriter, length_code: int = 160, data_code: int = 310) -> None:
        if not isinstance(tagwriter, BinaryTagWriter):
            original(data, tagwriter, length_code, data_code)
            return
        assert tagwriter.dxfversion > const.DXF12
        if len(data) == 0:
            return
        tagwriter.write_tag2(length_code, len(data))
        tagwriter._write_binary_chunks(data_code, data)

    proxygraphic.export_proxy_graphic = patched
    dxfgfx.export_proxy_graphic = patched


_patch_proxy_graphic_export()


def append_planting_layer(
    root_file: Path,
    points: list[PlantingPoint],
    out_path: Path,
    layer_name: str = DEFAULT_LAYER,
) -> tuple[int, int]:
    """Writes out_path, returns (original_entity_count, entities_added)."""
    # Preserve the root drawing's own format on save. Converting a binary
    # DXF to ASCII is not just a format choice: ezdxf's ASCII writer never
    # rewraps a string value's embedded newline characters into further
    # group-300 continuation lines, while binary DXF (whose strings are
    # simply null-terminated, no per-line length limit) has no such
    # restriction and can legitimately contain them -- confirmed on a real
    # object (MATERIAL XRECORD data in "04_10001957_Старый Гай_ГЧ.dxf")
    # whose ezdxf.readfile()+doc.saveas() *ASCII* round-trip -- with zero
    # modifications, before this fix even added a single circle -- produced
    # a value spanning several physical lines, desyncing every group code
    # after it ("Invalid group code" from ODA, unreadable in AutoCAD).
    # Round-tripping in the source's native format sidesteps that class of
    # corruption entirely.
    out_fmt = "bin" if is_binary_dxf_file(str(root_file)) else "asc"
    doc = ezdxf.readfile(str(root_file))
    msp = doc.modelspace()
    orig_count = len(msp)

    if layer_name not in doc.layers:
        doc.layers.add(name=layer_name, color=3)  # green

    if XDATA_APPID not in doc.appids:
        doc.appids.new(XDATA_APPID)

    for pt in points:
        radius = TREE_RADIUS_M if pt.plant_type == "tree" else SHRUB_RADIUS_M
        circle = msp.add_circle(
            (pt.geometry.x, pt.geometry.y),
            radius,
            dxfattribs={"layer": layer_name},
        )
        circle.set_xdata(XDATA_APPID, [(1000, pt.plant_type), (1000, pt.rule_id), (1000, pt.id)])

    doc.saveas(str(out_path), fmt=out_fmt)
    return orig_count, len(msp) - orig_count

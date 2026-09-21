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

from greenplan.model import PlantingPoint

TREE_RADIUS_M = 1.5
SHRUB_RADIUS_M = 0.35
DEFAULT_LAYER = "GREENING_PROPOSED"
XDATA_APPID = "GREENPLAN"


def append_planting_layer(
    root_file: Path,
    points: list[PlantingPoint],
    out_path: Path,
    layer_name: str = DEFAULT_LAYER,
) -> tuple[int, int]:
    """Writes out_path, returns (original_entity_count, entities_added)."""
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

    doc.saveas(str(out_path))
    return orig_count, len(msp) - orig_count

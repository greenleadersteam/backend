"""list[PlantingPoint] -> GeoJSON FeatureCollection of Point features."""

from __future__ import annotations

from shapely.geometry import mapping

from greenplan.export.geojson import NO_CRS_LABEL
from greenplan.model import PlantingPoint


def planting_points_to_geojson(points: list[PlantingPoint], crs: str = NO_CRS_LABEL) -> dict:
    return {
        "type": "FeatureCollection",
        "metadata": {"crs": crs},
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(pt.geometry),
                "properties": {
                    "id": pt.id,
                    "plant_type": pt.plant_type,
                    "rule_id": pt.rule_id,
                },
            }
            for pt in points
        ],
    }

"""list[PlantingPoint] <-> GeoJSON FeatureCollection of Point features."""

from __future__ import annotations

from shapely.geometry import mapping, shape

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
                    "kind": pt.kind,
                    "rule_id": pt.rule_id,
                },
            }
            for pt in points
        ],
    }


def geojson_to_planting_points(data: dict) -> list[PlantingPoint]:
    """Inverse of planting_points_to_geojson. Extra properties (e.g. a
    manual point's `added_in_version`) are ignored; features written before
    `kind` existed default to "auto".
    """
    return [
        PlantingPoint(
            id=feature["properties"]["id"],
            geometry=shape(feature["geometry"]),
            plant_type=feature["properties"]["plant_type"],
            rule_id=feature["properties"].get("rule_id"),
            kind=feature["properties"].get("kind", "auto"),
        )
        for feature in data["features"]
    ]

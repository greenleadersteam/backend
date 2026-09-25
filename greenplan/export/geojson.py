"""Canonical FeatureCollection -> GeoJSON.

This is the internal/API representation (per the corrected ТЗ understanding:
the required competition deliverable is a DXF write-back, handled by a later
phase's greenplan.io.dxf_sink -- GeoJSON is for inspection/the FastAPI layer).
"""

from __future__ import annotations

from shapely.geometry import mapping, shape

from greenplan.model import Feature, FeatureCollection

_KNOWN_PROPERTY_KEYS = {
    "rule_id", "category", "subtype", "status", "layer", "dxftype", "source_file", "handle",
}

NO_CRS_LABEL = "local drawing coordinates, no geo-reference available"


def feature_collection_to_geojson(fc: FeatureCollection, crs: str = NO_CRS_LABEL) -> dict:
    return {
        "type": "FeatureCollection",
        "metadata": {
            "root_file": fc.root_file,
            "source_folder": fc.source_folder,
            "source_insunits": fc.insunits_code,
            "scale_to_meters": fc.scale_to_meters,
            "crs": crs,
        },
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(f.geometry),
                "properties": {
                    "rule_id": f.rule_id,
                    "category": f.category,
                    "subtype": f.subtype,
                    "status": f.status,
                    "layer": f.layer,
                    "dxftype": f.dxftype,
                    "source_file": f.source_file,
                    "handle": f.handle,
                    **f.extra,
                },
            }
            for f in fc.features
        ],
    }


def geojson_to_feature_collection(data: dict) -> FeatureCollection:
    """Inverse of feature_collection_to_geojson -- lets a previously-saved
    parse-stage GeoJSON be fed into the zoning/layout stages via
    `greenplan plant --from-geojson`, skipping a possibly multi-minute
    re-parse of the source DXF folder.
    """
    meta = data.get("metadata", {})
    features = []
    for feat in data["features"]:
        props = feat["properties"]
        extra = {k: v for k, v in props.items() if k not in _KNOWN_PROPERTY_KEYS}
        features.append(
            Feature(
                geometry=shape(feat["geometry"]),
                category=props["category"],
                subtype=props.get("subtype"),
                rule_id=props["rule_id"],
                status=props["status"],
                layer=props["layer"],
                dxftype=props["dxftype"],
                source_file=props["source_file"],
                handle=props.get("handle"),
                extra=extra,
            )
        )
    return FeatureCollection(
        root_file=meta.get("root_file", ""),
        source_folder=meta.get("source_folder", ""),
        insunits_code=meta.get("source_insunits", 0),
        scale_to_meters=meta.get("scale_to_meters", 1.0),
        features=features,
    )

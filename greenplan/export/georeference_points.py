"""Matched geodetic reference points -- both the DXF-local label and the
geobridge.ru catalog point it was matched to -- as one WGS84 GeoJSON
FeatureCollection, for manual QA of a georeferencing fit: load both sources
in a GIS viewer and see how far apart each matched pair actually landed,
rather than trusting the fitted residual number alone.
"""

from __future__ import annotations

from shapely.geometry import Point, mapping

from greenplan.georeference.apply import WGS84_CRS_LABEL, euclidean_transform_fn, reproject_fn
from greenplan.georeference.transform import GeoreferenceResult


def georeference_points_to_geojson(geo_result: GeoreferenceResult) -> dict:
    to_utm = euclidean_transform_fn(geo_result.local_to_utm)
    to_wgs84 = reproject_fn(geo_result.utm_crs)

    features = []
    # matched_points includes the excluded label too (if any) -- keep it
    # visible in the QA output, just tagged, rather than silently vanishing.
    for label, (local_point, geobridge_point) in geo_result.matched_points.items():
        excluded = label == geo_result.excluded_label
        residual_m = geo_result.excluded_residual_m if excluded else geo_result.residuals_m.get(label)

        dxf_wgs84 = to_wgs84(to_utm(local_point))
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(dxf_wgs84),
                "properties": {
                    "label": label,
                    "source": "dxf",
                    "residual_m": residual_m,
                    "excluded": excluded,
                },
            }
        )
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(Point(geobridge_point.lng, geobridge_point.lat)),
                "properties": {
                    "label": label,
                    "source": "geobridge",
                    "residual_m": residual_m,
                    "region": geobridge_point.region,
                    "excluded": excluded,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "metadata": {
            "crs": WGS84_CRS_LABEL,
            "confidence": geo_result.confidence,
            "n_matched": len(geo_result.matched_labels),
        },
        "features": features,
    }

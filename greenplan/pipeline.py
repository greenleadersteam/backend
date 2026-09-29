"""Single entry point tying discovery -> classification -> geometry ->
canonical FeatureCollection together. Both the CLI and a future API call
`parse_folder`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from greenplan.classify.base import Classifier, ClassificationResult
from greenplan.classify.rule_based import RuleBasedClassifier
from greenplan.coverage import CoverageBuilder, CoverageReport
from greenplan.explain.builder import build_explanations
from greenplan.geometry import (
    explode_insert,
    geometry_for_matched_entity,
    log,
    scale_geometry,
    unit_scale_to_meters,
)
from greenplan.georeference.apply import euclidean_transform_fn, transform_feature_collection
from greenplan.georeference.transform import GeoreferenceResult, georeference
from greenplan.io.dxf_source import NoDxfFilesError, collect_entities, detect_root, discover_dxf_files
from greenplan.layout.engine import generate_layout
from greenplan.layout.rules import PlantingRuleSet
from greenplan.model import Feature, FeatureCollection, PlantingPoint, ZoningResult
from greenplan.norms.schema import NormsTable
from greenplan.rules.schema import RulePack
from greenplan.zoning.engine import compute_zones

DEFAULT_RULE_PACK_PATH = Path(__file__).parent / "rules" / "default.yaml"
DEFAULT_NORMS_PATH = Path(__file__).parent / "norms" / "default.yaml"
DEFAULT_PLANTING_RULES_PATH = Path(__file__).parent / "layout" / "default.yaml"


def load_default_rule_pack() -> RulePack:
    return RulePack.load(DEFAULT_RULE_PACK_PATH)


def load_default_norms() -> NormsTable:
    return NormsTable.load(DEFAULT_NORMS_PATH)


def load_default_planting_rules() -> PlantingRuleSet:
    return PlantingRuleSet.load(DEFAULT_PLANTING_RULES_PATH)


def _classify_recursive(
    entity,
    classifier: Classifier,
    coverage_builder: CoverageBuilder,
    source: str,
) -> Iterator[tuple[object, ClassificationResult]]:
    """Classify one entity; if it's an unmatched INSERT, recurse into its
    exploded children instead of giving up on the whole subtree.

    collect_entities() only ever resolves *unresolved* xrefs by name -- a
    provider that binds an xref into the document instead of leaving it
    external produces an ordinary, non-xref INSERT whose own layer/block name
    is often a generic container (e.g. "сети") that matches no rule. Without
    this recursion, that single unmatched INSERT silently hides an arbitrary
    amount of real, classifiable geometry behind it (confirmed on a real
    object: one such bound block held 114k child entities across 43 real
    layers -- gas/water/heat networks, manholes, geodetic points -- none of
    it ever reachable). Matched top-level entities (the common case) return
    immediately without incurring any explosion cost.
    """
    result = classifier.classify(entity)
    if result is not None:
        yield entity, result
        return
    if entity.dxftype() != "INSERT":
        coverage_builder.record_unmatched(entity.dxf.layer, entity.dxftype(), source)
        return
    children = list(explode_insert(entity))
    if not children:
        coverage_builder.record_unmatched(entity.dxf.layer, entity.dxftype(), source)
        return
    for child in children:
        yield from _classify_recursive(child, classifier, coverage_builder, source)


def parse_folder(
    folder: Path,
    root: Path | None = None,
    rule_pack: RulePack | None = None,
    classifier: Classifier | None = None,
) -> tuple[FeatureCollection, CoverageReport]:
    folder = folder.resolve()
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")

    files = discover_dxf_files(folder)
    if not files:
        raise NoDxfFilesError(f"No .dxf files found under {folder}")
    log(f"Discovered {len(files)} .dxf files under {folder}")

    root_file = root.resolve() if root else detect_root(files)

    entities, root_doc = collect_entities(root_file, folder)
    log(f"Collected {len(entities)} model-space entities (root + resolved xrefs)")

    scale_factor, insunits_code = unit_scale_to_meters(root_doc)
    log(f"$INSUNITS={insunits_code} -> scale factor to meters: {scale_factor}")

    rule_pack = rule_pack or load_default_rule_pack()
    classifier = classifier or RuleBasedClassifier(rule_pack)
    coverage_builder = CoverageBuilder(rule_pack)

    features: list[Feature] = []
    for entity, source in entities:
        for matched_entity, result in _classify_recursive(entity, classifier, coverage_builder, source):
            geom = geometry_for_matched_entity(matched_entity, result.expand_blocks, result.rule_id)
            if geom is None or geom.is_empty:
                continue
            geom = scale_geometry(geom, scale_factor)

            extra = {}
            if result.category == "contours" and matched_entity.dxf.is_supported("elevation"):
                elevation = matched_entity.dxf.get("elevation", None)
                if elevation is not None:
                    extra["elevation"] = elevation
            if result.category == "geodetic_points" and matched_entity.dxftype() in ("TEXT", "MTEXT"):
                label = matched_entity.plain_text().strip()
                if label:
                    extra["label"] = label

            coverage_builder.record_matched(result.rule_id)
            features.append(
                Feature(
                    geometry=geom,
                    category=result.category,
                    subtype=result.subtype,
                    rule_id=result.rule_id,
                    status=result.status,
                    layer=matched_entity.dxf.layer,
                    dxftype=matched_entity.dxftype(),
                    source_file=source,
                    handle=matched_entity.dxf.handle,
                    extra=extra,
                )
            )

    fc = FeatureCollection(
        root_file=str(root_file),
        source_folder=str(folder),
        insunits_code=insunits_code,
        scale_to_meters=scale_factor,
        features=features,
    )
    return fc, coverage_builder.build()


def georeference_feature_collection(
    fc: FeatureCollection,
    bbox: tuple[float, float, float, float],
    *,
    client,
    **georeference_kwargs,
) -> tuple[FeatureCollection, GeoreferenceResult]:
    """Fit a local<->UTM rigid transform from fc's geodetic benchmark points
    (see greenplan.georeference) and return an all-features-transformed copy
    of fc in UTM meters, ready to feed into plant_folder(), alongside the fit
    itself (kept around for its inverse transform, used to bring the
    resulting PlantingPoints back to the local frame for DXF export).

    `client` is an httpx.Client, injected so callers control its lifetime;
    `georeference_kwargs` are forwarded to georeference() (timeout, base_url,
    utm_crs, min_matched_points, residual_threshold_m).
    """
    geo_result = georeference(fc, bbox, client=client, **georeference_kwargs)
    fc_utm = transform_feature_collection(fc, euclidean_transform_fn(geo_result.local_to_utm))
    return fc_utm, geo_result


OVERTURE_CACHE_NAME = "moscow"
# Beyond the project extent: the largest building setback (10m, schools) plus
# room for a school ground that only partly overlaps the site.
OVERTURE_QUERY_MARGIN_M = 50.0
# Polygon parts farther than this from the largest one are drawing junk, not
# part of the site (seen: 13m boundary-line stubs ~21km away on Старый Гай).
_EXTENT_CLUSTER_DISTANCE_M = 500.0


def _project_extent_utm(fc: FeatureCollection) -> tuple[float, float, float, float] | None:
    """Bounds of the site boundary (else lawn) as zoning would build it, keeping
    only the polygon parts clustered around the largest one."""
    from greenplan.zoning.engine import _polygonal_union

    for category, subtype in (("site_boundary", None), ("green_existing", "lawn")):
        geoms = [f.geometry for f in fc.features if f.category == category and f.subtype == subtype]
        area = _polygonal_union(geoms) if geoms else None
        if area is None or area.is_empty:
            continue
        parts = list(area.geoms) if hasattr(area, "geoms") else [area]
        largest = max(parts, key=lambda p: p.area)
        core = [p for p in parts if p.distance(largest) <= _EXTENT_CLUSTER_DISTANCE_M]
        minx, miny, maxx, maxy = zip(*(p.bounds for p in core))
        return min(minx), min(miny), max(maxx), max(maxy)
    return None


def _whole_segment_off_ground(road_flags) -> bool:
    """A tunnel/bridge flag covering the whole segment (between is null)."""
    for flag in road_flags or []:
        if flag.get("between") is None and {"is_tunnel", "is_bridge"} & set(flag.get("values") or []):
            return True
    return False


def fuse_with_overture(
    fc_utm: FeatureCollection,
    utm_crs: str,
    cache_dir: Path,
    cache_name: str = OVERTURE_CACHE_NAME,
):
    """Merge Overture data from the local cache into a georeferenced (UTM)
    FeatureCollection: buildings (greenplan.fusion.buildings), then roads
    (greenplan.fusion.roads -- after buildings, since building footprints are
    barriers for the road faces). Each step is skipped with a warning if its
    Overture type isn't in the cache, since a missing/stale cache shouldn't
    fail a project that parsed fine. Returns (fc, {"buildings": report|None,
    "roads": report|None}).
    """
    from pyproj import Transformer
    from shapely.geometry import box

    from greenplan.fusion.buildings import EducationArea, OvertureBuilding, fuse_buildings
    from greenplan.fusion.roads import RoadSegment, fuse_roads
    from greenplan.georeference.apply import reproject_fn
    from greenplan.overture.reader import read_bbox, resolve_cache_file

    reports = {"buildings": None, "roads": None}
    extent = _project_extent_utm(fc_utm)
    if extent is None:
        log("WARN: no site boundary or lawn to derive a project extent from -- skipping Overture fusion")
        return fc_utm, reports

    m = OVERTURE_QUERY_MARGIN_M
    query_utm = box(extent[0] - m, extent[1] - m, extent[2] + m, extent[3] + m)
    to_wgs84 = Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)
    lons, lats = to_wgs84.transform(*query_utm.exterior.xy)
    bbox_wgs84 = (min(lons), min(lats), max(lons), max(lats))
    to_utm = reproject_fn("EPSG:4326", utm_crs)

    buildings_file = resolve_cache_file(cache_dir, cache_name, "building")
    if buildings_file is None:
        log(f"WARN: no usable Overture 'building' data in cache {cache_dir} -- skipping building fusion")
    else:
        path, release = buildings_file
        rows = read_bbox(
            path,
            bbox_wgs84,
            ["id", "class", "subtype", "height", "num_floors", "names.primary AS name"],
            where="is_underground IS NOT TRUE",
        )
        overture_buildings = [
            OvertureBuilding(
                id=r["id"], geometry=to_utm(g), cls=r["class"], subtype=r["subtype"],
                height=r["height"], num_floors=r["num_floors"], name=r["name"],
            )
            for r, g in rows
        ]

        education_areas: list[EducationArea] = []
        land_use_file = resolve_cache_file(cache_dir, cache_name, "land_use")
        if land_use_file is None:
            log("WARN: no Overture 'land_use' data in cache -- school/kindergarten detection uses building class only")
        else:
            lu_rows = read_bbox(
                land_use_file[0], bbox_wgs84, ["class"],
                where="subtype = 'education' AND class IN ('school', 'kindergarten')",
            )
            education_areas = [EducationArea(geometry=to_utm(g), cls=r["class"]) for r, g in lu_rows]

        fc_utm, reports["buildings"] = fuse_buildings(
            fc_utm, overture_buildings, education_areas, source_label=f"overture:{release}"
        )
        log(f"Overture building fusion ({release}): {reports['buildings'].model_dump()}")

    segment_file = resolve_cache_file(cache_dir, cache_name, "segment")
    if segment_file is None:
        log(f"WARN: no usable Overture 'segment' data in cache {cache_dir} -- skipping road fusion")
    else:
        path, release = segment_file
        rows = read_bbox(path, bbox_wgs84, ["id", "class", "road_flags"], where="subtype = 'road'")
        segments = [
            RoadSegment(id=r["id"], geometry=to_utm(g), cls=r["class"])
            for r, g in rows
            if not _whole_segment_off_ground(r["road_flags"])
        ]
        fc_utm, reports["roads"] = fuse_roads(fc_utm, segments, query_utm, source_label=f"overture:{release}")
        log(f"Overture road fusion ({release}): {reports['roads'].model_dump()}")

    return fc_utm, reports


def plant_folder(
    fc: FeatureCollection,
    norms: NormsTable | None = None,
    planting_rules: PlantingRuleSet | None = None,
    verbose: bool = False,
) -> tuple[ZoningResult, list[PlantingPoint], list[dict]]:
    """Zoning + layout + explanations, on top of an already-parsed
    FeatureCollection (from parse_folder(), or loaded from a previously
    saved GeoJSON via export.geojson.geojson_to_feature_collection()).
    """
    norms = norms or load_default_norms()
    planting_rules = planting_rules or load_default_planting_rules()

    zoning = compute_zones(fc, norms, verbose=verbose)
    points = generate_layout(zoning, fc, planting_rules, verbose=verbose, norms=norms)
    explanations = build_explanations(points, planting_rules)
    return zoning, points, explanations

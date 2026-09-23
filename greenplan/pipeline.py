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
    points = generate_layout(zoning, fc, planting_rules, verbose=verbose)
    explanations = build_explanations(points, planting_rules)
    return zoning, points, explanations

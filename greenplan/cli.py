from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

import typer

from greenplan.export.geojson import feature_collection_to_geojson, geojson_to_feature_collection
from greenplan.export.planting import planting_points_to_geojson
from greenplan.export.zones import zoning_result_to_geojson
from greenplan.io.dxf_sink import append_planting_layer
from greenplan.layout.rules import PlantingRuleSet
from greenplan.norms.schema import NormsTable
from greenplan.pipeline import (
    load_default_norms,
    load_default_planting_rules,
    load_default_rule_pack,
    parse_folder,
    plant_folder,
)
from greenplan.rules.schema import RulePack

app = typer.Typer(help="greenplan: DXF master-plan parsing and greening-layout generation.")


@app.command()
def parse(
    folder: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="Project delivery folder to scan for .dxf files"
    ),
    root: Optional[Path] = typer.Option(None, help="Main drawing file (auto-detected if omitted)"),
    rules: Optional[Path] = typer.Option(None, help="Rule pack YAML to use instead of the built-in default"),
    out: Path = typer.Option(Path("output.geojson"), help="Output GeoJSON path"),
    report: Optional[Path] = typer.Option(None, help="Optional JSON coverage report path"),
) -> None:
    """Parse a DXF project delivery folder into a canonical GeoJSON FeatureCollection."""
    rule_pack = RulePack.load(rules) if rules else load_default_rule_pack()
    fc, coverage = parse_folder(folder, root=root, rule_pack=rule_pack)

    out.write_text(json.dumps(feature_collection_to_geojson(fc), ensure_ascii=False, indent=2))
    typer.echo(f"Wrote {len(fc.features)} features to {out}", err=True)

    typer.echo("\nCoverage by requirement:", err=True)
    for rule in coverage.rules:
        flag = "" if rule.count else "  <-- NOT FOUND"
        typer.echo(f"  [{rule.rule_id:>2}] {rule.name_ru} ({rule.status}): {rule.count} features{flag}", err=True)

    if coverage.unmatched_layers:
        typer.echo(f"\nUnmatched layers ({len(coverage.unmatched_layers)}), by entity count:", err=True)
        for layer in coverage.unmatched_layers[:20]:
            dxftypes = ", ".join(layer.dxftypes)
            typer.echo(
                f"  {layer.layer}: {layer.entity_count} entities ({dxftypes}) e.g. {layer.sample_source_file}",
                err=True,
            )

    if report:
        report.write_text(json.dumps(coverage.to_dict(), ensure_ascii=False, indent=2))
        typer.echo(f"Wrote coverage report to {report}", err=True)


@app.command()
def plant(
    folder: Optional[Path] = typer.Argument(
        None, exists=True, file_okay=False,
        help="Project delivery folder to scan for .dxf files (omit if using --from-geojson)",
    ),
    from_geojson: Optional[Path] = typer.Option(
        None, help="Load a previously-saved `greenplan parse` GeoJSON instead of re-parsing the DXF folder"
    ),
    root: Optional[Path] = typer.Option(
        None, help="Main drawing file (auto-detected if omitted); ignored with --from-geojson"
    ),
    rules: Optional[Path] = typer.Option(None, help="Parser rule pack YAML override"),
    norms: Optional[Path] = typer.Option(None, help="Setback-norms YAML override"),
    planting_rules_path: Optional[Path] = typer.Option(
        None, "--planting-rules", help="Planting-pattern YAML override"
    ),
    out_dxf: Path = typer.Option(Path("planting.dxf"), help="Output DXF path (root DXF + one new planting layer)"),
    out_zones_geojson: Path = typer.Option(Path("zones.geojson"), help="Output GeoJSON of allowed/prohibited zones"),
    out_planting_geojson: Path = typer.Option(Path("planting.geojson"), help="Output GeoJSON of planting points"),
    out_explanation: Path = typer.Option(Path("explanation.json"), help="Output JSON of per-point explanations"),
    verbose: bool = typer.Option(
        False, "-v", "--verbose",
        help="Print extra zoning/layout diagnostics to stderr (intermediate areas, per-category "
             "buffered areas, curb length, candidate counts at each layout filtering step)",
    ),
) -> None:
    """Parse (or load) a project delivery, compute allowed/prohibited planting zones, lay
    out new trees/shrubs, and export DXF + GeoJSON + explanation results."""
    if from_geojson is None and folder is None:
        raise typer.BadParameter("Pass either FOLDER or --from-geojson")

    if from_geojson is not None:
        data = json.loads(from_geojson.read_text(encoding="utf-8"))
        fc = geojson_to_feature_collection(data)
        typer.echo(f"Loaded {len(fc.features)} features from {from_geojson}", err=True)
    else:
        rule_pack = RulePack.load(rules) if rules else load_default_rule_pack()
        fc, _coverage = parse_folder(folder, root=root, rule_pack=rule_pack)
        typer.echo(f"Parsed {len(fc.features)} features from {folder}", err=True)

    norms_table = NormsTable.load(norms) if norms else load_default_norms()
    planting_rule_set = (
        PlantingRuleSet.load(planting_rules_path) if planting_rules_path else load_default_planting_rules()
    )

    zoning, points, explanations = plant_folder(
        fc, norms=norms_table, planting_rules=planting_rule_set, verbose=verbose
    )

    typer.echo(
        f"\nZoning: {len(zoning.prohibited)} prohibited-zone group(s); site boundary "
        f"{'found' if zoning.used_site_boundary else 'NOT found -- using the full lawn extent'}",
        err=True,
    )
    if zoning.uncovered_categories:
        typer.echo(
            f"  WARN: {len(zoning.uncovered_categories)} category/subtype(s) have no setback norm "
            f"and were not treated as an obstacle: {zoning.uncovered_categories}",
            err=True,
        )

    by_rule = Counter(p.rule_id for p in points)
    typer.echo(f"\nPlanting: {len(points)} point(s) total", err=True)
    for rule in planting_rule_set.rules:
        typer.echo(f"  [{rule.id}] {rule.name_ru}: {by_rule.get(rule.id, 0)}", err=True)

    out_zones_geojson.write_text(json.dumps(zoning_result_to_geojson(zoning), ensure_ascii=False, indent=2))
    typer.echo(f"\nWrote zones to {out_zones_geojson}", err=True)

    out_planting_geojson.write_text(json.dumps(planting_points_to_geojson(points), ensure_ascii=False, indent=2))
    typer.echo(f"Wrote planting points to {out_planting_geojson}", err=True)

    out_explanation.write_text(json.dumps(explanations, ensure_ascii=False, indent=2))
    typer.echo(f"Wrote explanations to {out_explanation}", err=True)

    orig_count, added = append_planting_layer(Path(fc.root_file), points, out_dxf)
    typer.echo(f"Wrote DXF to {out_dxf} ({orig_count} original entities unchanged, {added} new)", err=True)


if __name__ == "__main__":
    app()

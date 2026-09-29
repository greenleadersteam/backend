# greenplan — ЛЦТ-2026 automated greening-plan tool

## What this is

Hackathon submission (ЛЦТ-2026, run by Moscow's ДПиООС) for a service that reads a
messy, non-georeferenced DXF master plan of a street/site, figures out where new
trees/shrubs are regulatorily allowed to go (setback distances from underground
utilities, curbs, existing trees per 743-ПП), lays them out following basic
landscaping patterns, and writes the result back into DXF plus GeoJSON/JSON reports.
Full brief: `../ТЗ/3. Департамент природопользования и охраны окружающей среды.pdf`.

**Hard requirements from the ТЗ** (easy to underestimate from a casual skim):
- Deliverable is a **DXF → DXF round trip**: read the root drawing, add exactly one
  new layer (`GREENING_PROPOSED`) with the result, leave every existing entity/layer
  byte-identical. GeoJSON is the internal/API representation, not the primary
  deliverable.
- Every planting decision needs a citation to a specific normative act (743-ПП,
  СП 42.13330.2016, 623-ПП/МГСН) — interpretability is scored, not optional.
- CLI-only is explicitly fine for the whole competition; a graphical editor is not
  required. Docker packaging is mandatory. Swagger/OpenAPI is only required *if* a
  backend/HTTP API exists (relevant to the API work this doc is being written for).
- Target runtime is МосТех.ОС (Ubuntu-like Linux) — no Windows-only assumptions.

**Current status:** the CLI pipeline (parse → zone → lay out → export) and the FastAPI
wrapper are both complete and have been validated end-to-end against real pilot
objects — including a full run of all 20 pilot objects through the live API (not just
CLI smoke tests). See "API layer" for the structured-error-reporting design and the
real-world findings that came out of that run (most notably: `ambiguous_root_dxf` is
common, not an edge case, once "Исходные данные" is pointed at as a whole rather than a
hand-picked single delivery subfolder).

Since that milestone, two more features have landed as real package code (not just ad
hoc scripts): **point-based DXF georeferencing** (mandatory for every API-created
project; optional via CLI `--bbox`) and an **Overture Maps data cache** (buildings/
roads/land-use/etc. for Moscow, fetched via DuckDB straight to GeoParquet). See
"Georeferencing" and "Overture data cache" below. The Overture cache is *populated*
but **not yet consumed by zoning** — that integration (using it for a soil/non-soil
"obviously excluded" tier ahead of the existing 743-ПП setback buffering) is designed
but not implemented; see that section's "not yet done" note.

Most recently: **versioned planting plans with manual edits** (API only, for the web
app's editor) -- see "Planting versions".

## Where things are

- `greenplan/` — the installable package (Poetry, `pyproject.toml` in this directory).
  `greenplan = "greenplan.cli:app"` is registered as a console script.
- `tests/` — pytest suite, 161 tests, all passing (`.venv/bin/python -m pytest tests/ -q`).
- `../Пилотный проект 20 улиц/` — the full pilot dataset, all 20 objects, each with a
  real, usable `Исходные данные`-equivalent source-data folder (naming varies:
  `Исходные данные`/`ИСХОДНЫЕ ДАННЫЙ`/`Исходные данный`; object 2 has no such folder
  at all, its source-equivalent is `Генеральный план редформат`). This **replaced** an
  earlier, incomplete `../Данные/` copy (3 objects only, now deleted) — if anything
  references `../Данные/`, it's stale, update it to point here instead. Two objects
  are still genuinely non-viable from source: object 11 (Фрунзенская набережная) has
  no `.dxf`/`.dwg` at all; object 3 (3-я Парковая) has real content but its source
  folder bundles enough independent delivery packages that root detection can't
  resolve it in one pass (see "Known data landmines" #12). See "Known data landmines"
  below before pointing the CLI at any object's full `Исходные данные` folder.
- `../НПА/`, `../ТЗ/` — source regulation documents and the brief itself.
- `/home/pavel/.claude/projects/-home-pavel-data---------------------------------------2026-backend/memory/`
  — auto-memory with the fuller narrative history (`project_lct2026_task.md`,
  `project_greenplan_architecture.md`, `project_lct2026_dataset.md`,
  `project_lct2026_georeferencing.md`, `project_lct2026_overture_cache.md`). This file
  is a condensed, current-state snapshot; memory has more of the "why" and session
  history. Note: `dxf_summary.py`, `dxf_to_geojson.py`, `georeference_plan.py`,
  `download_overture.py` in this directory are the **original ad hoc prototypes** for
  what are now real `greenplan.georeference`/`greenplan.overture` package modules (see
  "Georeferencing"/"Overture data cache" below) — they're left in place as historical
  reference/standalone scratch tools, but the package modules are the actual
  implementation now, not these scripts.
- `../ci/` — a **separate git repo** (sibling to this one, `git@github.com:greenleadersteam/ci.git`),
  holding the shared `docker-compose.yml`/`Caddyfile` for the whole deployed stack
  (`backend` + `frontend` + a static `site`, all behind one Caddy instance). See
  "Deployment" below — this used to live inside this repo, it was split out once the
  frontend repo existed too.

## Package layout

```
greenplan/
  model.py            canonical schema: Feature, FeatureCollection, ZoningResult,
                       ProhibitedZone, PlantingPoint (pydantic, arbitrary_types_allowed
                       for shapely geometries)
  io/
    dxf_source.py       discover_dxf_files, detect_root (graph-based: references
                         others, isn't itself referenced; falls back to a
                         self-contained-root heuristic when nothing has *unresolved*
                         xrefs, only auto-picks when that narrows to exactly one
                         candidate), scoped xref resolution, collect_entities.
                         RootDetectionError carries `.candidates` (the ranked list,
                         not just a log message) and NoDxfFilesError is now a distinct
                         type -- both exist so the API layer can classify failures by
                         type instead of parsing exception text.
    dxf_sink.py          append_planting_layer: writes the result DXF (single
                         GREENING_PROPOSED layer, circle per point, XDATA tag with
                         plant_type/rule_id/point id). Opens the root file read-only,
                         saves to a *different* path -- source is never mutated.
  geometry.py           DXF entity -> shapely geometry (HATCH via ezdxf.path.from_hatch,
                        INSERT block explosion, polygon validity repair via
                        shapely.make_valid -- see repair_polygon()). TEXT/MTEXT ->
                        Point at the entity's insertion point (added for
                        georeferencing -- see below; every other entity type was
                        already handled, this one wasn't, so a labeled geodetic
                        benchmark point's catalog-ID text was invisible to the whole
                        pipeline until this was added)
  classify/             Classifier ABC + RuleBasedClassifier (regex rule packs) +
                        CompositeClassifier (seam for a future ML fallback, unused)
  rules/default.yaml     layer/block-name -> category/subtype regex rules (the
                         parser's extensibility point for new data-provider dialects).
                         Patterns are deliberately *not* full-string-anchored (a past
                         bug: 3 rules used to be `^...$`-anchored and silently stopped
                         matching once a provider bound an xref instead of leaving it
                         external, which renames the child layer to
                         "<original>|Category" or "<original>$0$Category" -- common in
                         this dataset). Includes a `geodetic_points` rule (id "90",
                         outside the ТЗ's 1-20 numbering) purely so benchmark-point
                         layers are tracked/exported instead of falling into
                         "unmatched" -- not a zoning input, but load-bearing for
                         point-based DXF georeferencing (see "Georeferencing" below --
                         real package code now, not just an ad hoc scratch-script
                         feature).
  coverage.py            per-rule match counts + unmatched-layer report (the feedback
                         loop for extending rules/default.yaml)
  norms/default.yaml     setback distances: (obstacle_category, obstacle_subtype)
                        -> {tree_m, shrub_m, citation}. Source of truth is the
                        project's rule table (Google Sheet, link in memory
                        `reference_setback_rules_sheet.md`), cross-checked against
                        743-ПП табл. 3.6.1/3.6.2, СП 42 табл. 9.1, МГСН 1.02-02 табл.
                        9.1. Ranges -> upper bound; "-" shrub -> tree value. Some
                        subtypes (school buildings, street categories, power-line
                        voltages) have no producer yet -- intended for Overture fusion
  zoning/
    engine.py             compute_zones(): lawn ∩ site_boundary -> base_area; buffer
                          each norms-covered obstacle category, subtract from
                          allowed[plant_type]; grouped ProhibitedZone per category
    topology.py            _polygonal_union's real machinery -- see "Known data
                          landmines", this module exists entirely because of them
  layout/default.yaml     declarative planting patterns (row-along-curb, grid-fill;
                        spacing/offset per rule)
  layout/engine.py        generate_layout(): places points inside allowed[plant_type],
                        mutual-spacing against earlier-placed same-type points
  explain/builder.py      short per-point explanation records (id, plant_type, kind,
                        rule_id, rule_name_ru, x, y; for edited planting versions
                        also moved/displacement_m/added_in_version/zone_check/note,
                        see "Planting versions") -- always in the DXF's own local
                        frame, even for a georeferenced project (paired with
                        planting.dxf, which must stay local -- see "Georeferencing").
                        Detailed "why is this area allowed" lives once in
                        zones.geojson's prohibited features, not repeated per point
  export/                 FeatureCollection/ZoningResult/PlantingPoint[] -> GeoJSON;
                        geojson_to_feature_collection() is the inverse (enables
                        `plant --from-geojson`). georeference_points.py: matched
                        geodetic reference points (DXF-local + geobridge, both
                        reprojected to WGS84) as one FeatureCollection tagged by
                        `source`/`excluded` -- manual-QA export for a georeferencing
                        fit, see "Georeferencing"
  georeference/            point-based DXF georeferencing -- see "Georeferencing"
                        below. geobridge.py (HTTP client), points.py (extract
                        labeled points from a FeatureCollection), transform.py (match
                        + rigid-transform fit + auto-outlier-exclusion),
                        apply.py (apply a fitted transform/reprojection to
                        FeatureCollection/ZoningResult/PlantingPoint[])
  overture/                Overture Maps cache population -- see "Overture data
                        cache" below. client.py (DuckDB fetch-to-GeoParquet),
                        cache.py (manifest/freshness bookkeeping), reader.py
                        (per-project bbox reads from the cache)
  fusion/buildings.py      merge plan buildings with Overture footprints -- see
                        "Overture fusion" below
  pipeline.py              parse_folder() and plant_folder(), the two orchestration
                        entry points; load_default_* helpers for each YAML config.
                        _classify_recursive() is the fix for a real blind spot: a
                        *bound* (non-xref) INSERT whose own layer/block name matches no
                        rule used to hide 100k+ real child entities behind it
                        unexplored (confirmed on a real object). Now, an unmatched
                        INSERT is exploded and each child classified individually by
                        its own layer, recursively -- matched top-level entities (the
                        common case) still short-circuit with zero extra cost.
                        georeference_feature_collection() is the georeferencing
                        entry point (see "Georeferencing").
  cli.py                   typer app: `parse` and `plant` commands
  cli_overture.py          typer sub-app (`greenplan overture ...`) -- see "Overture
                        data cache" below. A cache-maintenance tool, not invoked by
                        `parse`/`plant`.
  api/                     FastAPI wrapper over pipeline.py -- see "API layer" below
```

## CLI usage

```bash
cd backend  # this directory; venv at .venv/

# Parse only -> canonical GeoJSON + coverage report
.venv/bin/python -m greenplan.cli parse "<project delivery folder>" \
  --out parsed.geojson --report coverage.json

# Full pipeline: zoning + layout + DXF/GeoJSON/explanation export
.venv/bin/python -m greenplan.cli plant "<project delivery folder>" \
  --out-dxf planting.dxf --out-zones-geojson zones.geojson \
  --out-planting-geojson planting.geojson --out-explanation explanation.json \
  [-v]   # extra zoning/layout diagnostics to stderr

# Skip re-parsing (parsing a large object can take minutes) by reusing a saved parse:
.venv/bin/python -m greenplan.cli plant --from-geojson parsed.geojson --out-dxf ...

# Georeference first (see "Georeferencing" below) -- optional for the CLI, unlike the
# API where it's mandatory. Omit --bbox for byte-for-byte-identical local-frame output.
.venv/bin/python -m greenplan.cli plant "<folder>" --bbox 37.55,55.75,37.62,55.80 \
  --max-residual-m 1.0 --out-geo-points geo_points.geojson --out-dxf ... [other outputs]

# Populate/inspect the shared Overture cache (see "Overture data cache" below) --
# NOT part of parse/plant, a separate cache-maintenance command:
.venv/bin/python -m greenplan.cli overture fetch   # all defaults: Moscow bbox, latest release
.venv/bin/python -m greenplan.cli overture status
```

Both `parse`/`plant` commands accept `--root <file>` (manual root-drawing override when
auto-detection is ambiguous), `--rules <yaml>` (parser rule-pack override), and `--bbox`
(triggers georeferencing). `plant` additionally takes `--norms <yaml>`,
`--planting-rules <yaml>`, `--max-residual-m` and `--out-geo-points` (both only
meaningful with `--bbox`).

**Always point at the single project-delivery folder, not a parent that bundles
several deliveries** — e.g. use
`Пилотный проект 20 улиц/1. Олимпийская деревня/Исходные данные/10000176_Генплан_Олимп - Standard`,
not `Пилотный проект 20 улиц/1. Олимпийская деревня/Исходные данные` (which also
contains two more, largely-duplicate-or-genuinely-separate copies of the delivery
under sibling folders). The pipeline is now robust to duplicated content if you do
point at the wider folder (see below), but it's slower and was the direct cause of
several real bugs already found this way — including, confirmed via a full live-API
run across all 20 objects, `detect_root()` hitting `ambiguous_root_dxf` for **16 of
20** objects when pointed at the full `Исходные данные` folder (a same-run
client-side retry that excludes the losing candidate and re-uploads resolved 12 of
those 16; landmine #12 below). `--root <file>` is the CLI escape hatch when this
happens.

## `zones.geojson` output shape

One `FeatureCollection`, every feature tagged `zone_type`:
- `site_boundary`, `lawn_raw`, `base_area` — diagnostic extents (site boundary and
  lawn as extracted, and their intersection) for telling which pipeline stage a
  discrepancy comes from.
- `allowed` (one per `plant_type`: tree/shrub) — the final plantable polygon.
- `prohibited` (grouped by obstacle category/subtype × plant_type, ~15-20 features) —
  each carries `distance_m`, `citation`, `reason`.

## Known data landmines (read before touching zoning/geometry code)

Real bugs found by testing against actual pilot-object data, not hypothetical —
each blocked or silently corrupted a real run. Fixes are in place; understand them
before "fixing" something that looks like a duplicate of one of these:

1. **GEOS buffer perf: union-then-buffer is catastrophically slow.**
   `union(many points).buffer(d)` took >2 minutes (didn't finish) vs. `union([p.buffer(d)
   for p in points])` at ~2s for a few thousand points. Always buffer individual
   geometries first, union second (`zoning/engine.py`).
2. **GEOS buffer perf: sub-millimeter geometry noise hangs indefinitely.** An
   exploded block's decorative hatch/texture symbol can contain ~100+ segments each
   under 1mm long; buffering that made a single `.buffer()` call hang. Fixed at two
   layers: `geometry.py` drops negligible parts (<1μm length / <1e-9 m² area) when
   unioning exploded-block children; `zoning/engine.py` additionally snaps every
   obstacle geometry to a 1cm grid (`shapely.set_precision`) before any union/buffer.
3. **Provider-specific lawn-layer naming.** The real plantable-lawn source is a
   *zone-fill-diagram* HATCH layer, not the raw topo-survey layer (which is mostly
   decorative grass-symbol ticks with near-zero real area). Confirmed 3 different
   naming dialects so far: `_ГЗН-ГЗН` (Багрицкого), `!Project_hatch road grass`
   (Песчаный переулок). `rules/default.yaml`'s `green_existing`/`lawn` subtype
   pattern is the place to add more as new objects surface them.
4. **A boundary/lawn polygon split across multiple entities with small seam gaps.**
   `shapely.ops.polygonize()` requires *exact* endpoint coincidence; a real
   site-boundary was split across two `LWPOLYLINE`s with a 1.09m gap at one seam,
   silently dropping the entire (large) fragment. Fixed by
   `zoning/topology.py:close_line_soup()`, which snaps nearby line endpoints
   (default tolerance 1.5m) before polygonizing.
5. **Polygon-with-holes reconstruction (even-odd nesting).** A site boundary is
   routinely one big polygon with several smaller "hole" loops (real exclusions,
   confirmed by the user). `zoning/topology.py:resolve_nesting()` builds this
   properly: a polygon fully inside another becomes a hole of its *immediate*
   enclosing polygon; a polygon nested one level deeper (inside a hole) is solid
   again (an island), recursively. Degrades to a plain union when nothing is nested,
   so it's safe to apply generically (also used for `lawn`).
6. **Duplicated source folders corrupt nesting, not just waste time.** Pointing at a
   parent folder that bundles multiple copies of the same delivery (common in this
   dataset — see `project_lct2026_dataset.md`) produces 4-6 near-identical copies of
   the same boundary/hole. This isn't just redundant: it actively breaks nesting
   three ways (duplicate hole copies make `Polygon()` construction invalid; duplicate
   root copies silently re-fill holes a single copy carved out correctly; near-dup
   pairs can get misclassified as outer+hole, producing a degenerate sliver that
   crashes `unary_union` with `TopologyException: side location conflict`).
   `zoning/topology.py:_dedupe_near_identical()` collapses IoU ≥ 0.98 polygon groups
   to one representative *before* any nesting logic runs (STRtree-accelerated,
   ~0.02s for 1700 polygons — safe to always run, not just for known-duplicated
   inputs). A separate size-ratio guard (`_MAX_HOLE_TO_EXTERIOR_AREA_RATIO = 0.95`)
   is a secondary defense for near-but-not-quite-duplicate pairs.
7. **`$INSUNITS` (DXF unit header) can simply be wrong.** Песчаный переулок declares
   millimeters but its content was very likely authored in meters — every category's
   coordinates land in an implausible sub-1m range once the declared scale factor is
   applied, while every other tested object (declared meters) looks physically
   correct. Left un-"fixed" deliberately (trusting declared metadata is the safer
   default for a general tool) — flagged here so it isn't mistaken for a new bug.
8. **Root detection reads every candidate file in full.** `detect_root()` must
   `ezdxf.readfile()` every discovered `.dxf` to find xref block names before it can
   pick a root — pointing at a wide folder multiplies this cost for no benefit
   (reinforces landmine 6's "point at the single delivery" guidance).
9. **Full-string-anchored rule patterns silently stop matching bound-xref layers.**
   A provider that binds an xref instead of leaving it external renames the child
   layer to `"<original>|Category"` or `"<original>$0$Category"` (confirmed common
   across this dataset). Three rules (`buildings`, `red_lines`, `contours`) used to be
   `^...$`-anchored and broke against this, silently, while every other unanchored
   rule in the pack kept working right next to them — easy to miss precisely because
   most of the file "already handles" this style of name. Fixed by dropping the
   anchors; if you add a new rule, don't anchor it either unless you have a specific
   reason to.
10. **A bound (non-xref) container INSERT can hide an arbitrary amount of real data.**
    `collect_entities()` only ever resolves *unresolved* xrefs by name; a provider
    that binds instead produces an ordinary INSERT whose own layer/block name is often
    a generic container (e.g. `"сети"`) matching no rule — so it was silently skipped
    whole, no matter how much real content sat inside it (confirmed on a real object:
    114k child entities across 43 real layers, including underground utilities and
    geodetic points, all invisible). Fixed in `pipeline.py:_classify_recursive()` —
    see "Package layout" above.
11. **`detect_root()` can't find a candidate at all when everything is already bound.**
    The primary heuristic (references others, isn't referenced) requires at least one
    file with *unresolved* xrefs; a delivery where every xref was bound in has none,
    so the candidate list comes back empty even when there's an obvious single root
    sitting next to a couple of empty/trivial files. Fixed with a Tier-2 fallback:
    among unreferenced files, if exactly one has real content (`entity_counts > 0`),
    pick it; if two or more genuinely independent content-bearing files exist (a real,
    confirmed case — a delivery with no xref web between its genplan and dendroplan at
    all), it correctly stays ambiguous rather than guessing between them.
12. **`ambiguous_root_dxf` is the default outcome, not an edge case, once you point at
    the whole `Исходные данные` folder.** Confirmed via a full live-API run across all
    20 objects: 16 of 20 hit it. Two recurring shapes: (a) two *alternate* top-level
    assemblies wrapping ~the same tile pool (e.g. a genplan and a
    dendroplan-on-masterplan file both referencing 98% the same xref names — picking
    either gives materially the same result), and (b) a main design delivery vs. a
    separate `ИП_`/"изыскания" survey delivery (genuinely different content, no safe
    way to auto-resolve by name alone — a folder-path-based "deprioritize ИП_" rule
    looks appealing but breaks when the *whole* source delivery happens to be
    ИП_-prefixed, which happens; filename-based matching is safer but still not
    universal). A "prefer dendro in the name, else pick first" client-side retry
    (exclude the losing root, re-upload — re-upload now correctly clears stale state
    first, see landmine #13) resolved 12 of the 16 in one pass; the other 4 either had
    no dendro/name signal at all or surfaced a *new* candidate after exclusion (a file
    that was only reachable via the one just removed), which a single retry pass can't
    resolve by design.
13. **A re-upload used to resurrect files the new zip didn't even contain.**
    `run_processing_job` extracted straight into the existing `raw/`/`processed/`
    directories without clearing them first; since `zipfile.extractall()` only ever
    adds/overwrites and never removes, a file present in an earlier failed attempt but
    deliberately excluded from a corrected re-upload would silently linger and keep
    influencing processing — found live, via the landmine #12 retry loop, as an
    excluded `ambiguous_root_dxf` candidate mysteriously reappearing on retry. Fixed:
    `run_processing_job` now `shutil.rmtree`s both directories before extracting.
14. **geobridge.ru's geodetic-point catalog is crowdsourced and NOT globally unique by
    ID.** `GET {base_url}/title={label}` returns a base64-encoded JSON array (confirmed
    live, not documented) of every point whose title *contains* the query -- a
    country-wide substring search. Two real, confirmed problems this causes: (a)
    catalog numbers collide across regions (querying "1763" returns 27 candidates
    across 8 regions) and even *within* one region (two different physical points both
    titled "15521" in Московская область, 13km apart) -- this is why georeferencing
    requires a caller-supplied bbox (`bbox_user`) to filter candidates, not just an
    exact title match; a label with zero or multiple bbox-filtered candidates is
    treated as unmatched, never guessed. (b) Each raw candidate carries real third-party
    PII (a `rating` array with reviewer name/phone/email) -- stripped immediately on
    parse in `georeference/geobridge.py`, only `{title, lat, lng, region}` ever survive
    into a `GeobridgePoint`; never log/persist the raw response.
15. **Overture's STAC index is broken (confirmed 2026-09-26/27, both this project's own
    testing and an independent user report in `OvertureMaps/overturemaps-py` issue
    #145).** Every row in `stac.overturemaps.org/{release}/collections.parquet` has a
    null `collection` column -- the exact field any STAC-based file-selection query
    filters on -- so a STAC-indexed query can never match any type, at any release,
    for anyone, right now. Not a bug in this project's code or a release-freshness
    issue; `greenplan/overture/client.py` doesn't use Overture's STAC index at all,
    by design, for this reason -- see landmine #16 for what it uses instead.
16. **GDAL's GPKG writer is catastrophically slow for millions of features; GeoParquet
    is not, for the same data.** Confirmed live: fetching all of Moscow's `building`
    type (~3M rows) via `COPY ... TO 'x.gpkg' (FORMAT GDAL, DRIVER 'GPKG')` took over 2
    hours (genuinely still progressing the whole time, not hung -- diagnosed via
    `/proc/<pid>/io` sampled twice to confirm real, if slow, disk-write growth); the
    *exact same query* to `(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000)`
    finished in under 11 minutes and verified fully valid (0 null/empty/invalid
    geometries, 0 duplicate IDs, real GeoParquet spec metadata). Root cause: GPKG is a
    SQLite database and GDAL has to build a full R-tree spatial index for the whole
    layer, independent of source format or network speed; GeoParquet has no such index
    to build. This is *why* `greenplan/overture/client.py` writes GeoParquet, not
    GPKG -- don't "fix" it back to GPKG without re-reading this. A separate, now-moot
    finding from before this was diagnosed: an earlier implementation that fetched via
    DuckDB but converted to a GeoDataFrame in Python (`.arrow()`/`to_pandas()`/
    `shapely.from_wkb()`, then `gdf.to_file(driver="GPKG")`) stalled twice at
    whole-Moscow scale with S3 connections stuck in `CLOSE-WAIT` and zero forward
    progress for 30+ minutes -- that specific code path no longer exists (replaced by
    a single direct `COPY ... TO ... (FORMAT PARQUET)` SQL statement, no
    pyarrow/geopandas round-trip), so this was never conclusively separated from the
    GPKG-writer slowness above, but is the reason `client.py` is a single plain SQL
    query rather than anything that materializes a GeoDataFrame in between.

## Testing

`.venv/bin/python -m pytest tests/ -q` — 161 tests, all synthetic/
unit-level (no real DXF files, no real network -- `georeference`'s geobridge client and
`overture`'s DuckDB/S3 fetch are both untested by this suite, since they need real
network access; the API tests use a throwaway empty DXF from `ezdxf.new()` plus
synthetic geodetic-point TEXT entities for the georeferencing path, not real
pilot-object data). Real-object validation is done ad hoc: via the CLI against
individual objects, and — more thoroughly — via direct HTTP calls against a live
`uvicorn` instance for the whole 20-object dataset at once (see "API layer"'s
real-world-findings note). There's still no automated *pytest* integration fixture
using real DXFs, for the CLI or the API — the ad hoc real-object runs aren't committed
as regression tests, so a future change could silently reintroduce something they'd
have caught. Same gap now also applies to `georeference`/`overture`: real-network
verification so far is entirely ad hoc (see "Georeferencing"/"Overture data cache").

## Georeferencing

Point-based DXF georeferencing (`greenplan/georeference/`): extract labeled geodetic
benchmark points from a parsed `FeatureCollection`, look each up on geobridge.ru,
fit a rigid 2D transform (rotation + translation, **scale locked to 1.0** --
`skimage.transform.EuclideanTransform`, chosen specifically because it has no scale
parameter, unlike `SimilarityTransform` -- both sides are already real physical
meters, so a free scale would just absorb point-matching noise into a parameter that
should be exactly 1), and use it to re-express results in real-world coordinates.
Validated precedent before this became package code: 3 points, Makeeva object,
0.11-0.19m residuals (see `project_lct2026_georeferencing.md`).

**Mandatory for every API-created project, optional for the CLI.** `POST /projects`
requires `bbox_user` (no default in `ProjectCreateRequest`); the CLI's `parse`/`plant`
take an optional `--bbox` and produce byte-for-byte-identical local-frame output if
it's omitted. This is a deliberate difference, not an oversight -- "mandatory" is a
product decision made by whoever operates the API/frontend, not something the
underlying pipeline itself needs.

**Where the transform is applied — zoning/layout are untouched, only the CRS
changes.** `parse_folder`/`plant_folder`/`zoning/engine.py`/`layout/engine.py`/
`io/dxf_sink.py` have **zero code changes** for this feature and don't know
georeferencing exists. The actual flow (`pipeline.georeference_feature_collection`,
called from `cli.py`/`api/jobs.py`):
1. Parse as always → local-frame `FeatureCollection`.
2. Fit the rigid transform (local ↔ **EPSG:32637, UTM zone 37N** -- Moscow's zone,
   hardcoded, not auto-selected; all 20 pilot objects fall in it).
3. Apply the *forward* transform to the whole `FeatureCollection` → zoning/layout run
   on this UTM-frame copy. Every distance calculation in those modules (setback
   buffers, mutual spacing, `close_line_soup`'s tolerance, the 1cm snap grid) is
   translation/rotation-invariant, so this changes the numbers' magnitude, not the
   geometry relationships those modules reason about -- confirmed safe, not just
   assumed (float64 leaves 10+ significant digits of precision at UTM-scale
   coordinates, plenty for cm-level snapping).
4. **DXF export** (`dxf_sink.append_planting_layer`) needs the *inverse* transform
   applied to the resulting `PlantingPoint`s first -- it writes into the *existing*
   root drawing, which must stay in that drawing's own local frame. `explanation.json`
   is built from these same inverse-transformed (local-frame) points, deliberately --
   it's paired with the DXF, not the GeoJSON.
5. **GeoJSON export** (`zones.geojson`/`planting.geojson`/`parsed.geojson`) gets a
   plain CRS reprojection (UTM → WGS84 via `pyproj`, no rigid-fit math) on top of the
   UTM-frame zoning/layout results.

**Point matching is deliberately conservative, not best-effort.** geobridge.ru's
search is a country-wide substring match with real ID collisions (see landmine #14)
-- a label resolves to a real coordinate only on an *unambiguous*, bbox-filtered exact
(or normalized-fallback) title match; anything else is treated as unmatched rather
than guessed, since fitting against one wrong point is worse than fitting with fewer
points.

**Auto-outlier-exclusion (`transform.py:fit_rigid_transform`)**, added after a real
4-point fit (Старый Гай object) failed its residual threshold because of exactly one
bad point: with **4+** matched points, if the full-set fit's worst residual exceeds
`residual_threshold_m`, a leave-one-out search (try excluding each label once, keep
whichever single exclusion minimizes the worst remaining residual -- not just assumed
to be the full-fit's worst-residual point) gets one retry. Deliberately capped: never
excludes more than one point, and never attempts this with exactly 3 points (excluding
one would only leave 2, downgrading to the weaker `"unvalidated"` tier instead of
actually validating -- not a fair substitute for a failed check). The excluded point
stays visible in `matched_points`/the diagnostic export (`export/georeference_points.py`),
tagged `excluded: true`, never silently dropped.

**Confidence tiers**: exactly 2 matched points → `"unvalidated"` (no redundancy to
check against, by construction). 3+ points with worst residual within
`residual_threshold_m` (default 1.0m, `Settings.georeference_residual_threshold_m` /
CLI `--max-residual-m`) → `"validated"`. Otherwise → `ResidualTooHighError`
(→ API `UploadErrorCode.INSUFFICIENT_GEODETIC_POINTS`/`GEOREFERENCE_SERVICE_ERROR`,
see "API layer").

**Not done / open items:** no automated test exercises the real geobridge.ru network
call (client is injectable, but nothing in CI hits the real endpoint); the `land`/
`land_cover` Overture-cross-check idea and the fuller "soil vs non-soil" zone
hierarchy this was originally motivated by are separate, not-yet-implemented work
(see "Overture data cache").

## Overture data cache

`greenplan/overture/` populates a **shared, infrequently-refreshed local cache** of
Overture Maps data (buildings/roads/land-use/etc. for Moscow) -- explicitly **not** a
live per-project fetch (see landmines #15/#16 for why: Overture's STAC index is
broken, and even the unindexed path is only fast enough for an occasional refresh, not
a request-time query). `greenplan overture fetch`/`overture status`
(`cli_overture.py`) are cache-maintenance commands, run manually or on a schedule --
**not** invoked by `parse`/`plant`, and **not yet consumed by zoning at all** (see
below).

**Deliberately minimal implementation** (`client.py:fetch_type_to_parquet`): one
direct DuckDB SQL query per type --
`COPY (SELECT * EXCLUDE (sources) FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme={theme}/type={type}/*', hive_partitioning=1) WHERE bbox...) TO out.parquet (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000)`
-- no STAC, no pyarrow/geopandas round-trip through Python, no private-API reach-ins.
This replaced a more elaborate first attempt (overturemaps-py's private
`_prepare_query()`, then later a DuckDB-to-GeoDataFrame-to-GPKG path) after real
testing showed the extra machinery wasn't the fix it looked like -- see landmine #16.
`DEFAULT_CACHE_TYPES` = `building`, `connector`, `infrastructure`, `land`, `land_use`,
`segment`, `water` (curated, not `core.get_all_overture_types()` -- address/place/
division*/bathymetry/building_part have no consumer here; `land_cover` is a known type
with the same slow-partition shape as `segment` and no current consumer, excluded from
the default set but still fetchable via `--types`).

`cache.py` is pure manifest bookkeeping (format-agnostic, doesn't care whether the
cached file is `.parquet` or anything else): `latest_fetch_per_type()` reads every
`{name}-manifest-{release}.json` in a cache directory and picks each type's most
recent *successful* fetch regardless of release, so `overture status`/`--max-age-days`
freshness checks work across repeated refreshes at different releases over time.

**Row group size (50,000) is a deliberate tuning choice, not a default left alone**:
this cache exists to be *queried later* by small, per-project bboxes, and Parquet
readers skip whole row groups via bbox min/max stats -- smaller row groups give
finer-grained pruning for that access pattern, at a modest write-time/file-size cost.

**Docker/deployment**: designed to live under the existing `backend_data` volume
(`/data/overture_cache`, a subdirectory of a volume that's already mounted and already
persists across image rebuilds) -- refreshed via
`docker compose run --rm backend python -m greenplan.cli overture fetch ...`, fully
decoupled from `docker compose build`/deploys. **Not yet wired up** as an actual
`Settings.overture_cache_dir` field or Docker volume entry -- this is the intended
design, not yet-implemented state.

**Now partly consumed: buildings only** -- see "Overture fusion" below. The rest of
this paragraph is still true for everything else.

**Not done / open items (the big one):** none of this is consumed by `zoning/engine.py`
yet. The motivating design (a 4-tier zone hierarchy -- site boundary → soil/non-soil
hard exclusion using Overture buildings/land_use/parking/kerbs → today's existing
743-ПП setback buffering → an undefined 4th "suitability" tier) was discussed in depth
but is entirely unimplemented; `compute_zones()` still computes `base_area` as
`lawn ∩ site_boundary`, unchanged. Also open: a road/lane polygon-synthesis idea
(classify closed faces from kerb+segment line-soup via `polygonize`, validated
conceptually against real data but not built), and `segment`/`land_cover`'s ~128-huge-
file partitions are untested via the current simplified `client.py` (the old,
STAC-vs-no-STAC-branching implementation found these slow; unknown whether the
current plain-DuckDB-to-Parquet approach fares better or worse for them specifically).

## Overture fusion

`pipeline.fuse_with_overture(fc_utm, utm_crs, cache_dir)` runs right after
georeferencing (CLI `parse`/`plant` with `--bbox`, `--overture-cache` default
`data/overture_cache`; API job always, `Settings.overture_cache_dir`, default
`data_dir/overture_cache` = `/data/overture_cache` in Docker). It needs the UTM frame,
so there's no fusion without georeferencing. Missing/unusable cache data → a warning
and fc unchanged, never a job failure. Zoning/layout are unchanged: fused buildings
are ordinary `buildings` features, buffered by the existing norms.

Query extent = site boundary (else lawn) as zoning builds it, keeping only polygon
parts within 500m of the largest one (Старый Гай has 13m boundary stubs ~21km away,
which made a naive extent pull in 43k buildings instead of ~100), plus 50m margin.

Buildings (`fusion/buildings.py`), agreed design:
1. Plan polygon → kept; the best-overlapping Overture footprint only adds attributes
   (`overture_id`, class, floors, height, name in `extra`).
2. Overture footprint not covered by plan polygons → added (`dxftype="OVERTURE"`),
   `status` = `overture_confirmed` (plan wall lines within 2m along ≥30% of its outline)
   or `overture_unconfirmed` (kept anyway -- excluding space is the safe side).
3. Plan wall lines/points → kept unchanged (surveyed wall positions).
4. `subtype="school_kindergarten"` (10m norm) from Overture building class, or, for
   *unclassified* buildings ≥300m², ≥50% inside an Overture education land-use area of
   that class. The 300m² floor is empirical: on Старый Гай, school grounds held 12-113m²
   verandas/sheds, real school/kindergarten buildings were 900-1850m².

Measured on Старый Гай: 107 footprints in extent, 9 redundant with plan polygons, 20
confirmed, 78 unconfirmed -- but 55 of those are >20m from *any* plan building, i.e.
outside the surveyed strip rather than conflicting with it. 7 school/kindergarten
buildings, none within 10m of the plantable area. Zoning effect: tree-allowed area
1955 → 1895 m² (−3%), planting points 1179 → 1161.

Open: no "outside survey coverage" vs "inside but absent" distinction yet; fusion
stats aren't surfaced in `job.yaml`/API responses (logged only); roads/other types
not fused yet.

## API layer

FastAPI wrapper over `pipeline.parse_folder()`/`plant_folder()`, file-based (no
database) — `greenplan/api/`:

```
api/
  config.py    Settings (pydantic-settings, env prefix GREENPLAN_API_): data_dir,
               max_concurrent_jobs (default 2), max_upload_mb (default 300 -- raised
               from 100). A full "Исходные данные" folder zipped as-is (every file,
               DWG duplicates/textures/reports included) ran up to 587MB for one real
               object; a client should always strip non-.dxf content before zipping,
               since the pipeline never reads it anyway (`discover_dxf_files`/
               `resolve_xref` only ever glob `*.dxf`) -- doing that got every one of
               the 20 real objects' zips under 300MB in practice (the largest observed
               was ~160MB), though there's no hard guarantee a future object couldn't
               still exceed it.
  storage.py   ProjectRecord (metadata.yaml: id/name/description/timestamps/
               deleted_at/bbox_user) + ProjectStore, an in-memory cache built once at
               startup by scanning disk, written through on every mutation.
               `bbox_user` is optional *at this storage layer* (None default) even
               though `ProjectCreateRequest.bbox_user` is required -- deliberately,
               so ~50+ pre-existing project folders from before this field existed
               don't crash ProjectStore.load() at startup (same reasoning as
               JobRecord's legacy-string-error upgrade below)
  jobs.py      JobRecord (job.yaml: stage/progress_pct/error/georeference/timestamps;
               `error` is a structured JobError -- {code, message, candidates} -- not
               a bare string, see UploadErrorCode design decision below) +
               run_processing_job() (the actual extract -> parse_folder ->
               georeference (if bbox_user is on record) -> plant_folder ->
               DXF/GeoJSON export unit of work, must stay a top-level picklable
               function; clears raw/processed dirs before every extraction, see
               landmine #13) + run_version_export_job() (DXF/explanation for
               an edited planting version, see "Planting versions") + JobManager
               (concurrency-limit gate + submits to a concurrent.futures.Executor)
  plantings.py versioned planting plans (processed/plantings/{n}/): version
               metadata, applying edits, v1 creation -- see "Planting versions"
  schemas.py   request/response pydantic models
  app.py       create_app() factory (route handlers + lifespan startup/shutdown);
               module-level `app` is the production entrypoint
  _yamlio.py   tiny atomic-write-yaml helper shared by storage.py and jobs.py
```

Run it: `.venv/bin/uvicorn greenplan.api.app:app --reload` (Swagger UI at `/docs`,
satisfying the ТЗ's "API implies OpenAPI" requirement for free via FastAPI).

Key design decisions (see the conversation that produced this for the full
reasoning):
- **Two-step project creation.** `POST /projects` (metadata only, status `draft`)
  then `POST /projects/{id}/upload` (raw zip bytes as the body, *not* multipart —
  metadata already went in step one, so the upload endpoint needs nothing else).
  Re-upload is allowed only from `draft` or `failed`; `queued`/processing/`ready`
  reject with 409.
- **Metadata cache vs. live job status are separate files on purpose.**
  `metadata.yaml` (name/description/timestamps/soft-delete) is cached in memory at
  startup and only ever written by the API process. `job.yaml` (processing
  stage/progress/error) is always read fresh from disk, never cached — it's the
  thing a worker subprocess writes to, so caching it in the parent process would
  mean teaching that process to notice out-of-band file changes for no real
  benefit (job.yaml is tiny; a fresh read costs nothing).
- **No fine-grained percentage.** Progress is 6 coarse stages (extracting/parsing/
  georeferencing/zoning_layout/exporting/ready, plus queued/failed/draft) that reuse
  `parse_folder`/`plant_folder`'s existing call boundary — not a smooth bar. Real
  sub-stage percentages would require threading a progress callback through
  `zoning/engine.py`/`layout/engine.py`; deliberately not done for v1.
- **Concurrency gate rejects, never queues.** `JobManager.try_acquire()` is checked
  in the upload endpoint *before* a single byte of the (up to 300MB) body is read,
  so a 429 while at the limit costs ~nothing. The executor itself is injectable
  (`create_app(executor_factory=...)`) — production defaults to
  `ProcessPoolExecutor` (so a crashing/hanging DXF takes down its own worker
  process, not the API), tests inject a `ThreadPoolExecutor` so the *real* pipeline
  still runs, just cheaper/more deterministic under pytest. `job_fn` (and
  `export_fn`, for planting-version exports) are similarly injectable, used by tests that need a controllable fake job body (e.g. to force
  a 429 deterministically) without touching the real pipeline.
- **Soft delete is a hard 404 everywhere** (list and get-by-id both), not a visible
  `deleted` flag; files stay on disk for manual recovery only.
- **Zip extraction has explicit zip-slip protection** (`jobs._safe_extract_zip`,
  raising `UnsafeArchiveError` on a `"../"` path-traversal attempt) since the archive
  is an untrusted upload.
- **Upload failures are a structured `JobError` (`{code, message, candidates}`), not a
  bare exception string.** `UploadErrorCode` is now a 6-value enum (grew from 4 when
  georeferencing became mandatory):
  `bad_archive` (not a zip, or corrupted -- `zipfile.BadZipFile` or `UnsafeArchiveError`),
  `no_dxf_found` (`NoDxfFilesError`), `ambiguous_root_dxf` (`RootDetectionError`, whose
  `.candidates` get relativized to the zip root -- **never** the server's absolute
  path -- before being returned), `insufficient_geodetic_points`/
  `georeference_service_error` (see "Georeferencing" -- too few/unreliable geobridge
  matches vs. geobridge itself being unreachable, `httpx.HTTPError`), and `other`
  (everything else, by exception repr).
  `jobs._classify_error()` dispatches by exception *type*, not string-matching, and
  imports the DXF-specific exception types lazily inside itself for the same reason
  `run_processing_job` already deferred its own imports (keeps `ezdxf` out of the
  lightweight API process that imports this module on every status-check request --
  verified: importing `jobs`+`schemas` alone loads zero `ezdxf`/`shapely`/`geopandas`).
  A separate `400` (not a `JobError` -- this is a synchronous request-validation
  failure, before any job exists) covers an empty/missing upload body. `JobRecord`
  also carries a `field_validator` that upgrades a legacy plain-string `error` (the
  pre-this-change format) into `JobError(code=other, ...)` on read, since old
  `job.yaml` files with the bare-string format already existed on disk and
  `reconcile_interrupted_jobs` reads *every* project's `job.yaml` at startup — one
  unreadable old record used to crash startup for every project, not just that one
  (found live, restarting the dev server against real accumulated test data).
- **No server-side way to resolve `ambiguous_root_dxf` yet** — unlike the CLI's
  `--root`, there's no request field to hint at the intended root and reprocess.
  `candidates` is returned precisely so a client *could* act on it, but today that
  means rebuilding and re-uploading a trimmed zip, not a lightweight retry call. A
  `root_hint`-style field on upload/reprocess is the natural next step; not done yet.
- **CORS is on** (`CORSMiddleware`, `config.py`'s `cors_origins` — comma-separated
  env var `GREENPLAN_API_CORS_ORIGINS`, plain string not JSON so it's a single
  docker-compose `environment:` line). Default covers the production frontend
  (`https://app.greenleaders.online`) plus common local dev-server ports
  (Vite/CRA/Angular/webpack, both `localhost` and `127.0.0.1` — distinct origins
  to a browser). The raw-body upload endpoint isn't a CORS-"simple" request (its
  Content-Type isn't form/text), so it always triggers a preflight `OPTIONS` —
  without this middleware the frontend upload would fail before ever reaching the
  server.
- **Real-world validated, not just unit-tested:** all 20 pilot objects have been run
  through the live API end-to-end (create → zip `Исходные данные` as .dxf-only →
  upload → poll to terminal), not just against synthetic fixtures. Result: 15/20
  `ready`, 4/20 stuck on `ambiguous_root_dxf` even after one exclude-and-retry pass
  (landmine #12), 1/20 (object 11) genuinely has no DXF at all. This is real signal,
  not noise — see landmines #9-13 above, all four found this way, not by reasoning
  about the code. **Still not done:** none of this is a committed *pytest*
  integration test (see "Testing"), and there's no `root_hint` capability (see above).

## Planting versions

Planting plans are versioned so users can edit them (add/move/delete/retype trees
and shrubs in the web app). Agreed with the frontend developer; the frontend enables
it behind its `plantingEdits` flag. Code: `api/plantings.py` (storage + edit logic),
routes in `api/app.py`, export in `jobs.run_version_export_job`, tests in
`tests/api/test_plantings.py`.

**Endpoints** (under `/projects/{id}`, `ready` projects only, else 404 like the
other result endpoints):
- `GET /plantings` — `PlantingVersion` metadata list: `id` (int), `name`, `kind`
  (`auto`/`manual`), `created_at`, `based_on`, `counts` {tree, shrub, total},
  `export_status` (`none`/`pending`/`ready`/`failed`), `export_error`.
- `GET /plantings/{n}` — GeoJSON (WGS84), same shape as `/planting`.
- `POST /plantings/{n}/edit` — body `{name?, add: [{client_id, plant_type, lon, lat}],
  update: [{id, lon?, lat?, plant_type?}], delete: [id]}` → `201`
  `{version, id_map: {client_id: assigned id}}`. Always creates a *new* version
  based on `n` (editing an old version appends; history is linear).
- `GET /plantings/{n}/dxf`, `GET /plantings/{n}/explanation`.
  DXF is served as `image/vnd.dxf` with `Content-Disposition: attachment`, named
  `<project name> — версия N.dxf` (name sanitized in `app._dxf_filename`; non-ASCII
  via RFC 5987 `filename*`). CORS exposes `Content-Disposition` so a cross-origin
  frontend can read that name.
- `/planting`, `/dxf`, `/explanation` (the pre-versioning endpoints) serve the
  **latest** version -- a deliberate choice, so the old frontend keeps working
  (before any edit, latest = v1).

**Rules:**
- Auto-point ids are identical in every version (the frontend computes "moved by
  X m"/"restore" from them). Manual ids are `manual-NNNNN` from a project-wide
  counter (`plantings/counter.yaml`) and are never reused, even after deletion.
- Each point carries `kind` (`auto`/`manual`) in GeoJSON and as a 4th XDATA value
  in the DXF (appended last, so the old 3-value layout still reads). Manual points
  have `rule_id: null` and `added_in_version`. Everything stays on the single
  `GREENING_PROPOSED` layer (ТЗ: exactly one new layer).
- Edits are validated all-or-nothing: 422 in FastAPI's own `{loc, msg, type}`
  shape (raised as `RequestValidationError`, so schema errors and edit-rule errors
  look the same to the client), with edit-specific `type`s `empty_edit`,
  `unknown_id`, `duplicate_id`, `duplicate_client_id`, `update_delete_conflict`,
  `outside_project_bbox` (`bbox_user` + ~1km margin), `coordinates_not_finite`,
  `lon_lat_pair`, `nothing_to_update`. **No zone check on the backend** -- zone validation is frontend-only
  for now; explanations say so (`zone_check: "not_checked"`).
- `explanation.json` stays a **plain list** (the current frontend parses it as
  `ExplanationEntry[]`); version info lives in `/plantings` metadata, not a header.
  A moved or retyped auto point keeps its `rule_id` but gets `moved`/
  `displacement_m` (vs. v1, in drawing-frame meters, 1cm noise threshold)/
  `original_plant_type` and a note that the rule no longer guarantees placement.

**Storage** (`processed/plantings/`): `{n}/metadata.yaml` + `planting.geojson` +
`planting.dxf` + `explanation.json`. A version number is claimed by `mkdir` of its
folder, under a per-project lock (single API process); `metadata.yaml` is written
last, a folder without it is an unfinished save and ignored. `metadata.yaml` is read
fresh, never cached -- the export worker updates `export_status`. v1 is copied from
`processed/planting.*` at the end of `run_processing_job`; projects processed before
versioning get v1 lazily on first access (with `kind: auto` backfilled). Edits run
on raw GeoJSON dicts, so `plantings.py` imports no shapely/pyproj (verified: the API
process still loads none of shapely/ezdxf/pyproj/skimage/geopandas).

**DXF/explanation export for versions 2+** needs the georeferencing transform,
which only existed during processing -- so `run_processing_job` now writes
`processed/export_context.yaml` (`root_file` relative to `raw/`, `frame`
`utm`/`local`, `utm_crs`, `utm_to_local` 3x3 matrix). `run_version_export_job`
converts WGS84 → UTM → drawing frame and calls the same `append_planting_layer`.
It shares the `JobManager` concurrency limit with processing: started on save if a
slot is free, otherwise on the first `/dxf` or `/explanation` request. Responses:
`200` file; `202` `{version, export_status: pending}` + `Retry-After` while
generating (poll `/plantings`); `429` no free slot; `409 transform_unavailable` for
projects processed before `export_context.yaml` existed (only their v1 has a DXF);
`500` with `export_error` if it failed (not retried automatically). A `pending`
export orphaned by a restart is reset to `none` at startup
(`plantings.reconcile_interrupted_exports`), so it's retried on next request.

**OpenAPI is the frontend's contract** (it generates TS types from it), so it's
kept complete: every status code declared with a schema, GeoJSON under
`application/geo+json` (`PlantingFeatureCollection`), DXF under `image/vnd.dxf` (the IANA-registered type),
errors under `application/json`. Gotcha: FastAPI files every `responses={...:
{"model": ...}}` entry under the *route's* media type, so the GeoJSON/DXF routes use
`response_class=Response` (no media type) and declare their 200 content by hand --
see the comment above `_GEOJSON_CONTENT` in `app.py`;
`test_openapi_documents_media_types_and_export_statuses` guards this.

**Not done / open:** not yet run against a real pilot object -- each export re-reads
the whole root drawing (minutes and GBs for the large objects), only measured for
the original processing so far. No backend zone check, no renaming/deleting versions.
Reprocessing a `ready` project isn't possible today; if it becomes possible, it must
decide what happens to existing manual versions (reprocessing clears `processed/`).

## Deployment

**`docker-compose.yml`/`Caddyfile` no longer live in this directory** — they moved to
a separate sibling repo, `../ci/` (`git@github.com:greenleadersteam/ci.git`), once the
frontend repo existed too and a shared multi-service compose file made more sense one
level up. This repo now only has `Dockerfile` (single-stage `python:3.12-slim`,
`pip install .`, non-root, one uvicorn worker — see the file for why more than one
worker is unsafe here) and its own `DEPLOY.md` (backend-specific concerns only). See
`../ci/DEPLOY.md` for the full-stack picture: three services now sit behind one Caddy
instance on the same VM -- `backend` (this repo), `frontend`, and a static `site`
(`greenleaders.online` root) -- each with its own `.github/workflows/deploy.yml`
that resets its checkout over SSH and runs `docker compose up -d --build <service>`
from `../ci/`.

**`../ci/`'s own three files are explicitly *not* auto-deployed by anything** — unlike
the three app/site repos, `ci/DEPLOY.md` documents them as "plain, hand-maintained
files on the VM." This is a real, still-open gap: a `Caddyfile`/`docker-compose.yml`
change committed and pushed to `../ci/` does **not** reach the VM on its own --
someone has to manually sync it (`git pull` or similar) and then explicitly
`docker compose restart caddy`/`caddy reload` (a bind-mounted file changing on disk
does **not** make Caddy re-read it, and `docker compose up -d` alone only recreates a
container whose *service definition* changed, not one whose *mounted file's content*
changed). This has already caused a real, confirmed incident: a header-hardening
Caddyfile change was committed and presumed live, but the running server had never
actually picked it up. The fix that's actually needed -- giving `ci/` its own
`.github/workflows/deploy.yml` like the other three repos have -- is designed
(SSH + `git reset --hard` + an explicit `docker compose restart caddy`) but **not yet
implemented**.

Recent `../ci/` changes (Caddyfile/docker-compose.yml, done this session): CSP now
allows `server.arcgisonline.com` (img-src/connect-src, for the frontend's "Геопривязка"
Esri satellite-imagery module); `Strict-Transport-Security` added and `Referrer-Policy`/
`Permissions-Policy` values corrected (these had been silently not-live on the server
for the reason above); `config.json` -- the frontend's runtime config, fetched at
`/config.json` -- is now served from `../ci/config/config.json` via a dedicated
`handle /config.json { root * /srv/config }` Caddy block, **not** from the frontend
build's own `public/config.json` -- this makes the production config an ops-owned,
`ci`-repo-tracked value (same pattern already used for the PMTiles basemap archive),
so changing it never needs a server-side hand edit or a frontend rebuild.

`.dockerignore` in this repo is an allowlist (`greenplan/` + `pyproject.toml` only) —
this directory accumulates large ad hoc artifacts (real exports, a local `data/` from
manual API testing) that must never enter a build context.

Real-data memory sizing (see session history / ask if this doc feels stale):
measured peak RSS via `/usr/bin/time -v` on the two largest locally-usable pilot
objects at the time — 1.33GB (Багрицкого, 124MB delivery) and 3.70GB (Олимпийская
деревня, 259MB) — is now **stale as an upper bound, not just as a "3 of 20 tested"
caveat**. All 20 objects have since been run successfully end-to-end via the live API
(see "API layer"), several with larger accepted `Исходные данные` zips than either of
those two RSS-measured runs (`max_upload_mb` is now 300, was 100 when this note was
written) — but RSS was **not** re-measured for any of those larger runs. Memory scaled
worse than linearly with input size on the original two data points, so don't assume
the old 3.70GB figure still bounds the worst case; re-measure before trusting the
**8GB RAM recommended VM size** conclusion below at the new, larger scale. With the
default `GREENPLAN_API_MAX_CONCURRENT_JOBS=2`, worst-case concurrent load at the
*old* numbers is close to 8GB; 4GB only worked if concurrency was dropped to 1, and
even then had little margin.

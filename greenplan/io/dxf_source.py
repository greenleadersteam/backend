"""Discover, root-detect and read a folder of DXF files (one project delivery).

Ported from the original backend/dxf_to_geojson.py prototype, including the
scoped-xref-resolution fix: xref resolution is deliberately confined to the
root drawing's own directory subtree, never widened to a search_root that
bundles multiple project deliveries side by side (a file matching an xref's
name can genuinely exist only in a sibling delivery with no ambiguity to
warn about, and silently pulling it in mixes in a different local drawing
origin -- confirmed in practice on real data).
"""

from __future__ import annotations

import sys
from pathlib import Path

import ezdxf
import ezdxf.recover


class RootDetectionError(SystemExit):
    """Raised when the root drawing can't be unambiguously auto-detected."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def is_under_paxheader(path: Path) -> bool:
    return any(part.lower() == "paxheader" for part in path.parts)


def discover_dxf_files(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.rglob("*.dxf")
        if p.is_file() and not is_under_paxheader(p)
    )


def load_doc(path: Path):
    try:
        return ezdxf.readfile(str(path))
    except ezdxf.DXFStructureError:
        doc, _auditor = ezdxf.recover.readfile(str(path))
        return doc


def get_xref_block_names(doc) -> set[str]:
    return {b.name for b in doc.blocks if b.block.is_xref}


def detect_root(files: list[Path]) -> Path:
    """Pick the file that references other files but isn't itself referenced.

    Raises RootDetectionError (a SystemExit) with a ranked candidate list
    printed to stderr when detection is ambiguous, so the caller can prompt
    for --root instead of guessing.
    """
    xrefs_by_file: dict[Path, set[str]] = {}
    for f in files:
        try:
            xrefs_by_file[f] = get_xref_block_names(load_doc(f))
        except Exception as ex:
            log(f"WARN: could not open {f}: {ex}")

    referenced_stems = {name.lower() for refs in xrefs_by_file.values() for name in refs}
    candidates = [
        f for f, refs in xrefs_by_file.items()
        if refs and f.stem.lower() not in referenced_stems
    ]

    if len(candidates) == 1:
        log(f"Root drawing detected: {candidates[0]}")
        return candidates[0]
    if not candidates and len(files) == 1:
        return files[0]

    log("Could not unambiguously detect the root drawing. Candidates:")
    for f in candidates or files:
        log(f"  {f}")
    raise RootDetectionError("Pass --root <file> to pick the main drawing explicitly.")


def resolve_xref(name: str, root_file: Path) -> Path | None:
    name_lower = name.lower()
    base = root_file.parent
    candidates = sorted(
        (p for p in base.rglob("*.dxf") if p.stem.lower() == name_lower and not is_under_paxheader(p)),
        key=lambda p: len(p.relative_to(base).parts),
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        log(f"WARN: multiple files named '{name}' found, picking the shallowest:")
        for c in candidates:
            log(f"  {'-> ' if c == candidates[0] else '   '}{c}")
    return candidates[0]


def collect_entities(root_file: Path, search_root: Path):
    """Return (list of (entity, source_label) from model space only, root doc).

    search_root is only used to detect (and warn about) the case where the caller
    pointed at a folder wider than the root drawing's own directory -- xref resolution
    itself is always scoped to root_file.parent (see resolve_xref).
    """
    if root_file.parent.resolve() != search_root.resolve():
        log(f"NOTE: given folder ({search_root}) is wider than the root drawing's own "
            f"directory ({root_file.parent}); xref resolution is scoped to the latter "
            f"only, so support files that only exist elsewhere under the given folder "
            f"(e.g. a sibling project delivery) will show as 'not found'. Point the "
            f"folder argument directly at {root_file.parent} for full resolution.")

    root_doc = load_doc(root_file)
    msp = root_doc.modelspace()
    xref_block_names = get_xref_block_names(root_doc)

    # Xref-wrapper INSERTs are structural (they just pull in an external file's
    # content); their own block name often describes the whole referenced drawing
    # and can spuriously match a rule meant for actual symbol blocks. Keep them for
    # resolution below, but don't emit them as features themselves.
    entities = [
        (e, root_file.name) for e in msp
        if not (e.dxftype() == "INSERT" and e.dxf.name in xref_block_names)
    ]

    resolved_cache: dict[Path, object] = {}

    for e in msp:
        if e.dxftype() != "INSERT" or e.dxf.name not in xref_block_names:
            continue
        resolved = resolve_xref(e.dxf.name, root_file)
        if resolved is None:
            log(f"WARN: xref '{e.dxf.name}' not found under {search_root}, skipping")
            continue
        ext_doc = resolved_cache.get(resolved)
        if ext_doc is None:
            try:
                ext_doc = load_doc(resolved)
            except Exception as ex:
                log(f"WARN: failed to load xref file {resolved}: {ex}")
                continue
            resolved_cache[resolved] = ext_doc

        matrix = e.matrix44()
        source_label = f"{resolved.name} (via {e.dxf.name})"
        for ee in ext_doc.modelspace():
            try:
                ee2 = ee.copy()
                ee2.transform(matrix)
            except Exception:
                continue
            entities.append((ee2, source_label))

    return entities, root_doc

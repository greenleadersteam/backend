import pytest

import greenplan.io.dxf_source as dxf_source


def test_resolve_xref_scoped_to_root_dir_not_sibling_project(tmp_path):
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"
    (project_a / "sub").mkdir(parents=True)
    project_b.mkdir()

    root_file = project_a / "root.dxf"
    root_file.touch()
    (project_a / "sub" / "Foo.dxf").touch()
    (project_b / "Foo.dxf").touch()  # same-named file in a sibling delivery

    resolved = dxf_source.resolve_xref("Foo", root_file)
    assert resolved == project_a / "sub" / "Foo.dxf"


def test_resolve_xref_picks_shallowest_when_multiple_matches(tmp_path):
    (tmp_path / "sub").mkdir()
    root_file = tmp_path / "root.dxf"
    root_file.touch()
    shallow = tmp_path / "Foo.dxf"
    deep = tmp_path / "sub" / "Foo.dxf"
    shallow.touch()
    deep.touch()

    assert dxf_source.resolve_xref("Foo", root_file) == shallow


def test_resolve_xref_not_found_returns_none(tmp_path):
    root_file = tmp_path / "root.dxf"
    root_file.touch()
    assert dxf_source.resolve_xref("Missing", root_file) is None


def test_detect_root_single_unambiguous_candidate(tmp_path, monkeypatch):
    root_file = tmp_path / "root.dxf"
    child_file = tmp_path / "child.dxf"
    root_file.touch()
    child_file.touch()

    monkeypatch.setattr(dxf_source, "load_doc", lambda path: path)
    monkeypatch.setattr(
        dxf_source, "get_xref_block_names",
        lambda doc: {"child"} if doc == root_file else set(),
    )

    assert dxf_source.detect_root([root_file, child_file]) == root_file


def test_detect_root_single_file_with_no_xrefs(tmp_path, monkeypatch):
    only_file = tmp_path / "only.dxf"
    only_file.touch()

    monkeypatch.setattr(dxf_source, "load_doc", lambda path: path)
    monkeypatch.setattr(dxf_source, "get_xref_block_names", lambda doc: set())

    assert dxf_source.detect_root([only_file]) == only_file


def test_detect_root_raises_with_multiple_candidates(tmp_path, monkeypatch):
    a = tmp_path / "a.dxf"
    b = tmp_path / "b.dxf"
    a.touch()
    b.touch()

    monkeypatch.setattr(dxf_source, "load_doc", lambda path: path)
    monkeypatch.setattr(dxf_source, "get_xref_block_names", lambda doc: {"something_else"})

    with pytest.raises(SystemExit):
        dxf_source.detect_root([a, b])


class _FakeDocWithEntityCount:
    """Stand-in for an ezdxf Document exposing just what detect_root's Tier-2
    (self-contained-root) heuristic reads: modelspace() -> something with len().
    """

    def __init__(self, n_entities: int):
        self._n = n_entities

    def modelspace(self):
        return list(range(self._n))


def test_detect_root_self_contained_root_with_empty_sibling(tmp_path, monkeypatch):
    """A root with everything already bound in (no *unresolved* xrefs at all) has
    an empty xref set, so it's invisible to the primary heuristic (which requires
    refs to be non-empty). If it's the only file among several that actually
    carries content, it should still be picked automatically rather than forcing
    a --root prompt for a case with only one sensible answer.
    """
    root_file = tmp_path / "root.dxf"
    empty_aux = tmp_path / "empty_aux.dxf"
    root_file.touch()
    empty_aux.touch()

    docs = {root_file: _FakeDocWithEntityCount(50), empty_aux: _FakeDocWithEntityCount(0)}
    monkeypatch.setattr(dxf_source, "load_doc", lambda path: docs[path])
    monkeypatch.setattr(dxf_source, "get_xref_block_names", lambda doc: set())

    assert dxf_source.detect_root([root_file, empty_aux]) == root_file


def test_detect_root_stays_ambiguous_with_two_independent_self_contained_files(tmp_path, monkeypatch):
    """Two genuinely independent, self-contained files with real content each
    (no xref link either way -- confirmed to occur in practice, e.g. a separate
    genplan and dendroplan delivered with no xref web between them) must stay
    ambiguous rather than the Tier-2 fallback silently picking one.
    """
    a = tmp_path / "genplan.dxf"
    b = tmp_path / "dendroplan.dxf"
    a.touch()
    b.touch()

    docs = {a: _FakeDocWithEntityCount(10), b: _FakeDocWithEntityCount(10)}
    monkeypatch.setattr(dxf_source, "load_doc", lambda path: docs[path])
    monkeypatch.setattr(dxf_source, "get_xref_block_names", lambda doc: set())

    with pytest.raises(SystemExit):
        dxf_source.detect_root([a, b])


def test_discover_dxf_files_ignores_extension_case(tmp_path):
    (tmp_path / "ПЛАН.DXF").touch()
    (tmp_path / "b.Dxf").touch()
    (tmp_path / "c.dwg").touch()
    assert [p.name for p in dxf_source.discover_dxf_files(tmp_path)] == ["b.Dxf", "ПЛАН.DXF"]


def test_resolve_xref_ignores_extension_case(tmp_path):
    root_file = tmp_path / "root.dxf"
    root_file.touch()
    (tmp_path / "Foo.DXF").touch()
    assert dxf_source.resolve_xref("foo", root_file) == tmp_path / "Foo.DXF"

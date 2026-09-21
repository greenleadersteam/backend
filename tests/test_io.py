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

import pytest

from greenplan.api.storage import ProjectNotFoundError, ProjectStore


def test_create_writes_metadata_and_subfolders(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()

    record = store.create("Olympic Village", "pilot object 1")

    project_dir = tmp_path / "projects" / record.id
    assert (project_dir / "metadata.yaml").is_file()
    assert (project_dir / "raw").is_dir()
    assert (project_dir / "processed").is_dir()
    assert record.name == "Olympic Village"
    assert record.description == "pilot object 1"
    assert record.deleted_at is None


def test_get_and_list_active(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()

    a = store.create("A", None)
    b = store.create("B", None)

    assert store.get(a.id) == a
    assert store.get("missing") is None
    assert {p.id for p in store.list_active()} == {a.id, b.id}


def test_update_changes_only_metadata_fields(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()
    record = store.create("A", "first")

    updated = store.update(record.id, name=None, description="second")

    assert updated.name == "A"
    assert updated.description == "second"
    assert updated.updated_at >= record.updated_at
    assert store.get(record.id).description == "second"


def test_update_missing_project_raises(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()
    with pytest.raises(ProjectNotFoundError):
        store.update("missing", "x", None)


def test_soft_delete_hides_from_get_and_list(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()
    record = store.create("A", None)

    store.soft_delete(record.id)

    assert store.get(record.id) is None
    assert store.list_active() == []
    # soft delete: files stay on disk, only the API-facing view changes
    assert (tmp_path / "projects" / record.id / "metadata.yaml").is_file()


def test_soft_delete_missing_project_raises(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()
    with pytest.raises(ProjectNotFoundError):
        store.soft_delete("missing")


def test_soft_delete_twice_raises(tmp_path):
    store = ProjectStore(tmp_path)
    store.load()
    record = store.create("A", None)
    store.soft_delete(record.id)
    with pytest.raises(ProjectNotFoundError):
        store.soft_delete(record.id)


def test_reload_from_disk_restores_cache(tmp_path):
    first = ProjectStore(tmp_path)
    first.load()
    record = first.create("Persisted", "desc")

    second = ProjectStore(tmp_path)
    second.load()

    assert second.get(record.id) == record


def test_reload_from_disk_keeps_soft_delete(tmp_path):
    first = ProjectStore(tmp_path)
    first.load()
    record = first.create("A", None)
    first.soft_delete(record.id)

    second = ProjectStore(tmp_path)
    second.load()

    assert second.get(record.id) is None
    assert second.list_active() == []

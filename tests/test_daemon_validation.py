import daemon


def test_valid_color_accepts_hex():
    assert daemon._valid_color("#e53935")
    assert daemon._valid_color("#000000")
    assert daemon._valid_color("#FFFFFF")


def test_valid_color_rejects_bad_format():
    assert not daemon._valid_color("e53935")  # manca #
    assert not daemon._valid_color("#fff")  # troppo corto
    assert not daemon._valid_color("#gggggg")  # non esadecimale
    assert not daemon._valid_color(123)
    assert not daemon._valid_color(None)


def test_valid_dashboard_shape():
    good = {"id": "x", "name": "X", "rows": 1, "cols": 1, "buttons": []}
    assert daemon._valid_dashboard_shape(good)

    missing_id = {"name": "X", "rows": 1, "cols": 1, "buttons": []}
    assert not daemon._valid_dashboard_shape(missing_id)

    bad_button = {
        "id": "x", "name": "X", "rows": 1, "cols": 1,
        "buttons": [{"label": "senza id/row/col"}],
    }
    assert not daemon._valid_dashboard_shape(bad_button)


def test_valid_layout_shape_requires_nonempty_dashboards():
    assert not daemon._valid_layout_shape({"dashboards": []})
    assert not daemon._valid_layout_shape({})
    assert daemon._valid_layout_shape(daemon.DEFAULT_LAYOUT)


def test_migrate_old_layout_wraps_single_grid():
    old = {
        "rows": 2,
        "cols": 2,
        "buttons": [
            {"id": "record", "label": "Registra", "kind": "record", "row": 0, "col": 0}
        ],
    }
    migrated = daemon._migrate_old_layout(old)
    assert daemon._valid_layout_shape(migrated)
    assert len(migrated["dashboards"]) == 1
    d = migrated["dashboards"][0]
    assert d["rows"] == 2 and d["cols"] == 2
    assert d["buttons"] == old["buttons"]


def test_load_layout_returns_default_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "LAYOUT_PATH", tmp_path / "nope.json")
    layout = daemon._load_layout()
    assert layout == daemon.DEFAULT_LAYOUT


def test_load_layout_migrates_old_format_on_disk(tmp_path, monkeypatch):
    import json

    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"rows": 1, "cols": 1, "buttons": []}))
    monkeypatch.setattr(daemon, "LAYOUT_PATH", path)
    layout = daemon._load_layout()
    assert "dashboards" in layout
    assert layout["dashboards"][0]["rows"] == 1

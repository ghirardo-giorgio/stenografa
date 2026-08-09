"""Test della pulizia del layout salvato da versioni precedenti: i pulsanti
"Crea Tutorial" e la dashboard che generavano non esistono piu', ma possono
essere ancora nel layout.json di chi aggiorna."""
import json

import daemon


def _layout_with_tutorial():
    return {
        "dashboards": [
            {
                "id": "default",
                "name": "Stenografa",
                "rows": 2,
                "cols": 2,
                "buttons": [
                    {"id": "record", "label": "Registra", "kind": "record",
                     "row": 0, "col": 0},
                    {"id": "creatutorial", "label": "Crea Tutorial",
                     "kind": "tutorial", "row": 1, "col": 1},
                ],
            },
            {
                "id": "tutorial",
                "name": "Tutorial: come si fa",
                "rows": 1,
                "cols": 2,
                "buttons": [
                    {"id": "passo1", "label": "Passo 1", "kind": "keys",
                     "combo": "ctrl+a", "row": 0, "col": 0},
                ],
            },
        ]
    }


def test_load_layout_drops_tutorial_button_and_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "LAYOUT_PATH", tmp_path / "layout.json")
    monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
    daemon.LAYOUT_PATH.write_text(json.dumps(_layout_with_tutorial()))

    layout = daemon._load_layout()

    assert [d["id"] for d in layout["dashboards"]] == ["default"]
    assert [b["id"] for b in layout["dashboards"][0]["buttons"]] == ["record"]


def test_load_layout_rewrites_the_file_once(tmp_path, monkeypatch):
    """Ripulito una volta, il file non deve tornare sporco al riavvio."""
    monkeypatch.setattr(daemon, "LAYOUT_PATH", tmp_path / "layout.json")
    monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
    daemon.LAYOUT_PATH.write_text(json.dumps(_layout_with_tutorial()))

    daemon._load_layout()

    saved = json.loads(daemon.LAYOUT_PATH.read_text())
    assert all(d["id"] != "tutorial" for d in saved["dashboards"])
    assert all(
        b["kind"] != "tutorial"
        for d in saved["dashboards"]
        for b in d["buttons"]
    )


def test_load_layout_leaves_a_clean_layout_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "LAYOUT_PATH", tmp_path / "layout.json")
    monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
    clean = {
        "dashboards": [
            {
                "id": "default",
                "name": "Stenografa",
                "rows": 1,
                "cols": 2,
                "buttons": [
                    {"id": "record", "label": "Registra", "kind": "record",
                     "row": 0, "col": 0},
                    {"id": "copia", "label": "Copia", "kind": "keys",
                     "combo": "ctrl+c", "row": 0, "col": 1},
                ],
            }
        ]
    }
    daemon.LAYOUT_PATH.write_text(json.dumps(clean))
    before = daemon.LAYOUT_PATH.stat().st_mtime_ns

    layout = daemon._load_layout()

    assert layout == clean
    # niente riscrittura inutile ad ogni avvio
    assert daemon.LAYOUT_PATH.stat().st_mtime_ns == before

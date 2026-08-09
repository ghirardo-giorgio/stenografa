"""Test dell'estensione del comando vocale IA all'avvio di applicazioni
("apri gimp"). Il modello non riceve mai l'elenco delle app ne' puo'
inventare un id: propone solo un nome, che il demone risolve contro le
applicazioni davvero installate — stesso principio di vocabolario chiuso
gia' usato per le combinazioni di tasti."""
import pytest

import daemon as daemon_module


class FakeBackend:
    def __init__(self, apps=(), launch_ok=True, keys_ok=True):
        self._apps = list(apps)
        self._launch_ok = launch_ok
        self._keys_ok = keys_ok
        self.launched = []
        self.keys = []
        self.notifications = []

    def list_apps(self):
        return self._apps

    def launch_app(self, app_id):
        self.launched.append(app_id)
        return self._launch_ok

    def simulate_keys(self, combo):
        self.keys.append(combo)
        return self._keys_ok

    def notify(self, title, body, urgency="normal"):
        self.notifications.append((title, body, urgency))


APPS = [
    {"id": "/apps/gimp.desktop", "name": "GNU Image Manipulation Program"},
    {"id": "/apps/steam.desktop", "name": "Steam"},
    {"id": "/apps/stl.desktop", "name": "Steam Tinker Launch"},
    {"id": "/apps/code.desktop", "name": "Visual Studio Code"},
]


@pytest.fixture
def app(daemon_app):
    daemon_app.backend = FakeBackend(apps=APPS)
    return daemon_app


def test_resolves_app_name_to_installed_id(app):
    candidates = app._resolve_app_candidates("visual studio code", "Apri VS Code")
    assert len(candidates) == 1
    assert candidates[0]["app_id"] == "/apps/code.desktop"
    # con una sola corrispondenza si tiene l'etichetta proposta dal modello
    assert candidates[0]["label"] == "Apri VS Code"


def test_exact_match_wins_over_partial_ones(app):
    """"Steam" corrisponde esattamente a un'app: proporre anche "Steam
    Tinker Launch" chiederebbe una scelta che l'utente ha gia' fatto."""
    candidates = app._resolve_app_candidates("Steam", "Apri Steam")
    assert [c["app_id"] for c in candidates] == ["/apps/steam.desktop"]


def test_partial_matches_become_multiple_options(app):
    candidates = app._resolve_app_candidates("tinker", None)
    assert [c["app_id"] for c in candidates] == ["/apps/stl.desktop"]

    candidates = app._resolve_app_candidates("s", None)
    assert len(candidates) > 1
    # con piu' opzioni l'etichetta descrive l'app, non il comando dettato
    assert all(c["label"].startswith("Apri ") for c in candidates)


def test_unknown_app_name_yields_no_candidates(app):
    assert app._resolve_app_candidates("photoshop", None) == []
    assert app._resolve_app_candidates("", None) == []


def test_parse_candidates_mixes_shortcuts_and_apps(app):
    candidates = app._parse_shortcut_candidates(
        '[{"label": "Copia", "combo": "ctrl+c"}, '
        '{"label": "Apri Steam", "app": "Steam"}]'
    )
    assert candidates[0]["combo"] == "ctrl+c"
    assert candidates[1]["app_id"] == "/apps/steam.desktop"


def test_parse_candidates_respects_max_options(app, monkeypatch):
    monkeypatch.setattr(daemon_module, "AI_COMMAND_MAX_OPTIONS", 2)
    candidates = app._parse_shortcut_candidates(
        '[{"label": "a", "combo": "ctrl+a"}, {"label": "b", "combo": "ctrl+b"}, '
        '{"label": "c", "combo": "ctrl+c"}]'
    )
    assert len(candidates) == 2


def test_execute_candidate_launches_app(app):
    executed, error = app._execute_candidate(
        {"label": "Apri Steam", "app_id": "/apps/steam.desktop", "app_name": "Steam"}
    )
    assert error is None
    assert executed == "Steam"
    assert app.backend.launched == ["/apps/steam.desktop"]


def test_execute_candidate_reports_launch_failure(app):
    app.backend = FakeBackend(apps=APPS, launch_ok=False)
    _executed, error = app._execute_candidate(
        {"label": "Apri Steam", "app_id": "/apps/steam.desktop", "app_name": "Steam"}
    )
    assert error is not None


def test_execute_candidate_still_handles_key_combos(app):
    executed, error = app._execute_candidate({"label": "Copia", "combo": "ctrl+c"})
    assert error is None
    assert executed == "ctrl+c"
    assert app.backend.keys == ["ctrl+c"]


def test_choice_reply_accepts_only_proposed_app(app, monkeypatch):
    monkeypatch.setattr(daemon_module.Stenografa, "_set_state", lambda self, s: None)
    app._pending_choice = {
        "id": "req1",
        "text": "apri steam",
        "options": [
            {"label": "Apri Steam", "app_id": "/apps/steam.desktop",
             "app_name": "Steam"}
        ],
        "expires_at": float("inf"),
    }

    # un'app che non era fra le opzioni non viene avviata: il pannello non e'
    # una via per far eseguire al demone avvii arbitrari
    app._on_ai_choice_reply("req1", None, "/apps/gimp.desktop")
    assert app.backend.launched == []

    app._on_ai_choice_reply("req1", None, "/apps/steam.desktop")
    assert app.backend.launched == ["/apps/steam.desktop"]
    assert app._pending_choice is None

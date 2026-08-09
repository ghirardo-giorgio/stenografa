import queue
import sys
import threading
from pathlib import Path

# permette "import daemon" / "import key_combo" indipendentemente da come
# viene invocato pytest (es. `pytest` dalla cartella stenografa/ o `pytest
# tests/` da altrove)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import daemon as daemon_module


@pytest.fixture
def daemon_app(tmp_path, monkeypatch):
    """Istanza di Stenografa senza gli effetti collaterali di __init__
    (nessun socket/thread/notifica reale): utile per testare la logica pura
    di gestione del layout/configurazione. Usa un layout.json e un
    config.json temporanei e isolati da quelli reali dell'utente."""
    monkeypatch.setattr(daemon_module, "LAYOUT_PATH", tmp_path / "layout.json")
    monkeypatch.setattr(daemon_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(daemon_module, "CONFIG_PATH", tmp_path / "config.json")

    app = object.__new__(daemon_module.Stenografa)
    app.command_queue = queue.Queue()
    app.layout = daemon_module._load_layout()
    app._layout_lock = threading.RLock()
    app._net_clients = set()
    app._net_lock = threading.Lock()
    app.model = daemon_module.ModelManager(language=daemon_module.MODEL_LANGUAGE)
    app.restore_clipboard = False
    app._clipboard_before = None
    app._recording_mode = "paste"
    app._recording_dashboard_id = None
    app._recording_vocabulary = ""
    app._recording_watchdog_timer = None
    app.translate_enabled = False
    app.translate_target = "en"
    app.translate_engine = "whisper"
    app.lock_handle = None
    app._restart_requested = False
    app.vocabulary = ""
    app.confirm_before_paste = False
    app.require_tls = False
    app.tls_context = None
    app.tls_fingerprint = None
    app._pending_paste = None
    app._pending_choice = None
    app._history = []
    app._history_lock = threading.Lock()
    app.pause_media_while_recording = False
    app._media_players = []
    app._paused_for_recording = []
    app._muted_for_recording = []
    app._muted_apps_guard = None
    app._media_lock = threading.Lock()
    app.auth_token = "12345"
    app._last_focused_dashboard_id = None
    app._auth_failures = {}
    app._auth_failures_lock = threading.Lock()
    app._apps_cache = None
    app._apps_cache_at = 0.0
    app._apps_cache_lock = threading.Lock()
    app._app_icon_cache = {}
    app._app_icon_lock = threading.Lock()
    return app


class FakeResponse:
    """Sostituisce la risposta di requests.get/post verso LM Studio nei
    test (comando vocale IA, traduzione): nessuna chiamata di rete reale."""

    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise daemon_module.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class ImmediateThread:
    """Sostituisce threading.Thread nei test: esegue il target subito, nello
    stesso thread, cosi' non serve sincronizzarsi con un thread reale per
    leggere il risultato accodato su command_queue."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def fake_llm(monkeypatch, app, reply=None, error=None, capture=None):
    """Sostituisce il backend LLM del demone (vedi Stenografa._llm_complete):
    nessuna chiamata di rete reale e nessuna dipendenza da quale provider e'
    configurato sulla macchina che esegue i test.

    Con `capture` (un dict) registra system/user/max_tokens dell'ultima
    chiamata, per i test che verificano cosa viene messo nel prompt."""

    def _complete(system, user, max_tokens=600, timeout=30):
        if capture is not None:
            capture["system"] = system
            capture["user"] = user
            capture["max_tokens"] = max_tokens
        return (reply, error)

    monkeypatch.setattr(app, "_llm_complete", _complete)
    return capture

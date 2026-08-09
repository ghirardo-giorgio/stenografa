"""Test delle funzionalita' che stanno attorno alla dettatura: vocabolario
passato a Whisper come contesto, storico delle dettature con re-incolla e
conferma prima dell'incolla."""
import pytest

import daemon as daemon_module


class FakeBackend:
    def __init__(self, paste_ok=True):
        self._paste_ok = paste_ok
        self.clipboard = ""
        self.keys = []
        self.notifications = []

    def copy_to_clipboard(self, text):
        self.clipboard = text

    def read_clipboard(self):
        return self.clipboard

    def simulate_keys(self, combo):
        self.keys.append(combo)
        return self._paste_ok

    def get_focused_window(self):
        return None

    def notify(self, title, body, urgency="normal"):
        self.notifications.append((title, body, urgency))


@pytest.fixture
def app(daemon_app):
    daemon_app.backend = FakeBackend()
    daemon_app.state = daemon_module.STATE_IDLE
    return daemon_app


# --- vocabolario di dettatura ---


def test_vocabulary_combines_global_and_dashboard(app):
    app.vocabulary = "Stenografa, Nobara"
    app._mutate_layout(
        "set_dashboard_vocabulary",
        {"dashboard_id": "default", "vocabulary": "InvokeAI, denoising"},
    )

    prompt = app._vocabulary_for_dashboard("default")

    assert "Stenografa, Nobara" in prompt
    assert "InvokeAI, denoising" in prompt


def test_vocabulary_is_empty_when_nothing_configured(app):
    assert app._vocabulary_for_dashboard("default") == ""


def test_vocabulary_ignores_unknown_dashboard(app):
    app.vocabulary = "Stenografa"
    prompt = app._vocabulary_for_dashboard("dashboard-cancellata")
    assert "Stenografa" in prompt


def test_set_vocabulary_rejects_oversized_value(app):
    ok, error = app._set_vocabulary("x" * (daemon_module.VOCABULARY_MAX_LENGTH + 1))
    assert not ok
    assert "vocabulary" in error


def test_transcribe_worker_passes_vocabulary_as_initial_prompt(app, tmp_path):
    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"0" * 200)
    prompts = []
    app.model = type(
        "FakeModel",
        (),
        {
            "transcribe": lambda self, path, task="transcribe", initial_prompt=None: (
                prompts.append(initial_prompt) or "ciao"
            )
        },
    )()
    app._recording_vocabulary = "Termini ricorrenti: InvokeAI."

    app._transcribe_worker(str(wav))

    assert prompts == ["Termini ricorrenti: InvokeAI."]


# --- storico delle dettature ---


def test_successful_paste_enters_history(app):
    app._paste_text("ciao mondo", None)
    assert [e["text"] for e in app._history] == ["ciao mondo"]
    assert app._history[0]["pasted"] is True


def test_failed_paste_is_recorded_as_not_pasted(app):
    app.backend = FakeBackend(paste_ok=False)
    app._paste_text("finito nel posto sbagliato", None)
    assert app._history[0]["pasted"] is False


def test_history_is_capped_and_newest_first(app):
    for i in range(daemon_module.HISTORY_MAX_ENTRIES + 5):
        app._history_add(f"testo {i}", pasted=True)
    assert len(app._history) == daemon_module.HISTORY_MAX_ENTRIES
    assert app._history[0]["text"] == (
        f"testo {daemon_module.HISTORY_MAX_ENTRIES + 4}"
    )


def test_history_snapshot_hides_full_text(app):
    app._history_add("x" * (daemon_module.HISTORY_PREVIEW_LENGTH + 50), pasted=True)
    snapshot = app._history_snapshot()
    assert "text" not in snapshot[0]
    assert snapshot[0]["preview"].endswith("...")


def test_paste_history_entry_repastes_without_duplicating_history(app):
    app._paste_text("prima dettatura", None)
    entry_id = app._history[0]["id"]
    app.backend.clipboard = "altro"

    app._paste_history_entry(entry_id)

    assert app.backend.clipboard == "prima dettatura"
    assert len(app._history) == 1  # non si accumula una copia


def test_paste_history_entry_ignores_unknown_id(app):
    app._paste_history_entry("id-inventato")
    assert app.backend.clipboard == ""


# --- conferma prima dell'incolla ---


def test_deliver_text_pastes_directly_when_confirmation_disabled(app):
    app.confirm_before_paste = False
    app._deliver_text("ciao", None)
    assert app.backend.clipboard == "ciao"
    assert app._pending_paste is None


def test_deliver_text_waits_for_confirmation_when_enabled(app):
    app.confirm_before_paste = True
    app._deliver_text("ciao", None)
    assert app.backend.clipboard == ""  # nulla incollato finche' non conferma
    assert app._pending_paste["text"] == "ciao"


def test_confirmed_paste_uses_the_corrected_text(app):
    app.confirm_before_paste = True
    app._deliver_text("ciao mnodo", None)
    request_id = app._pending_paste["id"]

    app._on_paste_confirmed(request_id, "ciao mondo")

    assert app.backend.clipboard == "ciao mondo"
    assert app._pending_paste is None


def test_confirmed_paste_falls_back_to_original_text(app):
    app.confirm_before_paste = True
    app._deliver_text("ciao", None)
    app._on_paste_confirmed(app._pending_paste["id"], None)
    assert app.backend.clipboard == "ciao"


def test_stale_confirmation_is_ignored(app):
    app.confirm_before_paste = True
    app._deliver_text("ciao", None)

    app._on_paste_confirmed("request-vecchia", "altro")

    assert app.backend.clipboard == ""
    assert app._pending_paste is not None


def test_cancelled_confirmation_keeps_text_in_history(app):
    """Annullare l'incolla non deve voler dire perdere la dettatura."""
    app.confirm_before_paste = True
    app._deliver_text("ciao", None)

    app._cancel_pending_paste(app._pending_paste["id"])

    assert app.backend.clipboard == ""
    assert [e["text"] for e in app._history] == ["ciao"]


def test_expired_confirmation_closes_itself(app, monkeypatch):
    app.confirm_before_paste = True
    app._deliver_text("ciao", None)
    app._pending_paste["expires_at"] = -1  # gia' scaduta

    app._expire_pending_paste()

    assert app._pending_paste is None
    assert app.backend.clipboard == ""


def test_new_recording_supersedes_pending_confirmation(app, monkeypatch):
    app.confirm_before_paste = True
    app._deliver_text("prima", None)
    monkeypatch.setattr(daemon_module.Stenografa, "_set_state", lambda self, s: None)
    monkeypatch.setattr(
        app.backend, "start_recording", lambda path: None, raising=False
    )
    monkeypatch.setattr(app.model, "preload", lambda: None)

    app._start_recording()

    assert app._pending_paste is None

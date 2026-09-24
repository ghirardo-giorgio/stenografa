"""Filtro delle notifiche di sistema (config "notifications", vedi
daemon.NOTIFICATION_LEVELS): serve a chi trova invadente il messaggio che
GNOME mostra a fine dettatura ma non vuole rinunciare agli errori."""
import daemon


class RecordingBackend:
    def __init__(self):
        self.notified = []

    def notify(self, title, body, urgency="normal"):
        self.notified.append((title, body, urgency))


def test_valid_notifications_accepts_known_levels():
    assert daemon._valid_notifications("all")
    assert daemon._valid_notifications("ERRORS")  # maiuscolo, normalizzato dopo
    assert daemon._valid_notifications("none")


def test_valid_notifications_rejects_bad_values():
    assert not daemon._valid_notifications("silent")
    assert not daemon._valid_notifications("")
    assert not daemon._valid_notifications(None)
    assert not daemon._valid_notifications(True)


def test_notify_sends_everything_by_default(daemon_app):
    daemon_app.backend = RecordingBackend()
    daemon_app._notify("Stenografa", "Nessun testo rilevato.")
    daemon_app._notify("Stenografa - errore", "boom", urgency="critical")
    assert len(daemon_app.backend.notified) == 2


def test_notify_errors_only_keeps_critical(daemon_app):
    daemon_app.backend = RecordingBackend()
    daemon_app._set_notifications("errors")
    daemon_app._notify("Stenografa", "Nessun testo rilevato.")
    daemon_app._notify("Stenografa - errore", "boom", urgency="critical")
    assert daemon_app.backend.notified == [
        ("Stenografa - errore", "boom", "critical")
    ]


def test_notify_none_silences_everything(daemon_app):
    daemon_app.backend = RecordingBackend()
    daemon_app._set_notifications("none")
    daemon_app._notify("Stenografa", "Nessun testo rilevato.")
    daemon_app._notify("Stenografa - errore", "boom", urgency="critical")
    assert daemon_app.backend.notified == []


def test_set_notifications_persists_and_normalizes(daemon_app):
    ok, error = daemon_app._set_notifications("ERRORS")
    assert ok, error
    assert daemon_app.notifications == "errors"
    assert daemon._load_notifications() == "errors"


def test_set_notifications_rejects_unknown_level(daemon_app):
    ok, error = daemon_app._set_notifications("silent")
    assert not ok
    assert "notifications" in error
    assert daemon_app.notifications == "all"


def test_load_notifications_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "CONFIG_PATH", tmp_path / "config.json")
    assert daemon._load_notifications() == "all"


def test_transcription_error_still_notifies_with_errors_level(daemon_app):
    """Con "errors" l'errore di trascrizione arriva ancora: e' il caso in
    cui l'utente non vedrebbe altrimenti nulla sul PC."""
    daemon_app.backend = RecordingBackend()
    daemon_app._set_notifications("errors")
    daemon_app._on_transcription_done(None, "modello non caricato")
    assert [n[2] for n in daemon_app.backend.notified] == ["critical"]


def test_empty_transcription_is_silent_with_errors_level(daemon_app):
    """"Nessun testo rilevato" e' la notifica che compare a fine dettatura
    quando non si e' detto nulla: con "errors" resta solo nel broadcast."""
    daemon_app.backend = RecordingBackend()
    daemon_app._set_notifications("errors")
    daemon_app._on_transcription_done("", None)
    assert daemon_app.backend.notified == []

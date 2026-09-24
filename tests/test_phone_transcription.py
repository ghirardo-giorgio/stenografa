"""Quando la GPU non e' utilizzabile la trascrizione puo' toccare al telefono:
detta col proprio riconoscimento vocale e consegna il testo gia' pronto, che il
demone incolla come se l'avesse trascritto lui.

Serve a non dover aspettare la CPU, che sullo stesso audio impiega all'incirca
il tempo reale (vedi MODEL_FALLBACK_* in daemon.py)."""
import daemon


def _prepare(app, monkeypatch):
    monkeypatch.setattr(app, "_pause_media_for_recording", lambda: None)
    monkeypatch.setattr(app, "_resume_media_after_recording", lambda: None)
    monkeypatch.setattr(app, "_broadcast", lambda msg: None)
    monkeypatch.setattr(app, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(
        app, "_set_state", lambda state: setattr(app, "state", state)
    )
    aperture = []

    class Backend:
        def start_recording(self, path, slot="main"):
            aperture.append(path)

        def stop_recording(self, slot="main"):
            pass

        def read_clipboard(self):
            return None

    app.backend = Backend()
    app.layout = {
        "dashboards": [
            {
                "id": "d",
                "name": "d",
                "rows": 1,
                "cols": 1,
                "buttons": [{"id": "record", "kind": "record", "row": 0, "col": 0}],
            }
        ]
    }
    return aperture


def test_the_pc_microphone_stays_closed(daemon_app, monkeypatch):
    """Registrare anche dal PC servirebbe solo a tenere occupata la scheda
    audio e a scrivere un file che nessuno leggerebbe."""
    app = daemon_app
    aperture = _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE

    app._handle_button_press("record", transcribe="phone")

    assert app._recording_by_phone is True
    assert app.state == daemon.STATE_RECORDING
    assert aperture == []
    assert app.record_file is None


def test_the_pc_records_normally_otherwise(daemon_app, monkeypatch):
    app = daemon_app
    aperture = _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE

    app._handle_button_press("record")

    assert app._recording_by_phone is False
    assert len(aperture) == 1
    assert app.record_file is not None
    import os

    os.unlink(app.record_file)


def test_the_delivered_text_is_pasted(daemon_app, monkeypatch):
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", transcribe="phone")

    consegnati = []
    monkeypatch.setattr(
        app, "_on_transcription_done", lambda text, err: consegnati.append((text, err))
    )
    app._on_dictated_text("  ciao mondo  ")

    assert consegnati == [("ciao mondo", None)]


def test_empty_text_follows_the_usual_no_text_path(daemon_app, monkeypatch):
    """Testo vuoto: si comporta come una dettatura senza parlato, senza
    incollare una stringa vuota."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", transcribe="phone")

    consegnati = []
    monkeypatch.setattr(
        app, "_on_transcription_done", lambda text, err: consegnati.append((text, err))
    )
    app._on_dictated_text("   ")

    assert consegnati == [(None, None)]


def test_text_is_ignored_when_the_pc_is_transcribing(daemon_app, monkeypatch):
    """Se sta registrando il PC, il testo che arrivasse dal telefono sarebbe
    una duplicazione: si ignora e la trascrizione del PC fa il suo corso."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record")
    import os

    percorso = app.record_file

    consegnati = []
    monkeypatch.setattr(
        app, "_on_transcription_done", lambda text, err: consegnati.append((text, err))
    )
    app._on_dictated_text("testo di troppo")
    assert consegnati == []
    os.unlink(percorso)


def test_text_is_ignored_when_not_recording(daemon_app, monkeypatch):
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    consegnati = []
    monkeypatch.setattr(
        app, "_on_transcription_done", lambda text, err: consegnati.append((text, err))
    )
    app._on_dictated_text("testo in ritardo")
    assert consegnati == []


def test_the_flag_does_not_survive_the_dictation(daemon_app, monkeypatch):
    """La dettatura successiva potrebbe toccare al PC."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", transcribe="phone")
    app._net_clients = set()
    app._on_transcription_done(None, None)
    assert app._recording_by_phone is False


def test_the_daemon_knows_the_gpu_is_missing_before_dictating(monkeypatch):
    """Il telefono deve poterlo sapere in anticipo: aspettare il primo
    caricamento fallito vorrebbe dire subire una dettatura lenta prima di
    accorgersene."""
    monkeypatch.setattr(daemon, "_cuda_available", lambda: False)
    manager = daemon.ModelManager()
    assert manager.device == daemon.MODEL_FALLBACK_DEVICE

    monkeypatch.setattr(daemon, "_cuda_available", lambda: True)
    assert daemon.ModelManager().device == daemon.MODEL_DEVICE


def test_the_stop_phrase_is_removed_from_the_delivered_text(
    daemon_app, monkeypatch
):
    """Fermando a voce, il riconoscitore del telefono sente anche la frase di
    stop: finirebbe nel testo incollato."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", source="wake", transcribe="phone")

    consegnati = []
    monkeypatch.setattr(
        app, "_on_transcription_done", lambda t, e: consegnati.append(t)
    )
    app._on_dictated_text("jarvis scrivi a Marco jarvis stop")
    assert consegnati == ["scrivi a Marco"]


def test_text_is_untouched_without_voice_activation(daemon_app, monkeypatch):
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", transcribe="phone")

    consegnati = []
    monkeypatch.setattr(
        app, "_on_transcription_done", lambda t, e: consegnati.append(t)
    )
    app._on_dictated_text("ho gia visto quel film")
    assert consegnati == ["ho gia visto quel film"]

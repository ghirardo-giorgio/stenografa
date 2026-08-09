import daemon


def test_valid_language_accepts_two_letter_codes_and_auto():
    assert daemon._valid_language("it")
    assert daemon._valid_language("EN")  # maiuscolo, normalizzato dopo
    assert daemon._valid_language("auto")


def test_valid_language_rejects_bad_values():
    assert not daemon._valid_language("italiano")  # non un codice ISO 639-1
    assert not daemon._valid_language("i")
    assert not daemon._valid_language("123")
    assert not daemon._valid_language("")
    assert not daemon._valid_language(None)


def test_set_language_updates_model_and_persists(daemon_app):
    ok, error = daemon_app._set_language("en")
    assert ok, error
    assert daemon_app.model.language == "en"
    assert daemon.CONFIG_PATH.exists()

    # ricaricando da disco si ottiene la stessa lingua
    assert daemon._load_language() == "en"


def test_set_language_rejects_invalid(daemon_app):
    ok, error = daemon_app._set_language("nope-not-a-code")
    assert not ok
    assert "ISO 639-1" in error
    # la lingua precedente resta invariata
    assert daemon_app.model.language == daemon.MODEL_LANGUAGE


def test_set_language_does_not_clobber_token(daemon_app, tmp_path):
    daemon._write_config_key("token", "12345")
    ok, error = daemon_app._set_language("fr")
    assert ok, error
    data = daemon._read_config()
    assert data["token"] == "12345"
    assert data["language"] == "fr"


def test_config_snapshot_reflects_current_language(daemon_app):
    daemon_app._set_language("de")
    assert daemon_app._config_snapshot() == {
        "language": "de",
        "restore_clipboard": False,
        "translate_enabled": False,
        "translate_target": "en",
        "translate_engine": "whisper",
        "vocabulary": "",
        "confirm_before_paste": False,
        "require_tls": False,
        "pause_media_while_recording": False,
        "tls_available": False,
        "tls_fingerprint": None,
    }


def test_load_language_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "CONFIG_PATH", tmp_path / "config.json")
    assert daemon._load_language() == daemon.MODEL_LANGUAGE


def test_set_restore_clipboard_updates_and_persists(daemon_app):
    ok, error = daemon_app._set_restore_clipboard(True)
    assert ok, error
    assert daemon_app.restore_clipboard is True
    assert daemon._load_restore_clipboard() is True

    ok, error = daemon_app._set_restore_clipboard(False)
    assert ok, error
    assert daemon_app.restore_clipboard is False
    assert daemon._load_restore_clipboard() is False


def test_set_restore_clipboard_rejects_non_bool(daemon_app):
    ok, error = daemon_app._set_restore_clipboard("yes")
    assert not ok
    assert "booleano" in error
    assert daemon_app.restore_clipboard is False


def test_set_translate_enabled_updates_and_persists(daemon_app):
    ok, error = daemon_app._set_translate_enabled(True)
    assert ok, error
    assert daemon_app.translate_enabled is True
    assert daemon_app._config_snapshot()["translate_enabled"] is True


def test_set_translate_enabled_rejects_non_bool(daemon_app):
    ok, error = daemon_app._set_translate_enabled("yes")
    assert not ok
    assert "booleano" in error


def test_set_translate_target_updates_and_persists(daemon_app):
    ok, error = daemon_app._set_translate_target("it")
    assert ok, error
    assert daemon_app.translate_target == "it"
    assert daemon.CONFIG_PATH.exists()


def test_set_translate_target_rejects_auto(daemon_app):
    """A differenza della lingua di dettatura, per la traduzione 'auto' non
    ha senso: bisogna sapere verso quale lingua tradurre."""
    ok, error = daemon_app._set_translate_target("auto")
    assert not ok
    assert "auto" in error.lower()


def test_set_translate_target_rejects_invalid_code(daemon_app):
    ok, error = daemon_app._set_translate_target("italiano")
    assert not ok


def test_set_translate_engine_updates(daemon_app):
    ok, error = daemon_app._set_translate_engine("llm")
    assert ok, error
    assert daemon_app.translate_engine == "llm"

    ok, error = daemon_app._set_translate_engine("whisper")
    assert ok, error
    assert daemon_app.translate_engine == "whisper"


def test_set_translate_engine_rejects_unknown_value(daemon_app):
    ok, error = daemon_app._set_translate_engine("google")
    assert not ok
    assert daemon_app.translate_engine == "whisper"  # invariato


def test_handle_config_cmd_dispatches_to_correct_setter(daemon_app):
    ok, error = daemon_app._handle_config_cmd(
        "set_translate_target", {"target": "fr"}
    )
    assert ok, error
    assert daemon_app.translate_target == "fr"


def test_load_restore_clipboard_defaults_to_false(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "CONFIG_PATH", tmp_path / "config.json")
    assert daemon._load_restore_clipboard() is False


def test_restore_clipboard_only_after_successful_paste(daemon_app, monkeypatch):
    """_on_transcription_done non deve ripristinare gli appunti se il paste
    automatico fallisce: il testo dettato deve restare disponibile per un
    Ctrl+V manuale dell'utente."""
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            pass

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return False  # paste fallito

        def get_focused_window(self):
            return None

    daemon_app.backend = FakeBackend()
    daemon_app.restore_clipboard = True
    daemon_app._clipboard_before = "testo precedente"
    daemon_app._net_clients = set()

    daemon_app._on_transcription_done("testo dettato", None)

    # copiato il testo dettato, ma NON ripristinato quello precedente
    assert ("copy", "testo dettato") in calls
    assert ("copy", "testo precedente") not in calls


def test_restore_clipboard_after_successful_paste(daemon_app):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            pass

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return True  # paste riuscito

        def get_focused_window(self):
            return None

    daemon_app.backend = FakeBackend()
    daemon_app.restore_clipboard = True
    daemon_app._clipboard_before = "testo precedente"
    daemon_app._net_clients = set()

    daemon_app._on_transcription_done("testo dettato", None)

    assert calls[0] == ("copy", "testo dettato")
    assert ("copy", "testo precedente") in calls

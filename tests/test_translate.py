"""Test della traduzione automatica del testo dettato (pulsanti "record",
non "ai_command"): task nativo di Whisper (solo verso inglese) o traduzione
via LM Studio (qualsiasi lingua), con fallback automatico dell'engine
quando il target non e' inglese."""
import daemon
from conftest import ImmediateThread, fake_llm


# --- config: setter gia' testati in test_daemon_language.py; qui solo la
# logica di scelta engine/task ---


def test_needs_llm_translation_false_when_disabled(daemon_app):
    daemon_app.translate_enabled = False
    assert not daemon_app._needs_llm_translation()


def test_needs_llm_translation_false_for_english_whisper_engine(daemon_app):
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "en"
    daemon_app.translate_engine = "whisper"
    assert not daemon_app._needs_llm_translation()


def test_needs_llm_translation_true_for_english_llm_engine(daemon_app):
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "en"
    daemon_app.translate_engine = "llm"
    assert daemon_app._needs_llm_translation()


def test_needs_llm_translation_true_for_non_english_target_even_if_whisper_engine(
    daemon_app,
):
    # il target non inglese forza sempre l'LLM: whisper non puo' tradurre
    # verso altre lingue, l'engine memorizzato viene ignorato a runtime
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "it"
    daemon_app.translate_engine = "whisper"
    assert daemon_app._needs_llm_translation()


# --- _transcribe_worker: scelta del task Whisper ---


def test_transcribe_worker_uses_translate_task_for_english_whisper_engine(
    daemon_app, monkeypatch, tmp_path
):
    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"0" * 200)  # supera la soglia minima di byte

    calls = []
    daemon_app.model = type(
        "FakeModel",
        (),
        {"transcribe": lambda self, path, task="transcribe", initial_prompt=None: calls.append(task) or "hello"},
    )()
    daemon_app._recording_mode = "paste"
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "en"
    daemon_app.translate_engine = "whisper"

    daemon_app._transcribe_worker(str(wav))

    assert calls == ["translate"]
    kind, text, error = daemon_app.command_queue.get_nowait()
    assert kind == "done"
    assert text == "hello"
    assert error is None


def test_transcribe_worker_uses_transcribe_task_when_translate_disabled(
    daemon_app, tmp_path
):
    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"0" * 200)

    calls = []
    daemon_app.model = type(
        "FakeModel",
        (),
        {"transcribe": lambda self, path, task="transcribe", initial_prompt=None: calls.append(task) or "ciao"},
    )()
    daemon_app._recording_mode = "paste"
    daemon_app.translate_enabled = False

    daemon_app._transcribe_worker(str(wav))

    assert calls == ["transcribe"]


def test_transcribe_worker_uses_transcribe_task_for_non_english_target(
    daemon_app, tmp_path
):
    """Target non inglese: la traduzione avviene via LLM DOPO la
    trascrizione fedele, non tramite il task 'translate' di Whisper (che
    puo' tradurre solo verso l'inglese)."""
    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"0" * 200)

    calls = []
    daemon_app.model = type(
        "FakeModel",
        (),
        {"transcribe": lambda self, path, task="transcribe", initial_prompt=None: calls.append(task) or "hello"},
    )()
    daemon_app._recording_mode = "paste"
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "it"
    daemon_app.translate_engine = "whisper"  # ignorato: target non inglese

    daemon_app._transcribe_worker(str(wav))

    assert calls == ["transcribe"]


def test_transcribe_worker_never_translates_ai_command_recordings(
    daemon_app, tmp_path
):
    """Anche con la traduzione attiva, un pulsante ai_command deve ricevere
    il testo trascritto fedelmente (non tradotto): deve interpretare il
    comando nella lingua originale."""
    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"0" * 200)

    calls = []
    daemon_app.model = type(
        "FakeModel",
        (),
        {"transcribe": lambda self, path, task="transcribe", initial_prompt=None: calls.append(task) or "copia"},
    )()
    daemon_app._recording_mode = "ai_command"
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "en"
    daemon_app.translate_engine = "whisper"

    daemon_app._transcribe_worker(str(wav))

    assert calls == ["transcribe"]


# --- _llm_translate / _translate_worker ---


def test_llm_translate_returns_translated_text(daemon_app, monkeypatch):
    captured = fake_llm(monkeypatch, daemon_app, reply="Ciao mondo", capture={})
    translated, error = daemon_app._llm_translate("Hello world", "it")
    assert error is None
    assert translated == "Ciao mondo"
    assert "italiano" in captured["system"].lower()


def test_llm_translate_propagates_backend_error(daemon_app, monkeypatch):
    """Un backend LLM non raggiungibile non deve produrre una traduzione
    vuota o silenziosa: l'errore risale al chiamante."""
    fake_llm(monkeypatch, daemon_app, error="LM Studio non raggiungibile")
    translated, error = daemon_app._llm_translate("Hello", "it")
    assert translated is None
    assert "non raggiungibile" in error


def test_llm_translate_rejects_empty_reply(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="   ")
    translated, error = daemon_app._llm_translate("Hello", "it")
    assert translated is None
    assert "vuota" in error


# --- flusso end-to-end tramite _on_transcription_done ---


def test_on_transcription_done_translates_via_llm_and_pastes(daemon_app, monkeypatch):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return True

        def get_focused_window(self):
            return None

    daemon_app.backend = FakeBackend()
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "it"
    daemon_app.translate_engine = "whisper"  # ignorato: target non inglese
    monkeypatch.setattr(daemon.threading, "Thread", ImmediateThread)
    fake_llm(monkeypatch, daemon_app, reply="Ciao mondo")

    daemon_app._on_transcription_done("Hello world", None)

    assert daemon_app.state == daemon.STATE_THINKING

    kind, translated, error, clipboard_before = daemon_app.command_queue.get_nowait()
    assert kind == "translate_done"
    assert translated == "Ciao mondo"
    daemon_app._on_translate_done(translated, error, clipboard_before)

    assert daemon_app.state == daemon.STATE_IDLE
    assert ("copy", "Ciao mondo") in calls
    assert ("paste", "ctrl+v") in calls


def test_on_transcription_done_english_whisper_translate_skips_llm(daemon_app):
    """Quando il testo e' gia' stato tradotto dal task nativo di Whisper
    (target inglese, engine whisper), non deve scattare nessuna chiamata
    aggiuntiva a LM Studio: si incolla direttamente."""
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return True

        def get_focused_window(self):
            return None

    daemon_app.backend = FakeBackend()
    daemon_app.translate_enabled = True
    daemon_app.translate_target = "en"
    daemon_app.translate_engine = "whisper"

    # "text" qui e' gia' il risultato del task "translate" di Whisper
    daemon_app._on_transcription_done("hello world", None)

    assert daemon_app.state == daemon.STATE_IDLE
    assert ("copy", "hello world") in calls


def test_on_translate_done_notifies_error_without_pasting(daemon_app):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return True

        def get_focused_window(self):
            return None

    daemon_app.backend = FakeBackend()
    daemon_app._on_translate_done(None, "LM Studio non raggiungibile", None)

    assert daemon_app.state == daemon.STATE_IDLE
    assert not any(c[0] == "copy" for c in calls)
    assert any(c[0] == "notify" for c in calls)


# --- restart del demone (vedi anche daemon.py: run()/os.execv, non
# testabile qui perche' sostituirebbe il processo di test) ---


def test_request_restart_sets_flags_and_enqueues_quit(daemon_app):
    daemon_app._running = True
    daemon_app._request_restart()

    assert daemon_app._restart_requested is True
    assert daemon_app._running is False
    kind = daemon_app.command_queue.get_nowait()
    assert kind == ("quit",)

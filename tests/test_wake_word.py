"""Attivazione vocale: riconoscimento della frase scelta dall'utente, pulizia
del testo dettato e commutazione della sorgente di ascolto (vedi
daemon.WakeWordListener).

Nessun audio ne' modello reale: si prova la logica pura, che e' quella dove
gli errori si vedono solo all'uso."""
import math
import os
import queue
import struct
import time

import daemon


# --- normalizzazione -------------------------------------------------------


def test_normalize_ignores_case_accents_and_punctuation():
    assert daemon._normalize_phrase("Jarvis, stop!") == "jarvis stop"
    assert daemon._normalize_phrase("Perché  no") == "perche no"
    assert daemon._normalize_phrase("  ") == ""
    assert daemon._normalize_phrase(None) == ""


# --- riconoscimento della frase --------------------------------------------


def test_phrase_matches_exact_and_inside_longer_text():
    assert daemon._phrase_in_text("jarvis", "Jarvis")
    assert daemon._phrase_in_text("jarvis", "Ok Jarvis, dimmi tutto")
    assert daemon._phrase_in_text("jarvis stop", "va bene, Jarvis stop.")


def test_phrase_tolerates_typical_transcription_errors():
    """Il modello dell'ascolto e' piccolo e sbaglia spesso i nomi propri:
    se questi casi non passassero, la funzione sarebbe inutile nell'uso reale."""
    for heard in ("Giarvis", "Jervis", "Jarvix", "giarvis "):
        assert daemon._phrase_in_text("jarvis", heard), heard


def test_phrase_does_not_match_unrelated_speech():
    assert not daemon._phrase_in_text("jarvis", "")
    assert not daemon._phrase_in_text("jarvis", "domani vado al mare")
    assert not daemon._phrase_in_text("computer accendi", "accendi la luce")


def test_start_phrase_does_not_match_stop_phrase():
    """Le due frasi si somigliano per costruzione ("jarvis" / "jarvis stop"):
    se la frase di avvio scattasse su quella di stop, la dettatura ripartirebbe
    da sola appena finita."""
    assert not daemon._phrase_in_text("jarvis stop", "Jarvis")


def test_empty_phrase_never_matches():
    assert not daemon._phrase_in_text("", "qualsiasi cosa")


# --- validazione della frase configurata -----------------------------------


def test_valid_wake_phrase_accepts_reasonable_phrases():
    assert daemon._valid_wake_phrase("jarvis")
    assert daemon._valid_wake_phrase("ok computer")


def test_valid_wake_phrase_rejects_too_short_or_empty():
    # frasi cortissime scatterebbero dentro parole comuni
    assert not daemon._valid_wake_phrase("ok")
    assert not daemon._valid_wake_phrase("")
    assert not daemon._valid_wake_phrase("!!")
    assert not daemon._valid_wake_phrase(None)


def test_valid_wake_phrase_rejects_too_long():
    assert not daemon._valid_wake_phrase("a" * (daemon.WAKE_PHRASE_MAX_CHARS + 1))


# --- pulizia del testo dettato ---------------------------------------------


def test_strip_removes_phrases_at_both_ends():
    text = "Jarvis questo e' il testo da incollare Jarvis stop"
    assert (
        daemon._strip_wake_phrases(text, "jarvis", "jarvis stop")
        == "questo e' il testo da incollare"
    )


def test_strip_removes_stop_phrase_with_punctuation():
    text = "questo e' il testo, Jarvis stop."
    assert (
        daemon._strip_wake_phrases(text, "jarvis", "jarvis stop")
        == "questo e' il testo"
    )


def test_strip_keeps_phrase_dictated_in_the_middle():
    """In mezzo alla frase e' una parola che l'utente ha dettato davvero."""
    text = "ho chiamato l'assistente jarvis ieri sera"
    assert daemon._strip_wake_phrases(text, "jarvis", "jarvis stop") == text


def test_strip_leaves_text_untouched_when_no_phrase_present():
    text = "solo del testo normale"
    assert daemon._strip_wake_phrases(text, "jarvis", "jarvis stop") == text


def test_strip_can_empty_the_text():
    """Se l'utente non ha detto altro, il testo resta vuoto e la dettatura
    seguira' il percorso "nessun testo rilevato" gia' esistente."""
    assert daemon._strip_wake_phrases("Jarvis stop", "jarvis", "jarvis stop") == ""


# --- lettura dell'audio ----------------------------------------------------


def _write_growing_wav(path, samples, extra_chunk=False):
    """WAV scritto a mano con una lunghezza dichiarata SBAGLIATA (zero), come
    quello di un processo di cattura ancora in corso."""
    pcm = struct.pack("<%dh" % len(samples), *samples)
    header = b"RIFF" + (0).to_bytes(4, "little") + b"WAVE"
    header += b"fmt " + (16).to_bytes(4, "little")
    header += struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    if extra_chunk:
        # chunk in piu' prima di "data": l'header non e' sempre 44 byte
        header += b"LIST" + (4).to_bytes(4, "little") + b"INFO"
    header += b"data" + (0).to_bytes(4, "little")
    path.write_bytes(header + pcm)
    return pcm


def test_read_wav_tail_returns_last_samples_despite_wrong_header(tmp_path):
    path = tmp_path / "growing.wav"
    _write_growing_wav(path, list(range(1000)))
    # 0.01s a 16kHz = 160 campioni
    tail = daemon._read_wav_tail(str(path), 0.01)
    assert len(tail) == 320
    assert struct.unpack("<h", tail[-2:])[0] == 999


def test_read_wav_tail_finds_data_after_extra_chunks(tmp_path):
    path = tmp_path / "with_list.wav"
    _write_growing_wav(path, list(range(500)), extra_chunk=True)
    tail = daemon._read_wav_tail(str(path), 10)
    assert len(tail) == 1000
    assert struct.unpack("<h", tail[:2])[0] == 0


def test_read_wav_tail_on_empty_file_returns_nothing(tmp_path):
    """Il file esiste ma la cattura non ha ancora scritto l'header."""
    path = tmp_path / "empty.wav"
    path.write_bytes(b"")
    assert daemon._read_wav_tail(str(path), 3) == b""


def test_silence_detection():
    assert daemon._is_silence(struct.pack("<1000h", *([0] * 1000)))
    assert daemon._is_silence(b"")
    loud = struct.pack("<1000h", *([8000, -8000] * 500))
    assert not daemon._is_silence(loud)


def _window(voice_rms, voice_seconds=0.5, total=3.0, background_rms=75):
    """Finestra di ascolto realistica: una parola breve dentro parecchio
    silenzio, come quando si dice "Jarvis" e si aspetta."""
    voiced = int(16000 * voice_seconds)
    quiet = int(16000 * (total - voice_seconds))
    samples = [int(voice_rms * math.sin(i / 3)) for i in range(voiced)]
    samples += [int(background_rms * math.sin(i / 7)) for i in range(quiet)]
    return struct.pack("<%dh" % len(samples), *samples)


def test_short_word_at_normal_volume_is_not_mistaken_for_silence():
    """Regressione: mediando il livello su tutta la finestra, l'energia di una
    parola di mezzo secondo si annacquava nei tre secondi di silenzio attorno.
    Una frase detta senza stare a un palmo dal microfono veniva buttata via
    prima di essere trascritta, e l'attivazione dal PC non scattava mai."""
    assert not daemon._is_silence(_window(500))
    assert not daemon._is_silence(_window(800))


def test_quiet_microphone_still_hears_speech():
    """Su questo PC il fondo sta a 75 e un suono riprodotto arriva a 116: con
    una soglia fissa alta il parlato di un microfono poco sensibile verrebbe
    scartato sempre."""
    assert not daemon._is_silence(_window(250, background_rms=75))


def test_sensitive_microphone_does_not_trigger_on_its_own_noise():
    """Il rovescio: dove il fondo e' alto, un livello che altrove sarebbe voce
    non deve svegliare il modello a ogni giro."""
    assert daemon._is_silence(_window(600, background_rms=400))


def test_continuous_speech_is_not_taken_for_silence():
    """Parlando senza pause per tutta la finestra, il tratto piu' silenzioso
    vale quanto il piu' sonoro: il confronto da solo direbbe "silenzio"."""
    assert not daemon._is_silence(_window(2000, voice_seconds=3.0))


def test_background_noise_alone_is_still_silence():
    # il contrario non deve succedere: senza voce non si sveglia il modello
    assert daemon._is_silence(_window(75, voice_seconds=0.0))
    assert daemon._is_silence(_window(10, voice_seconds=0.0, background_rms=10))


# --- reazione del demone ---------------------------------------------------


class FakeListener:
    def __init__(self):
        self.calls = []

    def follow_recording(self, path):
        self.calls.append(("recording", path))

    def follow_own_capture(self):
        self.calls.append(("own", None))

    def start(self):
        self.calls.append(("start", None))

    def stop(self):
        self.calls.append(("stop", None))


def _prepare(app, monkeypatch):
    app.wake_listener = FakeListener()
    monkeypatch.setattr(app, "_pause_media_for_recording", lambda: None)
    monkeypatch.setattr(app, "_resume_media_after_recording", lambda: None)
    monkeypatch.setattr(app, "_broadcast", lambda msg: None)
    monkeypatch.setattr(app, "_set_state", lambda state: setattr(app, "state", state))
    app.backend = type(
        "B",
        (),
        {
            "start_recording": lambda self, path, slot="main": None,
            "stop_recording": lambda self, slot="main": None,
            "read_clipboard": lambda self: None,
        },
    )()
    return app


def test_start_recording_switches_listener_to_the_dictation_file(
    daemon_app, monkeypatch
):
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._start_recording()
    try:
        assert app.wake_listener.calls == [("recording", app.record_file)]
    finally:
        # _start_recording crea un file temporaneo che di norma viene
        # consumato dalla trascrizione, qui saltata
        os.unlink(app.record_file)


def test_stop_recording_returns_listener_to_its_own_capture(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._start_recording()
    path = app.record_file
    monkeypatch.setattr(app, "_transcribe_worker", lambda path: None)
    app._stop_recording_and_transcribe()
    try:
        assert app.wake_listener.calls[-1] == ("own", None)
    finally:
        os.unlink(path)


def test_wake_start_is_ignored_while_already_recording(daemon_app, monkeypatch):
    """Fra il riconoscimento e l'esecuzione l'utente puo' aver toccato il
    pulsante: la frase di avvio non deve trasformarsi in uno stop."""
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_RECORDING
    calls = []
    monkeypatch.setattr(app, "toggle_recording", lambda: calls.append("toggle"))
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: calls.append("stop")
    )
    app._on_wake_word("start")
    assert calls == []


def test_wake_stop_is_ignored_when_not_recording(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_TRANSCRIBING
    calls = []
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: calls.append("stop")
    )
    app._on_wake_word("stop")
    assert calls == []


def test_wake_start_from_idle_starts_the_normal_dictation(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_IDLE
    calls = []
    monkeypatch.setattr(app, "toggle_recording", lambda: calls.append("toggle"))
    app._on_wake_word("start")
    assert calls == ["toggle"]


# --- configurazione --------------------------------------------------------


def test_set_wake_phrases_persist(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    ok, error = app._set_wake_phrase_start("computer")
    assert ok, error
    ok, error = app._set_wake_phrase_stop("computer basta")
    assert ok, error
    assert daemon._load_wake_phrase_start() == "computer"
    assert daemon._load_wake_phrase_stop() == "computer basta"


def test_set_wake_phrase_rejects_duplicate_of_the_other(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app.wake_phrase_stop = "jarvis stop"
    ok, error = app._set_wake_phrase_start("Jarvis, stop!")
    assert not ok
    assert "diverse" in error
    assert app.wake_phrase_start == daemon.WAKE_PHRASE_START_DEFAULT


def test_set_wake_phrase_rejects_too_short(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    ok, error = app._set_wake_phrase_start("ok")
    assert not ok
    assert "caratteri" in error


def test_set_wake_word_enabled_starts_and_stops_the_listener(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_IDLE
    ok, error = app._set_wake_word_enabled(True)
    assert ok, error
    assert app.wake_listener.calls == [("start", None)]
    assert daemon._load_wake_word_enabled() is True

    ok, error = app._set_wake_word_enabled(False)
    assert ok, error
    assert app.wake_listener.calls[-1] == ("stop", None)
    assert daemon._load_wake_word_enabled() is False


def test_enabling_during_dictation_listens_to_the_dictation_file(
    daemon_app, monkeypatch
):
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_RECORDING
    app.record_file = "/tmp/in-corso.wav"
    app._set_wake_word_enabled(True)
    assert app.wake_listener.calls == [
        ("start", None),
        ("recording", "/tmp/in-corso.wav"),
    ]


def test_set_wake_word_enabled_rejects_non_boolean(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    ok, error = app._set_wake_word_enabled("si")
    assert not ok
    assert "booleano" in error


# --- integrazione con la trascrizione --------------------------------------


def test_transcription_strips_phrases_only_when_wake_word_is_on(
    daemon_app, monkeypatch, tmp_path
):
    app = _prepare(daemon_app, monkeypatch)
    wav = tmp_path / "dettatura.wav"
    wav.write_bytes(b"x" * 200)  # oltre la soglia dei 100 byte
    monkeypatch.setattr(
        app.model, "transcribe", lambda *a, **k: "Jarvis ciao mondo Jarvis stop"
    )

    app.wake_word_enabled = False
    app._transcribe_worker(str(wav))
    assert app.command_queue.get()[1] == "Jarvis ciao mondo Jarvis stop"

    wav.write_bytes(b"x" * 200)
    app.wake_word_enabled = True
    app._transcribe_worker(str(wav))
    assert app.command_queue.get()[1] == "ciao mondo"


# --- tolleranza agli errori di trascrizione ---------------------------------


def test_edit_distance_counts_corrections():
    assert daemon._edit_distance("jarvis", "jarvis") == 0
    assert daemon._edit_distance("jarvis", "giarvis") == 2
    assert daemon._edit_distance("jarvis", "arrivi") == 4
    assert daemon._edit_distance("jarvis stop", "jarvis top") == 1


def test_at_least_one_error_is_always_allowed():
    assert daemon._max_edits("abc") >= 1
    assert daemon._max_edits("jarvis") == 2
    assert daemon._max_edits("jarvis stop") == 3


def test_dropped_letter_still_stops_the_dictation():
    """Il caso segnalato: detto "Jarvis stop", il riconoscitore scrive
    "Jarvis Top"."""
    assert daemon._phrase_in_text("jarvis stop", "Jarvis Top")


def test_stop_phrase_is_looked_for_only_at_the_end_when_asked():
    """Durante la dettatura la frase di stop va cercata in fondo: trovarla in
    mezzo significherebbe interrompere l'utente a meta' di una frase."""
    heard = "jarvis stop e poi continuo a dettare un testo lungo"
    assert daemon._phrase_in_text("jarvis stop", heard)
    assert not daemon._phrase_in_text("jarvis stop", heard, only_tail=True)
    coda = "scrivi una mail a Marco per la riunione jarvis stop"
    assert daemon._phrase_in_text("jarvis stop", coda, only_tail=True)


# --- varianti della frase ---------------------------------------------------
# Il riconoscitore scrive quello che sente nella lingua di dettatura: "Jarvis"
# detto in italiano diventa "già visto". Sul PC si corregge col suggerimento al
# modello, sul telefono no: li' l'utente elenca le varianti separate da virgola.


def test_variants_are_split_and_normalized():
    assert daemon._phrase_variants("jarvis, già visto , Ciarvis") == [
        "jarvis",
        "gia visto",
        "ciarvis",
    ]
    assert daemon._phrase_variants("jarvis, jarvis") == ["jarvis"]
    assert daemon._phrase_variants("") == []


def test_any_variant_triggers_the_match():
    phrase = "jarvis, già visto"
    assert daemon._phrase_in_text(phrase, "Già visto")
    assert daemon._phrase_in_text(phrase, "Jarvis")
    assert not daemon._phrase_in_text(phrase, "andiamo al mare")


def test_variant_too_short_makes_the_whole_phrase_invalid():
    # basterebbe una variante di due lettere per far scattare l'attivazione
    # dentro le parole di una conversazione qualsiasi
    assert not daemon._valid_wake_phrase("jarvis, ok")
    assert daemon._valid_wake_phrase("jarvis, già visto")


def test_prompt_only_suggests_the_intended_spelling():
    """Le varianti sono le rese sbagliate: suggerirle al modello lo
    spingerebbe proprio verso quelle."""
    prompt = daemon._wake_prompt("Jarvis, già visto", "Jarvis stop, già visto stop")
    assert prompt == "Jarvis. Jarvis stop."


def test_strip_removes_the_italian_rendering_at_the_end(daemon_app, monkeypatch):
    """Il caso segnalato: dicendo "Jarvis stop" il modello scrive "già visto"
    e quella coda restava nel testo incollato."""
    text = "questo e' il testo da incollare già visto"
    assert (
        daemon._strip_wake_phrases(text, "jarvis", "jarvis stop, già visto")
        == "questo e' il testo da incollare"
    )


def test_strip_handles_variants_of_different_length():
    text = "ciao mondo ciarvis stop"
    assert (
        daemon._strip_wake_phrases(text, "jarvis", "jarvis stop, ciarvis stop, gia visto")
        == "ciao mondo"
    )


# --- le frasi vanno suggerite anche al modello della dettatura ---------------


def test_dictation_vocabulary_includes_the_phrases_when_listening_on_pc(
    daemon_app, monkeypatch
):
    app = daemon_app
    app.wake_word_enabled = True
    vocabulary = app._vocabulary_for_dashboard(None)
    assert "jarvis" in vocabulary.lower()


def test_dictation_vocabulary_includes_the_phrases_when_started_by_phone_voice(
    daemon_app, monkeypatch
):
    """L'ascolto e' sul telefono (wake_word_enabled resta spento sul PC) ma le
    frasi finiscono comunque nell'audio registrato dal PC."""
    app = daemon_app
    app.wake_word_enabled = False
    app._recording_from_wake = True
    assert "jarvis" in app._vocabulary_for_dashboard(None).lower()


def test_dictation_vocabulary_untouched_without_voice_activation(daemon_app):
    app = daemon_app
    app.wake_word_enabled = False
    app._recording_from_wake = False
    assert app._vocabulary_for_dashboard(None) == ""


def test_phone_voice_press_marks_the_recording(daemon_app, monkeypatch):
    """Il telefono segnala con source="wake" che a premere e' stata la voce."""
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app.layout = {
        "dashboards": [
            {
                "id": "d",
                "name": "d",
                "rows": 1,
                "cols": 1,
                "buttons": [
                    {"id": "record", "kind": "record", "row": 0, "col": 0}
                ],
            }
        ]
    }
    monkeypatch.setattr(app, "toggle_recording", lambda **kw: None)
    app._handle_button_press("record", source="wake")
    assert app._recording_from_wake is True

    # un tocco normale non deve lasciare il segno alla dettatura successiva
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record")
    assert app._recording_from_wake is False


def test_voice_stop_after_a_finger_start_still_strips(daemon_app, monkeypatch):
    """Avviata col dito e fermata a voce: la frase di stop e' comunque
    nell'audio."""
    app = _prepare(daemon_app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app.layout = {
        "dashboards": [
            {
                "id": "d",
                "name": "d",
                "rows": 1,
                "cols": 1,
                "buttons": [
                    {"id": "record", "kind": "record", "row": 0, "col": 0}
                ],
            }
        ]
    }
    monkeypatch.setattr(app, "toggle_recording", lambda **kw: None)
    app._handle_button_press("record")  # dito
    app.state = daemon.STATE_RECORDING
    app._handle_button_press("record", source="wake")  # voce
    assert app._recording_from_wake is True


# --- listener: commutazione della sorgente ----------------------------------


def _listener():
    return daemon.WakeWordListener(
        backend=None,
        command_queue=queue.Queue(),
        phrases_provider=lambda: ("jarvis", "jarvis stop"),
        language_provider=lambda: "it",
    )


def test_listener_ignores_source_changes_while_stopped():
    """Con l'ascolto spento le chiamate del demone non devono fare nulla: e'
    quello che succede per tutti gli utenti che non usano la funzione."""
    listener = _listener()
    listener.follow_recording("/tmp/x.wav")
    listener.follow_own_capture()
    assert listener._source is None
    assert not listener.running


def test_listener_tick_does_nothing_without_a_source():
    listener = _listener()
    listener._tick()  # non deve sollevare
    assert listener._queue.empty()


class FakeCaptureBackend:
    """Backend che tiene traccia degli slot aperti, senza toccare l'audio."""

    def __init__(self):
        self.open_slots = []
        self.started = []

    def start_recording(self, path, slot="main"):
        self.open_slots.append(slot)
        self.started.append((slot, path))

    def stop_recording(self, slot="main"):
        if slot in self.open_slots:
            self.open_slots.remove(slot)


def _listener_with_backend():
    listener = daemon.WakeWordListener(
        FakeCaptureBackend(),
        queue.Queue(),
        lambda: ("jarvis", "jarvis stop"),
        lambda: "it",
    )
    listener._running = True  # senza far partire il thread
    return listener


def test_rotation_does_not_leave_the_listener_deaf(monkeypatch):
    """La rotazione del file non e' un riconoscimento appena avvenuto: se
    applicasse la pausa piena, l'ascolto sarebbe sordo per qualche secondo
    ogni minuto."""
    listener = _listener_with_backend()
    listener._start_own_capture()
    listener._own_started_at = time.monotonic() - daemon.WAKE_ROTATE_SECONDS - 1
    before = time.monotonic()
    listener._rotate_own_capture_if_needed()
    assert listener._muted_until - before <= daemon.WAKE_ROTATE_MUTE_SECONDS + 0.1
    # la cattura resta una sola: quella vecchia va chiusa prima di riaprire
    assert listener._backend.open_slots == ["wake"]
    listener.stop()


def test_returning_from_a_dictation_waits_before_listening_again():
    """Dopo la frase di stop la sua coda e' ancora nel microfono: senza pausa
    la dettatura ripartirebbe da sola."""
    listener = _listener_with_backend()
    before = time.monotonic()
    listener.follow_own_capture()
    assert listener._muted_until - before >= daemon.WAKE_COOLDOWN_SECONDS - 0.1
    listener.stop()


def test_following_a_dictation_closes_the_listener_own_capture():
    listener = _listener_with_backend()
    listener.follow_own_capture()
    assert listener._backend.open_slots == ["wake"]
    listener.follow_recording("/tmp/dettatura.wav")
    # durante la dettatura si legge il file di quella: nessuna cattura propria
    assert listener._backend.open_slots == []
    assert listener._source == ("recording", "/tmp/dettatura.wav")
    listener.stop()


def test_stopping_the_listener_closes_the_microphone(tmp_path):
    listener = _listener_with_backend()
    listener.follow_own_capture()
    path = listener._own_path
    listener.stop()
    assert listener._backend.open_slots == []
    assert not os.path.exists(path)

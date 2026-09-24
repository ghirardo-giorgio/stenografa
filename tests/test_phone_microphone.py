"""Il microfono del telefono al posto di quello del PC, con la trascrizione
che resta sulla GPU del PC.

Serve quando il microfono del PC non e' utilizzabile — occupato da un'altra
applicazione, come la webcam che lo ospita — ma la scheda grafica c'e' ed e'
molto piu' brava del telefono a trascrivere.

E' l'opposto di test_phone_transcription.py: li' il telefono consegna il testo
gia' fatto, qui consegna l'audio e basta."""
import base64
import os
import wave

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


def _pcm(nbytes):
    """Audio finto: quello che conta e' quanti byte arrivano, non cosa dicono."""
    return bytes(range(256)) * (nbytes // 256) + bytes(nbytes % 256)


# --- il file scritto dal telefono e' un WAV come gli altri ---


def test_the_written_file_is_a_readable_wav(tmp_path):
    percorso = str(tmp_path / "dettatura.wav")
    sink = daemon.PhoneAudioSink(percorso)
    sink.append(_pcm(3200))
    sink.append(_pcm(3200))
    sink.close()

    with wave.open(percorso, "rb") as letto:
        assert letto.getnchannels() == 1
        assert letto.getsampwidth() == 2
        assert letto.getframerate() == 16000
        # 6400 byte a 2 byte per campione
        assert letto.getnframes() == 3200


def test_the_length_is_wrong_until_it_is_closed(tmp_path):
    """Come pw-record: finche' si registra l'header dichiara zero. Non e' un
    difetto, e' la condizione in cui _read_wav_tail sa gia' lavorare."""
    percorso = str(tmp_path / "dettatura.wav")
    sink = daemon.PhoneAudioSink(percorso)
    sink.append(_pcm(3200))

    with open(percorso, "rb") as fh:
        fh.seek(daemon.PhoneAudioSink._DATA_SIZE_OFFSET)
        assert int.from_bytes(fh.read(4), "little") == 0

    sink.close()
    with open(percorso, "rb") as fh:
        fh.seek(daemon.PhoneAudioSink._DATA_SIZE_OFFSET)
        assert int.from_bytes(fh.read(4), "little") == 3200


def test_the_silence_watchdog_can_read_while_it_grows(tmp_path):
    """La chiusura automatica per silenzio guarda la coda del file mentre
    viene scritto: se non riuscisse a leggerla, in questa modalita' non
    scatterebbe mai."""
    percorso = str(tmp_path / "dettatura.wav")
    sink = daemon.PhoneAudioSink(percorso)
    sink.append(_pcm(32000))  # un secondo di audio

    coda = daemon._read_wav_tail(percorso, 0.5)
    assert len(coda) == 16000
    sink.close()


def test_closing_twice_is_harmless(tmp_path):
    """Lo stop puo' arrivare due volte (pulsante e silenzio quasi insieme)."""
    percorso = str(tmp_path / "dettatura.wav")
    sink = daemon.PhoneAudioSink(percorso)
    sink.append(_pcm(320))
    sink.close()
    sink.close()
    with wave.open(percorso, "rb") as letto:
        assert letto.getnframes() == 160


def test_audio_after_the_close_is_dropped(tmp_path):
    """I blocchi ancora in volo quando la dettatura si chiude non devono
    finire nel file gia' sigillato."""
    percorso = str(tmp_path / "dettatura.wav")
    sink = daemon.PhoneAudioSink(percorso)
    sink.append(_pcm(320))
    sink.close()
    sink.append(_pcm(320))
    with wave.open(percorso, "rb") as letto:
        assert letto.getnframes() == 160


# --- il ciclo di vita della dettatura ---


def test_the_pc_microphone_stays_closed(daemon_app, monkeypatch):
    """E' il motivo per cui questa modalita' esiste: la scheda audio del PC
    non va toccata."""
    app = daemon_app
    aperture = _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE

    app._handle_button_press("record", mic="phone")

    assert app._recording_mic_from_phone is True
    assert app.state == daemon.STATE_RECORDING
    assert aperture == []
    # il file pero' esiste: e' il telefono a riempirlo
    assert app.record_file is not None
    assert app._phone_audio is not None
    app._close_phone_audio()
    os.unlink(app.record_file)


def test_the_audio_from_the_phone_lands_in_the_recording(daemon_app, monkeypatch):
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", mic="phone")
    percorso = app.record_file

    app._append_phone_audio(base64.b64encode(_pcm(3200)).decode("ascii"))
    app._append_phone_audio(base64.b64encode(_pcm(3200)).decode("ascii"))
    app._close_phone_audio()

    with wave.open(percorso, "rb") as letto:
        assert letto.getnframes() == 3200
    os.unlink(percorso)


def test_the_pc_transcribes_what_the_phone_recorded(daemon_app, monkeypatch):
    """Il punto di tutta la modalita': l'audio viene dal telefono, il modello
    che lo legge e' quello del PC."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", mic="phone")
    percorso = app.record_file
    app._append_phone_audio(base64.b64encode(_pcm(32000)).decode("ascii"))

    trascritti = []

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(daemon.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        app.model,
        "transcribe",
        lambda path, **kw: trascritti.append(path) or "quello che ho detto",
    )

    app._stop_recording_and_transcribe()

    # il file era chiuso e leggibile nel momento in cui il modello l'ha aperto
    assert trascritti == [percorso]
    assert app.command_queue.get_nowait() == ("done", "quello che ho detto", None)
    assert app._phone_audio is None
    assert not os.path.exists(percorso)


def test_audio_without_a_recording_is_dropped(daemon_app, monkeypatch):
    """Blocchi in ritardo, arrivati quando la dettatura era gia' chiusa: non
    devono far cadere la connessione ne' aprire file dal nulla."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._append_phone_audio(base64.b64encode(_pcm(320)).decode("ascii"))
    assert app._phone_audio is None


def test_a_malformed_block_does_not_break_the_dictation(daemon_app, monkeypatch):
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", mic="phone")
    percorso = app.record_file

    app._append_phone_audio("questo non e' base64!!")
    app._append_phone_audio(base64.b64encode(_pcm(320)).decode("ascii"))
    app._close_phone_audio()

    with wave.open(percorso, "rb") as letto:
        assert letto.getnframes() == 160
    os.unlink(percorso)


def test_the_silence_watchdog_is_armed(daemon_app, monkeypatch):
    """A differenza della trascrizione sul telefono, qui il file esiste: la
    chiusura automatica per silenzio deve restare in funzione."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    sorvegliati = []
    monkeypatch.setattr(
        app, "_start_silence_watchdog", lambda path: sorvegliati.append(path)
    )

    app._handle_button_press("record", mic="phone")

    assert sorvegliati == [app.record_file]
    app._close_phone_audio()
    os.unlink(app.record_file)


def test_a_disconnection_closes_the_dictation(daemon_app, monkeypatch):
    """Senza il telefono non arrivera' piu' audio: restare in registrazione
    vorrebbe dire aspettare per sempre."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", mic="phone")
    percorso = app.record_file

    fermate = []
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: fermate.append(True)
    )
    app._on_phone_audio_lost()

    assert fermate == [True]
    app._close_phone_audio()
    os.unlink(percorso)


def test_a_disconnection_is_ignored_when_the_pc_records(daemon_app, monkeypatch):
    """Un telefono che si scollega mentre detta il PC non c'entra niente con
    quella dettatura."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record")
    percorso = app.record_file

    fermate = []
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: fermate.append(True)
    )
    app._on_phone_audio_lost()

    assert fermate == []
    os.unlink(percorso)


def test_the_flag_does_not_survive_the_dictation(daemon_app, monkeypatch):
    """La dettatura successiva potrebbe usare il microfono del PC."""
    app = daemon_app
    _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE
    app._handle_button_press("record", mic="phone")
    app._close_phone_audio()
    os.unlink(app.record_file)

    app._net_clients = set()
    app._on_transcription_done(None, None)

    assert app._recording_mic_from_phone is False


def test_the_pc_records_normally_without_the_field(daemon_app, monkeypatch):
    """Le versioni dell'app che non mandano "mic" continuano a funzionare."""
    app = daemon_app
    aperture = _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE

    app._handle_button_press("record")

    assert app._recording_mic_from_phone is False
    assert app._phone_audio is None
    assert len(aperture) == 1
    os.unlink(app.record_file)


def test_push_to_talk_keeps_the_pc_microphone_closed(daemon_app, monkeypatch):
    """Tenendo premuto per parlare la pressione arriva separata dal rilascio:
    anche li' chi registra e' il telefono, altrimenti l'impostazione varrebbe
    solo per il tocco singolo."""
    app = daemon_app
    aperture = _prepare(app, monkeypatch)
    app.state = daemon.STATE_IDLE

    app._handle_button_press("record", phase="down", mic="phone")

    assert app._recording_mic_from_phone is True
    assert app.state == daemon.STATE_RECORDING
    assert aperture == []
    assert app._phone_audio is not None
    app._close_phone_audio()
    os.unlink(app.record_file)

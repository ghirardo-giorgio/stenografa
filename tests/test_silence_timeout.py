"""Chiusura automatica della dettatura dopo un silenzio prolungato: passati
tot secondi senza sentire parlare, si trascrive e si incolla quello che si e'
raccolto invece di restare a registrare (vedi SILENCE_TIMEOUT_DEFAULT).

Serve soprattutto all'attivazione vocale, dove capita di dimenticare la frase
di stop, ma vale per qualunque dettatura a interruttore."""
import math
import struct
import wave

import daemon


def _write_wav(path, samples):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(struct.pack("<%dh" % len(samples), *samples))


def _silence(seconds, level=40):
    n = int(16000 * seconds)
    return [int(level * math.sin(i / 7)) for i in range(n)]


def _speech(seconds, level=3000):
    n = int(16000 * seconds)
    return [int(level * math.sin(i / 3)) for i in range(n)]


# --- lettura della coda -----------------------------------------------------


def _voce_per(secondi, monkeypatch):
    """Sostituisce il rilevatore di voce: i toni sintetici di questi test non
    sono voce umana e il rilevatore — giustamente — non li riconoscerebbe.
    Qui interessa la decisione che il demone prende a partire dal suo esito."""
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: secondi)


def test_tail_is_silent_after_a_long_pause(tmp_path, monkeypatch):
    _voce_per(0.0, monkeypatch)
    path = tmp_path / "dettatura.wav"
    _write_wav(path, _silence(11))
    assert daemon._tail_is_silent(str(path), 10)


def test_tail_is_not_silent_while_still_talking(tmp_path, monkeypatch):
    _voce_per(2.5, monkeypatch)
    path = tmp_path / "dettatura.wav"
    _write_wav(path, _silence(11))
    # negli ultimi 10 secondi si e' parlato: la dettatura non va chiusa
    assert not daemon._tail_is_silent(str(path), 10)


def test_a_hint_of_voice_is_not_enough_to_keep_it_open(tmp_path, monkeypatch):
    """Il rilevatore prende ogni tanto un colpo secco per voce: sotto la
    frazione di secondo non deve contare."""
    _voce_per(0.2, monkeypatch)
    path = tmp_path / "colpo.wav"
    _write_wav(path, _silence(11))
    assert daemon._tail_is_silent(str(path), 10)


def test_falls_back_to_levels_without_the_voice_detector(tmp_path, monkeypatch):
    """Se il rilevatore non e' disponibile si torna a guardare l'energia:
    meno affidabile, ma meglio che lasciare la dettatura aperta per sempre."""
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: None)
    muta = tmp_path / "muta.wav"
    _write_wav(muta, _silence(11))
    assert daemon._tail_is_silent(str(muta), 10)

    parlata = tmp_path / "parlata.wav"
    _write_wav(parlata, _silence(5) + _speech(1) + _silence(5))
    assert not daemon._tail_is_silent(str(parlata), 10)


def test_recording_shorter_than_the_window_is_never_silent(tmp_path):
    """Altrimenti una dettatura appena iniziata verrebbe chiusa subito, per il
    solo fatto di non avere ancora dieci secondi di audio."""
    path = tmp_path / "appena_iniziata.wav"
    _write_wav(path, _silence(3))
    assert not daemon._tail_is_silent(str(path), 10)


def test_tail_on_missing_file_is_not_silent(tmp_path):
    assert not daemon._tail_is_silent(str(tmp_path / "non-esiste.wav"), 10)


def test_fallback_survives_fluctuating_room_noise(tmp_path, monkeypatch):
    """Regressione dalla prova dal vivo: il rumore di una stanza respira, e
    prendendo il tratto piu' quieto come riferimento le sue stesse fluttuazioni
    passavano per parlato."""
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: None)
    rumore = []
    for blocco in range(50):
        rumore += _silence(0.2, level=30 + (blocco % 5) * 22)
    path = tmp_path / "stanza_vuota.wav"
    _write_wav(path, rumore)
    assert daemon._tail_is_silent(str(path), 10)


def test_fallback_ignores_an_isolated_bang(tmp_path, monkeypatch):
    """Una porta che sbatte non e' qualcuno che parla."""
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: None)
    path = tmp_path / "colpo.wav"
    _write_wav(path, _silence(5) + _speech(0.2) + _silence(5.5))
    assert daemon._tail_is_silent(str(path), 10)


# --- impostazione -----------------------------------------------------------


def test_default_is_ten_seconds():
    assert daemon.SILENCE_TIMEOUT_DEFAULT == 10


def test_setting_accepts_zero_and_valid_range(daemon_app):
    ok, error = daemon_app._set_silence_timeout(0)
    assert ok, error
    assert daemon_app.silence_timeout == 0
    assert daemon._load_silence_timeout() == 0

    ok, error = daemon_app._set_silence_timeout(20)
    assert ok, error
    assert daemon._load_silence_timeout() == 20


def test_setting_rejects_values_that_would_cut_a_pause(daemon_app):
    ok, error = daemon_app._set_silence_timeout(1)
    assert not ok
    assert "secondi" in error
    ok, _ = daemon_app._set_silence_timeout(9999)
    assert not ok
    ok, error = daemon_app._set_silence_timeout("dieci")
    assert not ok


def test_setting_rejects_booleans(daemon_app):
    # True varrebbe 1 secondo: una pausa per respirare chiuderebbe la dettatura
    ok, _ = daemon_app._set_silence_timeout(True)
    assert not ok


def test_broken_config_falls_back_to_the_default(daemon_app):
    daemon._write_config_key("silence_timeout", "molto")
    assert daemon._load_silence_timeout() == daemon.SILENCE_TIMEOUT_DEFAULT
    daemon._write_config_key("silence_timeout", 99999)
    assert daemon._load_silence_timeout() == daemon.SILENCE_TIMEOUT_DEFAULT


# --- reazione del demone ----------------------------------------------------


def test_stops_the_dictation_when_the_silence_is_reported(daemon_app, monkeypatch):
    app = daemon_app
    app.state = daemon.STATE_RECORDING
    app.record_file = "/tmp/dettatura.wav"
    monkeypatch.setattr(app, "_notify", lambda *a, **kw: None)
    fermate = []
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: fermate.append(True)
    )
    app._on_silence_timeout_reached("/tmp/dettatura.wav")
    assert fermate == [True]


def test_ignores_a_stale_report_from_a_previous_dictation(daemon_app, monkeypatch):
    """Fra il rilevamento e l'esecuzione l'utente puo' aver fermato e
    riavviato: chiudere qui interromperebbe la dettatura nuova."""
    app = daemon_app
    app.state = daemon.STATE_RECORDING
    app.record_file = "/tmp/nuova.wav"
    fermate = []
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: fermate.append(True)
    )
    app._on_silence_timeout_reached("/tmp/vecchia.wav")
    assert fermate == []


def test_ignores_the_report_when_no_longer_recording(daemon_app, monkeypatch):
    app = daemon_app
    app.state = daemon.STATE_TRANSCRIBING
    app.record_file = None
    fermate = []
    monkeypatch.setattr(
        app, "_stop_recording_and_transcribe", lambda: fermate.append(True)
    )
    app._on_silence_timeout_reached("/tmp/dettatura.wav")
    assert fermate == []


def test_watchdog_is_not_started_when_disabled(daemon_app):
    app = daemon_app
    app.silence_timeout = 0
    app._start_silence_watchdog("/tmp/dettatura.wav")
    assert app._silence_thread is None


def test_watchdog_reports_the_silence(daemon_app, tmp_path, monkeypatch):
    """Prova d'insieme: sentita una prima parola, il controllo periodico legge
    la coda del file e segnala il silenzio."""
    app = daemon_app
    app.silence_timeout = daemon.SILENCE_TIMEOUT_MIN
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: 1.0)
    monkeypatch.setattr(daemon, "_tail_is_silent", lambda path, sec: True)
    path = tmp_path / "muta.wav"
    _write_wav(path, _silence(daemon.SILENCE_TIMEOUT_MIN + 1))
    app._start_silence_watchdog(str(path))
    try:
        kind, reported = app.command_queue.get(timeout=5)
    finally:
        app._stop_silence_watchdog()
    assert kind == "silence_timeout"
    assert reported == str(path)


def test_watchdog_waits_for_the_first_word(daemon_app, tmp_path, monkeypatch):
    """Il caso segnalato: si tocca il pulsante e si comincia a parlare qualche
    secondo dopo. Contando il silenzio da subito, la dettatura veniva chiusa
    prima ancora di cominciare e tornava "Nessun testo rilevato"."""
    import queue

    import pytest

    app = daemon_app
    app.silence_timeout = daemon.SILENCE_TIMEOUT_MIN
    # nessuna voce, mai: la coda e' muta ma non si e' ancora parlato
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: 0.0)
    monkeypatch.setattr(daemon, "_tail_is_silent", lambda path, sec: True)
    path = tmp_path / "in_attesa.wav"
    _write_wav(path, _silence(daemon.SILENCE_TIMEOUT_MIN + 1))
    app._start_silence_watchdog(str(path))
    try:
        with pytest.raises(queue.Empty):
            app.command_queue.get(timeout=3)
    finally:
        app._stop_silence_watchdog()


def test_watchdog_counts_from_the_start_without_the_voice_detector(
    daemon_app, tmp_path, monkeypatch
):
    """Senza rilevatore non si puo' sapere quando si e' cominciato a parlare:
    si torna a contare dall'inizio, com'era prima."""
    app = daemon_app
    app.silence_timeout = daemon.SILENCE_TIMEOUT_MIN
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: None)
    monkeypatch.setattr(daemon, "_tail_is_silent", lambda path, sec: True)
    path = tmp_path / "muta.wav"
    _write_wav(path, _silence(daemon.SILENCE_TIMEOUT_MIN + 1))
    app._start_silence_watchdog(str(path))
    try:
        kind, _ = app.command_queue.get(timeout=5)
    finally:
        app._stop_silence_watchdog()
    assert kind == "silence_timeout"


def test_watchdog_stays_quiet_while_speaking(daemon_app, tmp_path, monkeypatch):
    import queue

    import pytest

    app = daemon_app
    app.silence_timeout = daemon.SILENCE_TIMEOUT_MIN
    monkeypatch.setattr(daemon, "_speech_seconds", lambda pcm: 2.0)
    path = tmp_path / "parlata.wav"
    _write_wav(path, _silence(daemon.SILENCE_TIMEOUT_MIN + 1))
    app._start_silence_watchdog(str(path))
    try:
        with pytest.raises(queue.Empty):
            app.command_queue.get(timeout=3)
    finally:
        app._stop_silence_watchdog()

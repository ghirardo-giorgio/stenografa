"""Test della rete di sicurezza sulla registrazione "a interruttore"
(RECORDING_MAX_DURATION_SECONDS): un tocco accidentale che avvia la
registrazione e non viene mai fermato manualmente deve interrompersi da
solo dopo il limite. Non si applica a "tieni premuto per parlare"
(fase "down"/"up"), gia' limitata dalla durata della pressione."""
import daemon as daemon_module


class FakeBackend:
    def __init__(self):
        self.notifications = []
        self.recording_started = []
        self.recording_stopped = 0

    def read_clipboard(self):
        return None

    def start_recording(self, path):
        self.recording_started.append(path)

    def stop_recording(self):
        self.recording_stopped += 1

    def notify(self, title, body, urgency="normal"):
        self.notifications.append((title, body, urgency))


class FakeTimer:
    """Sostituisce threading.Timer: registra l'intervallo/la funzione senza
    avviare un vero thread in background, cosi' i test non aspettano
    davvero RECORDING_MAX_DURATION_SECONDS (3 minuti)."""

    instances = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class NoOpThread:
    """threading.Thread finto che non esegue mai il target: usato per
    _stop_recording_and_transcribe, che altrimenti lancerebbe una vera
    trascrizione in background (irrilevante per questi test)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        pass

    def start(self):
        pass


def _prepare(daemon_app, monkeypatch):
    daemon_app.backend = FakeBackend()
    daemon_app.state = daemon_module.STATE_IDLE
    monkeypatch.setattr(daemon_app.model, "preload", lambda: None)
    monkeypatch.setattr(daemon_module.threading, "Timer", FakeTimer)
    FakeTimer.instances.clear()
    return daemon_app


def test_tap_start_arms_watchdog_timer(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app._start_recording(phase="tap")

    assert len(FakeTimer.instances) == 1
    timer = FakeTimer.instances[0]
    assert timer.interval == daemon_module.RECORDING_MAX_DURATION_SECONDS
    assert timer.function == app._on_recording_timeout
    assert timer.daemon is True
    assert timer.started is True
    assert app._recording_watchdog_timer is timer


def test_default_phase_is_tap_and_arms_watchdog(daemon_app, monkeypatch):
    """Il default di _start_recording deve restare "tap": sia il toggle da
    telefono (kind="button") sia la scorciatoia da tastiera GNOME (kind=
    "toggle") chiamano _start_recording senza specificare la fase."""
    app = _prepare(daemon_app, monkeypatch)
    app._start_recording()

    assert len(FakeTimer.instances) == 1
    assert app._recording_watchdog_timer is not None


def test_push_to_talk_down_does_not_arm_watchdog(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app._start_recording(phase="down")

    assert FakeTimer.instances == []
    assert app._recording_watchdog_timer is None


def test_stop_recording_cancels_watchdog(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    monkeypatch.setattr(daemon_module.threading, "Thread", NoOpThread)
    app._start_recording(phase="tap")
    timer = app._recording_watchdog_timer

    app._stop_recording_and_transcribe()

    assert timer.cancelled is True
    assert app._recording_watchdog_timer is None


def test_new_recording_cancels_stale_watchdog_defensively(daemon_app, monkeypatch):
    """_start_recording cancella sempre un eventuale timer residuo prima di
    (ri)armarlo, anche se in condizioni normali non dovrebbe essercene uno
    (viene sempre cancellato allo stop)."""
    app = _prepare(daemon_app, monkeypatch)
    app._start_recording(phase="tap")
    first_timer = app._recording_watchdog_timer

    app.state = daemon_module.STATE_IDLE  # richiesto per un secondo avvio
    app._start_recording(phase="tap")

    assert first_timer.cancelled is True
    assert app._recording_watchdog_timer is not first_timer


def test_on_recording_timeout_enqueues_command(daemon_app):
    daemon_app._on_recording_timeout()
    assert daemon_app.command_queue.get_nowait() == ("recording_timeout",)


def test_on_recording_timeout_reached_stops_if_still_recording(daemon_app, monkeypatch):
    daemon_app.backend = FakeBackend()
    daemon_app.state = daemon_module.STATE_RECORDING
    stopped = []
    monkeypatch.setattr(
        daemon_app, "_stop_recording_and_transcribe", lambda: stopped.append(True)
    )

    daemon_app._on_recording_timeout_reached()

    assert stopped == [True]
    assert daemon_app.backend.notifications  # avvisato del motivo dello stop


def test_on_recording_timeout_reached_noop_if_already_stopped(daemon_app, monkeypatch):
    """Piccola finestra di corsa fra i due thread: se la registrazione e'
    gia' stata fermata normalmente prima che il timer scadesse davvero, lo
    stop accodato non deve rifermarla o notificare a sproposito."""
    daemon_app.backend = FakeBackend()
    daemon_app.state = daemon_module.STATE_IDLE
    stopped = []
    monkeypatch.setattr(
        daemon_app, "_stop_recording_and_transcribe", lambda: stopped.append(True)
    )

    daemon_app._on_recording_timeout_reached()

    assert stopped == []
    assert daemon_app.backend.notifications == []


def test_handle_button_press_tap_toggle_arms_watchdog(daemon_app, monkeypatch):
    """Verifica end-to-end: un tocco normale (kind="record", nessuna fase
    esplicita, come manda l'app telefono in modalita' a interruttore) arma
    la rete di sicurezza."""
    app = _prepare(daemon_app, monkeypatch)
    app._handle_button_press("record")

    assert app.state == daemon_module.STATE_RECORDING
    assert len(FakeTimer.instances) == 1


def test_handle_button_press_push_to_talk_does_not_arm_watchdog(daemon_app, monkeypatch):
    app = _prepare(daemon_app, monkeypatch)
    app._handle_button_press("record", phase="down")

    assert app.state == daemon_module.STATE_RECORDING
    assert FakeTimer.instances == []

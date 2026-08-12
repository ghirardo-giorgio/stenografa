import daemon

"""Test dell'incolla adattivo: Ctrl+Shift+V al posto di Ctrl+V quando la
finestra col focus sembra un emulatore di terminale (dove Ctrl+V e' spesso
un carattere di controllo, non il paste)."""


def test_paste_combo_default_ctrl_v_when_no_focus_info(daemon_app):
    class FakeBackend:
        def get_focused_window(self):
            return None

    daemon_app.backend = FakeBackend()
    assert daemon_app._paste_combo() == "ctrl+v"


def test_paste_combo_ctrl_v_for_normal_app(daemon_app):
    class FakeBackend:
        def get_focused_window(self):
            return ("123", "firefox", "Mozilla Firefox")

    daemon_app.backend = FakeBackend()
    assert daemon_app._paste_combo() == "ctrl+v"


def test_paste_combo_ctrl_shift_v_for_gnome_terminal(daemon_app):
    class FakeBackend:
        def get_focused_window(self):
            return ("123", "gnome-terminal-server", "user@host: ~")

    daemon_app.backend = FakeBackend()
    assert daemon_app._paste_combo() == "ctrl+shift+v"


def test_paste_combo_recognizes_various_terminals(daemon_app):
    class FakeBackend:
        def __init__(self, wm_class):
            self._wm_class = wm_class

        def get_focused_window(self):
            return ("1", self._wm_class, "")

    for wm_class in [
        "konsole", "xterm", "uxterm", "rxvt-unicode", "kitty",
        "Alacritty", "terminator", "Tilix", "xfce4-terminal",
        "lxterminal", "mate-terminal", "terminology", "iTerm2",
        "Warp", "wezterm", "foot", "cmd.exe", "powershell.exe",
        "conhost.exe", "org.gnome.Terminal",
    ]:
        daemon_app.backend = FakeBackend(wm_class)
        assert daemon_app._paste_combo() == "ctrl+shift+v", wm_class


def test_paste_combo_does_not_false_positive_on_steam(daemon_app):
    """Riproduce il rischio di falso positivo con una parola chiave troppo
    corta (es. "st"): "steam" non deve essere scambiato per un terminale."""
    class FakeBackend:
        def get_focused_window(self):
            return ("1", "Steam", "Steam")

    daemon_app.backend = FakeBackend()
    assert daemon_app._paste_combo() == "ctrl+v"


def test_paste_combo_case_insensitive(daemon_app):
    class FakeBackend:
        def get_focused_window(self):
            return ("1", "KONSOLE", "")

    daemon_app.backend = FakeBackend()
    assert daemon_app._paste_combo() == "ctrl+shift+v"


def test_paste_text_uses_terminal_combo_and_hints_it_on_failure(daemon_app):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))
            self.clipboard = text

        def read_clipboard(self):
            return getattr(self, "clipboard", None)

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return False  # incolla automatico fallito

        def get_focused_window(self):
            return ("1", "gnome-terminal-server", "bash")

    daemon_app.backend = FakeBackend()
    daemon_app._paste_text("testo dettato", None)

    assert ("paste", "ctrl+shift+v") in calls
    notify_call = next(c for c in calls if c[0] == "notify")
    assert "Ctrl+Shift+V" in notify_call[1][1]


def test_paste_text_uses_ctrl_v_for_normal_app(daemon_app):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def copy_to_clipboard(self, text):
            calls.append(("copy", text))
            self.clipboard = text

        def read_clipboard(self):
            return getattr(self, "clipboard", None)

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return True

        def get_focused_window(self):
            return ("1", "firefox", "")

    daemon_app.backend = FakeBackend()
    daemon_app._paste_text("testo dettato", None)

    assert ("paste", "ctrl+v") in calls


def test_paste_waits_for_the_clipboard_to_be_ready(daemon_app):
    """Regressione: copiare non e' istantaneo. Se si preme Ctrl+V prima che
    il compositor abbia registrato la nuova selezione si incolla quello che
    c'era prima — il caso classico del primo incolla dopo un po' che l'app
    non veniva usata."""

    class SlowClipboardBackend:
        def __init__(self):
            self.clipboard = "roba vecchia"
            self._pending = None
            self._letture = 0
            self.pasted_with = None

        def notify(self, *a, **kw):
            pass

        def copy_to_clipboard(self, text):
            # il testo diventa disponibile solo dopo qualche lettura
            self._pending = text

        def read_clipboard(self):
            self._letture += 1
            if self._letture >= 3 and self._pending is not None:
                self.clipboard = self._pending
            return self.clipboard

        def simulate_keys(self, combo):
            self.pasted_with = self.clipboard
            return True

        def get_focused_window(self):
            return None

    daemon_app.backend = SlowClipboardBackend()

    daemon_app._paste_text("testo appena dettato", None)

    # al momento dell'incolla gli appunti contenevano gia' il testo nuovo
    assert daemon_app.backend.pasted_with == "testo appena dettato"


def test_paste_does_not_wait_forever_if_the_clipboard_never_matches(
    daemon_app, monkeypatch
):
    """Se gli appunti non arrivano mai a contenere il testo (backend
    bizzarro, contenuto trasformato) si incolla comunque invece di restare
    bloccati."""

    class StuckClipboardBackend:
        def __init__(self):
            self.pasted = False

        def notify(self, *a, **kw):
            pass

        def copy_to_clipboard(self, text):
            pass

        def read_clipboard(self):
            return "sempre lo stesso"

        def simulate_keys(self, combo):
            self.pasted = True
            return True

        def get_focused_window(self):
            return None

    daemon_app.backend = StuckClipboardBackend()
    # l'attesa vera dura un paio di secondi: qui interessa solo che finisca
    monkeypatch.setattr(daemon, "CLIPBOARD_READY_TIMEOUT", 0.2)

    daemon_app._paste_text("qualcosa", None)

    assert daemon_app.backend.pasted

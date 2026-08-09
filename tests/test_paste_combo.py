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

        def simulate_keys(self, combo):
            calls.append(("paste", combo))
            return True

        def get_focused_window(self):
            return ("1", "firefox", "")

    daemon_app.backend = FakeBackend()
    daemon_app._paste_text("testo dettato", None)

    assert ("paste", "ctrl+v") in calls

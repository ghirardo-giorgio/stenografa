"""Test del pulsante "comando vocale IA" (kind="ai_command"): validazione
del layout, interpretazione del comando tramite LM Studio (mockato, nessuna
chiamata di rete reale) ed esecuzione dello shortcut risultante."""
import daemon
from conftest import ImmediateThread, fake_llm


# --- validazione layout ---


def test_add_button_kind_ai_command_defaults_label_and_ignores_combo(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "kind": "ai_command", "row": 0, "col": 1},
    )
    assert ok, error
    new_button = daemon_app.layout["dashboards"][0]["buttons"][-1]
    assert new_button["label"] == "Comando vocale"
    assert new_button["kind"] == "ai_command"
    assert "combo" not in new_button


def test_ai_command_button_style_not_customizable(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "kind": "ai_command", "row": 0, "col": 1},
    )
    button_id = daemon_app.layout["dashboards"][0]["buttons"][-1]["id"]
    ok, error = daemon_app._mutate_layout(
        "set_button_style", {"id": button_id, "color": "#e53935"}
    )
    assert not ok
    assert "personalizzat" in error


def test_ai_command_button_not_removal_protected(daemon_app):
    """A differenza di 'record', un pulsante ai_command si puo' sempre
    rimuovere: non e' l'unico modo di avviare la registrazione."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "kind": "ai_command", "row": 0, "col": 1},
    )
    button_id = daemon_app.layout["dashboards"][0]["buttons"][-1]["id"]
    ok, error = daemon_app._mutate_layout("remove_button", {"id": button_id})
    assert ok, error


# --- _interpret_as_shortcuts (backend LLM mockato) ---
# La scoperta del modello caricato in LM Studio vive ora in
# llm_provider.LMStudioProvider: vedi tests/test_llm_provider.py


def test_interpret_as_shortcut_returns_combo(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="Ctrl+C")
    candidates, error = daemon_app._interpret_as_shortcuts("copia")
    assert error is None
    # risposta a combo singola (formato vecchio): resta supportata e produce
    # un solo candidato, quindi verra' eseguita subito senza chiedere nulla
    assert [c["combo"] for c in candidates] == ["ctrl+c"]


def test_interpret_as_shortcut_rejects_none_reply(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="NONE")
    candidates, error = daemon_app._interpret_as_shortcuts("raccontami una barzelletta")
    assert candidates is None
    assert "non riconosciuto" in error


def test_interpret_as_shortcut_rejects_invalid_combo_syntax(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+pippo")
    candidates, error = daemon_app._interpret_as_shortcuts("qualcosa")
    assert candidates is None
    assert "non riconosciuto" in error


def test_interpret_as_shortcut_no_model_loaded(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, error="LM Studio non raggiungibile")
    candidates, error = daemon_app._interpret_as_shortcuts("copia")
    assert candidates is None
    assert "LM Studio" in error


def test_interpret_as_shortcut_handles_backend_error(daemon_app, monkeypatch):
    """Un errore del backend (rete, timeout, credenziali) risale intatto al
    chiamante invece di essere scambiato per 'comando non riconosciuto'."""
    fake_llm(monkeypatch, daemon_app, error="errore di comunicazione con LM Studio: timeout")
    candidates, error = daemon_app._interpret_as_shortcuts("copia")
    assert candidates is None
    assert "LM Studio" in error


# --- priorita' alle scorciatoie della dashboard di provenienza ---


def test_dashboard_shortcuts_context_lists_keys_buttons(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 3}
    )
    daemon_app._mutate_layout(
        "rename_dashboard", {"id": "default", "name": "Invoke AI"}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "Invoca", "combo": "ctrl+enter", "row": 0, "col": 1},
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "Annulla", "combo": "esc", "row": 0, "col": 2},
    )

    context = daemon_app._dashboard_shortcuts_context("default")
    assert "Invoke AI" in context
    assert '"Invoca" -> ctrl+enter' in context
    assert '"Annulla" -> esc' in context
    # il pulsante "record" di default non ha combo: non deve comparire
    assert "Registra" not in context


def test_dashboard_shortcuts_context_empty_when_no_keys_buttons(daemon_app):
    assert daemon_app._dashboard_shortcuts_context("default") == ""


def test_dashboard_shortcuts_context_empty_for_unknown_dashboard(daemon_app):
    assert daemon_app._dashboard_shortcuts_context("nope") == ""


def test_interpret_as_shortcut_prioritizes_dashboard_button_label(daemon_app, monkeypatch):
    """Se l'utente detta un comando che corrisponde all'etichetta di una
    scorciatoia gia' configurata nella dashboard di provenienza (es. "invoca"
    sulla dashboard InvokeAI), il prompt inviato all'LLM deve includerla."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "Invoca", "combo": "ctrl+enter", "row": 0, "col": 1},
    )
    captured = fake_llm(monkeypatch, daemon_app, reply="ctrl+enter", capture={})
    candidates, error = daemon_app._interpret_as_shortcuts("invoca", dashboard_id="default")

    assert error is None
    assert [c["combo"] for c in candidates] == ["ctrl+enter"]
    assert '"Invoca" -> ctrl+enter' in captured["system"]


def test_interpret_as_shortcut_without_dashboard_id_has_no_context(daemon_app, monkeypatch):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "Invoca", "combo": "ctrl+enter", "row": 0, "col": 1},
    )
    captured = fake_llm(monkeypatch, daemon_app, reply="ctrl+c", capture={})
    daemon_app._interpret_as_shortcuts("copia")  # nessun dashboard_id

    assert "Invoca" not in captured["system"]


def test_interpret_as_shortcut_normalizes_spaces_in_combo(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl + enter")
    candidates, error = daemon_app._interpret_as_shortcuts("invoca")
    assert error is None
    assert [c["combo"] for c in candidates] == ["ctrl+enter"]


def test_handle_button_press_ai_command_passes_dashboard_id(daemon_app):
    """_handle_button_press deve propagare l'id della dashboard di
    provenienza a toggle_recording, cosi' _interpret_as_shortcuts puo' dare
    priorita' alle sue scorciatoie."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "kind": "ai_command", "row": 0, "col": 1},
    )
    button_id = daemon_app.layout["dashboards"][0]["buttons"][-1]["id"]

    calls = []
    daemon_app.state = daemon.STATE_IDLE

    def fake_toggle_recording(
        mode="paste", dashboard_id=None, phase="tap", auto_enter=False
    ):
        calls.append((mode, dashboard_id))

    daemon_app.toggle_recording = fake_toggle_recording
    daemon_app._handle_button_press(button_id)

    assert calls == [("ai_command", "default")]


# --- _ai_command_worker ---


def test_ai_command_worker_reports_error_when_simulate_keys_fails(daemon_app, monkeypatch):
    class FakeBackend:
        def simulate_keys(self, combo):
            return False

    daemon_app.backend = FakeBackend()
    monkeypatch.setattr(
        daemon_app,
        "_interpret_as_shortcuts",
        lambda text, dashboard_id=None: ([{"label": "Copia", "combo": "ctrl+c"}], None),
    )

    daemon_app._ai_command_worker("copia")

    kind, text, combo, error = daemon_app.command_queue.get_nowait()
    assert kind == "ai_done"
    assert text == "copia"
    assert combo == "ctrl+c"
    assert "simulazione" in error


# --- flusso end-to-end tramite _on_transcription_done ---


def test_on_transcription_done_executes_shortcut_via_ai_command(daemon_app, monkeypatch):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def simulate_keys(self, combo):
            calls.append(("simulate", combo))
            return True

    daemon_app.backend = FakeBackend()
    daemon_app._recording_mode = "ai_command"
    monkeypatch.setattr(daemon.threading, "Thread", ImmediateThread)

    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")

    daemon_app._on_transcription_done("copia", None)

    # il worker (eseguito subito grazie a ImmediateThread) ha gia' accodato
    # il risultato, ma _on_ai_command_done non e' ancora stato eseguito: lo
    # stato resta "thinking" finche' il ciclo principale non lo processa
    assert daemon_app.state == daemon.STATE_THINKING

    kind, text, combo, error = daemon_app.command_queue.get_nowait()
    assert kind == "ai_done"
    daemon_app._on_ai_command_done(text, combo, error)

    assert daemon_app.state == daemon.STATE_IDLE
    assert ("simulate", "ctrl+c") in calls
    assert any(c[0] == "notify" and "eseguito" in c[1][0] for c in calls)
    # modalita' ripristinata a "paste" per la prossima registrazione
    assert daemon_app._recording_mode == "paste"


def test_on_transcription_done_ai_command_notifies_error_when_llm_unreachable(
    daemon_app, monkeypatch
):
    calls = []

    class FakeBackend:
        def notify(self, *a, **kw):
            calls.append(("notify", a))

        def simulate_keys(self, combo):
            calls.append(("simulate", combo))
            return True

    daemon_app.backend = FakeBackend()
    daemon_app._recording_mode = "ai_command"
    monkeypatch.setattr(daemon.threading, "Thread", ImmediateThread)
    fake_llm(monkeypatch, daemon_app, error="LM Studio non raggiungibile")

    daemon_app._on_transcription_done("qualcosa", None)

    kind, text, combo, error = daemon_app.command_queue.get_nowait()
    assert combo is None
    assert "LM Studio" in error

    daemon_app._on_ai_command_done(text, combo, error)

    assert daemon_app.state == daemon.STATE_IDLE
    assert not any(c[0] == "simulate" for c in calls)  # nessuno shortcut eseguito
    assert any(c[0] == "notify" for c in calls)


def test_on_transcription_done_paste_mode_unaffected(daemon_app):
    """Il percorso normale (mode="paste", il default) non deve cambiare:
    nessuna chiamata di rete, incolla diretto del testo dettato."""
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
            return None

    daemon_app.backend = FakeBackend()
    daemon_app._net_clients = set()

    daemon_app._on_transcription_done("ciao mondo", None)

    assert daemon_app.state == daemon.STATE_IDLE
    assert ("copy", "ciao mondo") in calls
    assert daemon_app._recording_mode == "paste"


# --- disambiguazione: comando ambiguo -> scelta sul telefono ---


def _fake_llm_reply(monkeypatch, daemon_app, content):

    fake_llm(monkeypatch, daemon_app, reply=content)


def test_interpret_parses_json_array_of_candidates(daemon_app, monkeypatch):
    _fake_llm_reply(
        monkeypatch,
        daemon_app,
        '[{"label": "Copia", "combo": "ctrl+c"},'
        ' {"label": "Copia stile", "combo": "ctrl+shift+c"}]',
    )
    candidates, error = daemon_app._interpret_as_shortcuts("copia")
    assert error is None
    assert [c["combo"] for c in candidates] == ["ctrl+c", "ctrl+shift+c"]


def test_interpret_strips_markdown_fence_around_json(daemon_app, monkeypatch):
    _fake_llm_reply(
        monkeypatch, daemon_app, '```json\n[{"label": "Copia", "combo": "ctrl+c"}]\n```'
    )
    candidates, error = daemon_app._interpret_as_shortcuts("copia")
    assert error is None
    assert [c["combo"] for c in candidates] == ["ctrl+c"]


def test_interpret_drops_invalid_candidates_but_keeps_good_ones(daemon_app, monkeypatch):
    """Un elemento malformato non deve far perdere anche quelli validi."""
    _fake_llm_reply(
        monkeypatch,
        daemon_app,
        '[{"label": "Copia", "combo": "ctrl+c"},'
        ' {"label": "Rotto", "combo": "ctrl+pippo"},'
        ' {"label": "Senza combo"}]',
    )
    candidates, error = daemon_app._interpret_as_shortcuts("copia")
    assert error is None
    assert [c["combo"] for c in candidates] == ["ctrl+c"]


def test_interpret_deduplicates_identical_combos(daemon_app, monkeypatch):
    """Due etichette diverse per la stessa combo non sono una vera scelta."""
    _fake_llm_reply(
        monkeypatch,
        daemon_app,
        '[{"label": "Copia", "combo": "ctrl+c"},'
        ' {"label": "Copia selezione", "combo": "ctrl+c"}]',
    )
    candidates, _ = daemon_app._interpret_as_shortcuts("copia")
    assert [c["combo"] for c in candidates] == ["ctrl+c"]


def test_interpret_caps_number_of_candidates(daemon_app, monkeypatch):
    many = ", ".join(
        f'{{"label": "Azione {i}", "combo": "ctrl+{c}"}}'
        for i, c in enumerate("abcdefghij")
    )
    _fake_llm_reply(monkeypatch, daemon_app, f"[{many}]")
    candidates, _ = daemon_app._interpret_as_shortcuts("qualcosa")
    assert len(candidates) == daemon.AI_COMMAND_MAX_OPTIONS


def test_ai_command_worker_single_candidate_executes_immediately(daemon_app, monkeypatch):
    """Comando non ambiguo: si esegue subito, senza chiedere niente."""
    executed = []

    class FakeBackend:
        def simulate_keys(self, combo):
            executed.append(combo)
            return True

    daemon_app.backend = FakeBackend()
    monkeypatch.setattr(
        daemon_app,
        "_interpret_as_shortcuts",
        lambda text, dashboard_id=None: ([{"label": "Copia", "combo": "ctrl+c"}], None),
    )

    daemon_app._ai_command_worker("copia")

    assert executed == ["ctrl+c"]
    assert daemon_app.command_queue.get_nowait()[0] == "ai_done"


def test_ai_command_worker_multiple_candidates_asks_instead_of_executing(
    daemon_app, monkeypatch
):
    """Comando ambiguo: NON deve eseguire nulla, ma accodare la scelta."""
    executed = []

    class FakeBackend:
        def simulate_keys(self, combo):
            executed.append(combo)
            return True

    options = [
        {"label": "Copia", "combo": "ctrl+c"},
        {"label": "Copia stile", "combo": "ctrl+shift+c"},
    ]
    daemon_app.backend = FakeBackend()
    monkeypatch.setattr(
        daemon_app,
        "_interpret_as_shortcuts",
        lambda text, dashboard_id=None: (options, None),
    )

    daemon_app._ai_command_worker("copia")

    assert executed == []
    kind, text, candidates = daemon_app.command_queue.get_nowait()
    assert kind == "ai_choice"
    assert text == "copia"
    assert candidates == options


def test_on_ai_choice_needed_broadcasts_options_and_sets_pending(daemon_app):
    sent = []
    daemon_app._broadcast = lambda msg: sent.append(msg)
    daemon_app.backend = type("B", (), {"notify": lambda *a, **k: None})()

    options = [
        {"label": "Copia", "combo": "ctrl+c"},
        {"label": "Copia stile", "combo": "ctrl+shift+c"},
    ]
    daemon_app._on_ai_choice_needed("copia", options)

    assert daemon_app._pending_choice is not None
    msg = [m for m in sent if m.get("type") == "choose_shortcut"][0]
    assert msg["options"] == options
    assert msg["request_id"] == daemon_app._pending_choice["id"]
    assert daemon_app.state == daemon.STATE_IDLE


def test_choice_reply_executes_selected_combo(daemon_app):
    executed = []

    class FakeBackend:
        def simulate_keys(self, combo):
            executed.append(combo)
            return True

        def notify(self, *a, **kw):
            pass

    daemon_app.backend = FakeBackend()
    daemon_app._broadcast = lambda msg: None
    daemon_app._on_ai_choice_needed(
        "copia",
        [
            {"label": "Copia", "combo": "ctrl+c"},
            {"label": "Copia stile", "combo": "ctrl+shift+c"},
        ],
    )
    request_id = daemon_app._pending_choice["id"]

    daemon_app._on_ai_choice_reply(request_id, "ctrl+shift+c")

    assert executed == ["ctrl+shift+c"]
    assert daemon_app._pending_choice is None


def test_choice_reply_ignores_stale_request_id(daemon_app):
    """Una risposta con id vecchio (pannello rimasto aperto su un altro
    telefono) non deve eseguire nulla."""
    executed = []

    class FakeBackend:
        def simulate_keys(self, combo):
            executed.append(combo)
            return True

        def notify(self, *a, **kw):
            pass

    daemon_app.backend = FakeBackend()
    daemon_app._broadcast = lambda msg: None
    daemon_app._on_ai_choice_needed(
        "copia", [{"label": "A", "combo": "ctrl+c"}, {"label": "B", "combo": "ctrl+v"}]
    )

    daemon_app._on_ai_choice_reply("id-inesistente", "ctrl+c")

    assert executed == []
    assert daemon_app._pending_choice is not None


def test_choice_reply_rejects_combo_not_among_options(daemon_app):
    """Il pannello non deve poter far eseguire combinazioni arbitrarie."""
    executed = []

    class FakeBackend:
        def simulate_keys(self, combo):
            executed.append(combo)
            return True

        def notify(self, *a, **kw):
            pass

    daemon_app.backend = FakeBackend()
    daemon_app._broadcast = lambda msg: None
    daemon_app._on_ai_choice_needed(
        "copia", [{"label": "A", "combo": "ctrl+c"}, {"label": "B", "combo": "ctrl+v"}]
    )
    request_id = daemon_app._pending_choice["id"]

    daemon_app._on_ai_choice_reply(request_id, "alt+f4")

    assert executed == []
    assert daemon_app._pending_choice is not None


def test_pending_choice_expires(daemon_app):
    sent = []
    daemon_app.backend = type("B", (), {"notify": lambda *a, **k: None})()
    daemon_app._broadcast = lambda msg: sent.append(msg)
    daemon_app._on_ai_choice_needed(
        "copia", [{"label": "A", "combo": "ctrl+c"}, {"label": "B", "combo": "ctrl+v"}]
    )
    # forza la scadenza invece di aspettare AI_COMMAND_CHOICE_TIMEOUT
    daemon_app._pending_choice["expires_at"] = 0

    daemon_app._expire_pending_choice()

    assert daemon_app._pending_choice is None
    closed = [m for m in sent if m.get("type") == "choose_shortcut_closed"]
    assert closed and closed[-1]["reason"] == "timeout"


def test_new_recording_cancels_pending_choice(daemon_app):
    sent = []
    daemon_app.backend = type(
        "B",
        (),
        {
            "notify": lambda *a, **k: None,
            "read_clipboard": lambda *a, **k: None,
            "start_recording": lambda *a, **k: None,
        },
    )()
    daemon_app._broadcast = lambda msg: sent.append(msg)
    daemon_app.model = type("M", (), {"preload": lambda *a, **k: None})()
    daemon_app._on_ai_choice_needed(
        "copia", [{"label": "A", "combo": "ctrl+c"}, {"label": "B", "combo": "ctrl+v"}]
    )

    daemon_app._start_recording()

    assert daemon_app._pending_choice is None
    closed = [m for m in sent if m.get("type") == "choose_shortcut_closed"]
    assert closed and closed[-1]["reason"] == "superseded"

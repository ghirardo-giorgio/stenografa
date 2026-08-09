"""Test dei tipi di pulsante introdotti oltre a "keys"/microfono: macro
(sequenza di combinazioni), text (snippet incollato) e launch (avvio di
un'applicazione installata). Verifica sia la validazione al momento della
creazione sia cosa succede alla pressione."""
import threading

import pytest

import daemon as daemon_module


class FakeBackend:
    """Backend finto: registra cosa gli viene chiesto invece di toccare
    davvero tastiera, appunti o applicazioni."""

    def __init__(self, apps=(), keys_ok=True, launch_ok=True):
        self._apps = list(apps)
        self._keys_ok = keys_ok
        self._launch_ok = launch_ok
        self.keys = []
        self.launched = []
        self.clipboard = ""
        self.notifications = []

    def list_apps(self):
        return self._apps

    def launch_app(self, app_id):
        self.launched.append(app_id)
        return self._launch_ok

    def simulate_keys(self, combo):
        self.keys.append(combo)
        return self._keys_ok

    def copy_to_clipboard(self, text):
        self.clipboard = text

    def read_clipboard(self):
        return self.clipboard

    def get_focused_window(self):
        return None

    def notify(self, title, body, urgency="normal"):
        self.notifications.append((title, body, urgency))


@pytest.fixture
def app(daemon_app):
    """daemon_app con un backend finto e una griglia con celle libere."""
    daemon_app.backend = FakeBackend(
        apps=[
            {"id": "/usr/share/applications/gimp.desktop", "name": "GIMP"},
            {"id": "/usr/share/applications/code.desktop", "name": "Visual Studio Code"},
        ]
    )
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 2, "cols": 3}
    )
    return daemon_app


def _add(app, **spec):
    spec.setdefault("dashboard_id", "default")
    return app._mutate_layout("add_button", spec)


def _find(app, label):
    for dashboard in app.layout["dashboards"]:
        for button in dashboard["buttons"]:
            if button["label"] == label:
                return button
    return None


# --- macro ---


def test_macro_button_stores_sequence_and_default_delay(app):
    ok, error = _add(
        app,
        label="Salva e compila",
        kind="macro",
        combos=["ctrl+s", "ctrl+shift+b"],
        row=0,
        col=1,
    )
    assert ok, error
    button = _find(app, "Salva e compila")
    assert button["combos"] == ["ctrl+s", "ctrl+shift+b"]
    assert button["delay_ms"] == daemon_module.MACRO_DEFAULT_DELAY_MS


def test_macro_button_rejects_empty_and_invalid_combos(app):
    ok, error = _add(app, label="Vuota", kind="macro", combos=[], row=0, col=1)
    assert not ok
    assert "combos" in error

    ok, error = _add(
        app,
        label="Sbagliata",
        kind="macro",
        combos=["ctrl+s", "ctrl+pippo", "alt+pluto"],
        row=0,
        col=1,
    )
    assert not ok
    # come per le liste di scorciatoie, l'errore le elenca tutte insieme
    assert "combos[1]" in error and "combos[2]" in error


def test_macro_button_rejects_out_of_range_delay(app):
    ok, error = _add(
        app,
        label="Lenta",
        kind="macro",
        combos=["ctrl+s"],
        delay_ms=999999,
        row=0,
        col=1,
    )
    assert not ok
    assert "delay_ms" in error


def test_macro_run_executes_all_steps_in_order(app):
    app._run_macro("Test", ["ctrl+s", "alt+tab", "ctrl+v"], delay_ms=0)
    assert app.backend.keys == ["ctrl+s", "alt+tab", "ctrl+v"]


def test_macro_run_stops_at_first_failure(app):
    app.backend = FakeBackend(keys_ok=False)
    app._run_macro("Test", ["ctrl+s", "alt+tab"], delay_ms=0)
    # i passi successivi presuppongono lo stato lasciato dal primo: tirare
    # dritto li eseguirebbe nel contesto sbagliato
    assert app.backend.keys == ["ctrl+s"]
    assert app.backend.notifications


# --- text ---


def test_text_button_stores_snippet(app):
    ok, error = _add(
        app, label="Firma", kind="text", text="Cordiali saluti", row=0, col=1
    )
    assert ok, error
    assert _find(app, "Firma")["text"] == "Cordiali saluti"


def test_text_button_requires_non_empty_text(app):
    ok, error = _add(app, label="Vuoto", kind="text", text="   ", row=0, col=1)
    assert not ok
    assert "text mancante" in error


def test_text_button_rejects_oversized_text(app):
    ok, error = _add(
        app,
        label="Enorme",
        kind="text",
        text="x" * (daemon_module.TEXT_BUTTON_MAX_LENGTH + 1),
        row=0,
        col=1,
    )
    assert not ok
    assert "troppo lungo" in error


def test_paste_snippet_does_not_enter_history(app):
    """Uno snippet fisso non e' una dettatura: lo storico serve a ripescare
    quello che si e' detto, non quello che si e' gia' salvato in un
    pulsante."""
    app._paste_snippet("Cordiali saluti")
    assert app.backend.clipboard == "Cordiali saluti"
    assert app._history == []


# --- launch ---


def test_launch_button_validates_app_id_and_copies_name(app):
    ok, error = _add(
        app,
        label="",
        kind="launch",
        app_id="/usr/share/applications/gimp.desktop",
        row=0,
        col=1,
    )
    assert ok, error
    button = _find(app, "GIMP")  # etichetta ricavata dal nome dell'app
    assert button is not None
    assert button["app_name"] == "GIMP"


def test_launch_button_rejects_unknown_app_id(app):
    ok, error = _add(
        app,
        label="Fantasma",
        kind="launch",
        app_id="/usr/share/applications/inesistente.desktop",
        row=0,
        col=1,
    )
    assert not ok
    assert "nessuna applicazione installata" in error


def test_launch_button_requires_app_id(app):
    ok, error = _add(app, label="Senza id", kind="launch", row=0, col=1)
    assert not ok
    assert "app_id mancante" in error


def test_launch_from_button_launches_and_reports(app):
    app._launch_from_button("/usr/share/applications/code.desktop", "VS Code")
    assert app.backend.launched == ["/usr/share/applications/code.desktop"]


# --- paste_last (re-incolla l'ultima dettatura) ---


def test_paste_last_button_needs_no_extra_field_and_has_default_label(app):
    ok, error = _add(app, kind="paste_last", row=0, col=1)
    assert ok, error
    assert _find(app, "Incolla ultimo")["kind"] == "paste_last"


def test_paste_last_repastes_most_recent_dictation(app):
    app._history_add("prima dettatura", pasted=True)
    app._history_add("seconda dettatura", pasted=True)

    app._paste_last_dictation()

    assert app.backend.clipboard == "seconda dettatura"
    assert app.backend.keys == ["ctrl+v"]


def test_paste_last_does_not_duplicate_the_history_entry(app):
    """Re-incollare non e' dettare: lo storico deve restare com'era,
    altrimenti la stessa frase comparirebbe piu' volte nell'elenco."""
    app._history_add("una frase", pasted=True)

    app._paste_last_dictation()

    assert [e["text"] for e in app._history] == ["una frase"]


def test_paste_last_with_empty_history_notifies_and_pastes_nothing(app):
    app._paste_last_dictation()

    assert app.backend.keys == []
    assert app.backend.clipboard == ""
    assert app.backend.notifications


def test_paste_last_button_press_repastes(app, monkeypatch):
    monkeypatch.setattr(daemon_module.threading, "Thread", _ImmediateThread)
    _add(app, kind="paste_last", row=0, col=1)
    button_id = _find(app, "Incolla ultimo")["id"]
    app._history_add("testo dettato", pasted=True)

    app._handle_button_press(button_id)

    assert app.backend.clipboard == "testo dettato"


# --- edit_button (modifica di un pulsante esistente) ---


def _edit(app, **msg):
    return app._mutate_layout("edit_button", msg)


def test_edit_button_changes_combo_keeping_position_and_style(app):
    _add(app, label="Copia", combo="ctrl+c", row=0, col=1, color="#1e88e5")
    button_id = _find(app, "Copia")["id"]

    ok, error = _edit(app, id=button_id, combo="ctrl+shift+c")

    assert ok, error
    button = _find(app, "Copia")
    assert button["combo"] == "ctrl+shift+c"
    # il motivo per cui esiste edit_button: rifare il pulsante costringerebbe
    # a riposizionarlo e ricolorarlo
    assert (button["row"], button["col"]) == (0, 1)
    assert button["color"] == "#1e88e5"


def test_edit_button_rejects_invalid_combo(app):
    _add(app, label="Copia", combo="ctrl+c", row=0, col=1)
    button_id = _find(app, "Copia")["id"]

    ok, error = _edit(app, id=button_id, combo="ctrl+pippo")

    assert not ok
    assert "non valida" in error
    assert _find(app, "Copia")["combo"] == "ctrl+c"


def test_edit_button_changes_label(app):
    _add(app, label="Copia", combo="ctrl+c", row=0, col=1)
    button_id = _find(app, "Copia")["id"]

    ok, error = _edit(app, id=button_id, label="Copia tutto")

    assert ok, error
    assert _find(app, "Copia tutto")["combo"] == "ctrl+c"


def test_edit_button_updates_macro_steps_and_delay(app):
    _add(app, label="Flusso", kind="macro", combos=["ctrl+s"], row=0, col=1)
    button_id = _find(app, "Flusso")["id"]

    ok, error = _edit(
        app, id=button_id, combos=["ctrl+s", "alt+tab"], delay_ms=300
    )

    assert ok, error
    button = _find(app, "Flusso")
    assert button["combos"] == ["ctrl+s", "alt+tab"]
    assert button["delay_ms"] == 300


def test_edit_button_can_change_only_the_macro_delay(app):
    """Cambiare la pausa non deve costringere a rimandare la sequenza."""
    _add(
        app,
        label="Flusso",
        kind="macro",
        combos=["ctrl+s", "alt+tab"],
        row=0,
        col=1,
    )
    button_id = _find(app, "Flusso")["id"]

    ok, error = _edit(app, id=button_id, delay_ms=50)

    assert ok, error
    button = _find(app, "Flusso")
    assert button["delay_ms"] == 50
    assert button["combos"] == ["ctrl+s", "alt+tab"]


def test_edit_button_refuses_a_field_of_another_kind(app):
    """Mandare `combo` a una macro e' un errore esplicito: silenziosamente
    ignorato lascerebbe credere che la modifica sia andata a buon fine."""
    _add(app, label="Flusso", kind="macro", combos=["ctrl+s"], row=0, col=1)
    button_id = _find(app, "Flusso")["id"]

    ok, error = _edit(app, id=button_id, combo="ctrl+c")

    assert not ok
    assert "macro" in error


def test_edit_button_rejects_unknown_id(app):
    ok, error = _edit(app, id="inesistente", label="X")

    assert not ok
    assert "nessun pulsante" in error


def test_edit_button_without_any_field_is_an_error(app):
    _add(app, label="Copia", combo="ctrl+c", row=0, col=1)
    button_id = _find(app, "Copia")["id"]

    ok, error = _edit(app, id=button_id)

    assert not ok
    assert "nessuna modifica" in error


# --- cache dell'elenco applicazioni ---


def test_apps_list_is_cached_between_calls(app):
    calls = []
    original = app.backend.list_apps

    def counting_list_apps():
        calls.append(1)
        return original()

    app.backend.list_apps = counting_list_apps
    app._list_apps_cached()
    app._list_apps_cached()
    # l'enumerazione vera (centinaia di file .desktop) avviene una volta sola
    assert len(calls) == 1


# --- pressione: push-to-talk e tipi non-microfono ---


def test_button_up_does_not_re_execute_non_microphone_button(app, monkeypatch):
    monkeypatch.setattr(daemon_module.threading, "Thread", _ImmediateThread)
    _add(app, label="Copia", combo="ctrl+c", row=0, col=1)
    button_id = _find(app, "Copia")["id"]

    app._handle_button_press(button_id, phase="down")
    app._handle_button_press(button_id, phase="up")

    # tenere premuto per sbaglio non deve eseguire due volte l'azione
    assert app.backend.keys == ["ctrl+c"]


def test_push_to_talk_starts_on_down_and_stops_on_up(app, monkeypatch):
    started, stopped = [], []
    app.state = daemon_module.STATE_IDLE
    monkeypatch.setattr(
        daemon_module.Stenografa,
        "_start_recording",
        lambda self, phase="tap": started.append(True) or setattr(
            self, "state", daemon_module.STATE_RECORDING
        ),
    )
    monkeypatch.setattr(
        daemon_module.Stenografa,
        "_stop_recording_and_transcribe",
        lambda self: stopped.append(True),
    )

    app._handle_button_press("record", phase="down")
    assert started and not stopped
    # un secondo "down" (es. da un altro telefono collegato) non riavvia
    app._handle_button_press("record", phase="down")
    assert len(started) == 1

    app._handle_button_press("record", phase="up")
    assert len(stopped) == 1


class _ImmediateThread:
    """threading.Thread finto: esegue subito, senza thread reali."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def test_layout_lock_is_reentrant_for_new_kinds(app):
    """Regressione: _validate_new_button consulta l'elenco applicazioni
    mentre il lock del layout e' gia' preso (vedi _mutate_layout)."""
    assert isinstance(app._layout_lock, type(threading.RLock()))


def test_edit_button_changes_the_launched_application(app):
    _add(
        app,
        label="Gimp",
        kind="launch",
        app_id="/usr/share/applications/code.desktop",
        row=0,
        col=1,
    )
    button_id = _find(app, "Gimp")["id"]

    ok, error = _edit(
        app, id=button_id, app_id="/usr/share/applications/gimp.desktop"
    )

    assert ok, error
    button = _find(app, "Gimp")
    assert button["app_id"] == "/usr/share/applications/gimp.desktop"
    # il nome mostrato nei dialoghi segue l'applicazione vera
    assert button["app_name"] == "GIMP"


def test_edit_button_rejects_an_unknown_application(app):
    _add(
        app,
        label="Gimp",
        kind="launch",
        app_id="/usr/share/applications/code.desktop",
        row=0,
        col=1,
    )
    button_id = _find(app, "Gimp")["id"]

    ok, error = _edit(app, id=button_id, app_id="/non/esiste.desktop")

    assert not ok
    assert "nessuna applicazione installata" in error


# --- pulsanti piu' grandi di una cella ---
# la griglia del fixture e' 2x3 con "record" gia' in (0,0): i test usano la
# riga 1, libera


def test_button_can_span_more_cells(app):
    ok, error = _add(
        app, label="Grande", combo="ctrl+g", row=1, col=0, col_span=2
    )

    assert ok, error
    assert _find(app, "Grande")["col_span"] == 2


def test_span_of_one_is_not_written_in_the_layout(app):
    """Il layout di chi non usa i pulsanti estesi resta identico a prima."""
    _add(app, label="Normale", combo="ctrl+n", row=1, col=0)

    button = _find(app, "Normale")
    assert "row_span" not in button and "col_span" not in button


def test_a_big_button_cannot_overlap_another(app):
    _add(app, label="Vicino", combo="ctrl+v", row=1, col=1)

    ok, error = _add(
        app, label="Grande", combo="ctrl+g", row=1, col=0, col_span=2
    )

    assert not ok
    assert "occupata" in error


def test_a_big_button_cannot_stick_out_of_the_grid(app):
    ok, error = _add(
        app, label="Enorme", combo="ctrl+e", row=1, col=2, col_span=2
    )

    assert not ok
    assert "esce dalla griglia" in error


def test_edit_button_can_resize_an_existing_button(app):
    _add(app, label="Da ingrandire", combo="ctrl+i", row=1, col=0)
    button_id = _find(app, "Da ingrandire")["id"]

    ok, error = _edit(app, id=button_id, col_span=3)

    assert ok, error
    assert _find(app, "Da ingrandire")["col_span"] == 3


def test_edit_button_refuses_to_resize_over_a_neighbour(app):
    _add(app, label="Da ingrandire", combo="ctrl+i", row=1, col=0)
    _add(app, label="Vicino", combo="ctrl+v", row=1, col=1)
    button_id = _find(app, "Da ingrandire")["id"]

    ok, error = _edit(app, id=button_id, col_span=2)

    assert not ok
    assert "gia' occupata" in error
    assert "col_span" not in _find(app, "Da ingrandire")


def test_shrinking_the_grid_accounts_for_big_buttons(app):
    """Una griglia piu' piccola non deve tagliare a meta' un pulsante
    esteso: prima si rimpicciolisce lui."""
    _add(app, label="Largo", combo="ctrl+l", row=1, col=0, col_span=3)

    ok, error = app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 2, "cols": 2}
    )

    assert not ok
    assert "Largo" in error


def test_moving_onto_a_big_button_is_refused(app):
    _add(app, label="Largo", combo="ctrl+l", row=1, col=0, col_span=2)
    _add(app, label="Piccolo", combo="ctrl+p", row=0, col=1)
    small_id = _find(app, "Piccolo")["id"]

    ok, error = app._mutate_layout(
        "move_button", {"id": small_id, "row": 1, "col": 1}
    )

    assert not ok
    assert "dimensione diversa" in error


def test_two_equal_buttons_still_swap_places(app):
    """Lo scambio trascinando resta com'era per i pulsanti della stessa
    forma: e' il gesto con cui si riordina la griglia."""
    _add(app, label="Uno", combo="ctrl+1", row=1, col=0)
    _add(app, label="Due", combo="ctrl+2", row=1, col=1)
    first = _find(app, "Uno")["id"]

    ok, error = app._mutate_layout(
        "move_button", {"id": first, "row": 1, "col": 1}
    )

    assert ok, error
    assert (_find(app, "Uno")["row"], _find(app, "Uno")["col"]) == (1, 1)
    assert (_find(app, "Due")["row"], _find(app, "Due")["col"]) == (1, 0)

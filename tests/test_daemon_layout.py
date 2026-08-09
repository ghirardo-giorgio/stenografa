"""Test della logica di mutazione del layout (add/remove/move pulsanti,
gestione dashboard). Usa la fixture `daemon_app` (vedi conftest.py): una
Stenografa costruita senza gli effetti collaterali di __init__ (nessun
socket/thread reale), cosi' si testa solo la logica pura."""


def test_add_button_and_list(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "add_button",
        {
            "dashboard_id": "default",
            "label": "Copia",
            "combo": "ctrl+c",
            "row": 0,
            "col": 0,
        },
    )
    # la cella (0,0) e' gia' occupata dal pulsante "record" di default
    assert not ok
    assert "occupata" in error

    ok, error = daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    assert ok, error

    ok, error = daemon_app._mutate_layout(
        "add_button",
        {
            "dashboard_id": "default",
            "label": "Copia",
            "combo": "ctrl+c",
            "row": 0,
            "col": 1,
        },
    )
    assert ok, error
    buttons = daemon_app.layout["dashboards"][0]["buttons"]
    assert any(b["label"] == "Copia" and b["combo"] == "ctrl+c" for b in buttons)


def test_add_button_rejects_invalid_combo(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_button",
        {
            "dashboard_id": "default",
            "label": "Bad",
            "combo": "ctrl+pippo",
            "row": 0,
            "col": 1,
        },
    )
    assert not ok
    assert "non valida" in error


def test_add_button_with_unknown_icon_falls_back_to_default_instead_of_failing(daemon_app):
    """Un nome icona inventato (l'LLM ne suggerisce a volte uno plausibile
    ma inesistente) non deve far fallire la creazione: il pulsante viene
    creato comunque, semplicemente senza icona personalizzata."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_button",
        {
            "dashboard_id": "default",
            "label": "Selezione",
            "combo": "r",
            "icon": "cursor",
            "row": 0,
            "col": 1,
        },
    )
    assert ok, error
    new_button = daemon_app.layout["dashboards"][0]["buttons"][-1]
    assert new_button["label"] == "Selezione"
    assert "icon" not in new_button


def test_add_button_accepts_new_drawing_tool_icons(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_button",
        {
            "dashboard_id": "default",
            "label": "Lazo",
            "combo": "f",
            "icon": "gesture",
            "row": 0,
            "col": 1,
        },
    )
    assert ok, error
    assert daemon_app.layout["dashboards"][0]["buttons"][-1]["icon"] == "gesture"


def test_add_buttons_bulk_not_blocked_by_one_bad_icon(daemon_app):
    """Riproduce il bug segnalato: un batch di piu' pulsanti non deve
    fallire per intero solo perche' uno di essi ha un'icona sconosciuta."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_buttons",
        {
            "dashboard_id": "default",
            "buttons": [
                {"label": "A", "combo": "ctrl+1", "icon": "cursor", "row": 0, "col": 1},
            ],
        },
    )
    assert ok, error
    assert "icon" not in daemon_app.layout["dashboards"][0]["buttons"][-1]


def test_set_button_style_still_rejects_unknown_icon(daemon_app):
    """A differenza di add_button/add_buttons, set_button_style modifica un
    solo pulsante gia' esistente: qui un'icona sconosciuta deve continuare a
    dare un errore esplicito invece di fallire in silenzio."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "Copia", "combo": "ctrl+c", "row": 0, "col": 1},
    )
    ok, error = daemon_app._mutate_layout(
        "set_button_style", {"id": "copia", "icon": "cursor"}
    )
    assert not ok
    assert "non valida" in error


def test_add_button_kind_record_defaults_label_and_ignores_combo(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "kind": "record", "row": 0, "col": 1},
    )
    assert ok, error
    new_button = daemon_app.layout["dashboards"][0]["buttons"][-1]
    assert new_button["label"] == "Registra"
    assert new_button["kind"] == "record"
    assert "combo" not in new_button


def test_add_buttons_bulk_is_atomic(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 2, "cols": 2}
    )
    before = len(daemon_app.layout["dashboards"][0]["buttons"])

    ok, error = daemon_app._mutate_layout(
        "add_buttons",
        {
            "dashboard_id": "default",
            "buttons": [
                {"label": "A", "combo": "ctrl+1", "row": 0, "col": 1},
                {"label": "B", "combo": "ctrl+2", "row": 1, "col": 0},
                # terzo elemento non valido: l'intero batch deve fallire
                {"label": "C", "combo": "ctrl+3", "row": 0, "col": 1},
            ],
        },
    )
    assert not ok
    assert "elemento 2" in error
    after = len(daemon_app.layout["dashboards"][0]["buttons"])
    assert after == before  # nessun pulsante aggiunto: tutto o niente


def test_add_buttons_bulk_reports_all_invalid_elements_not_just_first(daemon_app):
    """Su un batch lungo e' comune che l'LLM sbagli piu' di un elemento:
    l'errore deve elencarli tutti insieme, non solo il primo che trova,
    altrimenti li scopre uno alla volta a chiamate successive."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 2, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_buttons",
        {
            "dashboard_id": "default",
            "buttons": [
                {"label": "A", "combo": "ctrl+pippo", "row": 0, "col": 1},
                {"label": "B", "combo": "ctrl+2", "row": 1, "col": 0},
                {"label": "C", "combo": "/search", "row": 1, "col": 1},
            ],
        },
    )
    assert not ok
    assert "elemento 0" in error
    assert "elemento 2" in error
    assert "elemento 1" not in error  # quello valido non compare tra gli errori


def test_add_buttons_bulk_success(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 2, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "add_buttons",
        {
            "dashboard_id": "default",
            "buttons": [
                {"label": "A", "combo": "ctrl+1", "row": 0, "col": 1},
                {"label": "B", "combo": "ctrl+2", "row": 1, "col": 0},
            ],
        },
    )
    assert ok, error
    labels = {b["label"] for b in daemon_app.layout["dashboards"][0]["buttons"]}
    assert {"A", "B"}.issubset(labels)


def test_remove_last_record_button_is_blocked(daemon_app):
    ok, error = daemon_app._mutate_layout("remove_button", {"id": "record"})
    assert not ok
    assert "microfono" in error or "record" in error.lower()


def test_remove_extra_record_button_allowed(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, _ = daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "kind": "record", "row": 0, "col": 1, "label": "Mic2"},
    )
    assert ok
    ok, error = daemon_app._mutate_layout("remove_button", {"id": "mic2"})
    assert ok, error
    # il primo pulsante "record" resta, essendo ancora l'unico
    ok, error = daemon_app._mutate_layout("remove_button", {"id": "record"})
    assert not ok


def test_move_record_button_now_allowed(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "move_button", {"id": "record", "row": 0, "col": 1}
    )
    assert ok, error
    button = daemon_app.layout["dashboards"][0]["buttons"][0]
    assert (button["row"], button["col"]) == (0, 1)


def test_move_button_swaps_with_occupant(daemon_app):
    """Trascinare una casella sopra un'altra le scambia di posto invece di
    fallire: comodo per riordinare i pulsanti sul telefono."""
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "X", "combo": "ctrl+x", "row": 0, "col": 1},
    )
    # "record" e' in (0,0), "x" e' in (0,1): sposto "record" su (0,1)
    ok, error = daemon_app._mutate_layout(
        "move_button", {"id": "record", "row": 0, "col": 1}
    )
    assert ok, error
    buttons = {b["id"]: (b["row"], b["col"]) for b in daemon_app.layout["dashboards"][0]["buttons"]}
    assert buttons["record"] == (0, 1)
    assert buttons["x"] == (0, 0)


def test_move_button_to_empty_cell_still_works(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    ok, error = daemon_app._mutate_layout(
        "move_button", {"id": "record", "row": 0, "col": 1}
    )
    assert ok, error
    button = daemon_app.layout["dashboards"][0]["buttons"][0]
    assert (button["row"], button["col"]) == (0, 1)


def test_move_button_onto_itself_is_a_no_op(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "move_button", {"id": "record", "row": 0, "col": 0}
    )
    assert ok, error
    button = daemon_app.layout["dashboards"][0]["buttons"][0]
    assert (button["row"], button["col"]) == (0, 0)


def test_set_grid_size_rejects_shrink_that_strands_buttons(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 2, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {"dashboard_id": "default", "label": "X", "combo": "ctrl+x", "row": 1, "col": 1},
    )
    ok, error = daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 1}
    )
    assert not ok
    assert "fuori dalla griglia" in error


def test_create_and_remove_dashboard(daemon_app):
    ok, error = daemon_app._mutate_layout("create_dashboard", {"name": "InvokeAI"})
    assert ok, error
    assert any(d["id"] == "invokeai" for d in daemon_app.layout["dashboards"])

    ok, error = daemon_app._mutate_layout("remove_dashboard", {"id": "invokeai"})
    assert ok, error
    assert not any(d["id"] == "invokeai" for d in daemon_app.layout["dashboards"])


def test_create_dashboard_with_buttons_in_one_call(daemon_app):
    """Evita di dover fare create_dashboard e poi add_buttons separatamente:
    la griglia si dimensiona automaticamente sui pulsanti forniti."""
    ok, error = daemon_app._mutate_layout(
        "create_dashboard",
        {
            "name": "Invoke AI",
            "match": "invoke",
            "buttons": [
                {"label": "Invoca", "combo": "ctrl+enter", "row": 0, "col": 0},
                {"label": "Annulla", "combo": "esc", "row": 0, "col": 1},
                {"label": "Zoom", "combo": "z", "row": 1, "col": 0},
            ],
        },
    )
    assert ok, error
    dashboard = daemon_app.layout["dashboards"][-1]
    assert dashboard["name"] == "Invoke AI"
    assert dashboard["match"] == "invoke"
    # griglia auto-dimensionata sulla posizione massima usata (row 1, col 1)
    assert dashboard["rows"] == 2 and dashboard["cols"] == 2
    labels = {b["label"] for b in dashboard["buttons"]}
    assert labels == {"Invoca", "Annulla", "Zoom"}


def test_create_dashboard_without_buttons_stays_1x1_empty(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "create_dashboard", {"name": "Vuota"}
    )
    assert ok, error
    dashboard = daemon_app.layout["dashboards"][-1]
    assert dashboard["rows"] == 1 and dashboard["cols"] == 1
    assert dashboard["buttons"] == []


def test_create_dashboard_explicit_grid_size_overrides_auto_sizing(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "create_dashboard",
        {
            "name": "Con spazio extra",
            "rows": 4,
            "cols": 4,
            "buttons": [{"label": "A", "combo": "ctrl+a", "row": 0, "col": 0}],
        },
    )
    assert ok, error
    dashboard = daemon_app.layout["dashboards"][-1]
    assert dashboard["rows"] == 4 and dashboard["cols"] == 4


def test_create_dashboard_with_buttons_is_atomic(daemon_app):
    """Se anche un solo pulsante non e' valido, la dashboard non deve essere
    creata affatto (niente dashboard "a meta'")."""
    before = len(daemon_app.layout["dashboards"])
    ok, error = daemon_app._mutate_layout(
        "create_dashboard",
        {
            "name": "Da scartare",
            "buttons": [
                {"label": "OK", "combo": "ctrl+a", "row": 0, "col": 0},
                {"label": "Bad", "combo": "ctrl+pippo", "row": 0, "col": 1},
            ],
        },
    )
    assert not ok
    assert "elemento 1" in error
    assert len(daemon_app.layout["dashboards"]) == before
    assert not any(d["name"] == "Da scartare" for d in daemon_app.layout["dashboards"])


def test_create_dashboard_rejects_non_list_buttons(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "create_dashboard", {"name": "X", "buttons": "non una lista"}
    )
    assert not ok
    assert "lista" in error


def test_cannot_remove_last_dashboard(daemon_app):
    ok, error = daemon_app._mutate_layout("remove_dashboard", {"id": "default"})
    assert not ok
    assert "unica" in error


def test_cannot_remove_dashboard_with_only_record_button(daemon_app):
    daemon_app._mutate_layout("create_dashboard", {"name": "Altra"})
    # "default" contiene l'unico pulsante record del layout
    ok, error = daemon_app._mutate_layout("remove_dashboard", {"id": "default"})
    assert not ok
    assert "record" in error.lower() or "microfono" in error.lower()

    # ma la dashboard senza record si puo' rimuovere
    ok, error = daemon_app._mutate_layout("remove_dashboard", {"id": "altra"})
    assert ok, error


def test_set_dashboard_match(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_match", {"id": "default", "match": "code"}
    )
    assert ok, error
    assert daemon_app.layout["dashboards"][0]["match"] == "code"

    snapshot = daemon_app._layout_snapshot()
    assert snapshot["dashboards"][0]["match"] == "code"


def test_reset_layout(daemon_app):
    daemon_app._mutate_layout("create_dashboard", {"name": "Extra"})
    ok, error = daemon_app._mutate_layout("reset_layout", {})
    assert ok, error
    assert len(daemon_app.layout["dashboards"]) == 1
    assert daemon_app.layout["dashboards"][0]["id"] == "default"


def test_unknown_command_returns_error(daemon_app):
    ok, error = daemon_app._mutate_layout("does_not_exist", {})
    assert not ok
    assert "sconosciuto" in error


def test_reorder_dashboard(daemon_app):
    daemon_app._mutate_layout("create_dashboard", {"name": "B"})
    daemon_app._mutate_layout("create_dashboard", {"name": "C"})
    ids = [d["id"] for d in daemon_app.layout["dashboards"]]
    assert ids == ["default", "b", "c"]

    ok, error = daemon_app._mutate_layout(
        "reorder_dashboard", {"id": "default", "position": 2}
    )
    assert ok, error
    ids = [d["id"] for d in daemon_app.layout["dashboards"]]
    assert ids == ["b", "c", "default"]


def test_reorder_dashboard_clamps_out_of_range_position(daemon_app):
    daemon_app._mutate_layout("create_dashboard", {"name": "B"})
    ok, error = daemon_app._mutate_layout(
        "reorder_dashboard", {"id": "default", "position": 999}
    )
    assert ok, error
    ids = [d["id"] for d in daemon_app.layout["dashboards"]]
    assert ids == ["b", "default"]  # posizione troppo alta -> clampata in fondo


def test_reorder_dashboard_unknown_id(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "reorder_dashboard", {"id": "nope", "position": 0}
    )
    assert not ok
    assert "non trovata" in error


def test_duplicate_dashboard_copies_buttons_but_not_record_or_match(daemon_app):
    daemon_app._mutate_layout(
        "set_grid_size", {"dashboard_id": "default", "rows": 1, "cols": 2}
    )
    daemon_app._mutate_layout(
        "add_button",
        {
            "dashboard_id": "default",
            "label": "Copia",
            "combo": "ctrl+c",
            "row": 0,
            "col": 1,
            "color": "#e53935",
            "icon": "content_copy",
        },
    )
    daemon_app._mutate_layout(
        "set_dashboard_match", {"id": "default", "match": "code"}
    )

    ok, error = daemon_app._mutate_layout(
        "duplicate_dashboard", {"id": "default", "name": "Copia dashboard"}
    )
    assert ok, error
    dup = daemon_app.layout["dashboards"][-1]
    assert dup["name"] == "Copia dashboard"
    assert dup["rows"] == 1 and dup["cols"] == 2
    assert dup["match"] == ""  # il match non si copia
    # il pulsante "record" non viene duplicato, solo "Copia" (kind keys)
    assert len(dup["buttons"]) == 1
    assert dup["buttons"][0]["label"] == "Copia"
    assert dup["buttons"][0]["color"] == "#e53935"
    # id diverso dall'originale, anche se lo stesso testo di partenza
    original_id = daemon_app.layout["dashboards"][0]["buttons"][1]["id"]
    assert dup["buttons"][0]["id"] != original_id


def test_duplicate_dashboard_default_name(daemon_app):
    ok, error = daemon_app._mutate_layout("duplicate_dashboard", {"id": "default"})
    assert ok, error
    assert daemon_app.layout["dashboards"][-1]["name"] == "Stenografa (copia)"


def test_duplicate_dashboard_unknown_id(daemon_app):
    ok, error = daemon_app._mutate_layout("duplicate_dashboard", {"id": "nope"})
    assert not ok
    assert "non trovata" in error


def test_dashboard_match_supports_comma_separated_patterns(daemon_app):
    daemon_app._mutate_layout(
        "set_dashboard_match", {"id": "default", "match": "code, codium"}
    )
    assert daemon_app._dashboard_for_focus("code", "") == "default"
    assert daemon_app._dashboard_for_focus("codium", "") == "default"
    assert daemon_app._dashboard_for_focus("firefox", "qualcosa") is None


def test_dashboard_match_still_works_without_comma(daemon_app):
    daemon_app._mutate_layout(
        "set_dashboard_match", {"id": "default", "match": "firefox"}
    )
    assert daemon_app._dashboard_for_focus("firefox", "") == "default"
    assert daemon_app._dashboard_for_focus("chrome", "") is None


# --- vocabolario di scorciatoie nascoste (set_dashboard_shortcuts) ---


def test_set_dashboard_shortcuts_replaces_list(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [
                {"label": "Seleziona rettangolo", "combo": "r"},
                {"label": "Pennello", "combo": "p"},
            ],
        },
    )
    assert ok, error
    dashboard = daemon_app.layout["dashboards"][0]
    assert dashboard["shortcuts"] == [
        {"label": "Seleziona rettangolo", "combo": "r"},
        {"label": "Pennello", "combo": "p"},
    ]

    # una seconda chiamata sostituisce, non somma
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "Zoom", "combo": "z"}]},
    )
    assert ok, error
    assert daemon_app.layout["dashboards"][0]["shortcuts"] == [
        {"label": "Zoom", "combo": "z"}
    ]


def test_set_dashboard_shortcuts_append_keeps_existing(daemon_app):
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "Zoom", "combo": "z"}]},
    )
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [{"label": "Pennello", "combo": "p"}],
            "mode": "append",
        },
    )
    assert ok, error
    assert daemon_app.layout["dashboards"][0]["shortcuts"] == [
        {"label": "Zoom", "combo": "z"},
        {"label": "Pennello", "combo": "p"},
    ]


def test_set_dashboard_shortcuts_append_updates_same_label(daemon_app):
    """Ripassare un'etichetta gia' presente ne aggiorna la combinazione
    invece di lasciare due voci in conflitto per lo stesso nome."""
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "Zoom", "combo": "z"}]},
    )
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [{"label": "zoom", "combo": "ctrl+plus"}],
            "mode": "append",
        },
    )
    assert daemon_app.layout["dashboards"][0]["shortcuts"] == [
        {"label": "zoom", "combo": "ctrl+plus"}
    ]


def test_set_dashboard_shortcuts_rejects_unknown_mode(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [{"label": "Zoom", "combo": "z"}],
            "mode": "somma",
        },
    )
    assert not ok
    assert "mode" in error


def test_set_dashboard_shortcuts_is_atomic_on_invalid_entry(daemon_app):
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "Zoom", "combo": "z"}]},
    )
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [
                {"label": "OK", "combo": "ctrl+a"},
                {"label": "Bad", "combo": "ctrl+pippo"},
            ],
        },
    )
    assert not ok
    assert "shortcuts[1]" in error
    # il vocabolario precedente non viene toccato
    assert daemon_app.layout["dashboards"][0]["shortcuts"] == [
        {"label": "Zoom", "combo": "z"}
    ]


def test_set_dashboard_shortcuts_reports_all_invalid_entries_not_just_first(daemon_app):
    """Riproduce il caso reale: una lista lunga di scorciatoie con piu' di
    una voce sbagliata (es. token con underscore non canonico, o comandi
    testuali tipo "/search" che non sono combinazioni di tasti) deve
    segnalarle tutte in un colpo solo."""
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [
                {"label": "OK", "combo": "ctrl+a"},
                {"label": "Cerca", "combo": "/search"},
                {"label": "Rispondi", "combo": "/reply"},
            ],
        },
    )
    assert not ok
    assert "shortcuts[1]" in error
    assert "shortcuts[2]" in error
    assert "shortcuts[0]" not in error


def test_set_dashboard_shortcuts_rejects_missing_label_or_combo(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"combo": "ctrl+a"}]},
    )
    assert not ok
    assert "label" in error

    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "OK"}]},
    )
    assert not ok
    assert "combo" in error


def test_set_dashboard_shortcuts_unknown_dashboard(daemon_app):
    ok, error = daemon_app._mutate_layout(
        "set_dashboard_shortcuts", {"dashboard_id": "nope", "shortcuts": []}
    )
    assert not ok
    assert "non trovata" in error


def test_dashboard_shortcuts_included_in_layout_snapshot(daemon_app):
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "Zoom", "combo": "z"}]},
    )
    snapshot = daemon_app._layout_snapshot()
    assert snapshot["dashboards"][0]["shortcuts"] == [{"label": "Zoom", "combo": "z"}]


def test_create_dashboard_with_hidden_shortcuts(daemon_app):
    """Le scorciatoie in `shortcuts` non occupano la griglia (a differenza
    di `buttons`) ma vengono comunque salvate per il pulsante ai_command."""
    ok, error = daemon_app._mutate_layout(
        "create_dashboard",
        {
            "name": "Gimp",
            "shortcuts": [
                {"label": "Seleziona rettangolo", "combo": "r"},
                {"label": "Pennello", "combo": "p"},
            ],
        },
    )
    assert ok, error
    dashboard = daemon_app.layout["dashboards"][-1]
    assert dashboard["rows"] == 1 and dashboard["cols"] == 1  # nessun pulsante visibile
    assert dashboard["buttons"] == []
    assert dashboard["shortcuts"] == [
        {"label": "Seleziona rettangolo", "combo": "r"},
        {"label": "Pennello", "combo": "p"},
    ]


def test_create_dashboard_with_bad_shortcuts_creates_nothing(daemon_app):
    before = len(daemon_app.layout["dashboards"])
    ok, error = daemon_app._mutate_layout(
        "create_dashboard",
        {"name": "Da scartare", "shortcuts": [{"label": "Bad", "combo": "ctrl+pippo"}]},
    )
    assert not ok
    assert "shortcuts[0]" in error
    assert len(daemon_app.layout["dashboards"]) == before


def test_duplicate_dashboard_copies_hidden_shortcuts(daemon_app):
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {"dashboard_id": "default", "shortcuts": [{"label": "Zoom", "combo": "z"}]},
    )
    ok, error = daemon_app._mutate_layout(
        "duplicate_dashboard", {"id": "default"}
    )
    assert ok, error
    assert daemon_app.layout["dashboards"][-1]["shortcuts"] == [
        {"label": "Zoom", "combo": "z"}
    ]


def test_dashboard_shortcuts_context_includes_hidden_vocabulary(daemon_app):
    daemon_app._mutate_layout(
        "rename_dashboard", {"id": "default", "name": "Gimp"}
    )
    daemon_app._mutate_layout(
        "set_dashboard_shortcuts",
        {
            "dashboard_id": "default",
            "shortcuts": [{"label": "Seleziona rettangolo", "combo": "r"}],
        },
    )
    context = daemon_app._dashboard_shortcuts_context("default")
    assert '"Seleziona rettangolo" -> r' in context

"""Test del comando IA chiesto da una dashboard sullo stesso PC (socket di
controllo): dettatura innescata da li', candidati parcheggiati invece che
eseguiti, e comandi da terminale — che esistono solo per questa via e non
partono senza una conferma esplicita."""
import json

import daemon
from conftest import ImmediateThread, fake_llm


# --- candidati "comando da terminale" ---


def test_shell_candidate_normalises_single_command_to_list(daemon_app):
    entries = daemon_app._resolve_shell_candidate(
        {"label": "Compila", "shell": "npm run build"}
    )
    assert entries == [{"label": "Compila", "shell": ["npm run build"]}]


def test_shell_candidate_keeps_command_order(daemon_app):
    entries = daemon_app._resolve_shell_candidate(
        {"label": "Compila", "shell": ["cd ~/progetto", "npm run build"]}
    )
    assert entries[0]["shell"] == ["cd ~/progetto", "npm run build"]


def test_shell_candidate_rejected_without_label(daemon_app):
    assert daemon_app._resolve_shell_candidate({"shell": ["ls"]}) == []


def test_shell_candidate_rejected_when_empty(daemon_app):
    assert daemon_app._resolve_shell_candidate({"label": "X", "shell": []}) == []
    assert daemon_app._resolve_shell_candidate({"label": "X", "shell": ["  "]}) == []


def test_shell_candidate_rejects_hidden_newlines(daemon_app):
    """Un comando che ne nasconde altri dopo un a capo verrebbe confermato
    senza essere letto per intero: si scarta il candidato."""
    entries = daemon_app._resolve_shell_candidate(
        {"label": "Innocuo", "shell": ["ls\nrm -rf ~/Documenti"]}
    )
    assert entries == []


def test_shell_candidate_rejects_too_many_commands(daemon_app):
    entries = daemon_app._resolve_shell_candidate(
        {
            "label": "Troppi",
            "shell": ["echo x"] * (daemon.CONTROL_SHELL_MAX_COMMANDS + 1),
        }
    )
    assert entries == []


def test_shell_candidate_rejects_overlong_command(daemon_app):
    entries = daemon_app._resolve_shell_candidate(
        {"label": "Lungo", "shell": ["echo " + "x" * daemon.CONTROL_SHELL_MAX_LENGTH]}
    )
    assert entries == []


# --- il telefono non vede mai i comandi da terminale ---


def test_shell_candidates_ignored_without_allow_shell(daemon_app):
    raw = json.dumps([{"label": "Compila", "shell": ["npm run build"]}])
    assert daemon_app._parse_shortcut_candidates(raw) == []


def test_shell_candidates_parsed_with_allow_shell(daemon_app):
    raw = json.dumps([{"label": "Compila", "shell": ["npm run build"]}])
    candidates = daemon_app._parse_shortcut_candidates(raw, allow_shell=True)
    assert candidates == [{"label": "Compila", "shell": ["npm run build"]}]


def test_shell_prompt_only_when_allowed(daemon_app, monkeypatch):
    capture = fake_llm(monkeypatch, daemon_app, reply="[]", capture={})
    daemon_app._interpret_as_shortcuts("qualcosa")
    assert "shell" not in capture["system"]
    daemon_app._interpret_as_shortcuts("qualcosa", allow_shell=True)
    assert "\"shell\"" in capture["system"]


def test_distinct_shell_candidates_are_not_deduplicated(daemon_app):
    """Due comandi diversi restano due opzioni: la chiave di deduplicazione
    deve distinguerli (nessuno dei due ha combo o app_id)."""
    raw = json.dumps(
        [
            {"label": "Elenca", "shell": ["ls"]},
            {"label": "Stato git", "shell": ["git status"]},
        ]
    )
    candidates = daemon_app._parse_shortcut_candidates(raw, allow_shell=True)
    assert [c["label"] for c in candidates] == ["Elenca", "Stato git"]


# --- interpretazione senza esecuzione ---


def test_control_ai_command_parks_options_without_executing(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    executed = []
    monkeypatch.setattr(
        daemon_app, "_execute_candidate", lambda c: executed.append(c) or ("", None)
    )

    reply = daemon_app._handle_control_ai_command("copia")
    assert reply["ok"]
    assert reply["options"] == [{"label": "ctrl+c", "combo": "ctrl+c"}]
    # un solo candidato: il telefono lo eseguirebbe subito, qui no
    assert executed == []
    assert daemon_app._control_choice["id"] == reply["request_id"]


def test_control_ai_command_rejects_empty_text(daemon_app):
    assert not daemon_app._handle_control_ai_command("   ")["ok"]


def test_control_ai_command_rejects_overlong_text(daemon_app):
    reply = daemon_app._handle_control_ai_command("x" * (daemon.CONTROL_AI_MAX_TEXT + 1))
    assert not reply["ok"]
    assert "troppo lungo" in reply["error"]


def test_control_ai_worker_parks_and_frees_the_state(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    daemon_app._control_ai_worker("copia")

    session = daemon_app._control_session_snapshot()
    assert session["phase"] == "choice"
    assert session["text"] == "copia"
    assert session["options"][0]["combo"] == "ctrl+c"
    # lo stato torna libero passando dal loop principale, non da qui
    assert daemon_app.command_queue.get_nowait() == ("control_ai_ready",)


def test_control_ai_worker_reports_backend_errors(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, error="LM Studio non raggiungibile")
    daemon_app._control_ai_worker("copia")

    session = daemon_app._control_session_snapshot()
    assert session["phase"] == "error"
    assert "LM Studio" in session["error"]


# --- esecuzione dell'opzione scelta ---


def test_control_ai_choose_executes_chosen_option(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    monkeypatch.setattr(daemon_app, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(daemon_app, "_execute_candidate", lambda c: (c["combo"], None))

    parked = daemon_app._handle_control_ai_command("copia")
    reply = daemon_app._handle_control_ai_choose(parked["request_id"], 0)
    assert reply["ok"]
    assert reply["executed"] == "ctrl+c"
    assert daemon_app._control_session_snapshot()["phase"] == "done"


def test_control_ai_choose_runs_once(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    monkeypatch.setattr(daemon_app, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(daemon_app, "_execute_candidate", lambda c: (c["combo"], None))

    parked = daemon_app._handle_control_ai_command("copia")
    daemon_app._handle_control_ai_choose(parked["request_id"], 0)
    again = daemon_app._handle_control_ai_choose(parked["request_id"], 0)
    assert not again["ok"]


def test_control_ai_choose_rejects_unknown_request(daemon_app):
    assert not daemon_app._handle_control_ai_choose("inesistente", 0)["ok"]


def test_control_ai_choose_rejects_out_of_range_index(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    parked = daemon_app._handle_control_ai_command("copia")
    reply = daemon_app._handle_control_ai_choose(parked["request_id"], 5)
    assert not reply["ok"]
    # la richiesta resta valida: un indice sbagliato non brucia la scelta
    assert daemon_app._control_choice is not None


def test_control_ai_choose_rejects_invalid_delay(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    parked = daemon_app._handle_control_ai_command("copia")
    reply = daemon_app._handle_control_ai_choose(
        parked["request_id"], 0, daemon.MACRO_MAX_DELAY_MS + 1
    )
    assert not reply["ok"]
    assert "delay_ms" in reply["error"]


# --- conferma obbligatoria dei comandi da terminale ---


def test_shell_candidate_not_executed_without_confirmation(daemon_app, monkeypatch):
    fake_llm(
        monkeypatch,
        daemon_app,
        reply=json.dumps([{"label": "Compila", "shell": ["npm run build"]}]),
    )
    ran = []
    monkeypatch.setattr(
        daemon_app, "_run_shell_candidate", lambda c: ran.append(c) or ("", "", None)
    )

    parked = daemon_app._handle_control_ai_command("compila il progetto")
    reply = daemon_app._handle_control_ai_choose(parked["request_id"], 0)
    assert not reply["ok"]
    assert reply["needs_confirm"]
    assert reply["shell"] == ["npm run build"]
    assert ran == []
    # la richiesta resta in attesa: il pannello deve poterla confermare
    assert daemon_app._control_choice is not None


def test_shell_candidate_executed_with_confirmation(daemon_app, monkeypatch):
    fake_llm(
        monkeypatch,
        daemon_app,
        reply=json.dumps([{"label": "Compila", "shell": ["npm run build"]}]),
    )
    monkeypatch.setattr(daemon_app, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(
        daemon_app, "_run_shell_candidate", lambda c: ("npm run build", "ok", None)
    )

    parked = daemon_app._handle_control_ai_command("compila il progetto")
    reply = daemon_app._handle_control_ai_choose(
        parked["request_id"], 0, confirm_shell=True
    )
    assert reply["ok"]
    assert reply["output"] == "ok"


def test_run_shell_candidate_reports_failure(daemon_app):
    description, output, error = daemon_app._run_shell_candidate(
        {"label": "Fallisce", "shell": ["exit 3"]}
    )
    assert description == "exit 3"
    assert "codice 3" in error


def test_run_shell_candidate_keeps_directory_between_commands(daemon_app):
    """I comandi girano in una sola shell: un `cd` vale anche per i
    successivi, che e' il motivo per cui vengono eseguiti insieme."""
    _, output, error = daemon_app._run_shell_candidate(
        {"label": "Dove sono", "shell": ["cd /tmp", "pwd"]}
    )
    assert error is None
    assert output.strip().endswith("/tmp")


def test_run_shell_candidate_truncates_long_output(daemon_app):
    _, output, error = daemon_app._run_shell_candidate(
        {"label": "Tanto testo", "shell": [f"head -c {daemon.CONTROL_SHELL_OUTPUT_CHARS * 2} /dev/zero | tr '\\0' 'x'"]}
    )
    assert error is None
    assert len(output) <= daemon.CONTROL_SHELL_OUTPUT_CHARS + 8


# --- dettatura innescata dal PC ---


def test_control_ai_record_opens_a_session(daemon_app):
    daemon_app.state = daemon.STATE_IDLE
    reply = daemon_app._handle_control_ai_record()
    assert reply["ok"]
    assert reply["state"] == "recording"
    assert daemon_app._control_session_snapshot()["phase"] == "recording"
    # il microfono si tocca solo dal loop principale
    assert daemon_app.command_queue.get_nowait() == ("control_ai_record", None)


def test_control_ai_record_refuses_when_busy(daemon_app):
    daemon_app.state = daemon.STATE_THINKING
    assert not daemon_app._handle_control_ai_record()["ok"]


def test_control_ai_record_does_not_steal_a_phone_dictation(daemon_app):
    daemon_app.state = daemon.STATE_RECORDING
    daemon_app._recording_from_control = False
    reply = daemon_app._handle_control_ai_record()
    assert not reply["ok"]
    assert "telefono" in reply["error"]


def test_control_ai_record_stops_its_own_dictation(daemon_app):
    daemon_app.state = daemon.STATE_RECORDING
    daemon_app._recording_from_control = True
    reply = daemon_app._handle_control_ai_record()
    assert reply["ok"]
    assert reply["state"] == "transcribing"


def test_transcription_from_control_goes_to_the_panel(daemon_app, monkeypatch):
    """La frase dettata dal PC finisce a _control_ai_worker (che parcheggia)
    invece che a _ai_command_worker (che esegue)."""
    monkeypatch.setattr(daemon, "threading", type("M", (), {"Thread": ImmediateThread}))
    monkeypatch.setattr(daemon_app, "_set_state", lambda state: None)
    monkeypatch.setattr(daemon_app, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(daemon_app, "_broadcast", lambda obj: None)
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    executed = []
    monkeypatch.setattr(
        daemon_app, "_execute_candidate", lambda c: executed.append(c) or ("", None)
    )

    daemon_app._recording_mode = "ai_command"
    daemon_app._recording_from_control = True
    daemon_app._on_transcription_done("copia", None)

    assert executed == []
    session = daemon_app._control_session_snapshot()
    assert session["phase"] == "choice"
    assert session["options"][0]["combo"] == "ctrl+c"
    # consumato: la dettatura successiva potrebbe arrivare dal telefono
    assert daemon_app._recording_from_control is False


def test_transcription_error_from_control_reaches_the_panel(daemon_app, monkeypatch):
    monkeypatch.setattr(daemon_app, "_set_state", lambda state: None)
    monkeypatch.setattr(daemon_app, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(daemon_app, "_broadcast", lambda obj: None)

    daemon_app._recording_mode = "ai_command"
    daemon_app._recording_from_control = True
    daemon_app._on_transcription_done(None, "microfono occupato")

    session = daemon_app._control_session_snapshot()
    assert session["phase"] == "error"
    assert "microfono" in session["error"]


# --- stato seguito dal pannello ---


def test_session_seq_grows_on_every_change(daemon_app):
    first = daemon_app._control_session_snapshot()["seq"]
    daemon_app._set_control_session(phase="thinking")
    second = daemon_app._control_session_snapshot()["seq"]
    assert second > first


def test_cancel_discards_pending_options(daemon_app, monkeypatch):
    fake_llm(monkeypatch, daemon_app, reply="ctrl+c")
    daemon_app.state = daemon.STATE_IDLE
    daemon_app._handle_control_ai_command("copia")
    assert daemon_app._handle_control_ai_cancel()["ok"]
    assert daemon_app._control_choice is None
    assert daemon_app._control_session_snapshot()["phase"] == "idle"


def test_cancel_refuses_during_recording(daemon_app):
    daemon_app.state = daemon.STATE_RECORDING
    assert not daemon_app._handle_control_ai_cancel()["ok"]

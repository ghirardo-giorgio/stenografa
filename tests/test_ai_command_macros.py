"""Test dell'estensione del comando vocale IA alle macro multi-passo
("in gimp crea un nuovo file, aggiungi un livello e impostalo di rosso"):
il modello puo' proporre una sequenza di combinazioni ("combos" invece di
"combo") SOLO quando il comando parte da una dashboard nota, e le
combinazioni valgono per l'applicazione di QUELLA dashboard (vedi
AI_COMMAND_MACRO_PROMPT_TEMPLATE/_dashboard_name) - stesso principio di
vocabolario/contesto chiuso gia' usato per scorciatoie e avvio app."""
import daemon as daemon_module
from conftest import fake_llm


class FakeBackend:
    def __init__(self, keys_ok=True, fail_at=None):
        self.keys = []
        self._keys_ok = keys_ok
        self._fail_at = fail_at  # indice del passo che deve fallire, o None
        self.notifications = []

    def simulate_keys(self, combo):
        self.keys.append(combo)
        if self._fail_at is not None and len(self.keys) - 1 == self._fail_at:
            return False
        return self._keys_ok

    def notify(self, title, body, urgency="normal"):
        self.notifications.append((title, body, urgency))


# --- _dashboard_name ---


def test_dashboard_name_returns_name_for_existing_dashboard(daemon_app):
    assert daemon_app._dashboard_name("default") == "Stenografa"


def test_dashboard_name_none_for_unknown_dashboard(daemon_app):
    assert daemon_app._dashboard_name("nope") is None


# --- _resolve_macro_candidate ---


def test_resolve_macro_candidate_accepts_valid_spec(daemon_app):
    entries = daemon_app._resolve_macro_candidate(
        {"label": "Nuovo livello rosso", "combos": ["ctrl+n", "shift+ctrl+n"]}
    )
    assert entries == [
        {
            "label": "Nuovo livello rosso",
            "combos": ["ctrl+n", "shift+ctrl+n"],
            "delay_ms": daemon_module.MACRO_DEFAULT_DELAY_MS,
        }
    ]


def test_resolve_macro_candidate_rejects_missing_label(daemon_app):
    assert daemon_app._resolve_macro_candidate({"combos": ["ctrl+n"]}) == []


def test_resolve_macro_candidate_rejects_invalid_combo(daemon_app):
    assert (
        daemon_app._resolve_macro_candidate(
            {"label": "x", "combos": ["ctrl+n", "ctrl+pippo"]}
        )
        == []
    )


def test_resolve_macro_candidate_rejects_too_many_steps(daemon_app):
    combos = ["ctrl+n"] * (daemon_module.MACRO_MAX_STEPS + 1)
    assert daemon_app._resolve_macro_candidate({"label": "x", "combos": combos}) == []


# --- _parse_shortcut_candidates(allow_macro=...) ---


def test_parse_candidates_accepts_macro_when_allowed(daemon_app):
    candidates = daemon_app._parse_shortcut_candidates(
        '[{"label": "Nuovo livello", "combos": ["ctrl+n", "shift+ctrl+n"]}]',
        allow_macro=True,
    )
    assert candidates == [
        {
            "label": "Nuovo livello",
            "combos": ["ctrl+n", "shift+ctrl+n"],
            "delay_ms": daemon_module.MACRO_DEFAULT_DELAY_MS,
        }
    ]


def test_parse_candidates_drops_macro_when_not_allowed(daemon_app):
    # senza dashboard nota "combos" non e' un campo riconosciuto: lo spec
    # cade nella validazione di una scorciatoia singola, che rifiuta per
    # mancanza di "combo" - il candidato va perso, non eseguito a caso
    candidates = daemon_app._parse_shortcut_candidates(
        '[{"label": "Nuovo livello", "combos": ["ctrl+n", "shift+ctrl+n"]}]',
        allow_macro=False,
    )
    assert candidates == []


def test_parse_candidates_deduplicates_identical_macros(daemon_app):
    candidates = daemon_app._parse_shortcut_candidates(
        '[{"label": "a", "combos": ["ctrl+n", "ctrl+s"]}, '
        '{"label": "b", "combos": ["ctrl+n", "ctrl+s"]}]',
        allow_macro=True,
    )
    assert len(candidates) == 1


def test_parse_candidates_mixes_combo_and_macro(daemon_app):
    candidates = daemon_app._parse_shortcut_candidates(
        '[{"label": "Copia", "combo": "ctrl+c"}, '
        '{"label": "Nuovo livello", "combos": ["ctrl+n", "shift+ctrl+n"]}]',
        allow_macro=True,
    )
    assert candidates[0]["combo"] == "ctrl+c"
    assert candidates[1]["combos"] == ["ctrl+n", "shift+ctrl+n"]


# --- _execute_candidate (ramo macro) ---


def test_execute_candidate_runs_macro_steps_in_order(daemon_app):
    daemon_app.backend = FakeBackend()
    executed, error = daemon_app._execute_candidate(
        {"label": "x", "combos": ["ctrl+n", "shift+ctrl+n"], "delay_ms": 0}
    )
    assert error is None
    assert executed == "ctrl+n -> shift+ctrl+n"
    assert daemon_app.backend.keys == ["ctrl+n", "shift+ctrl+n"]


def test_execute_candidate_stops_macro_at_first_failure(daemon_app):
    daemon_app.backend = FakeBackend(fail_at=1)
    executed, error = daemon_app._execute_candidate(
        {
            "label": "x",
            "combos": ["ctrl+n", "shift+ctrl+n", "ctrl+s"],
            "delay_ms": 0,
        }
    )
    assert error is not None
    assert "passo 2" in error
    # il terzo passo non va eseguito: la macro si interrompe al fallimento
    assert daemon_app.backend.keys == ["ctrl+n", "shift+ctrl+n"]
    assert executed == "ctrl+n -> shift+ctrl+n -> ctrl+s"


# --- _interpret_as_shortcuts: il contesto/capacita' macro dipende dalla
# dashboard di provenienza ---


def test_interpret_includes_macro_capability_when_dashboard_known(
    daemon_app, monkeypatch
):
    captured = fake_llm(
        monkeypatch, daemon_app, capture={},
        reply=(
            '[{"label": "Nuovo livello", '
                                '"combos": ["ctrl+n", "shift+ctrl+n"]}]'
        ),
    )
    candidates, error = daemon_app._interpret_as_shortcuts(
        "crea un nuovo file e aggiungi un livello", dashboard_id="default"
    )
    assert error is None
    assert candidates == [
        {
            "label": "Nuovo livello",
            "combos": ["ctrl+n", "shift+ctrl+n"],
            "delay_ms": daemon_module.MACRO_DEFAULT_DELAY_MS,
        }
    ]
    # il nome della dashboard vincola esplicitamente a quale applicazione
    # devono valere le combinazioni generate
    assert '"Stenografa"' in captured["system"]
    assert "MACRO" in captured["system"]


def test_interpret_has_no_macro_capability_without_dashboard(daemon_app, monkeypatch):
    # il modello propone comunque una macro nonostante non le sia stata
    # descritta la capacita': il demone non deve eseguirla
    captured = fake_llm(
        monkeypatch, daemon_app, capture={},
        reply=(
            '[{"label": "Nuovo livello", '
            '"combos": ["ctrl+n", "shift+ctrl+n"]}]'
        ),
    )
    candidates, error = daemon_app._interpret_as_shortcuts(
        "crea un nuovo file e aggiungi un livello"
    )
    assert "MACRO" not in captured["system"]
    assert candidates is None
    assert "non riconosciuto" in error


# --- disambiguazione: una macro puo' comparire fra le opzioni proposte ---


def test_choice_reply_accepts_only_proposed_macro(daemon_app, monkeypatch):
    monkeypatch.setattr(daemon_module.Stenografa, "_set_state", lambda self, s: None)
    daemon_app.backend = FakeBackend()
    daemon_app._pending_choice = {
        "id": "req1",
        "text": "gimp: nuovo file e livello",
        "options": [
            {"label": "keys", "combo": "ctrl+n"},
            {
                "label": "macro",
                "combos": ["ctrl+n", "shift+ctrl+n"],
                "delay_ms": 0,
            },
        ],
        "expires_at": float("inf"),
    }

    # una macro non fra le opzioni non viene eseguita
    daemon_app._on_ai_choice_reply("req1", None, None, ["ctrl+n", "ctrl+q"])
    assert daemon_app.backend.keys == []

    daemon_app._on_ai_choice_reply("req1", None, None, ["ctrl+n", "shift+ctrl+n"])
    assert daemon_app.backend.keys == ["ctrl+n", "shift+ctrl+n"]
    assert daemon_app._pending_choice is None

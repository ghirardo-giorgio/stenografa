"""Test dei backend LLM intercambiabili (llm_provider): selezione del
provider, lettura/scrittura della configurazione e comportamento dei singoli
adattatori. Nessuna chiamata di rete reale e nessuna dipendenza da quale
servizio LLM e' effettivamente installato sulla macchina di test."""
import json

import pytest

import llm_provider
from llm_provider import (
    AnthropicProvider,
    ClaudeCodeProvider,
    LMStudioProvider,
    OllamaProvider,
    OpenAIProvider,
    get_provider,
    load_config,
    save_config,
)


class FakeHTTPResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise llm_provider.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Isola la configurazione in una directory temporanea: i test non
    leggono ne' sovrascrivono ~/.config/stenografa/llm.json dell'utente."""
    path = tmp_path / "llm.json"
    monkeypatch.setattr(llm_provider, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(llm_provider, "LLM_CONFIG_PATH", path)
    return path


# --- configurazione ---


def test_load_config_returns_defaults_when_missing(config_path):
    cfg = load_config()
    assert cfg["provider"] == "lmstudio"
    assert set(cfg["providers"]) == set(llm_provider.DEFAULT_CONFIG["providers"])


def test_load_config_ignores_corrupt_file(config_path):
    """Un file rotto non deve impedire l'avvio del demone: si ricade sui
    default invece di sollevare."""
    config_path.write_text("{ non json", encoding="utf-8")
    assert load_config()["provider"] == "lmstudio"


def test_load_config_completes_partial_file_with_defaults(config_path):
    """Un file scritto a mano (o salvato da una versione precedente) non
    deve far mancare chiavi al provider."""
    config_path.write_text(
        json.dumps({"provider": "ollama", "providers": {"ollama": {"model": "llama3"}}}),
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg["provider"] == "ollama"
    assert cfg["providers"]["ollama"]["model"] == "llama3"
    # base_url non era nel file: arriva dai default
    assert cfg["providers"]["ollama"]["base_url"]
    assert "openai" in cfg["providers"]


def test_save_config_roundtrip_and_permissions(config_path):
    cfg = load_config()
    cfg["provider"] = "openai"
    cfg["providers"]["openai"]["model"] = "gpt-test"
    save_config(cfg)

    assert load_config()["providers"]["openai"]["model"] == "gpt-test"
    # il file puo' contenere chiavi API: solo l'utente deve poterlo leggere
    assert (config_path.stat().st_mode & 0o777) == 0o600


def test_get_provider_falls_back_on_unknown_name(config_path):
    config_path.write_text(json.dumps({"provider": "inesistente"}), encoding="utf-8")
    assert isinstance(get_provider(), LMStudioProvider)


def test_get_provider_selects_configured_backend(config_path):
    for name, cls in (
        ("ollama", OllamaProvider),
        ("openai", OpenAIProvider),
        ("anthropic", AnthropicProvider),
        ("claude_code", ClaudeCodeProvider),
    ):
        cfg = load_config()
        cfg["provider"] = name
        save_config(cfg)
        assert isinstance(get_provider(), cls)


# --- LM Studio: scoperta del modello caricato (era Stenografa._llm_loaded_model) ---


def test_lmstudio_auto_picks_loaded_llm(monkeypatch):
    def fake_get(url, timeout=None):
        return FakeHTTPResponse(
            {
                "data": [
                    {"id": "embed-model", "type": "embeddings", "state": "loaded"},
                    {"id": "chat-model", "type": "llm", "state": "loaded"},
                    {"id": "other-model", "type": "llm", "state": "not-loaded"},
                ]
            }
        )

    monkeypatch.setattr(llm_provider.requests, "get", fake_get)
    provider = LMStudioProvider({"base_url": "http://localhost:1234", "model": "auto"})
    model, error = provider.resolve_model()
    assert error is None
    assert model == "chat-model"


def test_lmstudio_auto_errors_when_unreachable(monkeypatch):
    def fake_get(url, timeout=None):
        raise llm_provider.requests.RequestException("connection refused")

    monkeypatch.setattr(llm_provider.requests, "get", fake_get)
    provider = LMStudioProvider({"base_url": "http://localhost:1234", "model": "auto"})
    model, error = provider.resolve_model()
    assert model is None
    assert "LM Studio non raggiungibile" in error


def test_lmstudio_explicit_model_skips_discovery(monkeypatch):
    """Con un modello indicato a mano non si interroga LM Studio: niente
    chiamata di rete inutile prima di ogni comando vocale."""
    def fail(*a, **kw):
        raise AssertionError("non doveva interrogare /api/v0/models")

    monkeypatch.setattr(llm_provider.requests, "get", fail)
    provider = LMStudioProvider({"base_url": "http://x", "model": "mio-modello"})
    assert provider.resolve_model() == ("mio-modello", None)


# --- Ollama ---


def test_ollama_uses_single_installed_model(monkeypatch):
    monkeypatch.setattr(
        OllamaProvider, "list_models", lambda self: ["llama3.2:latest"]
    )
    provider = OllamaProvider({"base_url": "http://localhost:11434", "model": ""})
    assert provider.resolve_model() == ("llama3.2:latest", None)


def test_ollama_requires_choice_when_several_models(monkeypatch):
    """Con piu' modelli sceglierne uno a caso darebbe risultati diversi da
    un avvio all'altro: meglio un errore esplicito."""
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: ["a", "b"])
    provider = OllamaProvider({"base_url": "http://localhost:11434", "model": ""})
    model, error = provider.resolve_model()
    assert model is None
    assert "piu' modelli" in error


def test_ollama_errors_when_no_model_installed(monkeypatch):
    monkeypatch.setattr(OllamaProvider, "list_models", lambda self: [])
    provider = OllamaProvider({"base_url": "http://localhost:11434", "model": ""})
    model, error = provider.resolve_model()
    assert model is None
    assert "ollama pull" in error


# --- endpoint OpenAI-compatible ---


def test_openai_compatible_posts_expected_payload(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeHTTPResponse({"choices": [{"message": {"content": "ciao"}}]})

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    provider = OpenAIProvider(
        {"base_url": "https://api.openai.com/v1", "model": "gpt-test", "api_key": "sk-x"}
    )
    text, error = provider.complete("sistema", "utente", max_tokens=42)

    assert error is None and text == "ciao"
    # il base_url finisce gia' per /v1: non deve essere duplicato
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"
    assert captured["json"]["max_tokens"] == 42
    assert captured["json"]["messages"][0]["content"] == "sistema"


def test_local_base_url_gets_v1_appended(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        return FakeHTTPResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    LMStudioProvider({"base_url": "http://localhost:1234", "model": "m"}).complete("s", "u")
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"


def test_openai_compatible_reports_network_error(monkeypatch):
    def fake_post(*a, **kw):
        raise llm_provider.requests.Timeout("timeout")

    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    text, error = OpenAIProvider({"base_url": "http://x/v1", "model": "m"}).complete("s", "u")
    assert text is None
    assert "errore di comunicazione con OpenAI" in error


def test_api_key_env_var_takes_precedence(monkeypatch):
    """Tenere il segreto nell'ambiente invece che su disco deve funzionare
    anche quando nel file c'e' un valore vecchio."""
    monkeypatch.setenv("MIA_CHIAVE", "sk-da-ambiente")
    provider = OpenAIProvider(
        {"api_key_env": "MIA_CHIAVE", "api_key": "sk-da-file", "base_url": "", "model": "m"}
    )
    assert provider._api_key() == "sk-da-ambiente"


def test_api_key_falls_back_to_file(monkeypatch):
    monkeypatch.delenv("MIA_CHIAVE", raising=False)
    provider = OpenAIProvider(
        {"api_key_env": "MIA_CHIAVE", "api_key": "sk-da-file", "base_url": "", "model": "m"}
    )
    assert provider._api_key() == "sk-da-file"


def test_openai_check_fails_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ok, message = OpenAIProvider(
        {"api_key_env": "OPENAI_API_KEY", "api_key": "", "base_url": "", "model": "m"}
    ).check()
    assert not ok
    assert "chiave API" in message


# --- Claude Code CLI ---


def test_claude_code_builds_headless_command(monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "ctrl+c"
        stderr = ""

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return FakeProc()

    monkeypatch.setattr(llm_provider.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(llm_provider.subprocess, "run", fake_run)

    text, error = ClaudeCodeProvider({"binary": "claude", "model": "opus"}).complete(
        "sistema", "utente"
    )
    assert error is None and text == "ctrl+c"
    assert "-p" in captured["cmd"]
    assert "--system-prompt" in captured["cmd"]
    assert "sistema" in captured["cmd"]
    assert "--model" in captured["cmd"]
    # il testo dell'utente arriva su stdin, non come argomento: evita limiti
    # di lunghezza della riga di comando e problemi di quoting
    assert captured["input"] == "utente"


def test_claude_code_missing_binary(monkeypatch):
    monkeypatch.setattr(llm_provider.shutil, "which", lambda b: None)
    monkeypatch.setattr(llm_provider.os.path, "exists", lambda p: False)
    text, error = ClaudeCodeProvider({"binary": "claude"}).complete("s", "u")
    assert text is None
    assert "non trovato" in error


def test_claude_code_reports_failure(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "not authenticated"

    monkeypatch.setattr(llm_provider.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        llm_provider.subprocess, "run", lambda *a, **kw: FakeProc()
    )
    text, error = ClaudeCodeProvider({"binary": "claude"}).complete("s", "u")
    assert text is None
    assert "not authenticated" in error


def test_claude_code_timeout(monkeypatch):
    def fake_run(*a, **kw):
        raise llm_provider.subprocess.TimeoutExpired("claude", 5)

    monkeypatch.setattr(llm_provider.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(llm_provider.subprocess, "run", fake_run)
    text, error = ClaudeCodeProvider({"binary": "claude"}).complete("s", "u", timeout=5)
    assert text is None
    assert "non ha risposto" in error


# --- Anthropic ---


def test_anthropic_extracts_text_and_uses_configured_model(monkeypatch):
    captured = {}

    class FakeBlock:
        type = "text"
        text = "ctrl+c"

    class FakeResp:
        stop_reason = "end_turn"
        content = [FakeBlock()]

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResp()

    class FakeClient:
        messages = FakeMessages()

        def with_options(self, **kw):
            return self

    provider = AnthropicProvider({"model": "claude-opus-5"})
    monkeypatch.setattr(provider, "_client", lambda: FakeClient())

    text, error = provider.complete("sistema", "utente", max_tokens=100)
    assert error is None and text == "ctrl+c"
    assert captured["model"] == "claude-opus-5"
    assert captured["system"] == "sistema"
    # max_tokens limita pensiero + risposta insieme: serve margine sopra
    # quanto chiesto dal chiamante, altrimenti il testo esce troncato
    assert captured["max_tokens"] > 100


def test_anthropic_reports_refusal(monkeypatch):
    class FakeResp:
        stop_reason = "refusal"
        content = []

    class FakeMessages:
        def create(self, **kwargs):
            return FakeResp()

    class FakeClient:
        messages = FakeMessages()

        def with_options(self, **kw):
            return self

    provider = AnthropicProvider({"model": "claude-opus-5"})
    monkeypatch.setattr(provider, "_client", lambda: FakeClient())
    text, error = provider.complete("s", "u")
    assert text is None
    assert "rifiutata" in error

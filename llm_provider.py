"""
Backend LLM intercambiabili per il comando vocale IA, la traduzione e i
tutorial.

Il demone non parla mai direttamente con un servizio LLM: chiede un provider a
`get_provider()` e chiama `complete(system, user, ...)`, che ritorna sempre
`(testo, errore)` — esattamente una delle due valorizzata. Aggiungere un
backend significa scrivere una sottoclasse di `BaseProvider` e registrarla in
`PROVIDERS`, senza toccare daemon.py.

La configurazione sta in ~/.config/stenografa/llm.json (vedi setup_llm.py per
comporlo interattivamente). Il file contiene le impostazioni di TUTTI i
provider piu' il campo "provider" che dice quale usare: cambiare backend e'
una parola sola, e le credenziali degli altri restano dove sono.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import requests

CONFIG_DIR = Path.home() / ".config" / "stenografa"
LLM_CONFIG_PATH = CONFIG_DIR / "llm.json"

# timeout di default delle chiamate, in secondi. I provider locali rispondono
# in genere in pochi secondi; quelli cloud pagano anche la latenza di rete.
DEFAULT_TIMEOUT = 30

DEFAULT_CONFIG = {
    "provider": "lmstudio",
    "providers": {
        "lmstudio": {
            "base_url": "http://localhost:1234",
            # "auto" = usa il modello attualmente caricato in LM Studio
            "model": "auto",
        },
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            # la chiave si legge da questa variabile d'ambiente; in
            # alternativa si puo' mettere "api_key" direttamente nel file
            "api_key_env": "OPENAI_API_KEY",
            "api_key": "",
        },
        "anthropic": {
            "model": "claude-opus-5",
            "api_key_env": "ANTHROPIC_API_KEY",
            "api_key": "",
        },
        "claude_code": {
            # usa l'autenticazione gia' presente del CLI Claude Code:
            # nessuna chiave API da configurare
            "binary": "claude",
            "model": "",
        },
    },
}


def _deep_merge_defaults(cfg):
    """Completa una config parziale coi default, cosi' un file scritto a mano
    (o salvato da una versione precedente) non fa mancare chiavi al provider."""
    merged = {
        "provider": cfg.get("provider") or DEFAULT_CONFIG["provider"],
        "providers": {},
    }
    for name, defaults in DEFAULT_CONFIG["providers"].items():
        saved = (cfg.get("providers") or {}).get(name) or {}
        merged["providers"][name] = {**defaults, **saved}
    return merged


def load_config():
    """Legge la configurazione dei backend LLM, completandola coi default.
    Un file assente o illeggibile non e' un errore: si usano i default (LM
    Studio in locale), che e' il comportamento storico del demone."""
    try:
        with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _deep_merge_defaults(data)


def save_config(cfg):
    """Salva la configurazione con permessi 0600: puo' contenere chiavi API."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    merged = _deep_merge_defaults(cfg)
    tmp = LLM_CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LLM_CONFIG_PATH)
    return merged


class BaseProvider:
    """Interfaccia comune a tutti i backend."""

    name = "base"
    label = "Base"

    def __init__(self, settings):
        self.settings = settings

    # --- da implementare nelle sottoclassi ---

    def complete(self, system, user, max_tokens=600, timeout=DEFAULT_TIMEOUT):
        """Ritorna (testo, errore). Esattamente uno dei due e' valorizzato."""
        raise NotImplementedError

    def check(self):
        """Diagnostica per setup_llm.py: ritorna (ok, messaggio)."""
        raise NotImplementedError

    # --- utilita' condivise ---

    def _api_key(self):
        """Chiave API: prima la variabile d'ambiente indicata, poi il valore
        salvato nel file. L'ambiente ha la precedenza cosi' si puo' tenere il
        segreto fuori dal disco."""
        env_name = self.settings.get("api_key_env")
        if env_name:
            value = os.environ.get(env_name)
            if value:
                return value.strip()
        return (self.settings.get("api_key") or "").strip()

    def describe(self):
        return self.label


class _OpenAICompatibleProvider(BaseProvider):
    """Base per i servizi che espongono /v1/chat/completions in stile OpenAI
    (LM Studio, Ollama e OpenAI stesso). Cambia solo come si scopre il
    modello e se serve una chiave."""

    def _base_url(self):
        return (self.settings.get("base_url") or "").rstrip("/")

    def _chat_url(self):
        base = self._base_url()
        # OpenAI vuole gia' /v1 nel base_url; i server locali no
        return base + ("/chat/completions" if base.endswith("/v1")
                       else "/v1/chat/completions")

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def resolve_model(self):
        """Ritorna (model_id, errore)."""
        model = (self.settings.get("model") or "").strip()
        if not model:
            return None, f"nessun modello configurato per {self.label}"
        return model, None

    def complete(self, system, user, max_tokens=600, timeout=DEFAULT_TIMEOUT):
        model, error = self.resolve_model()
        if error:
            return None, error
        try:
            resp = requests.post(
                self._chat_url(),
                headers=self._headers(),
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            return None, f"errore di comunicazione con {self.label}: {exc}"
        if content is None:
            return None, f"risposta vuota da {self.label}"
        return content, None


class LMStudioProvider(_OpenAICompatibleProvider):
    name = "lmstudio"
    label = "LM Studio"

    def resolve_model(self):
        """Con model="auto" usa il modello gia' caricato in LM Studio,
        interrogando /api/v0/models (endpoint proprio di LM Studio, non
        standard OpenAI, che riporta anche lo stato di caricamento): evita di
        scegliere un modello scarico e innescare un caricamento JIT lento al
        posto di una risposta immediata."""
        model = (self.settings.get("model") or "auto").strip()
        if model and model != "auto":
            return model, None
        try:
            resp = requests.get(f"{self._base_url()}/api/v0/models", timeout=3)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm"):
                    return m.get("id"), None
        except (requests.RequestException, ValueError):
            pass
        return None, (
            f"LM Studio non raggiungibile su {self._base_url()} "
            "(nessun modello caricato)"
        )

    def check(self):
        model, error = self.resolve_model()
        if error:
            return False, error
        return True, f"modello caricato: {model}"


class OllamaProvider(_OpenAICompatibleProvider):
    name = "ollama"
    label = "Ollama"

    def list_models(self):
        try:
            resp = requests.get(f"{self._base_url()}/api/tags", timeout=3)
            resp.raise_for_status()
            return [m.get("name") for m in resp.json().get("models", []) if m.get("name")]
        except (requests.RequestException, ValueError):
            return []

    def resolve_model(self):
        model = (self.settings.get("model") or "").strip()
        if model:
            return model, None
        # nessun modello scelto: se ne e' installato uno solo si usa quello,
        # altrimenti va indicato esplicitamente (scegliere a caso fra piu'
        # modelli darebbe risultati imprevedibili da un avvio all'altro)
        available = self.list_models()
        if len(available) == 1:
            return available[0], None
        if not available:
            return None, (
                f"Ollama non raggiungibile su {self._base_url()} "
                "o nessun modello scaricato (usa: ollama pull <modello>)"
            )
        return None, (
            "piu' modelli Ollama disponibili: indica quale usare "
            f"({', '.join(available[:5])})"
        )

    def check(self):
        model, error = self.resolve_model()
        if error:
            return False, error
        return True, f"modello: {model}"


class OpenAIProvider(_OpenAICompatibleProvider):
    name = "openai"
    label = "OpenAI"

    def check(self):
        if not self._api_key():
            env_name = self.settings.get("api_key_env") or "OPENAI_API_KEY"
            return False, (
                f"nessuna chiave API: esporta {env_name} oppure salvala nel "
                f"campo api_key di {LLM_CONFIG_PATH}"
            )
        model, error = self.resolve_model()
        if error:
            return False, error
        try:
            resp = requests.get(
                f"{self._base_url()}/models",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            return False, f"chiave o endpoint non validi: {exc}"
        return True, f"modello: {model}"


class AnthropicProvider(BaseProvider):
    """API Claude tramite l'SDK ufficiale `anthropic`."""

    name = "anthropic"
    label = "Anthropic (API Claude)"

    def _client(self):
        import anthropic

        key = self._api_key()
        # senza chiave esplicita l'SDK risolve da solo le credenziali
        # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN o un profilo `ant auth login`)
        return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def complete(self, system, user, max_tokens=600, timeout=DEFAULT_TIMEOUT):
        model = (self.settings.get("model") or "claude-opus-5").strip()
        try:
            import anthropic
        except ImportError:
            return None, "SDK anthropic non installato (pip install anthropic)"
        try:
            resp = self._client().with_options(timeout=timeout).messages.create(
                model=model,
                # max_tokens limita thinking + risposta insieme: senza
                # margine il pensiero consuma il budget e il testo esce
                # troncato. Il costo reale resta quello effettivamente
                # generato, quindi allargare qui non spreca nulla.
                max_tokens=max(2048, max_tokens * 4),
                # questi prompt sono compiti brevi e sensibili alla latenza:
                # effort basso tiene corto il ragionamento senza disattivarlo
                # (a thinking spento i tag <thinking> possono finire nel testo)
                output_config={"effort": "low"},
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIStatusError as exc:
            return None, f"errore API Claude ({exc.status_code}): {exc.message}"
        except anthropic.APIConnectionError as exc:
            return None, f"Claude non raggiungibile: {exc}"
        except Exception as exc:
            return None, f"errore Claude: {type(exc).__name__}: {exc}"

        if resp.stop_reason == "refusal":
            return None, "richiesta rifiutata dai filtri di sicurezza di Claude"
        text = "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
        if not text:
            return None, "risposta vuota da Claude"
        return text, None

    def check(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "SDK anthropic non installato (pip install anthropic)"
        model = (self.settings.get("model") or "claude-opus-5").strip()
        text, error = self.complete(
            "Rispondi con una sola parola.", "Di' OK.", max_tokens=16, timeout=30
        )
        if error:
            return False, error
        return True, f"modello {model} raggiungibile (risposta: {text[:40]})"


class ClaudeCodeProvider(BaseProvider):
    """CLI Claude Code in modalita' headless (`claude -p`).

    Usa l'autenticazione gia' configurata del CLI, quindi non serve nessuna
    chiave API. In cambio ogni chiamata avvia un processo e un intero agente,
    quindi e' piu' lenta dell'API diretta: adatta a tutorial e traduzioni,
    meno alla latenza stretta del comando vocale.
    """

    name = "claude_code"
    label = "Claude Code (CLI)"

    def _binary(self):
        binary = (self.settings.get("binary") or "claude").strip()
        return shutil.which(binary) or binary

    def complete(self, system, user, max_tokens=600, timeout=DEFAULT_TIMEOUT):
        binary = self._binary()
        if not shutil.which(binary) and not os.path.exists(binary):
            return None, f"CLI Claude Code non trovato ({binary})"
        cmd = [binary, "-p", "--output-format", "text", "--system-prompt", system]
        model = (self.settings.get("model") or "").strip()
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(
                cmd,
                input=user,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, f"Claude Code non ha risposto entro {timeout}s"
        except OSError as exc:
            return None, f"impossibile avviare Claude Code: {exc}"
        if proc.returncode != 0:
            return None, f"Claude Code ha fallito: {proc.stderr.strip()[-300:]}"
        text = (proc.stdout or "").strip()
        if not text:
            return None, "risposta vuota da Claude Code"
        return text, None

    def check(self):
        binary = self._binary()
        if not shutil.which(binary) and not os.path.exists(binary):
            return False, f"CLI non trovato ({binary})"
        # il CLI avvia un agente completo: la prima chiamata puo' essere lenta
        text, error = self.complete(
            "Rispondi con una sola parola.", "Di' OK.", timeout=120
        )
        if error:
            return False, error
        return True, f"CLI funzionante (risposta: {text[:40]})"


PROVIDERS = {
    p.name: p
    for p in (
        LMStudioProvider,
        OllamaProvider,
        OpenAIProvider,
        AnthropicProvider,
        ClaudeCodeProvider,
    )
}


def get_provider(cfg=None):
    """Istanzia il provider selezionato nella configurazione. Un nome
    sconosciuto (file scritto a mano male) ricade sul default invece di far
    esplodere il demone all'avvio."""
    cfg = cfg or load_config()
    name = cfg.get("provider") or DEFAULT_CONFIG["provider"]
    cls = PROVIDERS.get(name) or PROVIDERS[DEFAULT_CONFIG["provider"]]
    return cls(cfg["providers"].get(cls.name, {}))

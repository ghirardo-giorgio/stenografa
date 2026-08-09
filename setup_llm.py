#!/home/oberon/.pyenv/shims/python3
"""
Setup interattivo del backend LLM usato da Stenografa (comando vocale IA,
traduzione, tutorial).

    ./setup_llm.py            # menu interattivo
    ./setup_llm.py --show     # mostra la configurazione attuale
    ./setup_llm.py --check    # prova il backend attualmente selezionato

Scrive ~/.config/stenografa/llm.json con permessi 0600 (puo' contenere chiavi
API). Il demone rilegge il file ad ogni chiamata, quindi il cambio di backend
ha effetto subito, senza riavviarlo.
"""
import sys

import llm_provider
from llm_provider import LLM_CONFIG_PATH, PROVIDERS, load_config, save_config

# ordine di presentazione nel menu (dal piu' semplice da avviare in locale
# ai servizi cloud che richiedono credenziali)
MENU_ORDER = ["lmstudio", "ollama", "openai", "anthropic", "claude_code"]

DESCRIPTIONS = {
    "lmstudio": "locale, nessuna chiave API. Usa il modello caricato in LM Studio.",
    "ollama": "locale, nessuna chiave API. Richiede un modello scaricato (ollama pull).",
    "openai": "cloud, richiede una chiave API OpenAI.",
    "anthropic": "cloud, richiede una chiave API Anthropic (o `ant auth login`).",
    "claude_code": "usa il CLI Claude Code gia' autenticato: nessuna chiave API, ma piu' lento.",
}


def _ask(prompt, current=""):
    """Chiede un valore mostrando quello attuale; Invio lo conferma."""
    suffix = f" [{current}]" if current else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return answer or current


def show(cfg):
    print(f"File di configurazione: {LLM_CONFIG_PATH}")
    print(f"Backend attivo: {cfg['provider']}\n")
    for name in MENU_ORDER:
        settings = cfg["providers"][name]
        marker = "*" if name == cfg["provider"] else " "
        label = PROVIDERS[name].label
        print(f" {marker} {name} ({label})")
        for key, value in settings.items():
            if key == "api_key" and value:
                value = "***"  # non stampare mai il segreto in chiaro
            print(f"      {key}: {value}")
    print("\n(* = backend attivo)")


def check(cfg):
    provider = llm_provider.get_provider(cfg)
    print(f"Verifico {provider.label}...")
    ok, message = provider.check()
    print(("  OK — " if ok else "  ERRORE — ") + message)
    return 0 if ok else 1


def configure(cfg):
    print("Backend LLM disponibili:\n")
    for i, name in enumerate(MENU_ORDER, start=1):
        marker = " (attuale)" if name == cfg["provider"] else ""
        print(f"  {i}. {PROVIDERS[name].label}{marker}")
        print(f"     {DESCRIPTIONS[name]}")
    print()

    choice = _ask(f"Scegli 1-{len(MENU_ORDER)}", str(MENU_ORDER.index(cfg["provider"]) + 1))
    try:
        name = MENU_ORDER[int(choice) - 1]
    except (ValueError, IndexError):
        print("Scelta non valida.")
        return 1

    settings = cfg["providers"][name]
    print(f"\n--- {PROVIDERS[name].label} ---")

    if name == "lmstudio":
        settings["base_url"] = _ask("URL del server", settings["base_url"])
        settings["model"] = _ask(
            "Modello ('auto' = quello caricato)", settings["model"] or "auto"
        )
    elif name == "ollama":
        settings["base_url"] = _ask("URL del server", settings["base_url"])
        available = llm_provider.OllamaProvider(settings).list_models()
        if available:
            print("  Modelli installati: " + ", ".join(available))
        settings["model"] = _ask("Modello da usare", settings["model"])
    elif name == "openai":
        settings["base_url"] = _ask("URL API", settings["base_url"])
        settings["model"] = _ask("Modello", settings["model"])
        print(
            "  La chiave puo' stare in una variabile d'ambiente (piu' sicuro,\n"
            "  resta fuori dal disco) oppure nel file di configurazione."
        )
        settings["api_key_env"] = _ask(
            "Variabile d'ambiente con la chiave", settings["api_key_env"]
        )
        key = _ask("Chiave API da salvare nel file (Invio per non salvarla)", "")
        if key:
            settings["api_key"] = key
    elif name == "anthropic":
        settings["model"] = _ask("Modello", settings["model"] or "claude-opus-5")
        print(
            "  Senza chiave esplicita l'SDK usa ANTHROPIC_API_KEY oppure un\n"
            "  profilo creato con `ant auth login`."
        )
        settings["api_key_env"] = _ask(
            "Variabile d'ambiente con la chiave", settings["api_key_env"]
        )
        key = _ask("Chiave API da salvare nel file (Invio per non salvarla)", "")
        if key:
            settings["api_key"] = key
    elif name == "claude_code":
        settings["binary"] = _ask("Eseguibile del CLI", settings["binary"])
        settings["model"] = _ask(
            "Modello (Invio = default del CLI)", settings["model"]
        )

    cfg["provider"] = name
    cfg["providers"][name] = settings
    cfg = save_config(cfg)
    print(f"\nSalvato in {LLM_CONFIG_PATH} (permessi 0600).")

    if _ask("Provo subito il backend? [s/N]", "s").lower().startswith("s"):
        return check(cfg)
    return 0


def main():
    cfg = load_config()
    if "--show" in sys.argv:
        show(cfg)
        return 0
    if "--check" in sys.argv:
        return check(cfg)
    return configure(cfg)


if __name__ == "__main__":
    sys.exit(main())

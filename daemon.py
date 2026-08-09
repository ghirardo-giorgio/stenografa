#!/home/oberon/.pyenv/shims/python3
"""
Stenografa - dettatura vocale con Whisper su GPU.

Gira senza interfaccia grafica: il riscontro all'utente arriva dalle
notifiche desktop e dallo stato inviato all'app telefono. La registrazione
si avvia/ferma inviando "toggle" al socket unix ascoltato da questo demone
(vedi toggle.py, da collegare a una scorciatoia da tastiera GNOME).

Al termine della registrazione il testo trascritto viene copiato negli
appunti e incollato automaticamente nella finestra col focus, e mostrato in
una notifica desktop. Le funzioni specifiche del sistema operativo (audio,
appunti, simulazione tasti, notifiche, rilevamento finestra attiva) sono
isolate in platform_backend.py: vedi quel modulo per lo stato del supporto
Linux/Windows/macOS.

Il modello (faster-whisper, CUDA se disponibile) viene caricato in VRAM al
primo utilizzo e vi resta finche' serve, cosi' le dettature successive
partono senza attesa. Dopo MODEL_IDLE_TIMEOUT secondi di inattivita' viene
scaricato per liberare la VRAM. Il caricamento parte insieme alla
registrazione, quindi avviene mentre l'utente sta ancora parlando.
"""
import base64
import gc
import json
import os
import queue
import secrets
import socket
import sys
import tempfile
import threading
import time
import signal
from pathlib import Path

import requests

import llm_provider
import platform_backend
from key_combo import is_valid_combo

RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())
LOCK_PATH = os.path.join(RUNTIME_DIR, "stenografa.lock")

# IPC locale (socket toggle da tastiera + socket di controllo per l'MCP
# server): TCP su loopback invece di AF_UNIX perche' quest'ultimo non e'
# disponibile su tutte le build Python di Windows.
LOCAL_HOST = "127.0.0.1"
TOGGLE_SOCKET_PORT = 8766
CONTROL_SOCKET_PORT = 8767


def _acquire_single_instance_lock(lock_path):
    """Prova ad acquisire un lock esclusivo su `lock_path`. Ritorna il file
    handle (da tenere aperto per tutta la vita del processo: il lock si
    rilascia da solo, anche in caso di crash, quando l'handle si chiude) se
    l'acquisizione riesce, altrimenti None (un'altra istanza e' gia' in
    esecuzione). Implementazione stdlib per SO: fcntl su Linux/macOS,
    msvcrt su Windows."""
    lock_file = open(lock_path, "w")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file

# --- controllo remoto da rete (app Flutter sul telefono) ---
# Il demone resta raggiungibile anche da un client TCP sulla stessa LAN, che
# puo' inviare "toggle" esattamente come lo scorciatoia da tastiera locale.
# Un token condiviso (generato al primo avvio) evita che altri dispositivi
# sulla stessa rete WiFi possano comandare la registrazione.
NETWORK_HOST = "0.0.0.0"
NETWORK_PORT = 8765
CONFIG_DIR = Path.home() / ".config" / "stenografa"
CONFIG_PATH = CONFIG_DIR / "config.json"

# rate-limiting sui tentativi di autenticazione falliti, per indirizzo IP
AUTH_MAX_ATTEMPTS = 5
AUTH_WINDOW_SECONDS = 60

# --- aspetto dei pulsanti: colore di sfondo e icona, scelti in base al tipo
# di azione (es. rosso per un'azione distruttiva) dall'LLM via MCP oppure a
# mano dall'utente (long-press su un pulsante in modalita' modifica). Vanno
# tenuti sincronizzati con la mappa icona->IconData in
# lib/models/button_spec.dart sul lato Flutter: nomi diversi da questi non
# sono renderizzabili dall'app.
ICON_NAMES = frozenset(
    {
        "keyboard", "delete", "delete_sweep", "cancel", "close",
        "warning", "refresh", "autorenew", "stop", "play_arrow", "pause",
        "check", "check_circle", "content_copy", "content_paste",
        "content_cut", "save", "folder", "image", "photo", "brush",
        "palette", "undo", "redo", "send", "download", "upload",
        "settings", "search", "star", "favorite", "lock", "lock_open",
        "visibility", "edit", "add", "remove", "arrow_upward",
        "arrow_downward", "arrow_back", "arrow_forward", "mic",
        "volume_up", "volume_off", "power_settings_new", "sync", "cloud",
        "home", "menu", "more_horiz", "info", "help", "layers",
        "terminal", "code", "bolt", "flash_on", "clear_all", "restart_alt",
        # strumenti di disegno/fotoritocco (es. dashboard per Gimp/InvokeAI):
        "crop_free", "gesture", "colorize", "healing", "gradient",
        "zoom_in", "rotate_right", "rotate_left", "straighten",
        "format_paint", "highlight", "touch_app", "near_me", "pan_tool",
        "opacity", "tune", "filter_alt", "crop", "grain", "auto_fix_high",
        "line_weight", "flip", "exposure", "contrast",
        # avvio applicazioni (kind="launch"), snippet di testo (kind="text")
        # e sequenze di scorciatoie (kind="macro")
        "rocket_launch", "open_in_new", "apps", "desktop_windows",
        "text_snippet", "notes", "short_text", "playlist_play", "history",
    }
)

# tipi di pulsante ammessi nel layout. I "microfono" (MIC_KINDS) avviano/
# fermano la stessa registrazione globale e hanno un aspetto che segue lo
# stato del demone, quindi non accettano colore/icona personalizzati.
MIC_KINDS = ("record", "ai_command")
BUTTON_KINDS = MIC_KINDS + ("keys", "launch", "text", "macro", "paste_last")

# --- pulsante "macro" (kind="macro"): una sequenza di combinazioni di tasti
# eseguite in ordine con una breve pausa fra l'una e l'altra, per i flussi
# che altrimenti richiederebbero tre tocchi separati (es. salva, cambia
# finestra, incolla).
# Un pulsante puo' occupare piu' celle (piu' largo/alto degli altri, per
# dare rilievo a quelli che si premono spesso). Il limite serve solo a
# fermare valori assurdi: la validazione vera e' che stia dentro la griglia
# e non si sovrapponga a nessun altro.
BUTTON_MAX_SPAN = 8


def _button_cells(row, col, row_span=1, col_span=1):
    """Celle occupate da un pulsante, estensione compresa."""
    return {
        (r, c)
        for r in range(row, row + row_span)
        for c in range(col, col + col_span)
    }


def _cells_of(button):
    return _button_cells(
        button["row"],
        button["col"],
        button.get("row_span", 1),
        button.get("col_span", 1),
    )


def _validate_spans(spec, current=None):
    """Estensione di un pulsante in celle. `current` e' il pulsante da
    modificare, per lasciare invariato cio' che non e' stato indicato.
    Ritorna (row_span, col_span, errore)."""
    base = current or {}
    values = {}
    for key in ("row_span", "col_span"):
        value = spec.get(key, base.get(key, 1))
        if not isinstance(value, int) or isinstance(value, bool):
            return None, None, f"{key} deve essere un intero"
        if not (1 <= value <= BUTTON_MAX_SPAN):
            return None, None, (
                f"{key} deve essere fra 1 e {BUTTON_MAX_SPAN} "
                "(quante celle occupa il pulsante)"
            )
        values[key] = value
    return values["row_span"], values["col_span"], None


def _occupied_cells(buttons, exclude_id=None):
    cells = set()
    for button in buttons:
        if exclude_id is not None and button["id"] == exclude_id:
            continue
        cells |= _cells_of(button)
    return cells


MACRO_MAX_STEPS = 20
MACRO_DEFAULT_DELAY_MS = 120
MACRO_MAX_DELAY_MS = 5000

# --- pulsante "testo" (kind="text"): incolla uno snippet fisso riusando la
# stessa pipeline del testo dettato (appunti + incolla adattivo + eventuale
# ripristino degli appunti precedenti).
TEXT_BUTTON_MAX_LENGTH = 5000

# --- elenco delle applicazioni installate: serve sia al pulsante
# kind="launch" (validazione dell'app_id) sia al comando vocale IA
# ("apri gimp"). Enumerarle costa (parsing di centinaia di file .desktop /
# una chiamata PowerShell), quindi il risultato viene tenuto in cache per
# pochi secondi: abbastanza da non ripagare l'enumerazione ad ogni pressione,
# troppo poco perche' un'app appena installata resti invisibile a lungo.
APPS_CACHE_TTL = 15.0


def _valid_color(color):
    """Valida un colore in formato '#RRGGBB'."""
    if not isinstance(color, str) or len(color) != 7 or color[0] != "#":
        return False
    try:
        int(color[1:], 16)
        return True
    except ValueError:
        return False


# --- layout dei pulsanti dell'app telefono: una o piu' "dashboard", ognuna
# con nome proprio e una griglia rows x cols in cui ogni pulsante occupa una
# cella (row, col). L'app telefono passa da una dashboard all'altra con uno
# swipe orizzontale. Modificabile sia dall'app telefono (l'utente crea/
# posiziona scorciatoie e dashboard a mano) sia da un server MCP esterno
# tramite il socket di controllo locale. Il pulsante "record" e' protetto
# (non rimovibile se e' l'unico rimasto): controlla avvio/stop registrazione,
# e vive di norma nella prima dashboard. Un pulsante "ai_command" fa lo
# stesso ma, invece di incollare il testo dettato, lo invia a un LLM locale
# (LM Studio) che lo traduce in una combinazione di tasti da eseguire (vedi
# _interpret_as_shortcut); non e' protetto, se ne possono avere quanti se ne
# vuole o nessuno. Gli altri pulsanti ("keys") simulano una combinazione di
# tasti fissa (es. "copia" -> ctrl+c, "incolla" -> ctrl+v) e possono stare in
# qualunque dashboard (es. una dashboard dedicata solo a scorciatoie per
# un'altra applicazione).
LAYOUT_PATH = CONFIG_DIR / "layout.json"
DEFAULT_LAYOUT = {
    "dashboards": [
        {
            "id": "default",
            "name": "Stenografa",
            "rows": 1,
            "cols": 1,
            "buttons": [
                {
                    "id": "record",
                    "label": "Registra",
                    "kind": "record",
                    "row": 0,
                    "col": 0,
                }
            ],
        }
    ]
}
LAYOUT_MUTATION_CMDS = {
    "add_button",
    "add_buttons",
    "remove_button",
    "move_button",
    "edit_button",
    "set_button_style",
    "set_grid_size",
    "create_dashboard",
    "remove_dashboard",
    "rename_dashboard",
    "reorder_dashboard",
    "duplicate_dashboard",
    "set_dashboard_match",
    "set_dashboard_shortcuts",
    "set_dashboard_vocabulary",
    "reset_layout",
}

# comandi di modifica di una singola impostazione di configurazione: campo
# atteso nel messaggio -> metodo setter su Stenografa (vedi _handle_config_cmd).
CONFIG_CMDS = {
    "set_language": ("language", "_set_language"),
    "set_restore_clipboard": ("enabled", "_set_restore_clipboard"),
    "set_translate_enabled": ("enabled", "_set_translate_enabled"),
    "set_translate_target": ("target", "_set_translate_target"),
    "set_translate_engine": ("engine", "_set_translate_engine"),
    "set_vocabulary": ("vocabulary", "_set_vocabulary"),
    "set_confirm_before_paste": ("enabled", "_set_confirm_before_paste"),
    "set_require_tls": ("enabled", "_set_require_tls"),
    "set_pause_media_while_recording": (
        "enabled",
        "_set_pause_media_while_recording",
    ),
}


def _valid_dashboard_shape(d):
    if not isinstance(d, dict):
        return False
    if not isinstance(d.get("id"), str) or not isinstance(d.get("name"), str):
        return False
    if not isinstance(d.get("rows"), int) or not isinstance(d.get("cols"), int):
        return False
    buttons = d.get("buttons")
    if not isinstance(buttons, list):
        return False
    return all(
        isinstance(b, dict) and "id" in b and "row" in b and "col" in b
        for b in buttons
    )


def _valid_layout_shape(data):
    if not isinstance(data, dict):
        return False
    dashboards = data.get("dashboards")
    if not isinstance(dashboards, list) or not dashboards:
        return False
    return all(_valid_dashboard_shape(d) for d in dashboards)


def _migrate_old_layout(data):
    """Converte il vecchio formato (una sola griglia, senza dashboard) in
    un'unica dashboard di nome "Stenografa"."""
    return {
        "dashboards": [
            {
                "id": "default",
                "name": "Stenografa",
                "rows": data.get("rows", 1),
                "cols": data.get("cols", 1),
                "buttons": data.get("buttons", []),
            }
        ]
    }


def _match_app_by_name(apps, patterns):
    """Applicazione installata a cui si riferisce una dashboard, a partire
    dai pattern del suo "match". Il confronto e' su parola intera: cercare
    "code" come semplice sottostringa peschera' "Nobara Codec Wizard" invece
    di "Visual Studio Code". A parita' di corrispondenza vince il nome piu'
    corto, che e' quasi sempre l'applicazione vera invece di un suo
    accessorio. Nessuna corrispondenza convincente -> None: meglio nessuna
    icona che quella di un'altra applicazione."""
    import re

    import os

    best = None
    for app in apps:
        name = app["name"].lower()
        # l'identificativo tecnico ("gimp.desktop", "org.videolan.VLC") dice
        # spesso quello che il nome per esteso nasconde: GIMP si presenta
        # come "GNU Image Manipulation Program"
        slug = os.path.splitext(os.path.basename(app["id"]))[0].lower()
        for pattern in patterns:
            if name == pattern or slug == pattern:
                score = 0
            elif re.search(rf"\b{re.escape(pattern)}\b", name):
                score = 1
            elif re.search(rf"(^|[.\-_]){re.escape(pattern)}($|[.\-_])", slug):
                score = 2
            elif name.startswith(pattern):
                score = 3
            else:
                continue
            candidate = (score, len(name), app["id"])
            if best is None or candidate < best:
                best = candidate
    return best[2] if best else None


def _drop_removed_kinds(layout):
    """Toglie dal layout salvato quello che una versione precedente poteva
    averci messo e che oggi non esiste piu': i pulsanti "Crea Tutorial"
    (kind="tutorial") e la dashboard "Tutorial" che generavano. Senza questa
    pulizia resterebbero pulsanti che non fanno nulla e una dashboard che
    prima era nascosta dallo swipe e ora comparirebbe fra le altre.

    Ritorna (layout, modificato): chi carica il layout usa il secondo valore
    per riscrivere il file una volta sola, invece di rifare la stessa
    pulizia ad ogni avvio."""
    dashboards = [d for d in layout.get("dashboards", []) if d.get("id") != "tutorial"]
    changed = len(dashboards) != len(layout.get("dashboards", []))
    for dashboard in dashboards:
        buttons = [
            b for b in dashboard.get("buttons", []) if b.get("kind") != "tutorial"
        ]
        if len(buttons) != len(dashboard.get("buttons", [])):
            changed = True
        dashboard["buttons"] = buttons
    layout["dashboards"] = dashboards
    return layout, changed


def _load_layout():
    if LAYOUT_PATH.exists():
        try:
            data = json.loads(LAYOUT_PATH.read_text())
            layout = None
            if _valid_layout_shape(data):
                layout = data
            elif isinstance(data, dict) and "rows" in data and "buttons" in data:
                layout = _migrate_old_layout(data)
            if layout is not None:
                layout, changed = _drop_removed_kinds(layout)
                if changed:
                    _save_layout(layout)
                return layout
        except (json.JSONDecodeError, OSError):
            pass
    return json.loads(json.dumps(DEFAULT_LAYOUT))  # copia profonda


def _save_layout(layout):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAYOUT_PATH.write_text(json.dumps(layout))


def _load_or_create_token():
    """Legge il token di autenticazione remota, generandolo se assente.

    Ritorna (token, appena_creato)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            token = data.get("token")
            if token:
                return token, False
        except (json.JSONDecodeError, OSError):
            pass
    # token numerico a 5 cifre: piu' comodo da digitare a mano sul telefono
    # rispetto a un token esadecimale lungo (accettabile per un demone
    # raggiungibile solo sulla LAN di casa)
    token = f"{secrets.randbelow(100000):05d}"
    CONFIG_PATH.write_text(json.dumps({"token": token}))
    os.chmod(CONFIG_PATH, 0o600)
    return token, True


def _read_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_config_key(key, value):
    """Aggiorna una singola chiave in config.json preservando le altre
    (es. non sovrascrive il token quando si cambia solo la lingua)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_config()
    data[key] = value
    CONFIG_PATH.write_text(json.dumps(data))
    os.chmod(CONFIG_PATH, 0o600)


def _load_language():
    lang = _read_config().get("language")
    return lang if isinstance(lang, str) and lang else MODEL_LANGUAGE


def _save_language(language):
    _write_config_key("language", language)


def _valid_language(language):
    if not isinstance(language, str):
        return False
    language = language.strip().lower()
    if language == "auto":
        return True
    return len(language) == 2 and language.isalpha()


def _load_restore_clipboard():
    value = _read_config().get("restore_clipboard")
    return bool(value) if isinstance(value, bool) else False


def _save_restore_clipboard(enabled):
    _write_config_key("restore_clipboard", enabled)


def _load_pause_media_while_recording():
    value = _read_config().get("pause_media_while_recording")
    # spento di default: mettere in pausa la riproduzione e' un effetto
    # collaterale visibile, meglio che sia l'utente a chiederlo
    return bool(value) if isinstance(value, bool) else False


def _save_pause_media_while_recording(enabled):
    _write_config_key("pause_media_while_recording", enabled)


# --- vocabolario di dettatura: elenco di termini (nomi propri, gergo
# tecnico, nomi di prodotto) passato a Whisper come `initial_prompt`, cioe'
# come se fosse il testo immediatamente precedente a quello da trascrivere.
# Il modello lo usa come contesto e tende a preferire quelle grafie: e' il
# modo supportato da faster-whisper per far riconoscere termini che
# altrimenti sbaglia sistematicamente. Non e' un vincolo rigido (il modello
# resta libero di trascrivere altro) e liste troppo lunghe peggiorano la
# trascrizione invece di migliorarla, da qui il limite di lunghezza.
VOCABULARY_MAX_LENGTH = 800


def _load_vocabulary():
    value = _read_config().get("vocabulary")
    return value if isinstance(value, str) else ""


def _save_vocabulary(vocabulary):
    _write_config_key("vocabulary", vocabulary)


def _valid_vocabulary(vocabulary):
    return (
        isinstance(vocabulary, str)
        and len(vocabulary) <= VOCABULARY_MAX_LENGTH
    )


def _load_confirm_before_paste():
    value = _read_config().get("confirm_before_paste")
    return bool(value) if isinstance(value, bool) else False


def _save_confirm_before_paste(enabled):
    _write_config_key("confirm_before_paste", enabled)


def _load_require_tls():
    value = _read_config().get("require_tls")
    return bool(value) if isinstance(value, bool) else False


def _save_require_tls(enabled):
    _write_config_key("require_tls", enabled)


# --- TLS sul canale telefono <-> demone ---
# Il protocollo di rete trasporta tutto il testo dettato e permette di
# simulare tasti sul PC: in chiaro, su una WiFi condivisa, sarebbe leggibile
# (e il token numerico a 5 cifre, comodo ma corto, catturabile) da chiunque
# sia sulla stessa rete. Il demone genera quindi un certificato self-signed
# al primo avvio e accetta connessioni TLS; l'app telefono lo fissa
# ("pinning") al primo collegamento e rifiuta un certificato diverso in
# seguito, cosi' un man-in-the-middle non puo' sostituirsi al PC.
#
# La stessa porta accetta ancora connessioni in chiaro (rilevate dal primo
# byte, vedi _wrap_if_tls): serve a non tagliare fuori una versione
# precedente dell'app durante l'aggiornamento. Con `require_tls` attivo le
# connessioni in chiaro vengono invece rifiutate.
CERT_PATH = CONFIG_DIR / "cert.pem"
KEY_PATH = CONFIG_DIR / "key.pem"
CERT_DAYS = 3650


def _generate_tls_cert():
    """Genera cert.pem/key.pem self-signed con openssl. Ritorna True se al
    termine i due file esistono. openssl e' presente di default su Linux e
    macOS; su Windows puo' mancare, e in quel caso il demone continua a
    funzionare in chiaro come prima (vedi _build_tls_context)."""
    import subprocess

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(KEY_PATH), "-out", str(CERT_PATH),
                "-days", str(CERT_DAYS), "-subj", "/CN=stenografa",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if not (CERT_PATH.exists() and KEY_PATH.exists()):
        return False
    os.chmod(KEY_PATH, 0o600)
    return True


def _build_tls_context():
    """Contesto TLS lato server, generando il certificato se assente.
    Ritorna (context, fingerprint) oppure (None, None) se il certificato non
    e' disponibile (openssl mancante): in tal caso il demone resta in chiaro
    come nelle versioni precedenti."""
    if not (CERT_PATH.exists() and KEY_PATH.exists()):
        if not _generate_tls_cert():
            return None, None
    try:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(CERT_PATH), str(KEY_PATH))
    except (OSError, ImportError, ValueError):
        return None, None
    return context, _cert_fingerprint()


def _cert_fingerprint():
    """Impronta SHA-1 del certificato, nel formato "AA:BB:..." usato dall'app
    telefono per il pinning. SHA-1 e non SHA-256 perche' e' l'unica che
    `X509Certificate` di Dart espone senza dipendenze aggiuntive; per fissare
    un certificato gia' noto serve una seconda preimmagine, non una
    collisione, quindi resta adeguata allo scopo."""
    import hashlib
    import ssl

    try:
        der = ssl.PEM_cert_to_DER_cert(CERT_PATH.read_text())
    except (OSError, ValueError):
        return None
    digest = hashlib.sha1(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# --- traduzione automatica del testo dettato (indipendente dal comando
# vocale IA: si applica solo ai pulsanti "record", non "ai_command") ---
# "whisper" e' il task nativo "translate" del modello: veloce, non richiede
# LM Studio, ma puo' tradurre SOLO verso l'inglese (limite del modello, non
# di questo demone). "llm" passa il testo gia' trascritto fedelmente a LM
# Studio (stesso motore usato per il comando vocale IA) perche' lo traduca
# in una lingua qualsiasi, non solo inglese, a costo di qualche secondo in
# piu' e di richiedere LM Studio avviato. Se target != "en" l'engine "llm"
# e' obbligatorio: un target "whisper" salvato in precedenza viene ignorato
# a runtime invece di essere rifiutato, cosi' cambiare target non richiede
# di ricordarsi di aggiornare anche l'engine.
TRANSLATE_ENGINES = ("whisper", "llm")


def _load_translate_enabled():
    value = _read_config().get("translate_enabled")
    return bool(value) if isinstance(value, bool) else False


def _save_translate_enabled(enabled):
    _write_config_key("translate_enabled", enabled)


def _load_translate_target():
    target = _read_config().get("translate_target")
    return target if isinstance(target, str) and target else "en"


def _save_translate_target(target):
    _write_config_key("translate_target", target)


def _valid_translate_target(target):
    # a differenza della lingua di dettatura, qui "auto" non ha senso:
    # bisogna sapere verso quale lingua tradurre
    return (
        isinstance(target, str)
        and _valid_language(target)
        and target.strip().lower() != "auto"
    )


def _load_translate_engine():
    engine = _read_config().get("translate_engine")
    return engine if engine in TRANSLATE_ENGINES else "whisper"


def _save_translate_engine(engine):
    _write_config_key("translate_engine", engine)


# lingue comuni per faster-whisper/Whisper (codici ISO 639-1). Whisper ne
# supporta molte di piu' (quasi 100): questo e' solo un elenco curato per
# l'interfaccia (menu a tendina sul telefono, elenco nel tool MCP), non una
# lista esaustiva ne' una validazione stretta (vedi _valid_language).
SUPPORTED_LANGUAGES = {
    "auto": "Rilevamento automatico",
    "it": "Italiano",
    "en": "Inglese",
    "es": "Spagnolo",
    "fr": "Francese",
    "de": "Tedesco",
    "pt": "Portoghese",
    "nl": "Olandese",
    "ru": "Russo",
    "zh": "Cinese",
    "ja": "Giapponese",
    "ko": "Coreano",
    "ar": "Arabo",
    "hi": "Hindi",
    "pl": "Polacco",
    "tr": "Turco",
    "sv": "Svedese",
    "el": "Greco",
    "cs": "Ceco",
    "ro": "Rumeno",
    "uk": "Ucraino",
}

# --- configurazione modello ---
# compute_type int8_float16 dimezza la VRAM rispetto a float16 con perdita
# di qualita' trascurabile; "medium" occupa cosi' circa 2 GB.
MODEL_NAME = "medium"
MODEL_DEVICE = "cuda"
MODEL_COMPUTE_TYPE = "int8_float16"
MODEL_LANGUAGE = "it"  # default se non presente in config.json
# secondi di inattivita' dopo i quali il modello viene tolto dalla VRAM
MODEL_IDLE_TIMEOUT = 300
# ogni quanto il ciclo principale verifica se scaricare il modello
IDLE_CHECK_INTERVAL = 30

# stati inviati all'app telefono: i nomi fanno parte del protocollo, non
# vanno cambiati senza aggiornare anche il lato Flutter
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_LOADING = "loading"
STATE_THINKING = "thinking"  # in attesa di LM Studio: ai_command o traduzione

# rete di sicurezza SOLO per la registrazione "a interruttore" (un tocco
# avvia, un altro ferma): un tocco accidentale puo' lasciarla avviata senza
# che l'utente se ne accorga. Non si applica a "tieni premuto per parlare"
# (fase "down"/"up"), gia' limitata dal tempo per cui il dito resta sul
# pulsante — vedi _start_recording/_handle_button_press.
RECORDING_MAX_DURATION_SECONDS = 180

# --- pulsante "comando vocale IA" (kind="ai_command"): in alternativa a
# dettare e incollare testo, invia la frase trascritta a un modello
# linguistico che la traduce in una combinazione di tasti da eseguire (es.
# "copia" -> "ctrl+c"). Quale backend LLM usare (LM Studio o Ollama in
# locale, OpenAI o l'API Claude nel cloud, oppure il CLI Claude Code gia'
# autenticato) e' configurabile: vedi llm_provider.py e setup_llm.py. Se il
# backend non e' raggiungibile il pulsante notifica l'errore e non fa nulla
# (nessun fallback su un incolla di testo grezzo, per non eseguire
# scorciatoie a caso).
# quanti candidati al massimo proporre quando il comando e' ambiguo: oltre
# una manciata il pannello di scelta sul telefono smette di essere piu'
# veloce che premere il tasto a mano
AI_COMMAND_MAX_OPTIONS = 6
# per quanti secondi resta valida una scelta in sospeso. L'utente puo'
# benissimo non rispondere mai (posa il telefono, cambia idea): scaduta,
# la richiesta si chiude da sola e non esegue nulla.
AI_COMMAND_CHOICE_TIMEOUT = 90
AI_COMMAND_SYSTEM_PROMPT = (
    "Interpreti un comando vocale dettato da un utente e lo traduci nelle "
    "combinazioni di tasti da premere su un PC per eseguirlo. Rispondi SOLO "
    "con un array JSON (nessun testo, spiegazione o markdown attorno), dove "
    "ogni elemento e' un oggetto con esattamente due campi: \"label\" (nome "
    "breve dell'azione, massimo 4 parole, es. \"Copia\") e \"combo\" (la "
    "combinazione di tasti, in minuscolo, con '+' come separatore fra i "
    "tasti, es. \"ctrl+c\"). Se il comando corrisponde chiaramente a UNA "
    "sola azione, restituisci un array con UN SOLO elemento: verra' "
    "eseguito immediatamente. Restituisci piu' elementi (al massimo "
    f"{AI_COMMAND_MAX_OPTIONS}, ordinati dal piu' probabile al meno "
    "probabile) SOLO se il comando e' realmente ambiguo, cioe' se piu' "
    "azioni diverse lo descrivono altrettanto bene: in quel caso non viene "
    "eseguito nulla e l'utente scegliera' a mano quale azione lanciare. "
    "Tasti validi: ctrl, shift, alt, super, enter, esc, tab, "
    "space, backspace, delete, up, down, left, right, home, end, pageup, "
    "pagedown, f1-f12, le lettere a-z, le cifre 0-9. Se il comando chiede "
    "invece di APRIRE o AVVIARE un'applicazione (es. \"apri gimp\", "
    "\"lancia il browser\"), l'elemento deve avere i campi \"label\" e "
    "\"app\" (nome dell'applicazione da avviare, es. \"GIMP\") al posto di "
    "\"combo\". Se il comando non corrisponde ne' a una combinazione di "
    "tasti ne' all'avvio di un'applicazione, rispondi con un array vuoto []."
)
# aggiunto al prompt SOLO quando il comando parte da una dashboard nota (vedi
# _interpret_as_shortcuts/_dashboard_name): la generazione di macro e'
# volutamente vincolata all'applicazione di quella dashboard, per non
# rischiare di applicare scorciatoie sbagliate a un'app diversa da quella che
# l'utente ha davanti in quel momento.
AI_COMMAND_MACRO_PROMPT_TEMPLATE = (
    "\n\nPuoi anche generare una MACRO multi-passo: se il comando descrive un "
    "task composto da piu' azioni in sequenza (es. \"crea un nuovo file, "
    "aggiungi un livello e impostalo di rosso\"), l'elemento deve avere i "
    "campi \"label\" e \"combos\" (array di combinazioni di tasti, "
    "nell'ordine in cui vanno eseguite, es. [\"ctrl+n\", \"shift+ctrl+n\"]) "
    "invece di \"combo\". Usa \"combos\" SOLO per task che richiedono "
    "davvero piu' passi in sequenza: per una singola azione usa sempre "
    "\"combo\". Le combinazioni — sia in \"combo\" che in \"combos\" — "
    "devono valere per l'applicazione dell'attuale dashboard, \"{name}\", e "
    "per nessun'altra: se il task descritto non ha senso per questa "
    "applicazione, rispondi con un array vuoto []."
)

# --- storico delle ultime dettature: tenuto solo in memoria (non finisce
# mai su disco: e' testo dettato dall'utente, spesso privato) e visibile
# dall'app telefono, che puo' chiederne il re-incolla per indice. Serve
# soprattutto quando l'incolla automatico e' finito nella finestra
# sbagliata: senza storico quel testo era perso.
HISTORY_MAX_ENTRIES = 20
# testo mostrato nell'elenco sul telefono: le dettature lunghe vengono
# troncate nell'anteprima ma re-incollate per intero
HISTORY_PREVIEW_LENGTH = 300

# --- conferma prima dell'incolla (config "confirm_before_paste"): invece di
# incollare subito, il demone propone il testo trascritto al telefono e
# aspetta un'approvazione (eventualmente con modifiche). Stessa struttura
# effimera della disambiguazione dei comandi vocali: vive in memoria, non
# tocca il layout, e scade da sola se l'utente non risponde.
PASTE_CONFIRM_TIMEOUT = 180

# --- rilevamento app attiva sul PC, per far seguire automaticamente al
# telefono la dashboard associata (campo "match" della dashboard). Il
# rilevamento vero e proprio e' delegato al backend per SO (vedi
# platform_backend.py): su Linux richiede l'estensione GNOME Shell "Window
# Calls" attiva, se assente la funzione fallisce silenziosamente e la
# funzionalita' resta inerte (nessun impatto sul resto del demone).
ACTIVE_WINDOW_POLL_INTERVAL = 1.5  # secondi

# --- controlli play/pausa dei video in riproduzione sul PC: il telefono ne
# mostra un'icona per flusso, cosi' si puo' fermare un video prima di dettare
# senza tornare alla tastiera (il microfono capterebbe l'audio del video).
# Il rilevamento e' delegato al backend (MPRIS su Linux, non implementato
# altrove): se la lista e' vuota il telefono non mostra nulla.
MEDIA_POLL_INTERVAL = 2.0  # secondi

# Il server audio ricorda volume e mute PER APPLICAZIONE: un flusso
# silenziato che finisce prima di essere riattivato (video terminato, scheda
# chiusa) lascia l'applicazione marcata "muta", e ogni suo flusso successivo
# nasce muto — l'utente si ritrova senza audio senza sapere perche'. Per
# questi secondi dopo una dettatura si tiene quindi d'occhio chi era stato
# silenziato, riattivandolo se ricompare muto.
MUTED_APPS_GUARD_SECONDS = 20.0

# --- incolla adattivo: nella maggior parte degli emulatori di terminale
# Linux (e in Terminal.app/iTerm su macOS, Windows Terminal/cmd/PowerShell)
# Ctrl+V non incolla — e' spesso un carattere di controllo o e' semplicemente
# non mappato — l'incolla vero e' su Ctrl+Shift+V. Se la finestra col focus
# sembra un terminale, _paste_combo usa quella combinazione al posto di
# Ctrl+V. Solo parole chiave specifiche (niente sigle corte tipo "st" per
# non scambiare per errore un'app come "Steam" per un terminale).
TERMINAL_WM_CLASS_KEYWORDS = (
    "terminal",  # gnome-terminal, xfce4-terminal, mate-terminal,
                 # Terminal.app, Windows Terminal...
    "konsole",
    "xterm",
    "rxvt",
    "kitty",
    "alacritty",
    "terminator",
    "tilix",
    "lxterminal",
    "terminology",
    "iterm",
    "warp",
    "wezterm",
    "foot",
    "cmd.exe",
    "powershell",
    "conhost",
)


class ModelManager:
    """Tiene il modello faster-whisper in VRAM, con scarico automatico.

    Tutti i metodi sono chiamati da thread secondari. Il lock e' rientrante
    perche' transcribe() richiama il caricamento tenendo gia' il lock.
    """

    def __init__(self, language=MODEL_LANGUAGE):
        self._lock = threading.RLock()
        self._model = None
        self._last_used = 0.0
        # lingua di dettatura, modificabile a caldo (set_language): non
        # richiede di ricaricare il modello, e' solo un parametro passato a
        # transcribe(). "auto" = rilevamento automatico della lingua.
        self.language = language

    def _load_locked(self):
        if self._model is None:
            # import ritardato: importare faster_whisper costa ~1s e non
            # serve finche' non si detta davvero
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                MODEL_NAME,
                device=MODEL_DEVICE,
                compute_type=MODEL_COMPUTE_TYPE,
            )
        self._last_used = time.monotonic()
        return self._model

    def preload(self):
        """Carica il modello in background, se non gia' presente."""
        threading.Thread(target=self._preload_worker, daemon=True).start()

    def _preload_worker(self):
        try:
            with self._lock:
                self._load_locked()
        except Exception:
            # l'errore verra' riportato all'utente al momento della
            # trascrizione vera e propria
            pass

    @property
    def is_loaded(self):
        return self._model is not None

    def transcribe(self, wav_path, task="transcribe", initial_prompt=None):
        """`task="translate"` usa la funzione nativa di Whisper che traduce
        DIRETTAMENTE in inglese (unica lingua di destinazione che supporta:
        vedi TRANSLATE_ENGINES in daemon.py per la traduzione verso altre
        lingue tramite LLM).

        `initial_prompt` e' il vocabolario di dettatura (vedi
        _vocabulary_for_dashboard in daemon.py): Whisper lo tratta come il
        testo che precede l'audio, quindi tende a riusarne le grafie per i
        termini che altrimenti sbaglierebbe."""
        with self._lock:
            model = self._load_locked()
            segments, _info = model.transcribe(
                wav_path,
                language=None if self.language == "auto" else self.language,
                task=task,
                initial_prompt=initial_prompt or None,
                beam_size=5,
                # filtra il silenzio: evita le allucinazioni tipiche di
                # whisper sulle pause e accorcia l'audio da elaborare
                vad_filter=True,
                # per dettature brevi il contesto precedente favorisce
                # ripetizioni a loop, meglio disattivarlo
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            self._last_used = time.monotonic()
            return text

    def unload_if_idle(self):
        """Libera la VRAM se il modello non viene usato da un po'."""
        # se il lock e' occupato c'e' una trascrizione in corso: si riprova
        # al giro successivo invece di bloccare il controllo
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if (
                self._model is not None
                and time.monotonic() - self._last_used > MODEL_IDLE_TIMEOUT
            ):
                self._model = None
                gc.collect()
                return True
            return False
        finally:
            self._lock.release()


class Stenografa:
    def __init__(self):
        self.state = STATE_IDLE
        self.record_file = None
        # timer di sicurezza per la registrazione a interruttore (vedi
        # RECORDING_MAX_DURATION_SECONDS): None quando non c'e' nessuna
        # registrazione in corso o quando e' partita in modalita' "tieni
        # premuto per parlare"
        self._recording_watchdog_timer = None
        self.command_queue = queue.Queue()
        self.model = ModelManager(language=_load_language())
        self.backend = platform_backend.get_backend(RUNTIME_DIR)
        self.restore_clipboard = _load_restore_clipboard()
        # contenuto degli appunti catturato all'avvio della registrazione
        # (solo se restore_clipboard e' attivo), da ripristinare dopo
        # l'incolla automatico del testo dettato
        self._clipboard_before = None
        # --- controlli play/pausa dei video (vedi MEDIA_POLL_INTERVAL) ---
        self.pause_media_while_recording = _load_pause_media_while_recording()
        # ultimo elenco mandato al telefono, per non ritrasmetterlo identico
        self._media_players = []
        # player messi in pausa dall'automatismo di inizio dettatura, da far
        # ripartire quando la dettatura finisce
        self._paused_for_recording = []
        # flussi audio silenziati dallo stesso automatismo: servono per i
        # video che i player non espongono (un browser ne pubblica uno solo
        # anche con piu' schede che riproducono)
        self._muted_for_recording = []
        # applicazioni silenziate di recente da tenere d'occhio (vedi
        # MUTED_APPS_GUARD_SECONDS): {"names": set, "until": monotonic}
        self._muted_apps_guard = None
        self._media_lock = threading.Lock()
        # traduzione automatica del testo dettato (solo pulsanti "record",
        # vedi TRANSLATE_ENGINES sopra)
        self.translate_enabled = _load_translate_enabled()
        self.translate_target = _load_translate_target()
        self.translate_engine = _load_translate_engine()
        # vocabolario di dettatura globale (vedi _vocabulary_for_dashboard):
        # si somma a quello della dashboard da cui parte la registrazione
        self.vocabulary = _load_vocabulary()
        # se il testo dettato va confermato sul telefono prima di essere
        # incollato (vedi _on_transcription_done/_pending_paste)
        self.confirm_before_paste = _load_confirm_before_paste()
        self.require_tls = _load_require_tls()
        # ultime dettature, dalla piu' recente: solo in memoria (vedi
        # HISTORY_MAX_ENTRIES)
        self._history = []
        self._history_lock = threading.Lock()
        # richiesta di conferma dell'incolla in sospeso: {"id", "text",
        # "clipboard_before", "expires_at"}
        self._pending_paste = None
        # elenco applicazioni installate, con scadenza (vedi APPS_CACHE_TTL)
        self._apps_cache = None
        self._apps_cache_at = 0.0
        self._apps_cache_lock = threading.Lock()
        # icone delle applicazioni gia' cercate (app_id -> PNG in base64, o
        # None se non ne ha una): vedi _app_icon_base64
        self._app_icon_cache = {}
        self._app_icon_lock = threading.Lock()
        # modalita' della registrazione in corso: "paste" (default, incolla
        # il testo dettato) oppure "ai_command" (interpreta il testo con un
        # LLM locale ed esegue lo shortcut risultante). Impostata da
        # _handle_button_press in base al kind del pulsante premuto.
        self._recording_mode = "paste"
        # dashboard da cui e' stato premuto il pulsante ai_command che ha
        # avviato la registrazione corrente: da' priorita' alle scorciatoie
        # gia' configurate in quella dashboard quando si interpreta il
        # comando (vedi _dashboard_shortcuts_context)
        self._recording_dashboard_id = None
        # richiesta di disambiguazione in sospeso quando un comando vocale IA
        # ha piu' interpretazioni plausibili: {"id", "text", "options",
        # "expires_at"}. Finche' e' valorizzata il telefono sta mostrando il
        # pannello di scelta e nessuna combinazione e' stata eseguita.
        self._pending_choice = None
        # file handle del lock di istanza singola, assegnato dall'esterno
        # (vedi if __name__ == "__main__"): serve a _request_restart per
        # rilasciarlo prima di rieseguire il processo con execv
        self.lock_handle = None
        self._restart_requested = False

        # vocabolario in vigore per la registrazione in corso, calcolato
        # all'avvio (la dashboard puo' cambiare mentre l'utente parla)
        self._recording_vocabulary = ""

        self.auth_token, token_created = _load_or_create_token()
        self.tls_context, self.tls_fingerprint = _build_tls_context()
        self._net_clients = set()
        self._net_lock = threading.Lock()
        # rate-limiting sui tentativi di autenticazione falliti: protegge il
        # token numerico a 5 cifre (comodo da digitare ma debole) da un
        # tentativo di forza bruta sulla LAN
        self._auth_failures = {}
        self._auth_failures_lock = threading.Lock()
        self.layout = _load_layout()
        # RLock: _mutate_layout tiene il lock mentre chiama _broadcast_layout,
        # che a sua volta richiama _layout_snapshot (stesso lock) per
        # ottenere una copia coerente da inviare ai client
        self._layout_lock = threading.RLock()
        self._last_focused_dashboard_id = None

        self._running = True

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        self._start_socket_server()
        self._start_network_server()
        self._start_control_server()
        self._start_active_window_watcher()
        self._start_media_players_watcher()
        self._warm_apps_cache()
        if token_created:
            self._notify(
                "Stenografa - token app telefono",
                f"Token: {self.auth_token}\n"
                f"(salvato anche in {CONFIG_PATH})",
            )
        if self.tls_context is None and self.require_tls:
            # senza certificato non c'e' modo di soddisfare require_tls:
            # meglio dirlo subito che lasciare il telefono a sbattere contro
            # un rifiuto senza spiegazione
            self._notify(
                "Stenografa - TLS non disponibile",
                "'Richiedi TLS' e' attivo ma il certificato non e' stato "
                "generato (openssl mancante?): nessun telefono potra' "
                "collegarsi.",
                urgency="critical",
            )

    # --- ciclo principale ---
    # I server (socket locale, TCP, controllo MCP) girano in thread separati e
    # accodano qui i comandi, che vengono eseguiti tutti dal thread principale:
    # cosi' le transizioni di stato restano serializzate senza bisogno di lock.

    def _main_loop(self):
        next_idle_check = time.monotonic() + IDLE_CHECK_INTERVAL
        while self._running:
            # ci si sveglia anche alla scadenza di un'eventuale scelta in
            # sospeso, altrimenti il pannello sul telefono resterebbe aperto
            # fino al successivo controllo periodico (fino a 30s di ritardo)
            deadline = next_idle_check
            if self._pending_choice is not None:
                deadline = min(deadline, self._pending_choice["expires_at"])
            if self._pending_paste is not None:
                deadline = min(deadline, self._pending_paste["expires_at"])
            timeout = max(0.1, deadline - time.monotonic())
            try:
                item = self.command_queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is not None:
                kind = item[0]
                if kind == "toggle":
                    self.toggle_recording()
                elif kind == "button":
                    self._handle_button_press(item[1])
                elif kind == "button_down":
                    self._handle_button_press(item[1], phase="down")
                elif kind == "button_up":
                    self._handle_button_press(item[1], phase="up")
                elif kind == "recording_timeout":
                    self._on_recording_timeout_reached()
                elif kind == "paste_history":
                    self._paste_history_entry(item[1])
                elif kind == "player_action":
                    self._handle_player_action(item[1], item[2])
                elif kind == "paste_reply":
                    self._on_paste_confirmed(item[1], item[2])
                elif kind == "paste_cancel":
                    self._cancel_pending_paste(item[1], reason="cancelled")
                elif kind == "done":
                    self._on_transcription_done(item[1], item[2])
                elif kind == "ai_done":
                    self._on_ai_command_done(item[1], item[2], item[3])
                elif kind == "ai_choice":
                    self._on_ai_choice_needed(item[1], item[2])
                elif kind == "ai_choice_reply":
                    self._on_ai_choice_reply(item[1], item[2], item[3], item[4])
                elif kind == "ai_choice_cancel":
                    self._cancel_pending_choice(item[1], reason="cancelled")
                elif kind == "translate_done":
                    self._on_translate_done(item[1], item[2], item[3])
                elif kind == "quit":
                    return

            self._expire_pending_choice()
            self._expire_pending_paste()

            if time.monotonic() >= next_idle_check:
                self._idle_check()
                next_idle_check = time.monotonic() + IDLE_CHECK_INTERVAL

    def _idle_check(self):
        """Scarica periodicamente il modello se inutilizzato."""
        if self.state == STATE_IDLE:
            threading.Thread(
                target=self.model.unload_if_idle, daemon=True
            ).start()

    # --- stato ---

    def _set_state(self, state):
        self.state = state
        self._broadcast({"type": "state", "state": state})
        if state == STATE_IDLE:
            # fine della dettatura, comunque sia andata (testo incollato,
            # comando eseguito, errore, niente da trascrivere): i video
            # fermati per registrare possono ripartire. Non fa nulla se non
            # ne era stato fermato nessuno.
            self._resume_media_after_recording()

    def _notify(self, title, body, urgency="normal"):
        self.backend.notify(title, body, urgency)

    # --- configurazione (lingua di dettatura, ripristino appunti) ---

    def _config_snapshot(self):
        return {
            "language": self.model.language,
            "restore_clipboard": self.restore_clipboard,
            "translate_enabled": self.translate_enabled,
            "translate_target": self.translate_target,
            "translate_engine": self.translate_engine,
            "vocabulary": self.vocabulary,
            "confirm_before_paste": self.confirm_before_paste,
            "require_tls": self.require_tls,
            "pause_media_while_recording": self.pause_media_while_recording,
            # informativi (non modificabili): servono all'app telefono per
            # mostrare l'impronta da confrontare e capire se il canale e'
            # cifrato
            "tls_available": self.tls_context is not None,
            "tls_fingerprint": self.tls_fingerprint,
        }

    def _set_language(self, language):
        if not _valid_language(language):
            return False, (
                "language deve essere un codice ISO 639-1 di 2 lettere "
                "(es. 'it', 'en') oppure 'auto'"
            )
        language = language.strip().lower()
        self.model.language = language
        _save_language(language)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_restore_clipboard(self, enabled):
        if not isinstance(enabled, bool):
            return False, "restore_clipboard deve essere un booleano"
        self.restore_clipboard = enabled
        _save_restore_clipboard(enabled)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_pause_media_while_recording(self, enabled):
        if not isinstance(enabled, bool):
            return False, "pause_media_while_recording deve essere un booleano"
        self.pause_media_while_recording = enabled
        _save_pause_media_while_recording(enabled)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_translate_enabled(self, enabled):
        if not isinstance(enabled, bool):
            return False, "translate_enabled deve essere un booleano"
        self.translate_enabled = enabled
        _save_translate_enabled(enabled)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_translate_target(self, target):
        if not _valid_translate_target(target):
            return False, (
                "translate_target deve essere un codice ISO 639-1 di 2 "
                "lettere (es. 'it', 'en'); 'auto' non e' valido come "
                "lingua di destinazione"
            )
        target = target.strip().lower()
        self.translate_target = target
        _save_translate_target(target)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_translate_engine(self, engine):
        if engine not in TRANSLATE_ENGINES:
            return False, f"translate_engine deve essere uno di {TRANSLATE_ENGINES}"
        self.translate_engine = engine
        _save_translate_engine(engine)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_vocabulary(self, vocabulary):
        if not _valid_vocabulary(vocabulary):
            return False, (
                "vocabulary deve essere una stringa di al massimo "
                f"{VOCABULARY_MAX_LENGTH} caratteri (elenco di termini "
                "separati da virgola)"
            )
        self.vocabulary = vocabulary.strip()
        _save_vocabulary(self.vocabulary)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_confirm_before_paste(self, enabled):
        if not isinstance(enabled, bool):
            return False, "confirm_before_paste deve essere un booleano"
        self.confirm_before_paste = enabled
        _save_confirm_before_paste(enabled)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_require_tls(self, enabled):
        if not isinstance(enabled, bool):
            return False, "require_tls deve essere un booleano"
        if enabled and self.tls_context is None:
            # accettarlo lascerebbe il demone irraggiungibile da qualunque
            # telefono, senza modo di tornare indietro dall'app
            return False, (
                "impossibile richiedere TLS: nessun certificato disponibile "
                "(openssl mancante?). Genera cert.pem/key.pem in "
                f"{CONFIG_DIR} e riavvia il demone."
            )
        self.require_tls = enabled
        _save_require_tls(enabled)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _handle_config_cmd(self, cmd, msg):
        """Applica un comando di CONFIG_CMDS (una singola impostazione),
        condiviso tra il protocollo di rete e il socket di controllo MCP."""
        field, setter_name = CONFIG_CMDS[cmd]
        return getattr(self, setter_name)(msg.get(field))

    # --- socket server locale (thread separato, gira in background) ---
    # Usato da toggle.py per la scorciatoia da tastiera GNOME.

    def _start_socket_server(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((LOCAL_HOST, TOGGLE_SOCKET_PORT))
        self.server_sock.listen(4)
        t = threading.Thread(target=self._serve_forever, daemon=True)
        t.start()

    def _serve_forever(self):
        while True:
            try:
                conn, _ = self.server_sock.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(64).strip()
                if data == b"toggle":
                    self.command_queue.put(("toggle",))
                elif data == b"quit":
                    self.command_queue.put(("quit",))

    # --- server di rete TCP (app Flutter sul telefono, stessa LAN) ---
    # Protocollo: righe JSON delimitate da "\n". Il client deve autenticarsi
    # per primo con {"cmd":"auth","token":"..."}; solo dopo puo' inviare
    # {"cmd":"toggle"}. Il demone trasmette a tutti i client autenticati i
    # cambi di stato ({"type":"state",...}) e il risultato della
    # trascrizione ({"type":"result",...}).

    def _start_network_server(self):
        self.net_server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.net_server_sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
        )
        try:
            self.net_server_sock.bind((NETWORK_HOST, NETWORK_PORT))
        except OSError as exc:
            self._notify(
                "Stenografa - errore rete",
                f"Impossibile aprire la porta {NETWORK_PORT}: {exc}",
                urgency="critical",
            )
            return
        self.net_server_sock.listen(4)
        t = threading.Thread(target=self._serve_network_forever, daemon=True)
        t.start()

    def _serve_network_forever(self):
        while True:
            try:
                conn, addr = self.net_server_sock.accept()
            except OSError:
                return
            t = threading.Thread(
                target=self._start_network_client, args=(conn, addr), daemon=True
            )
            t.start()

    def _start_network_client(self, conn, addr):
        """Decide se la connessione e' TLS o in chiaro e la passa al
        gestore vero e proprio. Il negoziato TLS e' bloccante, quindi va
        fatto qui (thread del client) e non nel ciclo di accept."""
        conn = self._wrap_if_tls(conn)
        if conn is None:
            return
        self._handle_network_client(conn, addr)

    def _wrap_if_tls(self, conn):
        """Avvolge `conn` in TLS se il client lo sta usando, altrimenti la
        restituisce invariata. Il tipo si riconosce dai primi byte senza
        consumarli (MSG_PEEK): un record TLS inizia sempre con 0x16
        (handshake) seguito dalla versione 0x03, mentre il protocollo in
        chiaro comincia con '{' di un oggetto JSON. Cosi' la stessa porta
        continua a servire una versione precedente dell'app durante
        l'aggiornamento, a meno che require_tls non lo vieti. Ritorna None
        se la connessione va chiusa senza servirla."""
        try:
            conn.settimeout(10)
            # MSG_PEEK puo' restituire meno byte di quanti richiesti se il
            # client non ha ancora finito di scrivere: si insiste finche' i
            # due byte che servono a distinguere i due protocolli ci sono
            head = b""
            for _ in range(5):
                head = conn.recv(2, socket.MSG_PEEK)
                if len(head) >= 2 or not head:
                    break
                time.sleep(0.05)
        except OSError:
            self._close_quietly(conn)
            return None
        is_tls = len(head) >= 2 and head[0] == 0x16 and head[1] == 0x03
        if not is_tls:
            if self.require_tls:
                # nessuna risposta cifrata da dare: si avvisa in chiaro e si
                # chiude, cosi' l'app puo' mostrare un motivo invece di un
                # generico "connessione caduta"
                self._send_to(
                    conn,
                    {
                        "type": "auth",
                        "ok": False,
                        # `reason` distingue i rifiuti temporanei (qui: la
                        # connessione non era cifrata, di solito perche' il
                        # telefono e' ricaduto in chiaro dopo un handshake
                        # TLS fallito) da un token davvero sbagliato: solo
                        # quest'ultimo deve far smettere l'app di riprovare
                        "reason": "tls_required",
                        "error": "questo demone accetta solo connessioni TLS",
                    },
                )
                self._close_quietly(conn)
                return None
            conn.settimeout(None)
            return conn
        if self.tls_context is None:
            self._close_quietly(conn)
            return None
        try:
            conn = self.tls_context.wrap_socket(conn, server_side=True)
        except OSError:
            self._close_quietly(conn)
            return None
        conn.settimeout(None)
        return conn

    @staticmethod
    def _close_quietly(conn):
        try:
            conn.close()
        except OSError:
            pass

    def _handle_network_client(self, conn, addr):
        authenticated = False
        try:
            fh = conn.makefile("r", encoding="utf-8", newline="\n")
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cmd = msg.get("cmd")

                if not authenticated:
                    ip = addr[0] if addr else "?"
                    if cmd != "auth":
                        return
                    if not self._check_auth_rate_limit(ip):
                        self._send_to(
                            conn,
                            {
                                "type": "auth",
                                "ok": False,
                                # temporaneo: passata la finestra di
                                # AUTH_WINDOW_SECONDS lo stesso token
                                # tornera' ad essere accettato
                                "reason": "rate_limited",
                                "error": "troppi tentativi falliti, riprova tra poco",
                            },
                        )
                        return
                    if secrets.compare_digest(
                        str(msg.get("token", "")), self.auth_token
                    ):
                        authenticated = True
                        with self._net_lock:
                            self._net_clients.add(conn)
                        self._send_to(conn, {"type": "auth", "ok": True})
                        self._send_to(
                            conn, {"type": "state", "state": self.state}
                        )
                        self._send_to(
                            conn, {"type": "layout", **self._layout_snapshot()}
                        )
                        self._send_to(
                            conn, {"type": "config", **self._config_snapshot()}
                        )
                        self._send_to(
                            conn, {"type": "history", "items": self._history_snapshot()}
                        )
                        with self._media_lock:
                            players = list(self._media_players)
                        self._send_to(conn, {"type": "players", "players": players})
                        if self._last_focused_dashboard_id:
                            self._send_to(
                                conn,
                                {
                                    "type": "active_app",
                                    "dashboard_id": self._last_focused_dashboard_id,
                                },
                            )
                    else:
                        self._record_auth_failure(ip)
                        self._send_to(
                            conn,
                            {
                                "type": "auth",
                                "ok": False,
                                # l'unico rifiuto che non ha senso ritentare:
                                # serve che l'utente corregga il token
                                "reason": "bad_token",
                                "error": "token non valido",
                            },
                        )
                        return
                    continue

                if cmd == "toggle":
                    self.command_queue.put(("button", "record"))
                elif cmd == "button":
                    self.command_queue.put(("button", msg.get("id")))
                elif cmd == "button_down":
                    # push-to-talk: pressione e rilascio arrivano separati,
                    # invece del singolo tocco che fa da interruttore
                    self.command_queue.put(("button_down", msg.get("id")))
                elif cmd == "button_up":
                    self.command_queue.put(("button_up", msg.get("id")))
                elif cmd == "list_apps":
                    self._send_to(
                        conn, {"type": "apps", "apps": self._list_apps_cached()}
                    )
                elif cmd == "get_history":
                    self._send_to(
                        conn, {"type": "history", "items": self._history_snapshot()}
                    )
                elif cmd == "paste_history":
                    self.command_queue.put(("paste_history", msg.get("id")))
                elif cmd == "get_app_icon":
                    self._send_app_icon(conn, msg.get("app_id"))
                elif cmd in ("player_pause", "player_play"):
                    self.command_queue.put(
                        ("player_action", msg.get("id"), cmd.split("_")[1])
                    )
                elif cmd == "confirm_paste":
                    self.command_queue.put(
                        ("paste_reply", msg.get("request_id"), msg.get("text"))
                    )
                elif cmd == "cancel_paste":
                    self.command_queue.put(("paste_cancel", msg.get("request_id")))
                elif cmd == "choose_shortcut":
                    # l'opzione scelta e' identificata da "combo", da
                    # "app_id" se e' l'avvio di un'applicazione, o da
                    # "combos" se e' una macro
                    self.command_queue.put(
                        (
                            "ai_choice_reply",
                            msg.get("request_id"),
                            msg.get("combo"),
                            msg.get("app_id"),
                            msg.get("combos"),
                        )
                    )
                elif cmd == "cancel_shortcut_choice":
                    self.command_queue.put(
                        ("ai_choice_cancel", msg.get("request_id"))
                    )
                elif cmd == "ping":
                    self._send_to(conn, {"type": "pong"})
                elif cmd == "list_layout":
                    self._send_to(conn, {"type": "layout", **self._layout_snapshot()})
                elif cmd == "get_config":
                    self._send_to(conn, {"type": "config", **self._config_snapshot()})
                elif cmd == "restart_daemon":
                    self._send_to(conn, {"type": "restart_result", "ok": True})
                    self._request_restart()
                elif cmd in CONFIG_CMDS:
                    ok, error = self._handle_config_cmd(cmd, msg)
                    self._send_to(
                        conn,
                        {"type": "config_result", "cmd": cmd, "ok": ok, "error": error},
                    )
                elif cmd in LAYOUT_MUTATION_CMDS:
                    ok, error = self._mutate_layout(cmd, msg)
                    self._send_to(
                        conn,
                        {"type": "layout_result", "cmd": cmd, "ok": ok, "error": error},
                    )
        except (OSError, UnicodeDecodeError):
            pass
        finally:
            with self._net_lock:
                self._net_clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _check_auth_rate_limit(self, ip):
        """True se `ip` puo' ancora tentare l'autenticazione (meno di
        AUTH_MAX_ATTEMPTS fallimenti negli ultimi AUTH_WINDOW_SECONDS)."""
        now = time.monotonic()
        with self._auth_failures_lock:
            attempts = [
                t
                for t in self._auth_failures.get(ip, [])
                if now - t < AUTH_WINDOW_SECONDS
            ]
            if attempts:
                self._auth_failures[ip] = attempts
            else:
                self._auth_failures.pop(ip, None)
            return len(attempts) < AUTH_MAX_ATTEMPTS

    def _record_auth_failure(self, ip):
        with self._auth_failures_lock:
            self._auth_failures.setdefault(ip, []).append(time.monotonic())

    def _send_to(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            with self._net_lock:
                self._net_clients.discard(conn)

    def _broadcast(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._net_lock:
            clients = list(self._net_clients)
        for conn in clients:
            try:
                conn.sendall(data)
            except OSError:
                with self._net_lock:
                    self._net_clients.discard(conn)

    # --- layout dei pulsanti: griglia rows x cols, modificabile da telefono
    # (creazione manuale di scorciatoie) e da MCP (socket di controllo
    # locale). Ogni mutazione salva su disco e trasmette il nuovo layout a
    # tutti i telefoni connessi. self._layout_lock protegge self.layout dagli
    # accessi concorrenti (thread di rete, socket di controllo, UI).

    def _layout_snapshot(self):
        with self._layout_lock:
            return {
                "dashboards": [
                    {
                        "id": d["id"],
                        "name": d["name"],
                        "rows": d["rows"],
                        "cols": d["cols"],
                        "match": d.get("match", ""),
                        "vocabulary": d.get("vocabulary", ""),
                        "shortcuts": [dict(s) for s in d.get("shortcuts", [])],
                        "buttons": [dict(b) for b in d["buttons"]],
                        # applicazione a cui la dashboard si riferisce: il
                        # telefono ne chiede l'icona (get_app_icon) per
                        # disegnarla in filigrana dietro i pulsanti
                        "app_id": self._dashboard_app_id(d),
                    }
                    for d in self.layout["dashboards"]
                ]
            }

    def _dashboard_app_id(self, dashboard):
        """A quale applicazione appartiene una dashboard: il pulsante "avvia
        applicazione" che contiene, altrimenti l'app installata il cui nome
        corrisponde al "match" con cui l'utente l'ha associata. `None` per
        le dashboard che non parlano di nessuna app in particolare."""
        for button in dashboard.get("buttons", []):
            if button.get("kind") == "launch" and button.get("app_id"):
                return button["app_id"]
        patterns = [
            p.strip().lower()
            for p in (dashboard.get("match") or "").split(",")
            if p.strip()
        ]
        if not patterns:
            return None
        # solo l'elenco gia' in cache: enumerare le applicazioni installate
        # costa centinaia di file da leggere, e questo snapshot viene
        # ricalcolato ad ogni modifica del layout. La cache viene scaldata
        # all'avvio (vedi _warm_apps_cache), quindi in pratica c'e' sempre;
        # quando manca si perde solo l'icona di sfondo.
        with self._apps_cache_lock:
            apps = list(self._apps_cache or [])
        return _match_app_by_name(apps, patterns)

    def _broadcast_layout(self):
        self._broadcast({"type": "layout", **self._layout_snapshot()})

    def _mutate_layout(self, cmd, msg):
        """Applica un comando di modifica del layout. Ritorna (ok, errore)."""
        with self._layout_lock:
            if cmd == "add_button":
                return self._add_button_locked(msg)
            if cmd == "add_buttons":
                return self._add_buttons_locked(msg)
            if cmd == "remove_button":
                return self._remove_button_locked(msg.get("id"))
            if cmd == "move_button":
                return self._move_button_locked(msg)
            if cmd == "edit_button":
                return self._edit_button_locked(msg)
            if cmd == "set_button_style":
                return self._set_button_style_locked(msg)
            if cmd == "set_grid_size":
                return self._set_grid_size_locked(msg)
            if cmd == "create_dashboard":
                return self._create_dashboard_locked(msg)
            if cmd == "remove_dashboard":
                return self._remove_dashboard_locked(msg.get("id"))
            if cmd == "rename_dashboard":
                return self._rename_dashboard_locked(msg)
            if cmd == "reorder_dashboard":
                return self._reorder_dashboard_locked(msg)
            if cmd == "duplicate_dashboard":
                return self._duplicate_dashboard_locked(msg)
            if cmd == "set_dashboard_match":
                return self._set_dashboard_match_locked(msg)
            if cmd == "set_dashboard_shortcuts":
                return self._set_dashboard_shortcuts_locked(msg)
            if cmd == "set_dashboard_vocabulary":
                return self._set_dashboard_vocabulary_locked(msg)
            if cmd == "reset_layout":
                self.layout = json.loads(json.dumps(DEFAULT_LAYOUT))
                _save_layout(self.layout)
                self._broadcast_layout()
                return True, None
        return False, f"comando sconosciuto: {cmd}"

    def _find_dashboard(self, dashboard_id):
        for d in self.layout["dashboards"]:
            if d["id"] == dashboard_id:
                return d
        return None

    def _find_button_global(self, button_id):
        """Cerca un pulsante in tutte le dashboard. Ritorna (dashboard, pulsante)."""
        for d in self.layout["dashboards"]:
            for b in d["buttons"]:
                if b["id"] == button_id:
                    return d, b
        return None, None

    def _new_button_id(self, label, existing_ids=None):
        base = "".join(ch for ch in label.lower() if ch.isalnum()) or "btn"
        if existing_ids is None:
            existing_ids = {
                b["id"] for d in self.layout["dashboards"] for b in d["buttons"]
            }
        if base not in existing_ids:
            return base
        suffix = 2
        while f"{base}{suffix}" in existing_ids:
            suffix += 1
        return f"{base}{suffix}"

    def _new_dashboard_id(self, name):
        base = "".join(ch for ch in name.lower() if ch.isalnum()) or "dashboard"
        existing = {d["id"] for d in self.layout["dashboards"]}
        if base not in existing:
            return base
        suffix = 2
        while f"{base}{suffix}" in existing:
            suffix += 1
        return f"{base}{suffix}"

    def _validate_new_button(self, dashboard, existing_ids, occupied, spec):
        """Valida uno spec di pulsante (label, kind, row, col, combo, color,
        icon) e ne costruisce il dict. Ritorna (button, errore). Non muta lo
        stato: chi chiama decide quando accodare il pulsante e aggiornare
        `existing_ids`/`occupied` (utile per validare piu' pulsanti in
        sequenza, es. add_buttons)."""
        kind = spec.get("kind", "keys")
        if kind not in BUTTON_KINDS:
            return None, (
                "kind deve essere uno di "
                + ", ".join(f"'{k}'" for k in BUTTON_KINDS)
            )
        # l'applicazione va risolta prima dell'etichetta: se chi crea il
        # pulsante non ne indica una, il nome dell'app e' gia' l'etichetta
        # piu' sensata
        app = None
        if kind == "launch":
            app_id = spec.get("app_id")
            if not app_id or not isinstance(app_id, str):
                return None, (
                    "app_id mancante (usa list_launchable_apps per ottenerlo, "
                    "non inventarlo)"
                )
            app = self._find_app(app_id)
            if app is None:
                return None, (
                    f"nessuna applicazione installata con id '{app_id}' "
                    "(richiama list_launchable_apps per un elenco aggiornato)"
                )

        label = str(spec.get("label") or "").strip()
        if not label and kind == "record":
            label = "Registra"
        if not label and kind == "ai_command":
            label = "Comando vocale"
        if not label and kind == "paste_last":
            label = "Incolla ultimo"
        if not label and app is not None:
            label = str(app.get("name") or "").strip()
        if not label:
            return None, "label mancante"
        row, col = spec.get("row"), spec.get("col")
        if not isinstance(row, int) or not isinstance(col, int):
            return None, "row/col mancanti o non interi"
        rows, cols = dashboard["rows"], dashboard["cols"]
        if not (0 <= row < rows and 0 <= col < cols):
            return None, (
                f"posizione ({row},{col}) fuori dalla griglia attuale "
                f"({rows}x{cols}) di '{dashboard['name']}'; usa set_grid_size "
                "per ingrandirla"
            )
        row_span, col_span, error = _validate_spans(spec)
        if error:
            return None, error
        if row + row_span > rows or col + col_span > cols:
            return None, (
                f"un pulsante {col_span}x{row_span} in ({row},{col}) esce "
                f"dalla griglia ({rows}x{cols}) di '{dashboard['name']}'; usa "
                "set_grid_size per ingrandirla"
            )
        wanted = _button_cells(row, col, row_span, col_span)
        clash = sorted(wanted & occupied)
        if clash:
            return None, (
                f"cella {clash[0]} gia' occupata in '{dashboard['name']}'"
            )

        button = {
            "id": self._new_button_id(label, existing_ids),
            "label": label,
            "kind": kind,
            "row": row,
            "col": col,
        }
        # si scrivono solo se diversi da 1: il layout di chi non usa i
        # pulsanti estesi resta identico a prima
        if row_span != 1:
            button["row_span"] = row_span
        if col_span != 1:
            button["col_span"] = col_span
        if kind in MIC_KINDS:
            # niente combo/colore/icona: questi pulsanti "microfono"
            # controllano avvio/stop registrazione (con o senza
            # interpretazione IA dello shortcut) e il loro aspetto segue lo
            # stato della registrazione, non e' personalizzabile
            return button, None

        if kind == "keys":
            combo = spec.get("combo")
            if not combo or not isinstance(combo, str):
                return None, "combo mancante (es. 'ctrl+c')"
            if not is_valid_combo(combo):
                return None, f"combinazione di tasti non valida: {combo}"
            button["combo"] = combo
        elif kind == "macro":
            combos, delay_ms, error = self._validate_macro_spec(spec)
            if error:
                return None, error
            button["combos"] = combos
            button["delay_ms"] = delay_ms
        elif kind == "text":
            text = spec.get("text")
            if not isinstance(text, str) or not text.strip():
                return None, "text mancante (il testo da incollare)"
            if len(text) > TEXT_BUTTON_MAX_LENGTH:
                return None, (
                    f"text troppo lungo ({len(text)} caratteri, massimo "
                    f"{TEXT_BUTTON_MAX_LENGTH})"
                )
            button["text"] = text
        elif kind == "launch":
            button["app_id"] = app["id"]
            # il nome viene ricopiato dall'elenco, non da chi crea il
            # pulsante: cosi' l'etichetta mostrata corrisponde davvero
            # all'app che verra' avviata
            button["app_name"] = app.get("name", "")

        color = spec.get("color")
        if color is not None and not _valid_color(color):
            return None, f"colore non valido: {color} (usa il formato '#RRGGBB')"
        icon = spec.get("icon")
        if icon is not None and icon not in ICON_NAMES:
            # a differenza di combo/colore, un nome icona sbagliato non fa
            # fallire la creazione (ne', in add_buttons, l'intero batch
            # atomico): l'LLM a volte ne inventa uno plausibile ma
            # inesistente, e perdere solo l'icona (il pulsante resta con
            # quella di default) e' un fallimento molto piu' tollerabile che
            # bloccare l'intera richiesta
            icon = None
        if color is not None:
            button["color"] = color
        if icon is not None:
            button["icon"] = icon
        return button, None

    def _validate_macro_spec(self, spec):
        """Valida la sequenza di combinazioni di un pulsante kind="macro".
        Ritorna (combos, delay_ms, errore). Come per le liste di scorciatoie,
        se piu' di una combinazione non e' valida l'errore le elenca tutte
        insieme invece di fermarsi alla prima."""
        combos = spec.get("combos")
        if not isinstance(combos, list) or not combos:
            return None, None, (
                "combos mancante o vuoto (lista di combinazioni da eseguire "
                "in sequenza, es. ['ctrl+s', 'alt+tab'])"
            )
        if len(combos) > MACRO_MAX_STEPS:
            return None, None, (
                f"troppe combinazioni ({len(combos)}, massimo "
                f"{MACRO_MAX_STEPS})"
            )
        errors = []
        valid = []
        for i, combo in enumerate(combos):
            if not isinstance(combo, str) or not is_valid_combo(combo):
                errors.append(f"combos[{i}]: combinazione non valida: {combo}")
            else:
                valid.append(combo)
        if errors:
            return None, None, "; ".join(errors)

        delay_ms = spec.get("delay_ms", MACRO_DEFAULT_DELAY_MS)
        if not isinstance(delay_ms, int) or not (0 <= delay_ms <= MACRO_MAX_DELAY_MS):
            return None, None, (
                f"delay_ms deve essere un intero fra 0 e {MACRO_MAX_DELAY_MS} "
                "(pausa in millisecondi fra un passo e il successivo)"
            )
        return valid, delay_ms, None

    def _validate_shortcut_spec(self, spec, index):
        """Valida un elemento di un "vocabolario" di scorciatoie nascoste
        (vedi _set_dashboard_shortcuts_locked): solo label + combo, niente
        posizione/colore/icona perche' non sono pulsanti visibili sul
        telefono. Ritorna ({"label", "combo"}, errore)."""
        if not isinstance(spec, dict):
            return None, f"shortcuts[{index}]: non e' un oggetto valido"
        label = str(spec.get("label") or "").strip()
        if not label:
            return None, f"shortcuts[{index}]: label mancante"
        combo = spec.get("combo")
        if not combo or not isinstance(combo, str):
            return None, f"shortcuts[{index}] ('{label}'): combo mancante (es. 'ctrl+c')"
        if not is_valid_combo(combo):
            return None, (
                f"shortcuts[{index}] ('{label}'): combinazione di tasti "
                f"non valida: {combo}"
            )
        return {"label": label, "combo": combo}, None

    def _validate_shortcut_list(self, specs):
        """Valida una lista di scorciatoie nascoste (vedi
        _validate_shortcut_spec). Ritorna (lista, errore): o tutte valide,
        o nessuna viene accettata. Se piu' di un elemento non e' valido,
        l'errore li elenca TUTTI insieme (non solo il primo): su una lista
        lunga (es. tutte le scorciatoie di un'app) e' comune che l'LLM
        sbagli piu' voci, e scoprirle una alla volta a chiamate successive
        e' inutilmente lento."""
        if not isinstance(specs, list):
            return None, "shortcuts deve essere una lista di oggetti {label, combo}"
        result = []
        errors = []
        for i, spec in enumerate(specs):
            entry, error = self._validate_shortcut_spec(spec, i)
            if error:
                errors.append(error)
            else:
                result.append(entry)
        if errors:
            return None, "; ".join(errors)
        return result, None

    def _add_button_locked(self, msg):
        dashboard = self._find_dashboard(msg.get("dashboard_id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('dashboard_id')}' non trovata"
        existing_ids = {
            b["id"] for d in self.layout["dashboards"] for b in d["buttons"]
        }
        occupied = _occupied_cells(dashboard["buttons"])
        button, error = self._validate_new_button(
            dashboard, existing_ids, occupied, msg
        )
        if error:
            return False, error
        dashboard["buttons"].append(button)
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _add_buttons_locked(self, msg):
        """Aggiunge piu' pulsanti in una sola chiamata (atomica: o vanno
        bene tutti, o non viene applicato nulla), utile per non dover fare
        una chiamata MCP per ogni singolo pulsante. Se piu' di un elemento
        non e' valido, l'errore li elenca TUTTI insieme (non solo il
        primo), cosi' si correggono in un colpo solo invece di scoprirli
        uno alla volta a chiamate successive."""
        dashboard = self._find_dashboard(msg.get("dashboard_id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('dashboard_id')}' non trovata"
        specs = msg.get("buttons")
        if not isinstance(specs, list) or not specs:
            return False, "buttons mancante o vuoto (lista di pulsanti da aggiungere)"

        occupied = _occupied_cells(dashboard["buttons"])
        existing_ids = {
            b["id"] for d in self.layout["dashboards"] for b in d["buttons"]
        }
        new_buttons = []
        errors = []

        for i, spec in enumerate(specs):
            if not isinstance(spec, dict):
                errors.append(f"elemento {i}: non e' un oggetto valido")
                continue
            button, error = self._validate_new_button(
                dashboard, existing_ids, occupied, spec
            )
            if error:
                label = spec.get("label") if isinstance(spec, dict) else None
                prefix = f"elemento {i}" + (f" ('{label}')" if label else "")
                errors.append(f"{prefix}: {error}")
                continue
            existing_ids.add(button["id"])
            occupied |= _cells_of(button)
            new_buttons.append(button)

        if errors:
            return False, "; ".join(errors)

        dashboard["buttons"].extend(new_buttons)
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _edit_button_locked(self, msg):
        """Modifica un pulsante gia' creato: l'etichetta, l'azione che esegue
        (la combinazione di tasti di un "keys", la sequenza di un "macro", lo
        snippet di un "text", l'applicazione di un "launch") e quante celle
        occupa. Cambiarlo senza rifarlo evita di perderne posizione, colore
        e icona.

        Ogni campo e' opzionale: quelli assenti restano com'erano. Un campo
        che non appartiene al tipo del pulsante e' un errore, non una
        modifica silenziosamente ignorata."""
        button_id = msg.get("id")
        dashboard, button = self._find_button_global(button_id)
        if button is None:
            return False, f"nessun pulsante con id '{button_id}'"
        kind = button["kind"]

        updates = {}
        if "label" in msg:
            label = str(msg.get("label") or "").strip()
            if not label:
                return False, "label vuota"
            updates["label"] = label

        if "combo" in msg:
            if kind != "keys":
                return False, (
                    f"il pulsante '{button['label']}' e' di tipo '{kind}': "
                    "solo i pulsanti 'keys' hanno una combinazione singola"
                )
            combo = msg.get("combo")
            if not combo or not isinstance(combo, str):
                return False, "combo mancante (es. 'ctrl+c')"
            if not is_valid_combo(combo):
                return False, f"combinazione di tasti non valida: {combo}"
            updates["combo"] = combo

        if "combos" in msg or "delay_ms" in msg:
            if kind != "macro":
                return False, (
                    f"il pulsante '{button['label']}' e' di tipo '{kind}': "
                    "solo i pulsanti 'macro' hanno una sequenza"
                )
            # i due campi si validano insieme: chi cambia solo la pausa non
            # deve rimandare anche l'elenco delle combinazioni
            spec = {
                "combos": msg.get("combos", button["combos"]),
                "delay_ms": msg.get(
                    "delay_ms",
                    button.get("delay_ms", MACRO_DEFAULT_DELAY_MS),
                ),
            }
            combos, delay_ms, error = self._validate_macro_spec(spec)
            if error:
                return False, error
            updates["combos"] = combos
            updates["delay_ms"] = delay_ms

        if "app_id" in msg:
            if kind != "launch":
                return False, (
                    f"il pulsante '{button['label']}' e' di tipo '{kind}': "
                    "solo i pulsanti 'launch' avviano un'applicazione"
                )
            app = self._find_app(msg.get("app_id"))
            if app is None:
                return False, (
                    f"nessuna applicazione installata con id "
                    f"'{msg.get('app_id')}' (richiama list_launchable_apps "
                    "per un elenco aggiornato)"
                )
            updates["app_id"] = app["id"]
            # il nome mostrato deve seguire l'applicazione, altrimenti il
            # pulsante direbbe di avviarne una e ne avvierebbe un'altra
            updates["app_name"] = app.get("name", "")

        if "text" in msg:
            if kind != "text":
                return False, (
                    f"il pulsante '{button['label']}' e' di tipo '{kind}': "
                    "solo i pulsanti 'text' hanno uno snippet"
                )
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                return False, "text mancante (il testo da incollare)"
            if len(text) > TEXT_BUTTON_MAX_LENGTH:
                return False, (
                    f"text troppo lungo ({len(text)} caratteri, massimo "
                    f"{TEXT_BUTTON_MAX_LENGTH})"
                )
            updates["text"] = text

        if "row_span" in msg or "col_span" in msg:
            row_span, col_span, error = _validate_spans(msg, button)
            if error:
                return False, error
            rows, cols = dashboard["rows"], dashboard["cols"]
            if button["row"] + row_span > rows or button["col"] + col_span > cols:
                return False, (
                    f"un pulsante {col_span}x{row_span} in "
                    f"({button['row']},{button['col']}) esce dalla griglia "
                    f"({rows}x{cols}); usa set_grid_size per ingrandirla"
                )
            wanted = _button_cells(button["row"], button["col"], row_span, col_span)
            clash = wanted & _occupied_cells(
                dashboard["buttons"], exclude_id=button_id
            )
            if clash:
                return False, (
                    f"ingrandimento non possibile: la cella "
                    f"{sorted(clash)[0]} e' gia' occupata"
                )
            # come alla creazione: si scrivono solo se diversi da 1
            updates["row_span"] = row_span
            updates["col_span"] = col_span

        if not updates:
            return False, (
                "nessuna modifica indicata (label, combo, combos, text, "
                "app_id, row_span, col_span)"
            )

        button.update(updates)
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _set_button_style_locked(self, msg):
        button_id = msg.get("id")
        _, button = self._find_button_global(button_id)
        if button is None:
            return False, f"nessun pulsante con id '{button_id}'"
        if button["kind"] in MIC_KINDS:
            return False, (
                "questo pulsante non supporta un colore/icona personalizzati "
                "(il suo aspetto segue lo stato della registrazione)"
            )
        color = msg.get("color")
        icon = msg.get("icon")
        if color is not None:
            if not _valid_color(color):
                return False, f"colore non valido: {color} (usa il formato '#RRGGBB')"
            button["color"] = color
        if icon is not None:
            if icon not in ICON_NAMES:
                return False, f"icona non valida: {icon}"
            button["icon"] = icon
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _record_button_count(self):
        return sum(
            1
            for d in self.layout["dashboards"]
            for b in d["buttons"]
            if b["kind"] == "record"
        )

    def _remove_button_locked(self, button_id):
        if not button_id:
            return False, "id mancante"
        dashboard, button = self._find_button_global(button_id)
        if button is None:
            return False, f"nessun pulsante con id '{button_id}'"
        if button["kind"] == "record" and self._record_button_count() <= 1:
            return False, (
                "questo e' l'unico pulsante 'microfono' del layout: non "
                "puo' essere rimosso (rimarresti senza modo di avviare la "
                "registrazione)"
            )
        dashboard["buttons"] = [
            b for b in dashboard["buttons"] if b["id"] != button_id
        ]
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _move_button_locked(self, msg):
        """Sposta un pulsante in (row, col) nella stessa dashboard. Se la
        cella di destinazione e' gia' occupata da un altro pulsante, i due
        si scambiano di posto invece di fallire — comodo per riordinare
        trascinando una casella sopra un'altra sul telefono."""
        button_id = msg.get("id")
        row, col = msg.get("row"), msg.get("col")
        dashboard, button = self._find_button_global(button_id)
        if button is None:
            return False, f"nessun pulsante con id '{button_id}'"
        if not isinstance(row, int) or not isinstance(col, int):
            return False, "row/col mancanti o non interi"
        rows, cols = dashboard["rows"], dashboard["cols"]
        if not (0 <= row < rows and 0 <= col < cols):
            return False, f"posizione ({row},{col}) fuori dalla griglia ({rows}x{cols})"
        row_span = button.get("row_span", 1)
        col_span = button.get("col_span", 1)
        if row + row_span > rows or col + col_span > cols:
            return False, (
                f"un pulsante {col_span}x{row_span} in ({row},{col}) esce "
                f"dalla griglia ({rows}x{cols})"
            )
        wanted = _button_cells(row, col, row_span, col_span)
        others = [b for b in dashboard["buttons"] if b["id"] != button_id]
        touched = [b for b in others if _cells_of(b) & wanted]
        if touched:
            # lo scambio ha senso solo fra due pulsanti della stessa forma:
            # con dimensioni diverse finirebbero l'uno sopra l'altro o fuori
            # griglia, quindi si preferisce dirlo invece di fare pasticci
            occupant = touched[0]
            same_shape = (
                len(touched) == 1
                and occupant.get("row_span", 1) == row_span
                and occupant.get("col_span", 1) == col_span
            )
            if not same_shape:
                return False, (
                    f"la destinazione ({row},{col}) e' occupata da "
                    f"'{occupant['label']}', di dimensione diversa: liberala "
                    "prima di spostare qui"
                )
            occupant["row"], occupant["col"] = button["row"], button["col"]
        button["row"], button["col"] = row, col
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _set_grid_size_locked(self, msg):
        dashboard = self._find_dashboard(msg.get("dashboard_id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('dashboard_id')}' non trovata"
        rows, cols = msg.get("rows"), msg.get("cols")
        if (
            not isinstance(rows, int)
            or not isinstance(cols, int)
            or rows < 1
            or cols < 1
        ):
            return False, "rows/cols non validi (minimo 1x1)"
        fuori = [
            b
            for b in dashboard["buttons"]
            if b["row"] + b.get("row_span", 1) > rows
            or b["col"] + b.get("col_span", 1) > cols
        ]
        if fuori:
            etichette = ", ".join(b["label"] for b in fuori)
            return False, (
                f"riduzione non possibile: i pulsanti [{etichette}] "
                "finirebbero fuori dalla griglia"
            )
        dashboard["rows"], dashboard["cols"] = rows, cols
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _create_dashboard_locked(self, msg):
        """Crea una dashboard, opzionalmente con le sue scorciatoie gia'
        incluse (`buttons`, stesso schema di add_buttons) in un'unica
        operazione atomica: se un pulsante non e' valido non viene creato
        nulla, cosi' un client MCP non deve fare due chiamate separate
        (create_dashboard poi add_buttons) per un caso comune. Puo' anche
        ricevere `shortcuts` (vedi _set_dashboard_shortcuts_locked) per
        salvare da subito un vocabolario di scorciatoie note all'IA ma non
        mostrate come pulsanti."""
        name = str(msg.get("name") or "").strip()
        if not name:
            return False, "name mancante"

        button_specs = msg.get("buttons")
        if button_specs is not None and not isinstance(button_specs, list):
            return False, "buttons deve essere una lista di pulsanti (vedi add_buttons)"
        button_specs = button_specs or []

        shortcuts, error = self._validate_shortcut_list(msg.get("shortcuts") or [])
        if error:
            return False, error

        match = msg.get("match")
        if match is not None and not isinstance(match, str):
            return False, "match deve essere una stringa"

        vocabulary = msg.get("vocabulary")
        if vocabulary is not None and not _valid_vocabulary(vocabulary):
            return False, (
                "vocabulary deve essere una stringa di al massimo "
                f"{VOCABULARY_MAX_LENGTH} caratteri"
            )

        # se rows/cols non sono specificati, la griglia si dimensiona sulla
        # posizione massima usata dai pulsanti forniti (minimo 1x1); righe/
        # colonne non intere vengono ignorate qui e segnalate poi dal
        # controllo vero e proprio in _validate_new_button
        rows_used = [b["row"] for b in button_specs if isinstance(b, dict) and isinstance(b.get("row"), int)]
        cols_used = [b["col"] for b in button_specs if isinstance(b, dict) and isinstance(b.get("col"), int)]
        rows, cols = msg.get("rows"), msg.get("cols")
        if not isinstance(rows, int) or rows < 1:
            rows = max(rows_used, default=0) + 1
        if not isinstance(cols, int) or cols < 1:
            cols = max(cols_used, default=0) + 1

        dashboard = {
            "id": self._new_dashboard_id(name),
            "name": name,
            "rows": rows,
            "cols": cols,
            "match": (match or "").strip(),
            "vocabulary": (vocabulary or "").strip(),
            "shortcuts": shortcuts,
            "buttons": [],
        }

        existing_ids = {
            b["id"] for d in self.layout["dashboards"] for b in d["buttons"]
        }
        occupied = set()
        new_buttons = []
        button_errors = []
        for i, spec in enumerate(button_specs):
            if not isinstance(spec, dict):
                button_errors.append(f"elemento {i}: non e' un oggetto valido")
                continue
            button, error = self._validate_new_button(
                dashboard, existing_ids, occupied, spec
            )
            if error:
                label = spec.get("label") if isinstance(spec, dict) else None
                prefix = f"elemento {i}" + (f" ('{label}')" if label else "")
                button_errors.append(f"{prefix}: {error}")
                continue
            existing_ids.add(button["id"])
            occupied |= _cells_of(button)
            new_buttons.append(button)

        if button_errors:
            return False, "; ".join(button_errors)

        dashboard["buttons"] = new_buttons
        self.layout["dashboards"].append(dashboard)
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _remove_dashboard_locked(self, dashboard_id):
        if len(self.layout["dashboards"]) <= 1:
            return False, "non puoi rimuovere l'unica dashboard rimasta"
        dashboard = self._find_dashboard(dashboard_id)
        if dashboard is None:
            return False, f"dashboard '{dashboard_id}' non trovata"
        has_record = any(b["kind"] == "record" for b in dashboard["buttons"])
        if has_record:
            record_altrove = any(
                d["id"] != dashboard_id
                and any(b["kind"] == "record" for b in d["buttons"])
                for d in self.layout["dashboards"]
            )
            if not record_altrove:
                return False, (
                    "questa dashboard contiene l'unico pulsante 'record': "
                    "non puo' essere rimossa (rimarresti senza modo di "
                    "avviare la registrazione)"
                )
        self.layout["dashboards"] = [
            d for d in self.layout["dashboards"] if d["id"] != dashboard_id
        ]
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _rename_dashboard_locked(self, msg):
        dashboard = self._find_dashboard(msg.get("id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('id')}' non trovata"
        name = str(msg.get("name") or "").strip()
        if not name:
            return False, "name mancante"
        dashboard["name"] = name
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _reorder_dashboard_locked(self, msg):
        dashboard_id = msg.get("id")
        position = msg.get("position")
        if not isinstance(position, int):
            return False, "position mancante o non intera"
        dashboards = self.layout["dashboards"]
        index = next(
            (i for i, d in enumerate(dashboards) if d["id"] == dashboard_id), None
        )
        if index is None:
            return False, f"dashboard '{dashboard_id}' non trovata"
        position = max(0, min(position, len(dashboards) - 1))
        dashboard = dashboards.pop(index)
        dashboards.insert(position, dashboard)
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _duplicate_dashboard_locked(self, msg):
        source = self._find_dashboard(msg.get("id"))
        if source is None:
            return False, f"dashboard '{msg.get('id')}' non trovata"
        name = str(msg.get("name") or "").strip() or f"{source['name']} (copia)"

        existing_button_ids = {
            b["id"] for d in self.layout["dashboards"] for b in d["buttons"]
        }
        new_buttons = []
        for b in source["buttons"]:
            if b["kind"] == "record":
                # non duplichiamo i pulsanti "microfono": eviterebbe un
                # secondo microfono involontario nella copia. Chi lo vuole
                # lo aggiunge a mano/via add_button con kind="record"
                continue
            new_button = dict(b)
            new_id = self._new_button_id(new_button["label"], existing_button_ids)
            existing_button_ids.add(new_id)
            new_button["id"] = new_id
            new_buttons.append(new_button)

        dashboard = {
            "id": self._new_dashboard_id(name),
            "name": name,
            "rows": source["rows"],
            "cols": source["cols"],
            # il match non si copia: evita due dashboard che rispondono
            # entrambe alla stessa app in modo ambiguo
            "match": "",
            # il vocabolario di scorciatoie nascoste invece si copia: e'
            # legato al contesto/app della dashboard, non all'auto-switch.
            # Stesso ragionamento per i termini di dettatura.
            "shortcuts": [dict(s) for s in source.get("shortcuts", [])],
            "vocabulary": source.get("vocabulary", ""),
            "buttons": new_buttons,
        }
        self.layout["dashboards"].append(dashboard)
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _set_dashboard_match_locked(self, msg):
        dashboard = self._find_dashboard(msg.get("id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('id')}' non trovata"
        match = msg.get("match")
        if match is not None and not isinstance(match, str):
            return False, "match deve essere una stringa"
        dashboard["match"] = (match or "").strip()
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _set_dashboard_vocabulary_locked(self, msg):
        """Termini specifici di questa dashboard (nomi di strumenti, gergo
        dell'app a cui e' dedicata) passati a Whisper come contesto quando la
        dettatura parte da un pulsante di questa dashboard. Vedi
        _vocabulary_for_dashboard."""
        dashboard = self._find_dashboard(msg.get("dashboard_id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('dashboard_id')}' non trovata"
        vocabulary = msg.get("vocabulary")
        if not _valid_vocabulary(vocabulary):
            return False, (
                "vocabulary deve essere una stringa di al massimo "
                f"{VOCABULARY_MAX_LENGTH} caratteri (elenco di termini "
                "separati da virgola)"
            )
        dashboard["vocabulary"] = vocabulary.strip()
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _set_dashboard_shortcuts_locked(self, msg):
        """Sostituisce il "vocabolario" di scorciatoie (label -> combo) note
        all'interpretazione IA (vedi _dashboard_shortcuts_context) ma non
        mostrate come pulsanti sul telefono: utile per far conoscere
        all'IA una lista completa di scorciatoie di un'app senza dover
        occupare la griglia con pulsanti che l'utente non premera' mai a
        mano. Con mode="replace" (default) sostituisce l'intera lista
        precedente; con mode="append" aggiunge in coda quelle nuove tenendo
        le esistenti (le voci con la stessa etichetta vengono aggiornate,
        non duplicate)."""
        dashboard = self._find_dashboard(msg.get("dashboard_id"))
        if dashboard is None:
            return False, f"dashboard '{msg.get('dashboard_id')}' non trovata"
        mode = msg.get("mode", "replace")
        if mode not in ("replace", "append"):
            return False, "mode deve essere 'replace' o 'append'"
        shortcuts, error = self._validate_shortcut_list(msg.get("shortcuts"))
        if error:
            return False, error
        if mode == "append":
            merged = [dict(s) for s in dashboard.get("shortcuts", [])]
            by_label = {s["label"].lower(): i for i, s in enumerate(merged)}
            for entry in shortcuts:
                index = by_label.get(entry["label"].lower())
                if index is None:
                    by_label[entry["label"].lower()] = len(merged)
                    merged.append(entry)
                else:
                    merged[index] = entry
            shortcuts = merged
        dashboard["shortcuts"] = shortcuts
        _save_layout(self.layout)
        self._broadcast_layout()
        return True, None

    def _handle_button_press(self, button_id, phase="tap"):
        """`phase` distingue il tocco normale ("tap", che sui pulsanti
        microfono fa da interruttore avvia/ferma) dal push-to-talk, in cui
        l'app invia separatamente la pressione ("down", avvia) e il rilascio
        ("up", ferma). Sui pulsanti non-microfono l'azione parte alla
        pressione e il rilascio non fa nulla, cosi' tenere premuto per
        sbaglio non la esegue due volte."""
        with self._layout_lock:
            dashboard, button = self._find_button_global(button_id)
        if button is None:
            return
        kind = button["kind"]
        dashboard_id = dashboard["id"] if dashboard else None

        if kind in MIC_KINDS:
            mode = "ai_command" if kind == "ai_command" else "paste"
            if phase == "down":
                # gia' in registrazione: il "down" e' un doppione (es. due
                # telefoni collegati), non un secondo comando da eseguire
                if self.state == STATE_IDLE:
                    self.toggle_recording(
                        mode=mode, dashboard_id=dashboard_id, phase=phase
                    )
            elif phase == "up":
                if self.state == STATE_RECORDING:
                    self.toggle_recording(
                        mode=mode, dashboard_id=dashboard_id, phase=phase
                    )
            else:
                self.toggle_recording(
                    mode=mode, dashboard_id=dashboard_id, phase=phase
                )
            return

        if phase == "up":
            return
        if kind == "keys":
            threading.Thread(
                target=self.backend.simulate_keys,
                args=(button["combo"],),
                daemon=True,
            ).start()
        elif kind == "macro":
            threading.Thread(
                target=self._run_macro,
                args=(button["label"], list(button["combos"]),
                      button.get("delay_ms", MACRO_DEFAULT_DELAY_MS)),
                daemon=True,
            ).start()
        elif kind == "text":
            threading.Thread(
                target=self._paste_snippet,
                args=(button["text"],),
                daemon=True,
            ).start()
        elif kind == "launch":
            threading.Thread(
                target=self._launch_from_button,
                args=(button["app_id"], button.get("app_name") or button["label"]),
                daemon=True,
            ).start()
        elif kind == "paste_last":
            threading.Thread(
                target=self._paste_last_dictation,
                daemon=True,
            ).start()

    def _run_macro(self, label, combos, delay_ms):
        """Esegue in sequenza le combinazioni di un pulsante "macro". Se una
        fallisce si interrompe: i passi successivi presuppongono lo stato
        lasciato dai precedenti (es. "salva" poi "chiudi"), quindi tirare
        dritto rischierebbe di eseguirli nel contesto sbagliato."""
        delay = max(0, delay_ms) / 1000.0
        for i, combo in enumerate(combos):
            if i:
                time.sleep(delay)
            if not self.backend.simulate_keys(combo):
                self._notify(
                    "Stenografa - macro interrotta",
                    f'"{label}": passo {i + 1} ({combo}) non riuscito',
                    urgency="critical",
                )
                return

    def _paste_snippet(self, text):
        """Incolla il testo fisso di un pulsante kind="text", riusando la
        stessa pipeline della dettatura (appunti + incolla adattivo +
        eventuale ripristino degli appunti precedenti)."""
        clipboard_before = (
            self.backend.read_clipboard() if self.restore_clipboard else None
        )
        self._paste_text(text, clipboard_before, remember=False)

    def _launch_from_button(self, app_id, app_name):
        result = self._handle_launch_app(app_id)
        if result.get("ok"):
            self._notify("Stenografa - avvio applicazione", app_name)
        else:
            self._notify(
                "Stenografa - avvio non riuscito",
                f"{app_name}: {result.get('error', 'errore sconosciuto')}",
                urgency="critical",
            )
        self._broadcast(
            {
                "type": "result",
                "kind": "launch",
                "text": app_name,
                "error": None if result.get("ok") else result.get("error"),
            }
        )

    # --- elenco delle applicazioni installate (cache condivisa fra pulsanti
    # kind="launch", comando vocale IA e MCP) ---

    def _list_apps_cached(self):
        now = time.monotonic()
        with self._apps_cache_lock:
            if (
                self._apps_cache is not None
                and now - self._apps_cache_at < APPS_CACHE_TTL
            ):
                return self._apps_cache
        # l'enumerazione vera avviene fuori dal lock: e' lenta (centinaia di
        # file .desktop) e non deve bloccare gli altri lettori
        apps = self.backend.list_apps()
        with self._apps_cache_lock:
            self._apps_cache = apps
            self._apps_cache_at = time.monotonic()
        return apps

    def _find_app(self, app_id):
        return next(
            (a for a in self._list_apps_cached() if a.get("id") == app_id), None
        )

    # --- app attiva sul PC -> dashboard suggerita al telefono ---

    def _dashboard_for_focus(self, wm_class, title):
        # "match" puo' contenere piu' pattern separati da virgola (es.
        # "code, codium"): basta che uno qualsiasi corrisponda
        wm_class_l = (wm_class or "").lower()
        title_l = (title or "").lower()
        with self._layout_lock:
            for d in self.layout["dashboards"]:
                raw = d.get("match") or ""
                for pattern in (p.strip().lower() for p in raw.split(",")):
                    if pattern and (pattern in wm_class_l or pattern in title_l):
                        return d["id"]
        return None

    def _start_active_window_watcher(self):
        t = threading.Thread(target=self._active_window_loop, daemon=True)
        t.start()

    def _active_window_loop(self):
        # il match va ricalcolato ad ogni ciclo (non solo quando cambia la
        # finestra col focus): l'utente puo' cambiare le regole "match" delle
        # dashboard mentre la stessa finestra resta a fuoco, e in tal caso va
        # comunque ri-suggerita. Il cambio di finestra e' invece necessario
        # per ri-notificare quando si torna sulla stessa app dopo essere
        # passati altrove (altrimenti dashboard_id non cambierebbe).
        last_window_id = None
        last_broadcast_dashboard_id = None
        while self._running:
            time.sleep(ACTIVE_WINDOW_POLL_INTERVAL)
            focused = self.backend.get_focused_window()
            if focused is None:
                continue
            window_id, wm_class, title = focused
            window_changed = window_id != last_window_id
            last_window_id = window_id
            dashboard_id = self._dashboard_for_focus(wm_class, title)
            if dashboard_id and (
                window_changed or dashboard_id != last_broadcast_dashboard_id
            ):
                last_broadcast_dashboard_id = dashboard_id
                self._last_focused_dashboard_id = dashboard_id
                self._broadcast({"type": "active_app", "dashboard_id": dashboard_id})

    # --- icone delle applicazioni per il telefono ---

    def _app_icon_base64(self, app_id):
        """Icona dell'applicazione in base64 (PNG), pronta da mandare al
        telefono. Con cache: trovare il file costa qualche decimo di secondo
        (si spulciano i temi installati) e la stessa icona viene richiesta
        ad ogni riconnessione. In cache finisce anche il "non trovata", che
        altrimenti si ripagherebbe la ricerca ogni volta."""
        with self._app_icon_lock:
            if app_id in self._app_icon_cache:
                return self._app_icon_cache[app_id]
        data = self.backend.app_icon_png(app_id)
        encoded = base64.b64encode(data).decode("ascii") if data else None
        with self._app_icon_lock:
            self._app_icon_cache[app_id] = encoded
        return encoded

    def _send_app_icon(self, conn, app_id):
        if not app_id or not isinstance(app_id, str):
            return
        self._send_to(
            conn,
            {
                "type": "app_icon",
                "app_id": app_id,
                "png": self._app_icon_base64(app_id),
            },
        )

    # --- controlli play/pausa dei video in riproduzione sul PC ---

    def _warm_apps_cache(self):
        """Enumera le applicazioni installate una volta, in sottofondo: serve
        ad associare le dashboard alla loro app (vedi _dashboard_app_id) senza
        far aspettare nessuno, visto che significa leggere centinaia di file."""
        threading.Thread(target=self._list_apps_cached, daemon=True).start()

    def _start_media_players_watcher(self):
        t = threading.Thread(target=self._media_players_loop, daemon=True)
        t.start()

    def _media_players_loop(self):
        while self._running:
            time.sleep(MEDIA_POLL_INTERVAL)
            self._refresh_media_players()
            self._unmute_guarded_apps()

    def _unmute_guarded_apps(self):
        """Riattiva l'audio di un'applicazione che era stata silenziata per
        la dettatura e ricompare muta: il server audio ricorda il mute per
        applicazione, quindi il flusso aperto dopo il ripristino nasce muto
        e l'utente resta senza audio senza capire perche' (vedi
        MUTED_APPS_GUARD_SECONDS). Passata la finestra di guardia non si
        tocca piu' nulla: da li' in poi un mute e' una scelta dell'utente."""
        with self._media_lock:
            guard = self._muted_apps_guard
            if guard is None:
                return
            if time.monotonic() > guard["until"]:
                self._muted_apps_guard = None
                return
            names = set(guard["names"])
        for stream in self.backend.list_audio_streams():
            if stream["muted"] and stream["name"] in names:
                self.backend.set_audio_stream_muted(stream["id"], False)

    def _refresh_media_players(self, expected=None):
        """Ricalcola l'elenco da mostrare sul telefono e lo trasmette solo se
        e' cambiato (il poll gira ogni pochi secondi: ritrasmetterlo identico
        sveglierebbe l'app per nulla).

        `expected` e' la coppia (id, playing) appena richiesta dal telefono:
        vedi _visible_media_players."""
        players = self._visible_media_players(expected=expected)
        with self._media_lock:
            if players == self._media_players:
                return
            self._media_players = players
        self._broadcast({"type": "players", "players": players})

    def _visible_media_players(self, expected=None):
        """Player da mostrare sul telefono: tutti quelli che il PC espone, in
        riproduzione o in pausa. Anche un video fermo merita il suo pulsante
        — serve proprio a farlo ripartire — e sono pochi: un browser ne
        pubblica uno per finestra, non uno per scheda.

        `expected` e' la coppia (id, playing) appena chiesta dal telefono: lo
        stato riportato dal player arriva con un attimo di ritardo, e senza
        questa correzione il primo elenco dopo il tocco direbbe ancora quello
        di prima, lasciando l'icona indietro rispetto al dito."""
        players = self.backend.list_media_players()
        if expected is not None:
            expected_id, expected_playing = expected
            for player in players:
                if player["id"] == expected_id:
                    player["playing"] = expected_playing
        return players

    def _handle_player_action(self, player_id, action):
        """Esegue play/pausa richiesti dal telefono. L'elenco viene
        ricalcolato subito invece di aspettare il prossimo poll: l'icona deve
        cambiare sotto il dito, non due secondi dopo."""
        if not player_id:
            return
        playing = action == "play"
        if playing:
            done = self.backend.media_player_play(player_id)
        else:
            done = self.backend.media_player_pause(player_id)
        # se il comando e' fallito non si annuncia uno stato che non c'e':
        # si lascia parlare l'elenco vero
        self._refresh_media_players(
            expected=(player_id, playing) if done else None
        )

    def _pause_media_for_recording(self):
        """Zittisce quello che sta suonando prima di aprire il microfono
        (altrimenti finisce nella dettatura) e ricorda cosa ha toccato, per
        rimettere tutto com'era alla fine.

        Due passaggi, perche' uno solo non basta: i player si mettono in
        pausa davvero, ma un browser ne pubblica uno solo anche quando piu'
        schede stanno riproducendo. Quello che resta a suonare lo si prende
        dai flussi audio, che invece sono uno per scheda: quei video non si
        fermano, ma almeno non si sentono.

        Sequenziale e prima della registrazione: sono pochi comandi e ognuno
        dura al massimo un secondo (vedi _mpris_call/_run)."""
        if not self.pause_media_while_recording:
            return
        paused = []
        for player in self.backend.list_media_players():
            if player["playing"] and self.backend.media_player_pause(player["id"]):
                paused.append(player["id"])
        self._paused_for_recording = paused

        muted = []
        for stream in self.backend.list_audio_streams():
            # i flussi gia' silenziati dall'utente non si toccano:
            # riattivarli alla fine sarebbe una sorpresa
            if not stream["active"] or stream["muted"]:
                continue
            if self.backend.set_audio_stream_muted(stream["id"], True):
                # si ricorda anche il nome dell'applicazione: gli id dei
                # flussi cambiano di continuo (il browser ne apre uno nuovo
                # ad ogni video) e il nome e' l'unico appiglio per ritrovare
                # cosa riattivare (vedi _resume_media_after_recording)
                muted.append({"id": stream["id"], "name": stream["name"]})
        self._muted_for_recording = muted
        if muted:
            with self._media_lock:
                self._muted_apps_guard = {
                    "names": {s["name"] for s in muted},
                    "until": time.monotonic() + MUTED_APPS_GUARD_SECONDS,
                }

        if paused:
            self._refresh_media_players()

    def _resume_media_after_recording(self):
        """Rimette com'era quello che _pause_media_for_recording aveva
        fermato o silenziato. Prima l'audio, poi i player: al contrario si
        sentirebbe l'attacco del video prima che l'audio torni su.

        Quello che l'utente ha nel frattempo fatto ripartire da solo non
        viene toccato due volte: Play su un player gia' in riproduzione non
        ha effetto."""
        muted, self._muted_for_recording = self._muted_for_recording, []
        if muted:
            current = {s["id"]: s for s in self.backend.list_audio_streams()}
            names = {s["name"] for s in muted}
            for stream in muted:
                if stream["id"] in current:
                    self.backend.set_audio_stream_muted(stream["id"], False)
            # il flusso puo' essere sparito mentre si dettava (video finito,
            # scheda chiusa) e il browser averne aperto un altro: senza
            # questo secondo passaggio resterebbe muto proprio quello nuovo
            for stream in current.values():
                if stream["muted"] and stream["name"] in names:
                    self.backend.set_audio_stream_muted(stream["id"], False)

        paused, self._paused_for_recording = self._paused_for_recording, []
        if not paused:
            return
        for player_id in paused:
            self.backend.media_player_play(player_id)
        self._refresh_media_players()

    # --- socket di controllo locale (solo localhost, per il server MCP) ---
    # Protocollo request/response: un JSON per connessione, una risposta e
    # chiusura. Non richiede token: e' in ascolto solo su 127.0.0.1, quindi
    # non raggiungibile da altri dispositivi in rete (ma, a differenza di un
    # socket unix con permessi 0600, resta raggiungibile da altri utenti
    # sulla stessa macchina).

    def _start_control_server(self):
        self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control_sock.bind((LOCAL_HOST, CONTROL_SOCKET_PORT))
        self.control_sock.listen(4)
        t = threading.Thread(target=self._serve_control_forever, daemon=True)
        t.start()

    def _serve_control_forever(self):
        while True:
            try:
                conn, _ = self.control_sock.accept()
            except OSError:
                return
            t = threading.Thread(
                target=self._handle_control_client, args=(conn,), daemon=True
            )
            t.start()

    def _handle_control_client(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                data += chunk
            if not data.strip():
                return
            msg = json.loads(data.decode("utf-8"))
            cmd = msg.get("cmd")
            if cmd == "list_layout":
                reply = {"ok": True, "layout": self._layout_snapshot()}
            elif cmd == "get_config":
                reply = {"ok": True, "config": self._config_snapshot()}
            elif cmd == "restart_daemon":
                reply = {"ok": True}
                self._request_restart()
            elif cmd == "list_apps":
                reply = {"ok": True, "apps": self._list_apps_cached()}
            elif cmd == "launch_app":
                reply = self._handle_launch_app(msg.get("id"))
            elif cmd in CONFIG_CMDS:
                ok, error = self._handle_config_cmd(cmd, msg)
                reply = {"ok": ok}
                if error:
                    reply["error"] = error
                else:
                    reply["config"] = self._config_snapshot()
            elif cmd in LAYOUT_MUTATION_CMDS:
                ok, error = self._mutate_layout(cmd, msg)
                reply = {"ok": ok}
                if error:
                    reply["error"] = error
                else:
                    reply["layout"] = self._layout_snapshot()
            else:
                reply = {"ok": False, "error": f"comando sconosciuto: {cmd}"}
            conn.sendall(json.dumps(reply).encode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            try:
                conn.sendall(json.dumps({"ok": False, "error": str(exc)}).encode())
            except OSError:
                pass
        finally:
            conn.close()

    def _handle_launch_app(self, app_id):
        """Avvia l'applicazione `app_id` (vedi Backend.launch_app),
        validandola prima contro l'elenco aggiornato di Backend.list_apps():
        l'MCP deve sempre selezionare un id da li', mai inventarne uno o
        riusarne uno di una chiamata precedente (l'app potrebbe nel
        frattempo essere stata disinstallata, o l'id potrebbe non esistere
        affatto)."""
        if not app_id:
            return {"ok": False, "error": "id mancante"}
        if self._find_app(app_id) is None:
            return {
                "ok": False,
                "error": "applicazione non trovata (richiama list_apps per un elenco aggiornato)",
            }
        ok = self.backend.launch_app(app_id)
        if not ok:
            return {"ok": False, "error": "avvio dell'applicazione non riuscito"}
        return {"ok": True}

    # --- registrazione / trascrizione ---

    def toggle_recording(self, mode="paste", dashboard_id=None, phase="tap"):
        if self.state == STATE_IDLE:
            self._recording_mode = mode
            self._recording_dashboard_id = dashboard_id
            self._start_recording(phase=phase)
        elif self.state == STATE_RECORDING:
            self._stop_recording_and_transcribe()
        # se sta caricando, trascrivendo o elaborando un comando IA, ignora
        # il toggle

    def _vocabulary_for_dashboard(self, dashboard_id):
        """Contesto da passare a Whisper come `initial_prompt`: il
        vocabolario globale piu' quello della dashboard da cui parte la
        dettatura (termini specifici dell'app a cui e' dedicata). Whisper lo
        interpreta come il testo immediatamente precedente all'audio, quindi
        va formulato come una frase e non come un elenco di parole sciolte:
        e' il motivo del prefisso qui sotto."""
        parts = []
        if self.vocabulary.strip():
            parts.append(self.vocabulary.strip())
        with self._layout_lock:
            dashboard = self._find_dashboard(dashboard_id)
            if dashboard is not None:
                specific = (dashboard.get("vocabulary") or "").strip()
                if specific:
                    parts.append(specific)
        if not parts:
            return ""
        return "Termini ricorrenti: " + "; ".join(parts) + "."

    def _start_recording(self, phase="tap"):
        # una nuova dettatura rende obsoleta un'eventuale scelta ancora
        # aperta sul telefono: si chiude senza eseguire nulla, invece di
        # lasciare due pannelli in competizione
        self._cancel_pending_choice(reason="superseded")
        self._cancel_pending_paste(reason="superseded")
        self._recording_vocabulary = self._vocabulary_for_dashboard(
            self._recording_dashboard_id
        )
        fd, path = tempfile.mkstemp(prefix="stenografa-", suffix=".wav")
        os.close(fd)
        self.record_file = path
        # cattura gli appunti attuali prima di sovrascriverli col testo
        # dettato, cosi' _on_transcription_done puo' ripristinarli dopo
        # l'incolla se l'utente ha attivato "Ripristina clipboard"
        self._clipboard_before = (
            self.backend.read_clipboard() if self.restore_clipboard else None
        )
        # prima di aprire il microfono, non dopo: l'audio di un video in
        # riproduzione finirebbe nella dettatura (vedi
        # pause_media_while_recording)
        self._pause_media_for_recording()
        self.backend.start_recording(path)
        # il modello si carica mentre l'utente parla, cosi' allo stop e'
        # gia' pronto e la trascrizione parte subito
        self.model.preload()
        self._set_state(STATE_RECORDING)
        self._cancel_recording_watchdog()
        if phase != "down":
            # "tap" (interruttore, sia da telefono che da scorciatoia da
            # tastiera): senza un rilascio garantito che la fermi, si arma
            # una rete di sicurezza. "down" (tieni premuto per parlare) non
            # ne ha bisogno: il rilascio ("up") ferma sempre la
            # registrazione da solo.
            timer = threading.Timer(
                RECORDING_MAX_DURATION_SECONDS, self._on_recording_timeout
            )
            timer.daemon = True
            timer.start()
            self._recording_watchdog_timer = timer

    def _cancel_recording_watchdog(self):
        if self._recording_watchdog_timer is not None:
            self._recording_watchdog_timer.cancel()
            self._recording_watchdog_timer = None

    def _on_recording_timeout(self):
        """Chiamato dal thread del timer quando la registrazione a
        interruttore supera RECORDING_MAX_DURATION_SECONDS: accoda la
        richiesta di stop invece di fermarla direttamente da qui, per
        restare nel thread principale come tutte le altre transizioni di
        stato (vedi il ciclo di dispatch di command_queue)."""
        self.command_queue.put(("recording_timeout",))

    def _on_recording_timeout_reached(self):
        """Gestisce lo scadere della rete di sicurezza (vedi
        _on_recording_timeout/RECORDING_MAX_DURATION_SECONDS), nel thread
        principale. Ricontrolla lo stato perche' tra l'accodamento e
        l'esecuzione la registrazione potrebbe gia' essere stata fermata
        normalmente (piccola finestra di corsa fra i due thread)."""
        if self.state != STATE_RECORDING:
            return
        self._notify(
            "Stenografa - registrazione fermata",
            "Limite di sicurezza di "
            f"{RECORDING_MAX_DURATION_SECONDS // 60} minuti raggiunto "
            '(attiva "Tieni premuto per parlare" per evitarlo)',
        )
        self._stop_recording_and_transcribe()

    def _stop_recording_and_transcribe(self):
        self._cancel_recording_watchdog()
        path = self.record_file
        self.record_file = None
        self.backend.stop_recording()

        self._set_state(
            STATE_TRANSCRIBING if self.model.is_loaded else STATE_LOADING
        )
        threading.Thread(
            target=self._transcribe_worker, args=(path,), daemon=True
        ).start()

    def _transcribe_worker(self, wav_path):
        text = None
        error = None
        # il task nativo "translate" di Whisper traduce direttamente in
        # inglese: si applica solo ai pulsanti "record" (non ha senso per
        # ai_command, che deve interpretare il testo nella lingua originale)
        # e solo quando target/engine lo richiedono davvero
        use_whisper_translate = (
            self._recording_mode == "paste"
            and self.translate_enabled
            and self.translate_target == "en"
            and self.translate_engine == "whisper"
        )
        try:
            if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 100:
                error = "Nessun audio registrato."
            else:
                text = self.model.transcribe(
                    wav_path,
                    task="translate" if use_whisper_translate else "transcribe",
                    initial_prompt=self._recording_vocabulary,
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)

        self.command_queue.put(("done", text, error))

    def _needs_llm_translation(self):
        """True se il testo dettato va tradotto tramite LM Studio (non con
        il task nativo di Whisper, gia' gestito in _transcribe_worker)."""
        return self.translate_enabled and not (
            self.translate_target == "en" and self.translate_engine == "whisper"
        )

    def _on_transcription_done(self, text, error):
        self._set_state(STATE_IDLE)
        mode = self._recording_mode
        self._recording_mode = "paste"
        dashboard_id = self._recording_dashboard_id
        self._recording_dashboard_id = None
        clipboard_before = self._clipboard_before
        self._clipboard_before = None
        if error:
            self._notify("Stenografa - errore", error, urgency="critical")
            self._broadcast({"type": "result", "text": None, "error": error})
            return
        if not text:
            self._notify("Stenografa", "Nessun testo rilevato.")
            self._broadcast(
                {"type": "result", "text": None, "error": "Nessun testo rilevato."}
            )
            return
        if mode == "ai_command":
            # l'interpretazione LLM e' una chiamata di rete bloccante: gira
            # in un thread separato, il risultato torna sulla coda comandi
            # (vedi _ai_command_worker/_on_ai_command_done) per restare
            # serializzato col resto delle transizioni di stato
            self._set_state(STATE_THINKING)
            threading.Thread(
                target=self._ai_command_worker,
                args=(text, dashboard_id),
                daemon=True,
            ).start()
            return
        if self._needs_llm_translation():
            # stessa logica dell'ai_command: chiamata di rete bloccante in
            # un thread separato, il risultato torna sulla coda comandi
            # (vedi _translate_worker/_on_translate_done)
            self._set_state(STATE_THINKING)
            threading.Thread(
                target=self._translate_worker,
                args=(text, self.translate_target, clipboard_before),
                daemon=True,
            ).start()
            return
        self._deliver_text(text, clipboard_before)

    def _deliver_text(self, text, clipboard_before):
        """Ultimo passaggio del testo dettato (gia' eventualmente tradotto):
        lo incolla subito, oppure — se "conferma prima di incollare" e'
        attiva — lo propone al telefono e aspetta l'approvazione."""
        if self.confirm_before_paste:
            self._request_paste_confirmation(text, clipboard_before)
            return
        self._paste_text(text, clipboard_before)

    # --- conferma dell'incolla (config "confirm_before_paste") ---

    def _request_paste_confirmation(self, text, clipboard_before):
        request_id = secrets.token_hex(8)
        self._pending_paste = {
            "id": request_id,
            "text": text,
            "clipboard_before": clipboard_before,
            "expires_at": time.monotonic() + PASTE_CONFIRM_TIMEOUT,
        }
        self._notify(
            "Stenografa - conferma richiesta",
            f"Rivedi il testo sul telefono prima dell'incolla:\n"
            f"{text if len(text) <= 200 else text[:200] + '...'}",
        )
        self._broadcast(
            {
                "type": "confirm_paste",
                "request_id": request_id,
                "text": text,
                "timeout": PASTE_CONFIRM_TIMEOUT,
            }
        )

    def _on_paste_confirmed(self, request_id, text):
        """Il telefono ha approvato l'incolla, eventualmente dopo aver
        corretto il testo (`text`); se non lo manda si incolla quello
        originale."""
        pending = self._pending_paste
        if pending is None or pending["id"] != request_id:
            return
        self._pending_paste = None
        self._broadcast(
            {"type": "confirm_paste_closed", "request_id": request_id,
             "reason": "confirmed"}
        )
        final_text = pending["text"]
        if isinstance(text, str) and text.strip():
            final_text = text[:TEXT_BUTTON_MAX_LENGTH]
        self._paste_text(final_text, pending["clipboard_before"])

    def _cancel_pending_paste(self, request_id=None, reason="cancelled"):
        """Chiude la conferma in sospeso senza incollare. Il testo resta
        comunque nello storico (vedi _history_add): annullare l'incolla non
        deve voler dire perdere la dettatura."""
        pending = self._pending_paste
        if pending is None:
            return
        if request_id is not None and pending["id"] != request_id:
            return
        self._pending_paste = None
        self._history_add(pending["text"], pasted=False)
        self._broadcast(
            {
                "type": "confirm_paste_closed",
                "request_id": pending["id"],
                "reason": reason,
            }
        )

    def _expire_pending_paste(self):
        pending = self._pending_paste
        if pending is not None and time.monotonic() >= pending["expires_at"]:
            self._cancel_pending_paste(pending["id"], reason="timeout")

    # --- storico delle dettature (solo in memoria) ---

    def _history_add(self, text, pasted):
        if not text:
            return
        entry = {
            "id": secrets.token_hex(6),
            "text": text,
            "preview": (
                text
                if len(text) <= HISTORY_PREVIEW_LENGTH
                else text[:HISTORY_PREVIEW_LENGTH] + "..."
            ),
            "pasted": pasted,
            "at": time.time(),
        }
        with self._history_lock:
            self._history.insert(0, entry)
            del self._history[HISTORY_MAX_ENTRIES:]
        self._broadcast({"type": "history", "items": self._history_snapshot()})

    def _history_snapshot(self):
        """Copia dello storico senza il testo integrale: l'elenco sul
        telefono mostra solo l'anteprima, e il re-incolla avviene per id
        (vedi _paste_history_entry), quindi il testo intero non ha motivo di
        viaggiare in rete piu' volte."""
        with self._history_lock:
            return [
                {
                    "id": e["id"],
                    "preview": e["preview"],
                    "pasted": e["pasted"],
                    "at": e["at"],
                }
                for e in self._history
            ]

    def _paste_history_entry(self, entry_id):
        """Re-incolla una dettatura passata scelta sul telefono. Accetta solo
        un id gia' presente nello storico: il telefono puo' scegliere fra
        testi che il demone ha gia' prodotto, non farne incollare di
        arbitrari."""
        with self._history_lock:
            entry = next((e for e in self._history if e["id"] == entry_id), None)
        if entry is None:
            return
        clipboard_before = (
            self.backend.read_clipboard() if self.restore_clipboard else None
        )
        self._paste_text(entry["text"], clipboard_before, remember=False)

    def _paste_last_dictation(self):
        """Re-incolla l'ultima dettatura (pulsante kind="paste_last"): serve
        quando il testo era finito nel posto sbagliato perche' il cursore
        non era dove doveva, cosi' basta rimetterlo a posto e toccare il
        pulsante invece di ridettare tutto."""
        with self._history_lock:
            entry = self._history[0] if self._history else None
        if entry is None:
            self._notify(
                "Stenografa - niente da incollare",
                "Nessuna dettatura in memoria: registrane una prima.",
            )
            self._broadcast(
                {
                    "type": "result",
                    "kind": "paste_last",
                    "text": None,
                    "error": "Nessuna dettatura in memoria.",
                }
            )
            return
        clipboard_before = (
            self.backend.read_clipboard() if self.restore_clipboard else None
        )
        # remember=False: e' lo stesso testo gia' nello storico, non una
        # nuova dettatura, e non deve comparire due volte nell'elenco
        self._paste_text(entry["text"], clipboard_before, remember=False)

    def _paste_combo(self):
        """Combinazione di tasti da usare per incollare: "ctrl+v", oppure
        "ctrl+shift+v" se la finestra col focus sembra un emulatore di
        terminale (vedi TERMINAL_WM_CLASS_KEYWORDS). Se il rilevamento della
        finestra attiva non e' disponibile (nessuna estensione GNOME Shell,
        errore, backend che non lo supporta), ricade su "ctrl+v"."""
        focused = self.backend.get_focused_window()
        if focused is None:
            return "ctrl+v"
        _, wm_class, _title = focused
        wm_class_l = (wm_class or "").lower()
        if any(keyword in wm_class_l for keyword in TERMINAL_WM_CLASS_KEYWORDS):
            return "ctrl+shift+v"
        return "ctrl+v"

    def _paste_text(self, text, clipboard_before, remember=True):
        """Copia `text` negli appunti e simula l'incolla: usato sia per il
        testo dettato cosi' com'e' sia, dopo la traduzione via LLM, per il
        testo gia' tradotto (vedi _on_translate_done).

        `remember=False` per il testo che nello storico c'e' gia' o non ci
        deve entrare: il re-incolla di una voce passata e gli snippet fissi
        dei pulsanti kind="text", che non sono dettature."""
        self.backend.copy_to_clipboard(text)
        # piccola pausa difensiva: garantisce che l'offerta clipboard sia
        # gia' registrata dal compositor prima di simulare il paste
        time.sleep(0.1)
        combo = self._paste_combo()
        pasted = self.backend.simulate_keys(combo)
        preview = text if len(text) <= 200 else text[:200] + "..."
        if pasted:
            # nessuna notifica quando l'incolla riesce: il testo e' gia'
            # comparso nella finestra col focus, la notifica sarebbe solo
            # un doppione che copre l'interfaccia. Restano notificati i
            # casi in cui l'utente non vede nulla (incolla fallito, errori)
            # ripristina gli appunti precedenti solo se l'incolla e'
            # riuscito: se fosse fallito il testo dettato deve restare negli
            # appunti come alternativa manuale (Ctrl+V dell'utente)
            if self.restore_clipboard and clipboard_before is not None:
                time.sleep(0.15)
                self.backend.copy_to_clipboard(clipboard_before)
        else:
            manual_hint = "Ctrl+Shift+V" if combo == "ctrl+shift+v" else "Ctrl+V"
            self._notify(
                "Stenografa - copiato negli appunti",
                preview + f"\n(incolla automatico non riuscito, usa {manual_hint})",
            )
        if remember:
            self._history_add(text, pasted=pasted)
        self._broadcast(
            {"type": "result", "text": text, "error": None, "pasted": pasted}
        )

    def _llm_translate(self, text, target_language):
        """Traduce `text` in `target_language` (codice ISO 639-1) tramite il
        backend LLM configurato (vedi llm_provider). Chiamata di rete
        bloccante: va eseguita in un thread separato (vedi
        _translate_worker). Ritorna (testo tradotto, errore): solo uno dei
        due e' valorizzato."""
        target_name = SUPPORTED_LANGUAGES.get(target_language, target_language)
        system_prompt = (
            f"Traduci il testo dell'utente in {target_name}. Rispondi SOLO "
            "con la traduzione, senza spiegazioni, commenti, virgolette o "
            "altro testo aggiuntivo. Se il testo e' gia' in quella lingua, "
            "restituiscilo invariato."
        )
        raw, error = self._llm_complete(
            system_prompt, text, max_tokens=1000, timeout=30
        )
        if error:
            return None, error
        translated = raw.strip()
        if not translated:
            return None, "traduzione vuota"
        return translated, None

    def _translate_worker(self, text, target_language, clipboard_before):
        translated, error = self._llm_translate(text, target_language)
        self.command_queue.put(
            ("translate_done", translated, error, clipboard_before)
        )

    def _on_translate_done(self, translated, error, clipboard_before):
        self._set_state(STATE_IDLE)
        if error:
            self._notify("Stenografa - errore traduzione", error, urgency="critical")
            self._broadcast({"type": "result", "text": None, "error": error})
            return
        self._deliver_text(translated, clipboard_before)

    # --- pulsante "comando vocale IA" (kind="ai_command") ---

    def _llm_complete(self, system, user, max_tokens=600, timeout=30):
        """Unico punto da cui il demone parla con un LLM: il backend vero
        (LM Studio, Ollama, OpenAI, API Claude o CLI Claude Code) e' scelto
        dalla configurazione, vedi llm_provider e setup_llm.py. Ritorna
        (testo, errore): solo uno dei due e' valorizzato.

        La configurazione viene riletta ad ogni chiamata, cosi' cambiare
        backend con setup_llm.py ha effetto subito senza riavviare il
        demone; il costo e' la lettura di un file piccolo, trascurabile
        rispetto alla chiamata di rete che segue."""
        try:
            provider = llm_provider.get_provider()
        except Exception as exc:
            return None, f"backend LLM non configurabile: {exc}"
        return provider.complete(system, user, max_tokens=max_tokens, timeout=timeout)

    def _dashboard_name(self, dashboard_id):
        """Nome della dashboard, o None se non esiste (piu') — usato per
        vincolare la generazione di macro (vedi AI_COMMAND_MACRO_PROMPT_
        TEMPLATE) all'applicazione della dashboard da cui e' partito il
        comando vocale, indipendentemente dal fatto che quella dashboard
        abbia gia' scorciatoie configurate o no."""
        with self._layout_lock:
            dashboard = self._find_dashboard(dashboard_id)
            return dashboard["name"] if dashboard else None

    def _dashboard_shortcuts_context(self, dashboard_id):
        """Testo da aggiungere al prompt per dare priorita' alle scorciatoie
        (kind="keys") gia' configurate nella dashboard `dashboard_id`, PIU'
        il "vocabolario" di scorciatoie salvate ma non mostrate come
        pulsanti (vedi _set_dashboard_shortcuts_locked) — es. se l'utente
        e' sulla dashboard "Invoke AI" e dice "invoca", una scorciatoia
        "Invoca" li' configurata (visibile o no) deve avere la precedenza
        su un'interpretazione generica. Stringa vuota se la dashboard non
        esiste piu' o non ha scorciatoie."""
        with self._layout_lock:
            dashboard = self._find_dashboard(dashboard_id)
            if dashboard is None:
                return ""
            name = dashboard["name"]
            shortcuts = [
                (b["label"], b["combo"])
                for b in dashboard["buttons"]
                if b["kind"] == "keys"
            ]
            shortcuts += [
                (s["label"], s["combo"]) for s in dashboard.get("shortcuts", [])
            ]
        if not shortcuts:
            return ""
        elenco = "\n".join(f'- "{label}" -> {combo}' for label, combo in shortcuts)
        return (
            f'\n\nL\'utente si trova sulla dashboard "{name}", che contiene '
            "queste scorciatoie gia' configurate (etichetta -> combinazione "
            f"di tasti):\n{elenco}\nSe il comando dettato corrisponde, anche "
            "solo per significato o sinonimia (non serve un'uguaglianza "
            "testuale esatta), a una di queste etichette, rispondi "
            "ESATTAMENTE con la combinazione di tasti associata a quella "
            "scorciatoia: ha priorita' su qualunque altra interpretazione "
            "generica del comando."
        )

    def _parse_shortcut_candidates(self, raw, allow_macro=False):
        """Estrae dalla risposta dell'LLM la lista di candidati
        {"label", "combo"} (o {"label", "combos"} per una macro, solo se
        `allow_macro`, vedi _interpret_as_shortcuts). Volutamente
        tollerante: scarta i singoli elementi non validi invece di
        rifiutare l'intera risposta (con piu' candidati, perdere quello
        malformato e' molto meglio che perdere anche quelli buoni) e
        accetta anche la vecchia risposta a combinazione singola
        ("ctrl+c"), che i modelli piu' piccoli producono ancora nonostante
        il prompt chieda un array JSON."""
        raw = (raw or "").strip()
        if raw.startswith("```"):
            # alcuni modelli avvolgono il JSON in un blocco markdown
            # nonostante le istruzioni contrarie: lo ripulisco
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            combo = raw.strip("`\"'. ").lower().replace(" ", "")
            if combo and combo != "none" and is_valid_combo(combo):
                return [{"label": combo, "combo": combo}]
            return []
        if not isinstance(parsed, list):
            return []

        candidates = []
        seen = set()
        for i, spec in enumerate(parsed):
            if isinstance(spec, dict) and spec.get("app"):
                # richiesta di aprire un'applicazione: il modello indica solo
                # un nome, la risoluzione all'id vero avviene qui contro
                # l'elenco delle app installate (vedi _resolve_app_candidates)
                entries = self._resolve_app_candidates(spec.get("app"), spec.get("label"))
            elif allow_macro and isinstance(spec, dict) and spec.get("combos"):
                entries = self._resolve_macro_candidate(spec)
            else:
                entry, error = self._validate_shortcut_spec(spec, i)
                entries = [] if error else [entry]
            for entry in entries:
                if entry.get("combos"):
                    key = "macro:" + ">".join(entry["combos"])
                else:
                    key = entry.get("combo") or f"app:{entry.get('app_id')}"
                if key in seen:
                    # duplicati: il modello a volte propone la stessa
                    # scorciatoia con due etichette diverse, che come "scelta"
                    # non offrirebbe nulla all'utente
                    continue
                seen.add(key)
                candidates.append(entry)
                if len(candidates) >= AI_COMMAND_MAX_OPTIONS:
                    return candidates
        return candidates

    def _resolve_app_candidates(self, name, label=None):
        """Traduce il nome di applicazione proposto dall'LLM ("gimp") negli
        id veri delle applicazioni installate. Il modello non riceve mai
        l'elenco completo (centinaia di voci) ne' puo' inventare un id: puo'
        solo nominare un'app, e il demone decide se e a cosa corrisponde.
        Piu' corrispondenze diventano opzioni fra cui l'utente sceglie sul
        telefono, con la stessa logica dei comandi ambigui."""
        if not isinstance(name, str) or not name.strip():
            return []
        needle = name.strip().lower()
        exact, partial = [], []
        for app in self._list_apps_cached():
            app_name = app.get("name") or ""
            lowered = app_name.lower()
            if lowered == needle:
                exact.append(app)
            elif needle in lowered:
                partial.append(app)
        # una corrispondenza esatta rende superflue quelle parziali: se esiste
        # un'app che si chiama esattamente "Steam" non ha senso proporre anche
        # "Steam Tinker Launch"
        matches = exact or partial
        return [
            {
                "label": (
                    str(label).strip()
                    if label and len(matches) == 1
                    else f"Apri {app.get('name')}"
                ),
                "app_id": app["id"],
                "app_name": app.get("name", ""),
            }
            for app in matches[:AI_COMMAND_MAX_OPTIONS]
        ]

    def _resolve_macro_candidate(self, spec):
        """Valida una macro proposta dal comando vocale IA (campo "combos"
        invece di "combo", vedi AI_COMMAND_MACRO_PROMPT_TEMPLATE): stessa
        validazione dei passi di un pulsante kind="macro"
        (_validate_macro_spec). Scartata singolarmente se non valida (label
        mancante, combos vuoto/malformato, ...), non l'intera risposta —
        stessa tolleranza degli altri candidati."""
        label = str(spec.get("label") or "").strip()
        if not label:
            return []
        combos, delay_ms, error = self._validate_macro_spec(spec)
        if error:
            return []
        return [{"label": label, "combos": combos, "delay_ms": delay_ms}]

    def _execute_candidate(self, candidate):
        """Esegue un'opzione proposta dal comando vocale IA: una
        combinazione di tasti, una macro (piu' combinazioni in sequenza)
        oppure l'avvio di un'applicazione. Ritorna (descrizione eseguita,
        errore)."""
        app_id = candidate.get("app_id")
        if app_id:
            result = self._handle_launch_app(app_id)
            description = candidate.get("app_name") or app_id
            if not result.get("ok"):
                return description, result.get("error", "avvio non riuscito")
            return description, None
        combos = candidate.get("combos")
        if combos:
            description = " -> ".join(combos)
            delay_ms = candidate.get("delay_ms", MACRO_DEFAULT_DELAY_MS)
            failed_at = self._execute_macro_steps(combos, delay_ms)
            if failed_at is not None:
                return description, (
                    f"macro interrotta al passo {failed_at + 1} "
                    f"({combos[failed_at]})"
                )
            return description, None
        combo = candidate.get("combo")
        if not self.backend.simulate_keys(combo):
            return combo, "simulazione della combinazione di tasti non riuscita"
        return combo, None

    def _execute_macro_steps(self, combos, delay_ms):
        """Esegue in sequenza le combinazioni di una macro generata dal
        comando vocale IA, bloccando il chiamante (gia' nel thread di
        _ai_command_worker, non nel thread principale). A differenza di
        _run_macro (usata dai pulsanti kind="macro": lanciata nel suo
        thread e notifica da sola in caso di fallimento) qui e' il
        chiamante a costruire subito il messaggio di risultato, quindi
        serve sapere l'esito invece che limitarsi a notificarlo. Ritorna
        l'indice del primo passo fallito, o None se sono andati tutti a
        buon fine."""
        delay = max(0, delay_ms) / 1000.0
        for i, combo in enumerate(combos):
            if i:
                time.sleep(delay)
            if not self.backend.simulate_keys(combo):
                return i
        return None

    def _interpret_as_shortcuts(self, text, dashboard_id=None):
        """Chiede al backend LLM configurato (vedi llm_provider) di tradurre
        `text` (la frase dettata) in una o piu' combinazioni plausibili. Se
        `dashboard_id` e' indicato, da' priorita' alle scorciatoie gia'
        configurate in quella dashboard (vedi _dashboard_shortcuts_context).
        Chiamata di rete bloccante: va eseguita in un thread separato (vedi
        _ai_command_worker). Ritorna (candidati, errore): solo uno dei due
        e' valorizzato. Un solo candidato = comando chiaro, si esegue
        subito; piu' candidati = comando ambiguo, sceglie l'utente."""
        # il nome della dashboard abilita la generazione di macro
        # (AI_COMMAND_MACRO_PROMPT_TEMPLATE) e la vincola a quell'app: senza
        # una dashboard nota non si generano macro, solo singole
        # combinazioni o avvii di applicazioni (vedi _dashboard_name)
        dashboard_name = self._dashboard_name(dashboard_id) if dashboard_id else None
        system_prompt = AI_COMMAND_SYSTEM_PROMPT
        if dashboard_name:
            system_prompt += AI_COMMAND_MACRO_PROMPT_TEMPLATE.format(name=dashboard_name)
        if dashboard_id:
            system_prompt += self._dashboard_shortcuts_context(dashboard_id)
        # un array JSON di candidati (specie con macro multi-passo) non sta
        # nei 20 token che bastavano alla vecchia risposta a combo singola
        reply, error = self._llm_complete(
            system_prompt, text, max_tokens=600, timeout=30
        )
        if error:
            return None, error

        candidates = self._parse_shortcut_candidates(reply, allow_macro=bool(dashboard_name))
        if not candidates:
            return None, f"comando vocale non riconosciuto: \"{text}\""
        return candidates, None

    def _ai_command_worker(self, text, dashboard_id=None):
        candidates, error = self._interpret_as_shortcuts(text, dashboard_id)
        if error:
            self.command_queue.put(("ai_done", text, None, error))
            return
        if len(candidates) > 1:
            # comando ambiguo: non si esegue nulla e si passa la scelta
            # all'utente (vedi _on_ai_choice_needed). Meglio un tocco in piu'
            # che eseguire a caso una scorciatoia potenzialmente distruttiva.
            self.command_queue.put(("ai_choice", text, candidates))
            return
        executed, error = self._execute_candidate(candidates[0])
        self.command_queue.put(("ai_done", text, executed, error))

    def _on_ai_command_done(self, text, executed, error):
        """`executed` descrive cio' che e' stato eseguito: una combinazione
        di tasti ("ctrl+c") o il nome dell'applicazione avviata. Viaggia
        verso il telefono nel campo "combo" del protocollo, invariato per
        compatibilita' con le versioni precedenti dell'app."""
        self._set_state(STATE_IDLE)
        if error:
            self._notify("Stenografa - comando AI", error, urgency="critical")
        else:
            self._notify("Stenografa - comando eseguito", f'"{text}" -> {executed}')
        self._broadcast(
            {
                "type": "result",
                "kind": "ai_command",
                "text": text,
                "combo": executed,
                "error": error,
            }
        )

    # --- disambiguazione di un comando vocale ambiguo ---
    # La scelta e' deliberatamente effimera: vive in memoria per qualche
    # decina di secondi e non tocca il layout su disco. Il telefono la
    # mostra come pannello sopra la dashboard corrente, quindi non c'e'
    # nessuna "dashboard originale" da ripristinare dopo la scelta.

    def _on_ai_choice_needed(self, text, candidates):
        """Il comando dettato ha piu' interpretazioni plausibili: le propone
        al telefono e aspetta. Nessuna combinazione viene eseguita finche'
        l'utente non sceglie."""
        self._set_state(STATE_IDLE)
        request_id = secrets.token_hex(8)
        self._pending_choice = {
            "id": request_id,
            "text": text,
            "options": candidates,
            "expires_at": time.monotonic() + AI_COMMAND_CHOICE_TIMEOUT,
        }
        self._notify(
            "Stenografa - comando ambiguo",
            f'"{text}": scegli sul telefono fra {len(candidates)} opzioni',
        )
        self._broadcast(
            {
                "type": "choose_shortcut",
                "request_id": request_id,
                "text": text,
                "options": candidates,
                "timeout": AI_COMMAND_CHOICE_TIMEOUT,
            }
        )

    def _on_ai_choice_reply(self, request_id, combo, app_id=None, combos=None):
        """Esegue l'opzione scelta dall'utente sul telefono: una
        combinazione di tasti, una macro o l'avvio di un'applicazione."""
        pending = self._pending_choice
        # request_id non combaciante: scelta gia' risolta, scaduta, o
        # arrivata da un telefono che aveva ancora aperto un pannello
        # vecchio. Si ignora: eseguire una combinazione che l'utente non sta
        # piu' guardando sarebbe peggio che non fare nulla.
        if pending is None or pending["id"] != request_id:
            return
        # si accetta solo una delle opzioni proposte: il pannello non e' una
        # via per far eseguire al demone combinazioni, macro o avvii
        # arbitrari
        chosen = next(
            (
                o
                for o in pending["options"]
                if (app_id and o.get("app_id") == app_id)
                or (combos and o.get("combos") == combos)
                or (
                    not app_id
                    and not combos
                    and combo
                    and o.get("combo") == combo
                )
            ),
            None,
        )
        if chosen is None:
            return
        self._pending_choice = None
        self._broadcast(
            {"type": "choose_shortcut_closed", "request_id": request_id,
             "reason": "chosen"}
        )
        executed, error = self._execute_candidate(chosen)
        self._on_ai_command_done(pending["text"], executed, error)

    def _cancel_pending_choice(self, request_id=None, reason="cancelled"):
        """Chiude la scelta in sospeso senza eseguire nulla. Con
        request_id=None chiude qualunque scelta pendente (usato quando parte
        una nuova registrazione, che rende obsoleta la precedente)."""
        pending = self._pending_choice
        if pending is None:
            return
        if request_id is not None and pending["id"] != request_id:
            return
        self._pending_choice = None
        self._broadcast(
            {
                "type": "choose_shortcut_closed",
                "request_id": pending["id"],
                "reason": reason,
            }
        )

    def _expire_pending_choice(self):
        pending = self._pending_choice
        if pending is not None and time.monotonic() >= pending["expires_at"]:
            self._cancel_pending_choice(pending["id"], reason="timeout")

    # --- cleanup ---

    def _on_signal(self, signum, frame):
        self._running = False
        # sveglia subito il ciclo principale se e' in attesa sulla coda
        self.command_queue.put(("quit",))

    def _request_restart(self):
        """Chiesto dal telefono o da MCP (comando 'restart_daemon'): ferma
        il ciclo principale esattamente come 'quit', ma run() rieseguira'
        il processo invece di lasciarlo terminare (vedi sotto)."""
        self._restart_requested = True
        self._running = False
        self.command_queue.put(("quit",))

    def run(self):
        try:
            self._main_loop()
        finally:
            # se il demone si ferma nel bel mezzo di una dettatura, l'audio
            # silenziato resterebbe muto senza che si capisca perche'
            try:
                self._resume_media_after_recording()
            except Exception:
                pass
            try:
                self.server_sock.close()
            except Exception:
                pass
            try:
                self.net_server_sock.close()
            except Exception:
                pass
            try:
                self.control_sock.close()
            except Exception:
                pass
            self.backend.shutdown()
        if self._restart_requested:
            # rilascia il lock di istanza singola prima di rieseguire,
            # altrimenti il nuovo processo (stesso PID, execv non ne crea
            # uno nuovo) lo troverebbe gia' occupato da se stesso e
            # si rifiuterebbe di ripartire
            if self.lock_handle is not None:
                try:
                    self.lock_handle.close()
                except OSError:
                    pass
            os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    _lock_handle = _acquire_single_instance_lock(LOCK_PATH)
    if _lock_handle is None:
        print(
            "Stenografa e' gia' in esecuzione (lock occupato): esco per "
            "evitare due demoni in conflitto sulla stessa porta/socket.",
            file=sys.stderr,
        )
        sys.exit(1)
    app = Stenografa()
    app.lock_handle = _lock_handle
    app.run()

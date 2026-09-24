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
import array
import base64
import gc
import json
import math
import os
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import signal
import unicodedata
import wave
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
    "set_notifications": ("level", "_set_notifications"),
    "set_wake_word_enabled": ("enabled", "_set_wake_word_enabled"),
    "set_wake_phrase_start": ("phrase", "_set_wake_phrase_start"),
    "set_wake_phrase_stop": ("phrase", "_set_wake_phrase_stop"),
    "set_silence_timeout": ("seconds", "_set_silence_timeout"),
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


# --- quante notifiche di sistema mandare (vedi Stenografa._notify) ---
# "all": tutte, com'e' sempre stato; "errors": solo quelle critiche (errori
# di trascrizione, di rete, di traduzione), che segnalano qualcosa da
# sistemare; "none": nessuna, per chi trova invadenti anche quelle. Gli
# avvisi informativi ("Nessun testo rilevato", conferme, comandi eseguiti)
# arrivano comunque all'app telefono via _broadcast, quindi silenziarli non
# nasconde nulla che non sia visibile altrove.
NOTIFICATION_LEVELS = ("all", "errors", "none")


def _valid_notifications(level):
    return isinstance(level, str) and level.strip().lower() in NOTIFICATION_LEVELS


def _load_notifications():
    value = _read_config().get("notifications")
    if _valid_notifications(value):
        return value.strip().lower()
    return "all"


def _save_notifications(level):
    _write_config_key("notifications", level)


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
# Ripiego quando la GPU non e' utilizzabile: scheda assente, driver mancanti,
# oppure — il caso piu' comune — VRAM gia' occupata da altro (un gioco, un
# altro modello). Senza, la dettatura fallisce e basta.
#
# Stesso modello, cosi' la qualita' non cambia e non c'e' niente da scaricare:
# si paga in velocita'. Misure su questa macchina, con 4,5 secondi di audio:
# medium in int8 su CPU impiega circa 4,5 secondi (in pratica il tempo reale),
# small 1,8 e base 0,7 ma con errori evidenti gia' su una frase semplice. Per
# una rete di sicurezza conviene la fedelta': chi preferisse la velocita' puo'
# mettere "small" qui sotto, tenendo presente che va scaricato al primo uso.
MODEL_FALLBACK_NAME = MODEL_NAME
MODEL_FALLBACK_DEVICE = "cpu"
MODEL_FALLBACK_COMPUTE_TYPE = "int8"
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

# --- chiusura automatica sul silenzio ---------------------------------------
# Dopo questi secondi senza sentire parlare, la dettatura si chiude da sola e
# il testo raccolto fin li' viene trascritto e incollato. Serve soprattutto
# all'attivazione vocale, dove capita di dimenticarsi la frase di stop, ma vale
# per qualunque dettatura a interruttore.
#
# Non si applica al "tieni premuto per parlare": li' e' il rilascio del dito a
# chiudere, e una pausa mentre si pensa non deve interrompere niente.
SILENCE_TIMEOUT_DEFAULT = 10
# ogni quanto si controlla la coda della registrazione
SILENCE_CHECK_INTERVAL = 1.0
# quanta voce deve esserci nella finestra perche' la dettatura resti aperta.
# Si misura col rilevatore di voce di faster-whisper (Silero), non contando
# l'energia del segnale: distinguere una voce dal rumore guardando solo quanto
# e' forte non funziona in una stanza qualsiasi. Sulla macchina di sviluppo il
# rumore di fondo — ventole — arriva a picchi sette volte il suo stesso livello
# medio, e col criterio a energia veniva scambiato per parlato: la dettatura si
# chiudeva dopo una ventina di secondi invece di dieci, quando capitava una
# finestra piu' quieta. Col rilevatore di voce lo stesso rumore da zero.
SILENCE_MIN_SPEECH_SECONDS = 0.4
# tratti di voce piu' brevi di cosi' non contano: sono i clic e i colpi secchi
# che il rilevatore, da solo, prende ogni tanto per voce
SILENCE_VAD_MIN_SPEECH_MS = 250
# quanti tratti sopra il fondo indicano parlato, quando il rilevatore di voce
# non e' disponibile e si ripiega sull'energia (vedi _speech_seconds)
SILENCE_MIN_VOICED_BLOCKS = 3
# quanto puo' valere l'impostazione: sotto i 3 secondi una pausa per
# riprendere fiato basterebbe a chiudere la dettatura
SILENCE_TIMEOUT_MIN = 3
SILENCE_TIMEOUT_MAX = 120

# --- attivazione vocale ("wake word") ---------------------------------------
# L'utente puo' avviare e fermare la dettatura pronunciando due frasi scelte
# da lui (es. "Jarvis" / "Jarvis stop") invece di toccare il pulsante.
#
# Perche' non una libreria wake word dedicata (Porcupine, openWakeWord): quelle
# usano modelli addestrati su UNA parola fissa, mentre qui la frase deve poter
# essere cambiata dall'utente in qualsiasi momento. Si usa quindi un secondo
# whisper generico, piccolo, e un confronto testuale tollerante.
#
# Il modello dell'ascolto e' volutamente diverso da quello della dettatura:
# "tiny" su CPU in int8 non tocca la VRAM (quindi non interferisce con lo
# scarico automatico del "medium", vedi MODEL_IDLE_TIMEOUT) e trascrive la
# finestra di ascolto in una frazione di secondo.
WAKE_MODEL_NAME = "tiny"
WAKE_MODEL_DEVICE = "cpu"
WAKE_MODEL_COMPUTE_TYPE = "int8"
# quanti secondi di audio guardare a ogni giro e ogni quanto rifarlo: la
# finestra e' piu' lunga dell'intervallo cosi' una frase a cavallo di due
# cicli viene comunque vista intera almeno una volta
WAKE_WINDOW_SECONDS = 3.0
WAKE_POLL_INTERVAL = 1.0
# dopo un riconoscimento si ignora l'audio per un po': la stessa frase resta
# dentro la finestra per qualche secondo e verrebbe riconosciuta piu' volte
WAKE_COOLDOWN_SECONDS = 2.5
# Quando una finestra vale la pena di essere trascritta (vedi _is_silence).
#
# Non si usa una soglia fissa sul livello: quanto "forte" arrivi una voce
# dipende dal guadagno del microfono e da quanto si e' lontani, e varia di
# ordini di grandezza da un PC all'altro. Su questa macchina il fondo sta
# intorno a 75 e un suono riprodotto dagli altoparlanti arriva a 116: una
# soglia fissa tarata piu' in alto scarterebbe sistematicamente il parlato,
# una tarata piu' in basso farebbe girare il modello di continuo su un
# microfono piu' sensibile.
#
# Si guarda invece di quanto il tratto piu' sonoro supera il piu' silenzioso
# della stessa finestra: e' il salto fra silenzio e voce, e non dipende dal
# guadagno.
WAKE_SILENCE_RATIO = 2.5
# livello sotto il quale non si considera parlato in nessun caso: evita che in
# una stanza silenziosissima un fruscio qualsiasi, essendo comunque molte
# volte il fondo, faccia partire il modello a ogni giro
WAKE_SILENCE_FLOOR = 60
# livello oltre il quale la finestra si trascrive comunque, senza guardare il
# rapporto: se si parla senza pause per tutti e tre i secondi, il tratto piu'
# silenzioso e' forte quanto il piu' sonoro e il confronto direbbe "silenzio"
WAKE_SILENCE_LOUD = 800
# su che durata si misura il livello. Deve stare sotto la durata di una parola
# pronunciata, altrimenti si torna a mediare voce e silenzio.
WAKE_SILENCE_BLOCK_SECONDS = 0.2
# ogni quanto ricominciare da capo il file di ascolto: senza rotazione
# crescerebbe indefinitamente (~2 MB/minuto)
WAKE_ROTATE_SECONDS = 60
# quanto restare fermi dopo una rotazione: solo il tempo che il file nuovo
# abbia qualche campione dentro
WAKE_ROTATE_MUTE_SECONDS = 0.5
# Quanto puo' discostarsi la trascrizione dalla frase attesa, in frazione dei
# suoi caratteri: si contano le lettere da correggere (distanza di edit), non
# una percentuale di somiglianza.
#
# Il criterio precedente (rapporto di somiglianza) si e' rivelato inadatto:
# su parole corte una sola lettera in piu' fa crollare il rapporto — "jarvis"
# contro "giarvis" vale 0.77 — quindi per tollerare gli errori veri bisognava
# tenere la soglia cosi' bassa da far passare anche parole diverse. Contare le
# modifiche separa meglio i due casi: "giarvis" dista 2, "arrivi" dista 4.
WAKE_MATCH_EDIT_FRACTION = 0.25
# frasi piu' corte di cosi' (dopo la normalizzazione) verrebbero riconosciute
# ovunque, dentro parole comuni: si rifiutano in fase di configurazione
WAKE_PHRASE_MIN_CHARS = 3
# la frase puo' contenere piu' varianti separate da virgola (vedi
# _phrase_variants), quindi il limite e' sulla riga intera
WAKE_PHRASE_MAX_CHARS = 160
WAKE_PHRASE_START_DEFAULT = "jarvis"
WAKE_PHRASE_STOP_DEFAULT = "jarvis stop"


def _load_wake_word_enabled():
    value = _read_config().get("wake_word_enabled")
    # spento di default: tenere il microfono sempre aperto e' una scelta che
    # deve essere esplicita, non un comportamento che compare da solo
    return bool(value) if isinstance(value, bool) else False


def _save_wake_word_enabled(enabled):
    _write_config_key("wake_word_enabled", enabled)


def _load_wake_phrase_start():
    value = _read_config().get("wake_phrase_start")
    return value if isinstance(value, str) and value.strip() else (
        WAKE_PHRASE_START_DEFAULT
    )


def _save_wake_phrase_start(phrase):
    _write_config_key("wake_phrase_start", phrase)


def _load_wake_phrase_stop():
    value = _read_config().get("wake_phrase_stop")
    return value if isinstance(value, str) and value.strip() else (
        WAKE_PHRASE_STOP_DEFAULT
    )


def _save_wake_phrase_stop(phrase):
    _write_config_key("wake_phrase_stop", phrase)


def _load_silence_timeout():
    value = _read_config().get("silence_timeout")
    if not isinstance(value, int) or isinstance(value, bool):
        return SILENCE_TIMEOUT_DEFAULT
    # 0 = spento; fuori intervallo si torna al default invece di rifiutare,
    # cosi' un config.json modificato a mano non blocca la dettatura
    if value == 0:
        return 0
    if SILENCE_TIMEOUT_MIN <= value <= SILENCE_TIMEOUT_MAX:
        return value
    return SILENCE_TIMEOUT_DEFAULT


def _save_silence_timeout(seconds):
    _write_config_key("silence_timeout", seconds)


def _normalize_phrase(text):
    """Riduce il testo alla forma usata per il confronto: minuscole, senza
    accenti ne' punteggiatura, spazi singoli. Serve a far combaciare quello
    che l'utente ha scritto nelle impostazioni con quello che whisper
    trascrive, che differisce quasi sempre per maiuscole e virgole."""
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped)
    return " ".join(cleaned.split())


def _phrase_variants(phrase):
    """Le forme accettate per una frase di attivazione, separate da virgola.

    Serve perche' i riconoscitori vocali scrivono quello che sentono nella
    lingua di dettatura: "Jarvis" detto in italiano diventa "già visto",
    "ciarvis", "Charles". Sul PC si puo' correggere il tiro con
    l'initial_prompt (vedi _wake_prompt), ma il riconoscitore del telefono non
    accetta suggerimenti: li' l'unico rimedio e' che l'utente aggiunga come
    suona davvero la sua frase.

    La prima variante e' quella "ufficiale": e' la grafia che si vuole
    ottenere, e l'unica che finisce nel suggerimento al modello."""
    if not isinstance(phrase, str):
        return []
    variants = []
    for piece in phrase.split(","):
        normalized = _normalize_phrase(piece)
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def _edit_distance(a, b):
    """Quante lettere bisogna cambiare, togliere o aggiungere per passare da
    `a` a `b`."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,  # cancellazione
                    current[j - 1] + 1,  # inserimento
                    previous[j - 1] + (char_a != char_b),  # sostituzione
                )
            )
        previous = current
    return previous[-1]


def _max_edits(target):
    """Quanti errori si accettano su una frase lunga `len(target)`. Almeno uno,
    altrimenti le frasi corte non tollererebbero nulla."""
    return max(1, round(len(target) * WAKE_MATCH_EDIT_FRACTION))


def _phrase_in_text(phrase, text, only_tail=False):
    """True se `phrase` (o una delle sue varianti) compare in `text`,
    tollerando gli errori di trascrizione. Confronta la frase con ogni
    sequenza di parole di `text` lunga quanto la frase (piu' una parola in
    meno e una in piu', perche' il modello a volte fonde o spezza le parole).

    `only_tail` limita la ricerca alle ultime parole: serve quando si cerca la
    frase di stop dentro una dettatura in corso, dove trovarla in mezzo
    significherebbe interrompere l'utente a meta' di una frase."""
    haystack = _normalize_phrase(text)
    if not haystack:
        return False
    all_words = haystack.split()
    for target in _phrase_variants(phrase):
        span = len(target.split())
        if only_tail and len(all_words) > span + 2:
            words = all_words[-(span + 2) :]
        else:
            words = all_words
        if target in " ".join(words):
            return True
        allowed = _max_edits(target)
        for size in {max(1, span - 1), span, span + 1}:
            for start in range(0, max(0, len(words) - size) + 1):
                window = " ".join(words[start : start + size])
                # una finestra molto piu' lunga o corta non puo' rientrare nel
                # margine: si evita di calcolare la distanza per niente
                if abs(len(window) - len(target)) > allowed:
                    continue
                if _edit_distance(target, window) <= allowed:
                    return True
    return False


def _wake_prompt(start_phrase, stop_phrase):
    """Contesto da dare al modello dell'ascolto perche' scriva le frasi con la
    grafia scelta dall'utente invece di renderle in italiano. Solo la prima
    variante di ciascuna: le altre esistono proprio perche' sono le rese
    sbagliate, non vanno suggerite al modello."""
    parts = []
    for phrase in (start_phrase, stop_phrase):
        variants = _phrase_variants(phrase)
        if variants:
            # si riprende il testo originale della prima variante, non quello
            # normalizzato, cosi' il suggerimento resta scritto come l'utente
            # lo ha inserito
            first = phrase.split(",")[0].strip()
            parts.append(first if first else variants[0])
    return ". ".join(parts) + "." if parts else None


def _valid_wake_phrase(phrase):
    if not isinstance(phrase, str):
        return False
    if len(phrase.strip()) > WAKE_PHRASE_MAX_CHARS:
        return False
    variants = _phrase_variants(phrase)
    if not variants:
        return False
    # ogni variante deve reggersi da sola: una troppo corta farebbe scattare
    # l'attivazione dentro le parole di una conversazione normale
    return all(len(v) >= WAKE_PHRASE_MIN_CHARS for v in variants)


def _strip_wake_phrases(text, start_phrase, stop_phrase):
    """Toglie dal testo dettato le frasi di attivazione, che il modello della
    dettatura trascrive come tutto il resto: la frase di avvio finisce in testa
    (l'inizio della registrazione la cattura ancora), quella di stop in coda.
    Rimuove solo le occorrenze ai bordi: in mezzo al testo sono parole che
    l'utente ha dettato davvero.

    La frase di stop va tolta per prima: contenendo di solito quella di avvio
    ("jarvis" / "jarvis stop"), togliere prima l'avvio la spezzerebbe a meta'
    lasciando un pezzo nel testo."""
    words = text.split()
    for phrase, from_start in ((stop_phrase, False), (start_phrase, True)):
        # ogni variante ha una lunghezza sua: si provano tutte, dalla piu'
        # lunga, cosi' "gia visto" viene tolto per intero e non a meta'
        spans = sorted(
            {len(v.split()) for v in _phrase_variants(phrase)}, reverse=True
        )
        sizes = []
        for span in spans:
            # per ciascuna: la lunghezza esatta, una parola in piu', una in
            # meno — stesso motivo di _phrase_in_text
            for size in (span, span + 1, max(1, span - 1)):
                if size not in sizes:
                    sizes.append(size)
        for size in sizes:
            if size > len(words):
                continue
            edge = words[:size] if from_start else words[len(words) - size :]
            if _phrase_in_text(phrase, " ".join(edge)):
                words = words[size:] if from_start else words[: len(words) - size]
                break
    return " ".join(words).strip(" ,.;:-").strip()


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
# lunghezza massima di un comando IA scritto arrivato dal socket di
# controllo (vedi _handle_control_ai_command): non e' una frase dettata ma
# testo digitato, quindi conviene un tetto esplicito prima di spedirlo
# all'LLM
CONTROL_AI_MAX_TEXT = 500
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

# --- comandi da terminale (solo per i comandi IA arrivati dal socket di
# controllo, cioe' da una dashboard sullo stesso PC: vedi
# _handle_control_ai_command). Non sono mai offerti al telefono, per due
# motivi: il pannello di scelta dell'app sa gestire solo scorciatoie e
# avvii di applicazioni, e soprattutto un comando shell va confermato
# leggendolo, cosa che ha senso su uno schermo davanti a chi lo esegue.
# Nessuna esecuzione avviene senza una conferma esplicita (vedi
# _handle_control_ai_choose): l'unica difesa che regge davvero contro un
# comando sbagliato o male interpretato e' che l'utente lo veda in chiaro
# prima, non una lista di comandi vietati che si aggira in dieci modi.
AI_COMMAND_SHELL_PROMPT = (
    "\n\nSe invece il comando descrive un'operazione da TERMINALE (compilare, "
    "avviare uno script, gestire file, git, pacchetti), l'elemento deve avere "
    "i campi \"label\" e \"shell\" (array di comandi bash da eseguire in "
    "sequenza nella stessa shell, es. [\"cd ~/progetto\", \"npm run build\"]) "
    "al posto di \"combo\". La shell parte dalla home dell'utente e ricorda "
    "le directory: un \"cd\" vale anche per i comandi successivi. Usa "
    "\"shell\" solo quando l'operazione richiesta e' davvero da riga di "
    "comando; se la stessa cosa si fa con una scorciatoia dell'applicazione "
    "in primo piano, preferisci \"combo\". Non proporre comandi distruttivi "
    "(cancellazioni ricorsive, formattazioni, modifiche a file di sistema) a "
    "meno che l'utente non li abbia chiesti esplicitamente e senza ambiguita'."
)
# quanti comandi al massimo puo' contenere un candidato shell, e quanto puo'
# essere lungo ognuno: tetti stretti perche' tutto deve restare leggibile in
# un pannello di conferma, non perche' proteggano da qualcosa
CONTROL_SHELL_MAX_COMMANDS = 8
CONTROL_SHELL_MAX_LENGTH = 500
# oltre questo tempo il comando viene interrotto: il pannello che ha chiesto
# l'esecuzione sta aspettando la risposta, non puo' restare appeso a una
# compilazione infinita
CONTROL_SHELL_TIMEOUT = 120
# quanto output riportare al pannello (il resto e' troncato): serve a capire
# se e' andata bene, non a leggere un log intero
CONTROL_SHELL_OUTPUT_CHARS = 2000

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

# --- attesa che gli appunti siano davvero pronti prima di incollare (vedi
# _wait_clipboard): il primo incolla dopo un periodo di inattivita' e' il
# caso in cui il ritardo si nota, perche' i processi che servono la
# clipboard vanno riavviati.
CLIPBOARD_READY_TIMEOUT = 2.0
CLIPBOARD_POLL_INTERVAL = 0.03
# usata solo dove non si puo' rileggere gli appunti per sapere quando sono
# pronti: e' la pausa fissa che c'era prima
CLIPBOARD_FALLBACK_WAIT = 0.1

# pausa fra l'incolla e l'Invio dell'invio automatico: certe applicazioni
# (chat web soprattutto) elaborano l'incolla in modo asincrono, e un Invio
# immediato partirebbe con il campo ancora vuoto
AUTO_ENTER_DELAY = 0.15

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


def _cuda_available():
    """Se c'e' almeno una GPU CUDA utilizzabile. Costa una manciata di
    millisecondi e non carica niente in VRAM: dice pero' solo che la scheda
    c'e', non che ci sia posto — la VRAM occupata si scopre solo caricando."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class ModelManager:
    """Tiene il modello faster-whisper in VRAM, con scarico automatico.

    Tutti i metodi sono chiamati da thread secondari. Il lock e' rientrante
    perche' transcribe() richiama il caricamento tenendo gia' il lock.
    """

    def __init__(self, language=MODEL_LANGUAGE, on_fallback=None):
        self._lock = threading.RLock()
        self._model = None
        self._last_used = 0.0
        # lingua di dettatura, modificabile a caldo (set_language): non
        # richiede di ricaricare il modello, e' solo un parametro passato a
        # transcribe(). "auto" = rilevamento automatico della lingua.
        self.language = language
        # su cosa sta girando davvero il modello: resta MODEL_DEVICE finche'
        # la GPU regge, diventa MODEL_FALLBACK_DEVICE quando si ripiega.
        # Se di schede non ce n'e' nessuna lo si sa subito, senza aspettare il
        # primo caricamento fallito: serve a chi detta dal telefono per sapere
        # in anticipo che il PC e' in difficolta' (vedi _transcribe_on_phone).
        self.device = (
            MODEL_DEVICE if _cuda_available() else MODEL_FALLBACK_DEVICE
        )
        # avvisa che si sta andando a CPU: la dettatura funziona ma diventa
        # molto piu' lenta, ed e' bene che l'utente sappia perche'
        self._on_fallback = on_fallback

    def _load_locked(self):
        if self._model is not None:
            self._last_used = time.monotonic()
            return self._model

        # import ritardato: importare faster_whisper costa ~1s e non
        # serve finche' non si detta davvero
        from faster_whisper import WhisperModel

        try:
            self._model = WhisperModel(
                MODEL_NAME,
                device=MODEL_DEVICE,
                compute_type=MODEL_COMPUTE_TYPE,
            )
            self.device = MODEL_DEVICE
        except Exception as exc:
            # non si distingue fra i motivi (scheda assente, driver, VRAM
            # occupata): l'unica cosa che conta e' che su GPU non si puo'
            # caricare, e che senza ripiego la dettatura fallirebbe
            self._model = WhisperModel(
                MODEL_FALLBACK_NAME,
                device=MODEL_FALLBACK_DEVICE,
                compute_type=MODEL_FALLBACK_COMPUTE_TYPE,
            )
            self.device = MODEL_FALLBACK_DEVICE
            if self._on_fallback is not None:
                self._on_fallback(f"{type(exc).__name__}: {exc}")

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
                # al prossimo caricamento si riprova dalla GPU: se si era
                # ripiegato perche' la VRAM era occupata, nel frattempo puo'
                # essersi liberata
                self.device = MODEL_DEVICE
                gc.collect()
                return True
            return False
        finally:
            self._lock.release()


def _wav_data_offset(fh):
    """Offset del primo byte di audio in un file WAV, scorrendo i chunk RIFF.

    Non si puo' assumere l'header canonico da 44 byte: pw-record e soundfile
    possono inserire chunk (LIST, fact) prima di "data"."""
    fh.seek(0)
    if fh.read(4) != b"RIFF":
        return None
    fh.seek(12)  # salta la dimensione RIFF e il tag "WAVE"
    while True:
        header = fh.read(8)
        if len(header) < 8:
            return None
        chunk_id = header[0:4]
        size = int.from_bytes(header[4:8], "little")
        if chunk_id == b"data":
            return fh.tell()
        # i chunk hanno padding a byte pari
        fh.seek(size + (size % 2), 1)


def _read_wav_tail(path, seconds, sample_rate=16000, sample_width=2):
    """Ritorna gli ultimi `seconds` di audio grezzo (PCM) da un WAV **ancora in
    scrittura**.

    Il modulo `wave` non serve in lettura: finche' il file e' aperto da chi
    registra, l'header dichiara una lunghezza sbagliata (spesso zero). Qui si
    ignora quel campo e si legge la coda reale del file."""
    with open(path, "rb") as fh:
        start = _wav_data_offset(fh)
        if start is None:
            return b""
        fh.seek(0, 2)
        end = fh.tell()
        wanted = int(seconds * sample_rate) * sample_width
        begin = max(start, end - wanted)
        # allinea al campione: partire a meta' campione sfaserebbe l'audio
        begin -= (begin - start) % sample_width
        fh.seek(begin)
        return fh.read(end - begin)


def _write_wav(path, pcm, sample_rate=16000, sample_width=2):
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(sample_width)
        out.setframerate(sample_rate)
        out.writeframes(pcm)


def _wav_header(sample_rate=16000, sample_width=2, channels=1):
    """Header WAV canonico da 44 byte con le lunghezze ancora a zero.

    E' lo stato in cui pw-record lascia il file mentre registra, e quello che
    _read_wav_tail sa gia' leggere; le lunghezze vere si scrivono alla
    chiusura (vedi PhoneAudioSink)."""
    byte_rate = sample_rate * channels * sample_width
    return b"".join(
        [
            b"RIFF",
            (0).to_bytes(4, "little"),
            b"WAVE",
            b"fmt ",
            (16).to_bytes(4, "little"),
            (1).to_bytes(2, "little"),  # formato PCM
            channels.to_bytes(2, "little"),
            sample_rate.to_bytes(4, "little"),
            byte_rate.to_bytes(4, "little"),
            (channels * sample_width).to_bytes(2, "little"),
            (sample_width * 8).to_bytes(2, "little"),
            b"data",
            (0).to_bytes(4, "little"),
        ]
    )


class PhoneAudioSink:
    """Scrive in un WAV l'audio che arriva dal telefono, un blocco alla volta.

    Prende il posto di pw-record quando a registrare e' il microfono del
    telefono e a trascrivere resta il PC (vedi _start_recording): il file che
    ne esce e' indistinguibile da quello registrato in locale, cosi' tutto
    quello che viene dopo — controllo del silenzio, trascrizione, vocabolario
    — non ha bisogno di sapere chi l'ha riempito.

    append() gira nel thread del socket, close() nel thread principale."""

    # posizione dei due campi di lunghezza dentro l'header da 44 byte
    _RIFF_SIZE_OFFSET = 4
    _DATA_SIZE_OFFSET = 40

    def __init__(self, path, sample_rate=16000, sample_width=2):
        self._lock = threading.Lock()
        self._written = 0
        self._fh = open(path, "wb")
        self._fh.write(_wav_header(sample_rate, sample_width))
        self._fh.flush()

    def append(self, pcm):
        with self._lock:
            if self._fh is None:
                return
            self._fh.write(pcm)
            # senza flush il controllo del silenzio leggerebbe un file fermo
            # e chiuderebbe la dettatura mentre l'utente sta ancora parlando
            self._fh.flush()
            self._written += len(pcm)

    def close(self):
        """Corregge le lunghezze dichiarate nell'header e chiude. Va fatto
        prima di trascrivere: faster-whisper decodifica il file con ffmpeg,
        che su un header a zero non troverebbe nessun audio."""
        with self._lock:
            if self._fh is None:
                return
            self._fh.seek(self._RIFF_SIZE_OFFSET)
            self._fh.write((36 + self._written).to_bytes(4, "little"))
            self._fh.seek(self._DATA_SIZE_OFFSET)
            self._fh.write(self._written.to_bytes(4, "little"))
            self._fh.close()
            self._fh = None


def _block_levels(pcm):
    """Livello (RMS) di ogni tratto da WAKE_SILENCE_BLOCK_SECONDS."""
    samples = array.array("h")
    # tronca a un numero intero di campioni
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % samples.itemsize)])
    block = int(16000 * WAKE_SILENCE_BLOCK_SECONDS)
    levels = []
    for start in range(0, len(samples), block):
        # si guarda un campione ogni 4: basta per distinguere voce da silenzio
        # e costa un quarto del tempo
        chunk = samples[start : start + block : 4]
        if chunk:
            levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    return levels


def _is_silence(pcm):
    """True se in tutta la finestra non c'e' niente che somigli a una voce.
    Evita di far girare whisper sul silenzio, che e' il caso normale mentre si
    aspetta la frase.

    Si confronta il tratto piu' sonoro col piu' silenzioso della stessa
    finestra, invece di misurare il livello assoluto: vedi WAKE_SILENCE_RATIO
    per il perche'. E si guarda per tratti brevi, non sull'intera finestra:
    "Jarvis" dura mezzo secondo dentro una finestra di tre, e mediando,
    l'energia della parola si annacqua nel silenzio che la circonda."""
    levels = _block_levels(pcm)
    if not levels:
        return True
    peak = max(levels)
    if peak < WAKE_SILENCE_FLOOR:
        return True
    if peak >= WAKE_SILENCE_LOUD:
        return False
    floor = min(levels)
    if floor <= 0:
        return False
    return peak < floor * WAKE_SILENCE_RATIO


def _speech_seconds(pcm):
    """Quanti secondi di voce ci sono nell'audio, secondo il rilevatore di
    voce di faster-whisper (Silero). None se non e' disponibile.

    E' lo stesso rilevatore che la trascrizione usa per saltare le pause
    (`vad_filter`), quindi non aggiunge dipendenze; costa una decina di
    millisecondi su una finestra di dieci secondi."""
    try:
        import numpy as np
        from faster_whisper.vad import get_speech_timestamps, VadOptions
    except Exception:
        return None
    try:
        # tronca a un numero intero di campioni: con un byte spaiato
        # frombuffer solleva, e il rilevatore verrebbe scartato in silenzio
        # ripiegando sul criterio a energia, molto meno affidabile
        pcm = pcm[: len(pcm) - (len(pcm) % 2)]
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments = get_speech_timestamps(
            audio,
            VadOptions(min_speech_duration_ms=SILENCE_VAD_MIN_SPEECH_MS),
        )
        return sum(s["end"] - s["start"] for s in segments) / 16000
    except Exception:
        return None


def _no_voiced_blocks(pcm):
    """Ripiego a energia, per quando il rilevatore di voce non e' disponibile.

    Meno affidabile in una stanza rumorosa (vedi SILENCE_MIN_SPEECH_SECONDS),
    ma meglio di lasciare la dettatura aperta per sempre."""
    levels = _block_levels(pcm)
    if not levels:
        return False
    # il fondo si stima sul quarto piu' silenzioso della finestra, non sul
    # minimo assoluto: il rumore di una stanza respira, e prendere il singolo
    # tratto piu' quieto come riferimento fa passare per voce le sue stesse
    # fluttuazioni
    background = sorted(levels)[len(levels) // 4]
    # il tetto serve a chi parla senza pause per tutta la finestra: li' anche
    # il "fondo" e' parlato, e senza un limite la soglia salirebbe sopra la
    # voce stessa facendola sembrare silenzio
    voice_level = min(
        max(WAKE_SILENCE_FLOOR, background * WAKE_SILENCE_RATIO),
        WAKE_SILENCE_LOUD,
    )
    loud_blocks = sum(1 for level in levels if level >= voice_level)
    return loud_blocks < SILENCE_MIN_VOICED_BLOCKS


def _tail_is_silent(wav_path, seconds):
    """True se negli ultimi `seconds` della registrazione in corso non si e'
    parlato.

    Non si riusa _is_silence: quello confronta il tratto piu' sonoro col piu'
    silenzioso per accorgersi che *c'e'* una voce, e su una finestra lunga di
    stanza vuota una fluttuazione qualsiasi del rumore basta a superare il
    rapporto. Nella prova dal vivo la dettatura si chiudeva dopo una ventina di
    secondi invece di dieci, quando capitava una finestra abbastanza uniforme.

    Qui interessa il contrario — accertarsi che *non* ci sia voce — quindi si
    conta per quanto tempo il suono sta sopra il fondo di quella stessa
    finestra. Un colpo isolato (una porta, un tasto) non tiene aperta la
    dettatura; mezzo secondo di parlato si'.

    Ritorna False finche' l'audio registrato e' piu' corto della finestra
    richiesta: altrimenti una dettatura appena iniziata verrebbe chiusa subito,
    avendo "tutto silenzio" semplicemente perche' non c'e' ancora niente. Lo
    stesso vale se il file non c'e' piu': puo' sparire fra un controllo e
    l'altro, quando la dettatura finisce e la trascrizione lo consuma."""
    try:
        pcm = _read_wav_tail(wav_path, seconds)
    except OSError:
        return False
    if len(pcm) < int(seconds * 16000) * 2:
        return False
    speech = _speech_seconds(pcm)
    if speech is None:
        return _no_voiced_blocks(pcm)
    return speech < SILENCE_MIN_SPEECH_SECONDS


class WakeWordListener:
    """Ascolta il microfono e segnala quando sente la frase di attivazione.

    Non tocca mai lo stato del demone: accoda ("wake", "start"|"stop") sulla
    command_queue, come fa _transcribe_worker con ("done", ...), cosi' le
    transizioni restano tutte nel thread principale.

    Non apre mai un secondo microfono. Quando non si sta dettando registra per
    conto proprio su uno slot separato del backend; quando la dettatura e' in
    corso legge la coda del file che sta gia' scrivendo la dettatura stessa,
    cercando la frase di stop."""

    def __init__(
        self,
        backend,
        command_queue,
        phrases_provider,
        language_provider,
        on_heard=None,
    ):
        self._backend = backend
        self._queue = command_queue
        self._phrases = phrases_provider
        self._language = language_provider
        # riceve quello che il microfono del PC ha capito, riconosciuto o no:
        # e' l'unico modo per l'utente di sapere perche' la sua frase non
        # scatta (il modello puo' averla sentita in tutt'altro modo)
        self._on_heard = on_heard
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._model = None
        # sorgente corrente: ("own", path) mentre si aspetta la frase di
        # avvio, ("recording", path) mentre si aspetta quella di stop
        self._source = None
        self._own_path = None
        self._own_started_at = 0.0
        self._muted_until = 0.0

    # --- ciclo di vita ---

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=WAKE_POLL_INTERVAL + 2)
        self._stop_own_capture()
        with self._lock:
            self._source = None
        # il modello resta caricato: e' piccolo (~75 MB in RAM) e ricaricarlo
        # a ogni riaccensione dell'ascolto costerebbe piu' di quanto valga

    # --- sorgente audio ---

    def follow_recording(self, wav_path):
        """Passa ad ascoltare il file della dettatura in corso (frase di stop)."""
        if not self._running:
            return
        self._stop_own_capture()
        with self._lock:
            self._source = ("recording", wav_path)
            self._muted_until = time.monotonic() + WAKE_COOLDOWN_SECONDS

    def follow_own_capture(self):
        """Torna ad ascoltare per conto proprio (frase di avvio)."""
        if not self._running:
            return
        with self._lock:
            self._source = None
        # l'inizio del file e' quasi sempre la coda della frase di stop appena
        # pronunciata: senza attesa la dettatura ripartirebbe da sola
        self._start_own_capture(mute_for=WAKE_COOLDOWN_SECONDS)

    def _start_own_capture(self, mute_for=WAKE_COOLDOWN_SECONDS):
        try:
            fd, path = tempfile.mkstemp(prefix="stenografa-wake-", suffix=".wav")
            os.close(fd)
            self._backend.start_recording(path, slot="wake")
        except Exception:
            return
        self._own_path = path
        self._own_started_at = time.monotonic()
        with self._lock:
            self._source = ("own", path)
            self._muted_until = time.monotonic() + mute_for

    def _stop_own_capture(self):
        path = self._own_path
        self._own_path = None
        if path is None:
            return
        try:
            self._backend.stop_recording(slot="wake")
        except Exception:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass

    def _rotate_own_capture_if_needed(self):
        if self._own_path is None:
            return
        if time.monotonic() - self._own_started_at < WAKE_ROTATE_SECONDS:
            return
        # il file cresce di ~2 MB al minuto: si ricomincia da capo, tanto
        # interessa solo la coda. Qui non serve la pausa di
        # WAKE_COOLDOWN_SECONDS (non c'e' nessuna frase appena riconosciuta da
        # cui difendersi): basta il tempo di avere qualche campione nel file
        # nuovo, altrimenti l'ascolto resterebbe sordo per qualche secondo ad
        # ogni rotazione
        self._stop_own_capture()
        self._start_own_capture(mute_for=WAKE_ROTATE_MUTE_SECONDS)

    # --- riconoscimento ---

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                WAKE_MODEL_NAME,
                device=WAKE_MODEL_DEVICE,
                compute_type=WAKE_MODEL_COMPUTE_TYPE,
            )
        return self._model

    def _transcribe(self, pcm):
        model = self._load_model()
        fd, path = tempfile.mkstemp(prefix="stenografa-wake-win-", suffix=".wav")
        os.close(fd)
        try:
            _write_wav(path, pcm)
            language = self._language()
            start_phrase, stop_phrase = self._phrases()
            segments, _info = model.transcribe(
                path,
                language=None if language == "auto" else language,
                beam_size=1,  # l'ascolto deve essere veloce, non accurato
                vad_filter=True,
                condition_on_previous_text=False,
                # decisivo per le frasi che non appartengono alla lingua di
                # dettatura: senza, un "Jarvis" detto in italiano viene
                # trascritto "Ciao, vi!" e non combacia con niente. Dando le
                # frasi come contesto il modello ne riusa la grafia (stesso
                # meccanismo del vocabolario di dettatura, vedi
                # _vocabulary_for_dashboard).
                initial_prompt=_wake_prompt(start_phrase, stop_phrase),
            )
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _loop(self):
        if self._source is None and self._own_path is None:
            self._start_own_capture()
        while self._running:
            time.sleep(WAKE_POLL_INTERVAL)
            if not self._running:
                return
            try:
                self._tick()
            except Exception:
                # un errore di lettura o di modello non deve spegnere
                # l'ascolto ne' il demone: si riprova al giro dopo
                time.sleep(WAKE_POLL_INTERVAL)

    def _tick(self):
        with self._lock:
            source = self._source
            muted = time.monotonic() < self._muted_until
        if source is None or muted:
            return
        mode, path = source
        if mode == "own":
            self._rotate_own_capture_if_needed()
        if not os.path.exists(path):
            return
        pcm = _read_wav_tail(path, WAKE_WINDOW_SECONDS)
        if len(pcm) < 16000 or _is_silence(pcm):
            return
        text = self._transcribe(pcm)
        if not text:
            return
        if self._on_heard is not None:
            self._on_heard(text)
        start_phrase, stop_phrase = self._phrases()
        phrase = stop_phrase if mode == "recording" else start_phrase
        if not _phrase_in_text(phrase, text):
            return
        with self._lock:
            # se nel frattempo la sorgente e' cambiata (la dettatura e'
            # partita o finita per altra via) il riconoscimento e' vecchio
            if self._source != source:
                return
            self._muted_until = time.monotonic() + WAKE_COOLDOWN_SECONDS
        self._queue.put(("wake", "stop" if mode == "recording" else "start"))


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
        self.model = ModelManager(
            language=_load_language(), on_fallback=self._on_model_fallback
        )
        self.backend = platform_backend.get_backend(RUNTIME_DIR)
        self.restore_clipboard = _load_restore_clipboard()
        # attivazione vocale: le frasi sono lette a ogni giro dal listener,
        # cosi' cambiarle dalle impostazioni ha effetto subito senza riavviare
        # l'ascolto (vedi WakeWordListener)
        self.wake_word_enabled = _load_wake_word_enabled()
        self.wake_phrase_start = _load_wake_phrase_start()
        self.wake_phrase_stop = _load_wake_phrase_stop()
        # true quando la dettatura in corso e' stata avviata a voce dal
        # telefono: l'ascolto e' sul telefono ma le frasi finiscono comunque
        # nell'audio registrato dal PC (vedi _handle_button_press)
        self._recording_from_wake = False
        # true quando a trascrivere e' il telefono invece del PC: succede
        # quando la GPU non e' utilizzabile e la CPU sarebbe troppo lenta
        # (vedi _handle_button_press e _on_dictated_text)
        self._recording_by_phone = False
        # true quando a registrare e' il microfono del telefono ma a
        # trascrivere resta il PC: serve quando il microfono del PC non e'
        # utilizzabile (occupato da un'altra applicazione). E' l'opposto di
        # _recording_by_phone e i due non valgono mai insieme: li' il telefono
        # consegna il testo, qui l'audio (vedi _append_phone_audio).
        self._recording_mic_from_phone = False
        # file aperto in scrittura mentre i blocchi audio arrivano dal
        # telefono, e connessione che li sta mandando: se quella cade a meta'
        # dettatura non arrivera' piu' niente da nessuno
        self._phone_audio = None
        self._phone_audio_conn = None
        # chiusura automatica dopo un silenzio prolungato (0 = spenta)
        self.silence_timeout = _load_silence_timeout()
        self._silence_stop = None
        self._silence_thread = None
        # ultima frase capita dal microfono del PC, riconosciuta o no: l'app
        # la mostra nelle impostazioni accanto a quella del telefono
        self.wake_heard = ""
        self.wake_listener = WakeWordListener(
            self.backend,
            self.command_queue,
            lambda: (self.wake_phrase_start, self.wake_phrase_stop),
            lambda: self.model.language,
            on_heard=lambda text: self.command_queue.put(("wake_heard", text)),
        )
        # quante notifiche di sistema mandare (vedi NOTIFICATION_LEVELS)
        self.notifications = _load_notifications()
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
        # se la dettatura in corso deve finire con un Invio: lo chiede il
        # pulsante "record" con l'invio automatico acceso (vedi _paste_text)
        self._recording_auto_enter = False
        # vale per l'incolla in arrivo, non per il re-incolla di una voce
        # passata ne' per gli snippet fissi
        self._auto_enter_pending = False
        # stessa cosa per il testo in attesa di approvazione sul telefono
        self._pending_auto_enter = False
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
        # come sopra, ma per i comandi IA scritti arrivati dal socket di
        # controllo (dashboard sul PC): tenuta separata da _pending_choice
        # perche' i due pannelli vivono su schermi diversi e una scelta in
        # sospeso sul telefono non deve annullare quella sul PC. Il socket
        # di controllo risponde da thread separati, quindi serve un lock.
        self._control_choice = None
        self._control_choice_lock = threading.Lock()
        # cosa sta facendo il comando IA chiesto dal PC, per il pannello che
        # lo segue interrogando ai_status: non avendo un canale su cui
        # ricevere eventi, gli si tiene pronta una fotografia
        self._control_session = {
            "phase": "idle",
            "text": "",
            "error": "",
            "request_id": "",
            "options": [],
            "executed": "",
            "output": "",
            "seq": 0,
        }
        # la dettatura in corso e' stata avviata dal pannello sul PC: il
        # testo trascritto va interpretato per lui (vedi _control_ai_worker)
        # invece di essere eseguito subito come per il telefono
        self._recording_from_control = False
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
                elif kind == "wake":
                    self._on_wake_word(item[1])
                elif kind == "wake_heard":
                    self._on_wake_heard(item[1])
                elif kind == "button":
                    # gli elementi in coda (origine del comando, chi
                    # trascrive, chi registra) mancano quando la pressione
                    # arriva dal socket locale o da una versione precedente
                    # dell'app
                    source = item[2] if len(item) > 2 else None
                    transcribe = item[3] if len(item) > 3 else None
                    mic = item[4] if len(item) > 4 else None
                    self._handle_button_press(
                        item[1], source=source, transcribe=transcribe, mic=mic
                    )
                elif kind == "button_down":
                    mic = item[2] if len(item) > 2 else None
                    self._handle_button_press(item[1], phase="down", mic=mic)
                elif kind == "button_up":
                    self._handle_button_press(item[1], phase="up")
                elif kind == "recording_timeout":
                    self._on_recording_timeout_reached()
                elif kind == "dictated_text":
                    self._on_dictated_text(item[1])
                elif kind == "silence_timeout":
                    self._on_silence_timeout_reached(item[1])
                elif kind == "phone_audio_lost":
                    self._on_phone_audio_lost()
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
                elif kind == "control_ai_record":
                    self._on_control_ai_record(item[1])
                elif kind == "control_ai_ready":
                    self._set_state(STATE_IDLE)
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
        # riscontro sullo schermo del PC: senza, una dettatura partita a voce
        # non si vede da nessuna parte (vedi show_dictation_overlay)
        try:
            self.backend.show_dictation_overlay(state)
        except Exception:
            pass
        if state == STATE_IDLE:
            # fine della dettatura, comunque sia andata (testo incollato,
            # comando eseguito, errore, niente da trascrivere): i video
            # fermati per registrare possono ripartire. Non fa nulla se non
            # ne era stato fermato nessuno.
            self._resume_media_after_recording()

    def _notify(self, title, body, urgency="normal"):
        # filtro delle notifiche di sistema (vedi NOTIFICATION_LEVELS): con
        # "errors" passano solo quelle critiche, con "none" nessuna. Il
        # _broadcast all'app telefono resta invariato in tutti i casi.
        if self.notifications == "none":
            return
        if self.notifications == "errors" and urgency != "critical":
            return
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
            "notifications": self.notifications,
            "wake_word_enabled": self.wake_word_enabled,
            "wake_phrase_start": self.wake_phrase_start,
            "wake_phrase_stop": self.wake_phrase_stop,
            "silence_timeout": self.silence_timeout,
            # informativi (non modificabili): servono all'app telefono per
            # mostrare l'impronta da confrontare e capire se il canale e'
            # cifrato
            # su cosa gira la trascrizione: "cuda" o, se la GPU non e'
            # utilizzabile, "cpu" (vedi MODEL_FALLBACK_DEVICE)
            "model_device": self.model.device,
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

    def _set_wake_word_enabled(self, enabled):
        if not isinstance(enabled, bool):
            return False, "wake_word_enabled deve essere un booleano"
        self.wake_word_enabled = enabled
        _save_wake_word_enabled(enabled)
        if enabled:
            self.wake_listener.start()
            # se si accende l'ascolto mentre si sta gia' dettando, la sorgente
            # giusta e' il file della dettatura in corso, non una cattura nuova
            if self.state == STATE_RECORDING and self.record_file:
                self.wake_listener.follow_recording(self.record_file)
        else:
            self.wake_listener.stop()
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_silence_timeout(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            return False, "silence_timeout deve essere un numero di secondi"
        if seconds != 0 and not (
            SILENCE_TIMEOUT_MIN <= seconds <= SILENCE_TIMEOUT_MAX
        ):
            return False, (
                f"silence_timeout deve essere 0 (spento) oppure fra "
                f"{SILENCE_TIMEOUT_MIN} e {SILENCE_TIMEOUT_MAX} secondi"
            )
        self.silence_timeout = seconds
        _save_silence_timeout(seconds)
        # se e' in corso una dettatura, il nuovo valore vale dalla prossima:
        # cambiare la finestra a meta' registrazione darebbe una chiusura a
        # sorpresa, calcolata su un silenzio che l'utente non sa di avere
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_wake_phrase(self, phrase, other, save, attribute, label):
        """Parte comune delle due frasi di attivazione: la sola differenza fra
        avvio e stop e' quale delle due si sta cambiando."""
        if not _valid_wake_phrase(phrase):
            return False, (
                f"{label} deve contenere almeno "
                f"{WAKE_PHRASE_MIN_CHARS} caratteri (lettere o numeri) e non "
                f"superare i {WAKE_PHRASE_MAX_CHARS}"
            )
        phrase = phrase.strip()
        if _normalize_phrase(phrase) == _normalize_phrase(other):
            return False, (
                "le frasi di avvio e di stop devono essere diverse fra loro"
            )
        setattr(self, attribute, phrase)
        save(phrase)
        self._broadcast({"type": "config", **self._config_snapshot()})
        return True, None

    def _set_wake_phrase_start(self, phrase):
        return self._set_wake_phrase(
            phrase,
            self.wake_phrase_stop,
            _save_wake_phrase_start,
            "wake_phrase_start",
            "wake_phrase_start",
        )

    def _set_wake_phrase_stop(self, phrase):
        return self._set_wake_phrase(
            phrase,
            self.wake_phrase_start,
            _save_wake_phrase_stop,
            "wake_phrase_stop",
            "wake_phrase_stop",
        )

    def _set_notifications(self, level):
        if not _valid_notifications(level):
            return False, (
                "notifications deve essere uno fra "
                + ", ".join(f"'{lv}'" for lv in NOTIFICATION_LEVELS)
            )
        level = level.strip().lower()
        self.notifications = level
        _save_notifications(level)
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
                    self.command_queue.put(("button", "record", msg.get("source")))
                elif cmd == "button":
                    # "source": "wake" quando a premere il pulsante e' stata
                    # l'attivazione vocale del telefono e non un dito. Serve al
                    # demone per sapere che nell'audio ci sono anche le frasi
                    # di attivazione, da suggerire al modello e da togliere dal
                    # testo (vedi _vocabulary_for_dashboard/_strip_wake_phrases).
                    # Campo opzionale: le versioni precedenti dell'app non lo
                    # mandano e continuano a funzionare.
                    if msg.get("mic") == "phone":
                        # chi chiede di registrare col proprio microfono e'
                        # anche chi mandera' l'audio: va ricordato per
                        # accorgersi se sparisce a meta' dettatura
                        self._phone_audio_conn = conn
                    self.command_queue.put(
                        (
                            "button",
                            msg.get("id"),
                            msg.get("source"),
                            msg.get("transcribe"),
                            msg.get("mic"),
                        )
                    )
                elif cmd == "button_down":
                    # push-to-talk: pressione e rilascio arrivano separati,
                    # invece del singolo tocco che fa da interruttore
                    if msg.get("mic") == "phone":
                        self._phone_audio_conn = conn
                    self.command_queue.put(
                        ("button_down", msg.get("id"), msg.get("mic"))
                    )
                elif cmd == "button_up":
                    self.command_queue.put(("button_up", msg.get("id")))
                elif cmd == "list_apps":
                    self._send_to(
                        conn, {"type": "apps", "apps": self._list_apps_cached()}
                    )
                elif cmd == "dictated_text":
                    # il telefono ha trascritto per conto suo e consegna il
                    # testo: vale anche come "ferma la dettatura"
                    self.command_queue.put(("dictated_text", msg.get("text")))
                elif cmd == "audio":
                    # un blocco del microfono del telefono: si scrive subito,
                    # qui nel thread del socket, invece di accodarlo. Sono una
                    # decina di messaggi al secondo per tutta la durata della
                    # dettatura, e passare dalla coda dei comandi vorrebbe dire
                    # occupare il thread principale con quello che e' solo I/O
                    # su file, rallentando le transizioni di stato.
                    self._append_phone_audio(msg.get("data"))
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
            if self._phone_audio_conn is conn:
                self._phone_audio_conn = None
                self.command_queue.put(("phone_audio_lost",))
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
            if kind == "record" and spec.get("auto_enter"):
                # invio automatico dopo l'incolla: vedi _paste_text
                button["auto_enter"] = True
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

        if "auto_enter" in msg:
            if kind != "record":
                return False, (
                    f"il pulsante '{button['label']}' e' di tipo '{kind}': "
                    "l'invio automatico vale solo per la dettatura normale"
                )
            enabled = msg.get("auto_enter")
            if not isinstance(enabled, bool):
                return False, "auto_enter deve essere un booleano"
            updates["auto_enter"] = enabled

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

    def _handle_button_press(
        self, button_id, phase="tap", source=None, transcribe=None, mic=None
    ):
        """`phase` distingue il tocco normale ("tap", che sui pulsanti
        microfono fa da interruttore avvia/ferma) dal push-to-talk, in cui
        l'app invia separatamente la pressione ("down", avvia) e il rilascio
        ("up", ferma). Sui pulsanti non-microfono l'azione parte alla
        pressione e il rilascio non fa nulla, cosi' tenere premuto per
        sbaglio non la esegue due volte.

        `source` vale "wake" quando il pulsante e' stato premuto
        dall'attivazione vocale del telefono: la dettatura contiene allora
        anche le frasi pronunciate, da trattare come quando ad ascoltare e' il
        PC (vedi _recording_from_wake)."""
        with self._layout_lock:
            dashboard, button = self._find_button_global(button_id)
        if button is None:
            return
        kind = button["kind"]
        dashboard_id = dashboard["id"] if dashboard else None

        if kind in MIC_KINDS:
            mode = "ai_command" if kind == "ai_command" else "paste"
            auto_enter = bool(button.get("auto_enter"))
            # va deciso prima di avviare: _start_recording costruisce subito
            # il vocabolario da passare al modello. Una frase di stop detta a
            # voce vale anche se la dettatura era stata avviata col dito: in
            # quel caso l'audio la contiene comunque, e va tolta dal testo.
            if source == "wake":
                self._recording_from_wake = True
            elif self.state == STATE_IDLE:
                self._recording_from_wake = False
            # chi trascrive e chi registra si decidono all'avvio e valgono
            # per tutta la dettatura
            if self.state == STATE_IDLE:
                self._recording_by_phone = transcribe == "phone"
                self._recording_mic_from_phone = mic == "phone"
            if phase == "down":
                # gia' in registrazione: il "down" e' un doppione (es. due
                # telefoni collegati), non un secondo comando da eseguire
                if self.state == STATE_IDLE:
                    self.toggle_recording(
                        mode=mode,
                        dashboard_id=dashboard_id,
                        phase=phase,
                        auto_enter=auto_enter,
                    )
            elif phase == "up":
                if self.state == STATE_RECORDING:
                    self.toggle_recording(
                        mode=mode, dashboard_id=dashboard_id, phase=phase
                    )
            else:
                self.toggle_recording(
                    mode=mode,
                    dashboard_id=dashboard_id,
                    phase=phase,
                    auto_enter=auto_enter,
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
            elif cmd == "ai_command":
                reply = self._handle_control_ai_command(
                    msg.get("text"), msg.get("dashboard_id")
                )
            elif cmd == "ai_record":
                reply = self._handle_control_ai_record(msg.get("dashboard_id"))
            elif cmd == "ai_status":
                reply = self._handle_control_ai_status()
            elif cmd == "ai_cancel":
                reply = self._handle_control_ai_cancel()
            elif cmd == "ai_choose":
                reply = self._handle_control_ai_choose(
                    msg.get("request_id"),
                    msg.get("index"),
                    msg.get("delay_ms"),
                    bool(msg.get("confirm_shell")),
                )
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

    def toggle_recording(
        self, mode="paste", dashboard_id=None, phase="tap", auto_enter=False
    ):
        if self.state == STATE_IDLE:
            self._recording_mode = mode
            self._recording_dashboard_id = dashboard_id
            self._recording_auto_enter = auto_enter
            self._start_recording(phase=phase)
        elif self.state == STATE_RECORDING:
            self._stop_recording_and_transcribe()
        # se sta caricando, trascrivendo o elaborando un comando IA, ignora
        # il toggle

    def _on_model_fallback(self, error):
        """La trascrizione e' ripiegata sulla CPU (vedi
        MODEL_FALLBACK_DEVICE). Va detto: la dettatura continua a funzionare
        ma diventa parecchio piu' lenta, e senza un avviso sembrerebbe solo
        che il PC si e' impallato."""
        self._notify(
            "Stenografa - trascrizione su CPU",
            "La GPU non e' utilizzabile, si continua sulla CPU: la "
            "trascrizione sara' piu' lenta. " + error,
            urgency="critical",
        )
        self._broadcast({"type": "config", **self._config_snapshot()})

    def _on_wake_heard(self, text):
        """Quello che il microfono del PC ha capito, che sia servito o no.

        Va mostrato all'utente: se la sua frase non fa scattare niente, questo
        e' l'unico modo di scoprire come viene sentita davvero — e quindi di
        aggiungerla come variante (vedi _phrase_variants)."""
        text = (text or "").strip()
        if not text or text == self.wake_heard:
            return
        self.wake_heard = text
        self._broadcast({"type": "wake_heard", "text": text})

    def _on_wake_word(self, action):
        """Frase di attivazione riconosciuta (vedi WakeWordListener), eseguita
        nel thread principale come ogni altro comando.

        Passa dal toggle normale, cosi' la dettatura per voce si comporta in
        tutto e per tutto come quella avviata dal pulsante: pausa dei video,
        rete di sicurezza sulla durata, vocabolario, notifiche.

        Lo stato viene ricontrollato qui perche' fra il riconoscimento e
        l'esecuzione puo' essere cambiato (l'utente ha toccato il pulsante nel
        frattempo): una frase di avvio a registrazione gia' partita non deve
        fermarla, sarebbe l'opposto di quello che l'utente ha chiesto."""
        if action == "start" and self.state == STATE_IDLE:
            self.toggle_recording()
        elif action == "stop" and self.state == STATE_RECORDING:
            self._stop_recording_and_transcribe()

    @property
    def _wake_phrases_in_audio(self):
        """True se nell'audio della dettatura ci sono anche le frasi di
        attivazione: succede sia quando ad ascoltare e' il PC, sia quando la
        dettatura e' stata avviata a voce dal telefono (che sente la frase
        mentre il microfono del PC la sta gia' registrando)."""
        return self.wake_word_enabled or self._recording_from_wake

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
        # con l'attivazione vocale accesa le frasi finiscono anche nella
        # dettatura vera, e vanno tolte dal testo (vedi _strip_wake_phrases).
        # Perche' sia possibile riconoscerle bisogna prima che il modello le
        # scriva come l'utente le ha configurate: senza questo suggerimento un
        # "Jarvis stop" detto in italiano diventa "già visto" e resta nel testo.
        if self._wake_phrases_in_audio:
            prompt = _wake_prompt(self.wake_phrase_start, self.wake_phrase_stop)
            if prompt:
                parts.append(prompt.rstrip("."))
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
        if self._recording_by_phone:
            # trascrive il telefono, col proprio microfono: aprire anche
            # quello del PC servirebbe solo a tenere occupata la scheda audio
            # e a registrare un file che nessuno leggerebbe. Il testo arrivera'
            # gia' pronto (vedi _on_dictated_text).
            self.record_file = None
            os.unlink(path)
        elif self._recording_mic_from_phone:
            # registra il telefono, trascrive il PC: i blocchi audio arrivano
            # dal socket e finiscono in questo stesso file (vedi
            # _append_phone_audio), che da qui in poi e' identico a quello che
            # avrebbe scritto pw-record.
            #
            # Il microfono del PC non si apre: e' esattamente il motivo per cui
            # questa modalita' esiste. Per lo stesso motivo niente
            # follow_recording, che serve solo a non tenere due catture aperte
            # sul microfono del PC.
            self._phone_audio = PhoneAudioSink(path)
            self.model.preload()
        else:
            # prima di aprire il microfono della dettatura: l'ascolto chiude la
            # propria cattura e passa a leggere questo stesso file, cosi' non ci
            # sono mai due catture aperte insieme (vedi WakeWordListener)
            self.wake_listener.follow_recording(path)
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
            if not self._recording_by_phone:
                # il controllo del silenzio guarda il file della dettatura, che
                # quando trascrive il telefono non esiste: li' a chiudere ci
                # pensa il telefono. Con il solo microfono di rete il file c'e'
                # e cresce, quindi il controllo funziona come sempre.
                self._start_silence_watchdog(path)

    def _cancel_recording_watchdog(self):
        if self._recording_watchdog_timer is not None:
            self._recording_watchdog_timer.cancel()
            self._recording_watchdog_timer = None
        self._stop_silence_watchdog()

    # --- chiusura automatica sul silenzio ---
    # Guarda la coda del file che la dettatura sta scrivendo: quando in quella
    # finestra non c'e' piu' parlato, chiude e fa trascrivere. Legge lo stesso
    # file della registrazione, senza aprire un secondo microfono — la stessa
    # strada dell'ascolto della frase di stop (vedi WakeWordListener).

    def _start_silence_watchdog(self, wav_path):
        if not self.silence_timeout:
            return
        stop = threading.Event()
        self._silence_stop = stop
        thread = threading.Thread(
            target=self._silence_worker,
            args=(wav_path, self.silence_timeout, stop),
            daemon=True,
        )
        self._silence_thread = thread
        thread.start()

    def _stop_silence_watchdog(self):
        stop = getattr(self, "_silence_stop", None)
        if stop is not None:
            stop.set()
        self._silence_stop = None
        self._silence_thread = None

    def _silence_worker(self, wav_path, timeout, stop):
        # finche' non si e' sentita almeno una parola non si chiude niente:
        # fra il tocco sul pulsante e l'inizio del discorso puo' passare
        # parecchio — ci si avvicina al microfono, si pensa a cosa dire — e
        # chiudere li' vorrebbe dire buttare via una dettatura mai cominciata,
        # restituendo un "Nessun testo rilevato" che sembra un guasto. A una
        # registrazione lasciata aperta per sbaglio pensa gia' la rete di
        # sicurezza sulla durata (RECORDING_MAX_DURATION_SECONDS).
        heard_voice = False
        while not stop.wait(SILENCE_CHECK_INTERVAL):
            try:
                if not os.path.exists(wav_path):
                    return
                if not heard_voice:
                    # si guarda solo l'ultimo tratto: basta accorgersi che
                    # qualcuno ha cominciato a parlare
                    recent = _read_wav_tail(wav_path, SILENCE_CHECK_INTERVAL * 2)
                    spoken = _speech_seconds(recent)
                    if spoken is None:
                        # senza rilevatore di voce non si puo' sapere quando
                        # si e' cominciato: si torna a contare dall'inizio
                        heard_voice = True
                    elif spoken > 0:
                        heard_voice = True
                    continue
                if _tail_is_silent(wav_path, timeout):
                    # la decisione la prende il thread principale: qui si
                    # segnala soltanto, come per ogni altro comando
                    self.command_queue.put(("silence_timeout", wav_path))
                    return
            except Exception:
                # un errore di lettura non deve interrompere la dettatura:
                # al massimo resta aperta come prima di questa funzione
                return

    def _on_dictated_text(self, text):
        """Il telefono ha trascritto per conto suo e consegna il testo (vedi
        _recording_by_phone). Vale anche come "ferma la dettatura": arriva un
        messaggio solo, cosi' non resta uno stato appeso ad aspettare il testo
        se l'app viene chiusa a meta'.

        Da qui in poi il percorso e' quello di sempre — traduzione, conferma,
        incolla, storico — perche' cambia solo *chi* ha trascritto."""
        if self.state != STATE_RECORDING or not self._recording_by_phone:
            return
        self._cancel_recording_watchdog()
        text = (text or "").strip()
        if text and self._wake_phrases_in_audio:
            # se la dettatura e' stata avviata o fermata a voce, il
            # riconoscitore del telefono ha sentito anche le frasi di
            # attivazione: si tolgono con lo stesso criterio usato sul testo
            # trascritto dal PC
            text = _strip_wake_phrases(
                text, self.wake_phrase_start, self.wake_phrase_stop
            )
        self._on_transcription_done(text or None, None)

    def _append_phone_audio(self, data):
        """Un blocco del microfono del telefono: base64 di PCM a 16 kHz, mono,
        16 bit — lo stesso formato che pw-record produce sul PC.

        Chiamato dal thread del socket. Se non c'e' nessun file aperto il
        blocco si scarta in silenzio: sono i frammenti ancora in volo quando la
        dettatura e' gia' stata chiusa (dal pulsante, dal silenzio o dalla rete
        di sicurezza sulla durata), e non c'e' niente di sbagliato da
        segnalare."""
        sink = self._phone_audio
        if sink is None or not data:
            return
        try:
            pcm = base64.b64decode(data, validate=True)
        except Exception:
            # un blocco malformato non deve far cadere la connessione: al
            # massimo nella dettatura mancheranno cento millisecondi
            return
        sink.append(pcm)

    def _close_phone_audio(self):
        """Chiude il file alimentato dal telefono, se ce n'e' uno aperto."""
        sink = self._phone_audio
        self._phone_audio = None
        self._phone_audio_conn = None
        if sink is not None:
            sink.close()

    def _on_phone_audio_lost(self):
        """Il telefono che stava registrando si e' disconnesso a meta'
        dettatura: nessun altro riempira' il file. Si trascrive quello che e'
        arrivato, come per il silenzio prolungato — meglio di una dettatura
        che resta aperta ad aspettare audio che non arrivera' mai."""
        if self.state != STATE_RECORDING or not self._recording_mic_from_phone:
            return
        self._notify(
            "Stenografa - dettatura chiusa",
            "Il telefono che stava registrando si e' disconnesso: "
            "trascrivo quello che ho ricevuto.",
        )
        self._stop_recording_and_transcribe()

    def _on_silence_timeout_reached(self, wav_path):
        """Silenzio prolungato durante la dettatura: si chiude e si trascrive
        quello che si e' raccolto.

        Si ricontrolla che sia ancora la stessa registrazione: fra il momento
        in cui il silenzio e' stato rilevato e questo istante l'utente puo'
        aver gia' fermato tutto, e una nuova dettatura potrebbe essere
        partita."""
        if self.state != STATE_RECORDING or self.record_file != wav_path:
            return
        self._notify(
            "Stenografa - dettatura chiusa",
            f"Nessuna voce per {self.silence_timeout} secondi: "
            "trascrivo quello che ho sentito.",
        )
        self._stop_recording_and_transcribe()

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
        # se a riempire il file era il telefono, l'header va corretto adesso:
        # la trascrizione parte subito dopo
        self._close_phone_audio()
        # il file della dettatura sta per essere consumato e cancellato:
        # l'ascolto torna alla propria cattura, in attesa della frase di avvio
        self.wake_listener.follow_own_capture()

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
                if self._wake_phrases_in_audio and text:
                    # il modello della dettatura trascrive anche le frasi di
                    # attivazione, che l'utente non intendeva dettare
                    text = _strip_wake_phrases(
                        text, self.wake_phrase_start, self.wake_phrase_stop
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
        # la dettatura era stata chiesta dal pannello sul PC: l'esito e' suo
        # e non va eseguito d'iniziativa (vedi _control_ai_worker)
        from_control = self._recording_from_control
        self._recording_from_control = False
        # valgono per la dettatura appena finita: la prossima potrebbe
        # partire da un dito, o toccare al PC trascrivere (vedi
        # _handle_button_press)
        self._recording_from_wake = False
        self._recording_by_phone = False
        self._recording_mic_from_phone = False
        dashboard_id = self._recording_dashboard_id
        self._recording_dashboard_id = None
        # l'invio automatico vale per questa dettatura, non per il
        # re-incolla di una voce passata o per uno snippet fisso
        self._auto_enter_pending = self._recording_auto_enter
        self._recording_auto_enter = False
        clipboard_before = self._clipboard_before
        self._clipboard_before = None
        if error:
            if from_control:
                self._set_control_session(phase="error", error=error)
            self._notify("Stenografa - errore", error, urgency="critical")
            self._broadcast({"type": "result", "text": None, "error": error})
            return
        if not text:
            if from_control:
                self._set_control_session(
                    phase="error", error="Nessun testo rilevato."
                )
            self._notify("Stenografa", "Nessun testo rilevato.")
            self._broadcast(
                {"type": "result", "text": None, "error": "Nessun testo rilevato."}
            )
            return
        if mode == "ai_command" and from_control:
            # stesso trattamento del ramo qui sotto (chiamata bloccante in
            # un thread), ma l'esito torna al pannello sul PC invece di
            # essere eseguito subito
            self._set_control_session(phase="thinking", text=text)
            self._set_state(STATE_THINKING)
            threading.Thread(
                target=self._control_ai_worker,
                args=(text, dashboard_id),
                daemon=True,
            ).start()
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
        auto_enter = self._auto_enter_pending
        self._auto_enter_pending = False
        if self.confirm_before_paste:
            # con la conferma attiva l'Invio parte dopo l'approvazione
            self._pending_auto_enter = auto_enter
            self._request_paste_confirmation(text, clipboard_before)
            return
        self._paste_text(text, clipboard_before, auto_enter=auto_enter)

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
        auto_enter = getattr(self, "_pending_auto_enter", False)
        self._pending_auto_enter = False
        self._paste_text(
            final_text, pending["clipboard_before"], auto_enter=auto_enter
        )

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

    def _wait_clipboard(self, text):
        """Aspetta che gli appunti contengano davvero il testo prima di
        simulare l'incolla.

        Copiare non e' istantaneo: su Wayland il programma che copia diventa
        proprietario della selezione e il compositor la registra con qualche
        decimo di ritardo — di piu' al primo incolla dopo un periodo di
        inattivita', quando i processi coinvolti vanno riavviati. Con una
        pausa fissa capitava di premere Ctrl+V troppo presto e di incollare
        quello che c'era negli appunti da prima, invece del testo appena
        dettato.

        Se il backend non sa rileggere gli appunti si ripiega sulla vecchia
        pausa fissa: meglio un'attesa alla cieca che nessuna attesa."""
        deadline = time.monotonic() + CLIPBOARD_READY_TIMEOUT
        while time.monotonic() < deadline:
            current = self.backend.read_clipboard()
            if current is None:
                time.sleep(CLIPBOARD_FALLBACK_WAIT)
                return False
            if current == text:
                return True
            time.sleep(CLIPBOARD_POLL_INTERVAL)
        return False

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

    def _paste_text(self, text, clipboard_before, remember=True, auto_enter=False):
        """Copia `text` negli appunti e simula l'incolla: usato sia per il
        testo dettato cosi' com'e' sia, dopo la traduzione via LLM, per il
        testo gia' tradotto (vedi _on_translate_done).

        `remember=False` per il testo che nello storico c'e' gia' o non ci
        deve entrare: il re-incolla di una voce passata e gli snippet fissi
        dei pulsanti kind="text", che non sono dettature."""
        self.backend.copy_to_clipboard(text)
        self._wait_clipboard(text)
        combo = self._paste_combo()
        pasted = self.backend.simulate_keys(combo)
        preview = text if len(text) <= 200 else text[:200] + "..."
        if pasted:
            # nessuna notifica quando l'incolla riesce: il testo e' gia'
            # comparso nella finestra col focus, la notifica sarebbe solo
            # un doppione che copre l'interfaccia. Restano notificati i
            # casi in cui l'utente non vede nulla (incolla fallito, errori)
            if auto_enter:
                # invio automatico: in una chat il messaggio parte da solo,
                # senza dover toccare la tastiera del PC. Una pausa breve
                # perche' certe applicazioni elaborano l'incolla in modo
                # asincrono e un Invio troppo rapido arriverebbe prima che
                # il testo sia nel campo
                time.sleep(AUTO_ENTER_DELAY)
                self.backend.simulate_keys("enter")
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

    def _parse_shortcut_candidates(self, raw, allow_macro=False, allow_shell=False):
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
            elif allow_shell and isinstance(spec, dict) and spec.get("shell"):
                entries = self._resolve_shell_candidate(spec)
            elif allow_macro and isinstance(spec, dict) and spec.get("combos"):
                entries = self._resolve_macro_candidate(spec)
            else:
                entry, error = self._validate_shortcut_spec(spec, i)
                entries = [] if error else [entry]
            for entry in entries:
                if entry.get("shell"):
                    key = "shell:" + "\n".join(entry["shell"])
                elif entry.get("combos"):
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

    def _resolve_shell_candidate(self, spec):
        """Valida un candidato "comando da terminale" (campo "shell", vedi
        AI_COMMAND_SHELL_PROMPT). Accetta sia una stringa sola sia un array
        di comandi e normalizza sempre in lista: il pannello di conferma li
        mostra una riga per comando, ed eseguirli in sequenza nella stessa
        shell e' l'unico modo perche' un "cd" valga anche per i successivi.

        Scartato singolarmente se malformato, come gli altri candidati. Qui
        non si giudica cosa faccia il comando — quello lo fa l'utente
        leggendolo nel pannello — ma solo che sia mostrabile: niente
        comandi vuoti, niente elenchi sterminati, niente righe lunghe come
        un paragrafo."""
        label = str(spec.get("label") or "").strip()
        if not label:
            return []
        raw = spec.get("shell")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            return []
        if len(raw) > CONTROL_SHELL_MAX_COMMANDS:
            return []
        commands = []
        for item in raw:
            if not isinstance(item, str):
                return []
            # una riga per comando: un candidato che nasconde altre righe
            # dentro una stringa sola sarebbe confermato senza essere letto
            command = item.strip()
            if not command or "\n" in command or len(command) > CONTROL_SHELL_MAX_LENGTH:
                return []
            commands.append(command)
        return [{"label": label, "shell": commands}]

    def _run_shell_candidate(self, candidate):
        """Esegue i comandi di un candidato shell gia' confermato
        dall'utente. Una sola shell per tutti i comandi (con `set -e`, cosi'
        il primo che fallisce ferma la sequenza): e' quello che rende utile
        un "cd" come primo passo, e corrisponde a quello che l'utente ha
        letto nel pannello.

        Ritorna (descrizione, output, errore)."""
        commands = candidate.get("shell") or []
        description = " ; ".join(commands)
        if sys.platform == "win32":
            # il resto del demone gira anche su Windows, questa parte no:
            # meglio dirlo che tradurre alla cieca comandi bash in cmd
            return description, "", "comandi da terminale non supportati su Windows"
        script = "set -e\n" + "\n".join(commands)
        try:
            # shell di login: senza il profilo dell'utente mancherebbero
            # PATH e simili, e comandi che nel suo terminale funzionano qui
            # fallirebbero senza un motivo comprensibile
            proc = subprocess.run(
                ["bash", "-lc", script],
                cwd=str(Path.home()),
                capture_output=True,
                text=True,
                timeout=CONTROL_SHELL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return description, "", (
                f"comando interrotto dopo {CONTROL_SHELL_TIMEOUT}s"
            )
        except OSError as exc:
            return description, "", f"impossibile eseguire il comando: {exc}"
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(output) > CONTROL_SHELL_OUTPUT_CHARS:
            output = output[:CONTROL_SHELL_OUTPUT_CHARS] + "\n[…]"
        if proc.returncode != 0:
            return description, output, f"uscito con codice {proc.returncode}"
        return description, output, None

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

    def _interpret_as_shortcuts(self, text, dashboard_id=None, allow_shell=False):
        """Chiede al backend LLM configurato (vedi llm_provider) di tradurre
        `text` (la frase dettata) in una o piu' combinazioni plausibili. Se
        `dashboard_id` e' indicato, da' priorita' alle scorciatoie gia'
        configurate in quella dashboard (vedi _dashboard_shortcuts_context).
        Chiamata di rete bloccante: va eseguita in un thread separato (vedi
        _ai_command_worker). Ritorna (candidati, errore): solo uno dei due
        e' valorizzato. Un solo candidato = comando chiaro, si esegue
        subito; piu' candidati = comando ambiguo, sceglie l'utente.

        `allow_shell` aggiunge ai candidati possibili i comandi da terminale
        (vedi AI_COMMAND_SHELL_PROMPT): lo passa solo il comando IA arrivato
        dal socket di controllo, che li mostra e li fa confermare."""
        # il nome della dashboard abilita la generazione di macro
        # (AI_COMMAND_MACRO_PROMPT_TEMPLATE) e la vincola a quell'app: senza
        # una dashboard nota non si generano macro, solo singole
        # combinazioni o avvii di applicazioni (vedi _dashboard_name)
        dashboard_name = self._dashboard_name(dashboard_id) if dashboard_id else None
        system_prompt = AI_COMMAND_SYSTEM_PROMPT
        if dashboard_name:
            system_prompt += AI_COMMAND_MACRO_PROMPT_TEMPLATE.format(name=dashboard_name)
        if allow_shell:
            system_prompt += AI_COMMAND_SHELL_PROMPT
        if dashboard_id:
            system_prompt += self._dashboard_shortcuts_context(dashboard_id)
        # un array JSON di candidati (specie con macro multi-passo) non sta
        # nei 20 token che bastavano alla vecchia risposta a combo singola
        reply, error = self._llm_complete(
            system_prompt, text, max_tokens=600, timeout=30
        )
        if error:
            return None, error

        candidates = self._parse_shortcut_candidates(
            reply, allow_macro=bool(dashboard_name), allow_shell=allow_shell
        )
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

    # --- comando IA dal socket di controllo (dashboard sul PC) ---
    # Stesso motore del comando vocale del telefono — stesso microfono,
    # stesso Whisper, stesso backend LLM — ma innescato da una finestra
    # sullo stesso PC. Due differenze cambiano il flusso:
    #
    #   * il fuoco della tastiera ce l'ha chi ha premuto il pulsante, quindi
    #     qui non si esegue mai nulla di propria iniziativa: il demone
    #     interpreta e parcheggia le opzioni, il pannello si toglie di mezzo
    #     e poi chiama ai_choose;
    #   * chi guarda ha uno schermo davanti, quindi puo' leggere e
    #     confermare un comando da terminale (vedi AI_COMMAND_SHELL_PROMPT),
    #     cosa che al telefono non viene mai proposta.
    #
    # Il pannello non ha un canale su cui ricevere notifiche: segue la
    # sessione interrogando ai_status (vedi _control_session_snapshot).

    def _set_control_session(self, **fields):
        """Aggiorna la sessione del comando IA in corso sul PC. `seq` cresce
        a ogni modifica: e' quello che permette al pannello di accorgersi
        che qualcosa e' cambiato senza confrontare tutto il resto."""
        with self._control_choice_lock:
            session = dict(self._control_session)
            session.update(fields)
            session["seq"] = self._control_session["seq"] + 1
            self._control_session = session

    def _reset_control_session(self, phase="idle"):
        with self._control_choice_lock:
            self._control_session = {
                "phase": phase,
                "text": "",
                "error": "",
                "request_id": "",
                "options": [],
                "executed": "",
                "output": "",
                "seq": self._control_session["seq"] + 1,
            }
            # le opzioni di una sessione precedente non sono piu' a video
            self._control_choice = None

    def _control_session_snapshot(self):
        with self._control_choice_lock:
            return dict(self._control_session)

    def _park_control_choice(self, text, candidates):
        """Mette le opzioni interpretate in attesa di una scelta e ritorna
        il request_id con cui richiamarle. Nessuna viene eseguita: e' il
        punto in cui il flusso del PC si separa da quello del telefono, che
        invece esegue subito quando il candidato e' uno solo."""
        request_id = secrets.token_hex(8)
        with self._control_choice_lock:
            # una nuova richiesta sostituisce la precedente: le opzioni di
            # prima non sono piu' sotto gli occhi di nessuno
            self._control_choice = {
                "id": request_id,
                "text": text,
                "options": candidates,
                "expires_at": time.monotonic() + AI_COMMAND_CHOICE_TIMEOUT,
            }
        return request_id

    def _handle_control_ai_record(self, dashboard_id=None):
        """Avvia (o ferma) la dettatura di un comando IA chiesta dal
        pannello sul PC: il pulsante fa da interruttore, un tocco per
        parlare e uno per finire. Lo stop automatico sul silenzio configurato
        nel demone continua a valere, quindi il secondo tocco e' facoltativo.

        La registrazione vera parte dal loop principale (vedi
        _on_control_ai_record): microfono e stato non si toccano dal thread
        di una connessione."""
        if self.state == STATE_RECORDING:
            if not self._recording_from_control:
                # dettatura avviata dal telefono: fermarla da qui
                # consegnerebbe il testo a un pannello che non l'ha chiesto
                return {
                    "ok": False,
                    "error": "e' in corso una dettatura avviata dal telefono",
                }
            self._set_control_session(phase="transcribing")
            self.command_queue.put(("control_ai_record", dashboard_id))
            return {"ok": True, "state": "transcribing"}
        if self.state != STATE_IDLE:
            return {"ok": False, "error": "il demone e' occupato, riprova fra un istante"}
        self._reset_control_session(phase="recording")
        self.command_queue.put(("control_ai_record", dashboard_id))
        # il seq della sessione appena aperta: chi si mette a seguirla lo usa
        # per non scambiare l'esito di quella precedente per il proprio
        return {
            "ok": True,
            "state": "recording",
            "seq": self._control_session_snapshot()["seq"],
        }

    def _on_control_ai_record(self, dashboard_id=None):
        """Meta' della registrazione eseguita sul loop principale, dove
        vivono stato e microfono."""
        if self.state == STATE_RECORDING:
            self.toggle_recording(mode="ai_command", dashboard_id=dashboard_id)
            return
        if self.state != STATE_IDLE:
            # nel frattempo il demone si e' messo a fare altro: la sessione
            # appena aperta non avra' mai un seguito, meglio dirlo subito
            self._set_control_session(
                phase="error", error="il demone e' occupato, riprova fra un istante"
            )
            return
        self._recording_from_control = True
        self.toggle_recording(mode="ai_command", dashboard_id=dashboard_id)

    def _control_ai_worker(self, text, dashboard_id=None):
        """Interpreta la frase dettata dal PC. Gemello di _ai_command_worker,
        con due differenze volute: i comandi da terminale sono ammessi, e
        nulla viene eseguito — nemmeno quando il candidato e' uno solo."""
        candidates, error = self._interpret_as_shortcuts(
            text, dashboard_id, allow_shell=True
        )
        if error:
            self._set_control_session(phase="error", text=text, error=error)
        else:
            request_id = self._park_control_choice(text, candidates)
            self._set_control_session(
                phase="choice",
                text=text,
                request_id=request_id,
                options=candidates,
                error="",
            )
        # lo stato torna libero sul loop principale, che e' l'unico a
        # muoverlo (vedi STATE_THINKING impostato in _on_transcription_done)
        self.command_queue.put(("control_ai_ready",))

    def _handle_control_ai_status(self):
        return {
            "ok": True,
            "state": self.state,
            "session": self._control_session_snapshot(),
        }

    def _handle_control_ai_cancel(self):
        """Scarta le opzioni in attesa di scelta. Non ferma una dettatura in
        corso: quella si chiude con lo stesso pulsante che l'ha avviata
        (ai_record)."""
        if self.state == STATE_RECORDING:
            return {
                "ok": False,
                "error": "dettatura in corso: fermala con lo stesso pulsante",
            }
        self._reset_control_session()
        return {"ok": True}

    def _handle_control_ai_command(self, text, dashboard_id=None):
        """Interpreta un comando IA gia' scritto (senza passare dal
        microfono) e ritorna le opzioni SENZA eseguirne nessuna, come fa la
        dettatura. Bloccante quanto la chiamata all'LLM: gira nel thread
        della connessione aperto da _handle_control_client, non nel loop
        principale."""
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "text mancante"}
        text = text.strip()
        if len(text) > CONTROL_AI_MAX_TEXT:
            return {
                "ok": False,
                "error": (
                    f"text troppo lungo ({len(text)} caratteri, massimo "
                    f"{CONTROL_AI_MAX_TEXT})"
                ),
            }
        candidates, error = self._interpret_as_shortcuts(
            text, dashboard_id, allow_shell=True
        )
        if error:
            self._set_control_session(phase="error", text=text, error=error)
            return {"ok": False, "error": error}
        request_id = self._park_control_choice(text, candidates)
        self._set_control_session(
            phase="choice",
            text=text,
            request_id=request_id,
            options=candidates,
            error="",
        )
        return {
            "ok": True,
            "request_id": request_id,
            "text": text,
            "options": candidates,
        }

    def _handle_control_ai_choose(
        self, request_id, index, delay_ms=None, confirm_shell=False
    ):
        """Esegue una delle opzioni proposte da _handle_control_ai_command.
        Si accetta solo un indice nell'elenco gia' proposto, mai una
        combinazione arbitraria: come il pannello di scelta del telefono
        (vedi _on_ai_choice_reply), questo comando non deve diventare una
        via per far premere al demone qualunque tasto.

        `delay_ms` e' la pausa prima di simulare i tasti: serve a chi ha
        appena nascosto la propria finestra per restituire il fuoco
        all'applicazione da comandare, che il compositor non sposta
        istantaneamente.

        `confirm_shell` deve valere True per eseguire un candidato da
        terminale: e' la conferma che l'utente ha letto i comandi. Un
        client che la omette si vede rifiutare l'esecuzione, cosi' la
        conferma e' parte del protocollo e non una buona intenzione della
        singola interfaccia."""
        if delay_ms is None:
            delay_ms = 0
        if not isinstance(delay_ms, int) or not (0 <= delay_ms <= MACRO_MAX_DELAY_MS):
            return {
                "ok": False,
                "error": (
                    f"delay_ms deve essere un intero fra 0 e "
                    f"{MACRO_MAX_DELAY_MS}"
                ),
            }
        with self._control_choice_lock:
            pending = self._control_choice
            if pending is None or pending["id"] != request_id:
                return {"ok": False, "error": "richiesta sconosciuta o gia' risolta"}
            if pending["expires_at"] < time.monotonic():
                self._control_choice = None
                return {"ok": False, "error": "richiesta scaduta"}
            if not isinstance(index, int) or not 0 <= index < len(pending["options"]):
                return {"ok": False, "error": f"index non valido: {index}"}
            chosen = pending["options"][index]
            if chosen.get("shell") and not confirm_shell:
                # la richiesta resta in attesa: il pannello deve mostrare i
                # comandi e richiamare con la conferma, non ritentare a vuoto
                return {
                    "ok": False,
                    "error": "un comando da terminale richiede una conferma esplicita",
                    "needs_confirm": True,
                    "shell": chosen["shell"],
                }
            # consumata: una richiesta esegue una sola volta, anche se il
            # client rimanda lo stesso comando due volte
            self._control_choice = None
            text = pending["text"]

        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        output = ""
        if chosen.get("shell"):
            executed, output, error = self._run_shell_candidate(chosen)
        else:
            executed, error = self._execute_candidate(chosen)
        # niente _broadcast qui: il comando non e' partito dal telefono e un
        # "result" inatteso li' non descriverebbe nulla che l'utente stia
        # guardando. La notifica di sistema arriva invece sullo stesso
        # schermo da cui e' stato dato il comando.
        if error:
            self._notify("Stenografa - comando AI", error, urgency="critical")
        else:
            self._notify("Stenografa - comando eseguito", f'"{text}" -> {executed}')
        self._set_control_session(
            phase="error" if error else "done",
            text=text,
            executed=executed,
            output=output,
            error=error or "",
            options=[],
            request_id="",
        )
        return {
            "ok": True,
            "text": text,
            "executed": executed,
            "output": output,
            "error": error,
        }

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
        if self.wake_word_enabled:
            self.wake_listener.start()
        try:
            self._main_loop()
        finally:
            # l'ascolto tiene aperto il microfono: va chiuso per primo,
            # altrimenti il processo di cattura sopravvive al demone
            try:
                self.wake_listener.stop()
            except Exception:
                pass
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

"""
Layer di astrazione tra daemon.py e il sistema operativo.

Le funzioni davvero specifiche del SO (registrazione audio, appunti, simulazione
di tasti, notifiche desktop, rilevamento della finestra col focus) sono isolate
qui dietro un'interfaccia comune (`Backend`), cosi' daemon.py resta identico su
tutti i sistemi operativi e la logica pura (parsing, validazione, layout) resta
testabile senza dover simulare l'ambiente grafico.

Stato dei backend:
- Linux: implementato e verificato (l'unico ambiente su cui e' stato testato
  finora). Usa `pw-record` (PipeWire) per la registrazione, `wl-copy` per gli
  appunti, `ydotool`/`ydotoold` per simulare i tasti, `notify-send` per le
  notifiche, l'estensione GNOME Shell "Window Calls" (via D-Bus) per il
  rilevamento della finestra col focus.
- Windows: scritto seguendo le API documentate (pywin32, pynput, sounddevice)
  ma MAI TESTATO su una macchina Windows reale. Va verificato e probabilmente
  corretto alla prima prova.
- macOS: idem, scritto con pyobjc/pynput/sounddevice ma mai testato. In piu'
  richiede che l'utente conceda manualmente il permesso "Accessibilita'"
  all'interprete Python in Impostazioni di Sistema > Privacy e Sicurezza,
  altrimenti la simulazione dei tasti fallisce silenziosamente (non e' un
  permesso concedibile da codice). Il rilevamento della finestra col focus su
  macOS usa solo il nome dell'applicazione in primo piano (non il titolo della
  finestra/scheda), perche' leggere il titolo richiederebbe anch'esso il
  permesso di Accessibilita' tramite le API AXUIElement.

Le dipendenze extra (pywin32/pynput/sounddevice/soundfile su Windows,
pyobjc/pynput/sounddevice/soundfile su macOS) si installano solo sul sistema
in cui servono: vedi requirements-windows.txt / requirements-macos.txt.
"""
import platform
import subprocess
import time


def _current_os():
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "unknown"


class Backend:
    """Interfaccia comune implementata da ogni backend per sistema operativo."""

    def start_recording(self, wav_path):
        """Avvia la registrazione del microfono su file (mono, 16kHz, s16)."""
        raise NotImplementedError

    def stop_recording(self):
        """Ferma la registrazione avviata da start_recording."""
        raise NotImplementedError

    def copy_to_clipboard(self, text):
        raise NotImplementedError

    def read_clipboard(self):
        """Ritorna il contenuto testuale attuale degli appunti, o None se
        non disponibile/non testuale/vuoto."""
        raise NotImplementedError

    def simulate_keys(self, combo):
        """Simula una combinazione di tasti (es. 'ctrl+v') nella finestra col
        focus. Ritorna True se la simulazione e' andata a buon fine."""
        raise NotImplementedError

    def notify(self, title, body, urgency="normal"):
        raise NotImplementedError

    def get_focused_window(self):
        """Ritorna (id, app_name, title) della finestra/app col focus, o
        None se non determinabile. `id` e' un identificatore stabile finche'
        la finestra resta la stessa (usato solo per rilevare i cambi di
        focus, non ha un formato garantito tra un backend e l'altro)."""
        raise NotImplementedError

    def list_apps(self):
        """Ritorna le applicazioni installate avviabili senza parametri,
        come lista di dict {"id": str, "name": str}. `id` e' opaco (il
        formato dipende dal backend: percorso di un file .desktop su Linux,
        percorso di un bundle .app su macOS, AppID su Windows) e va passato
        invariato a launch_app."""
        raise NotImplementedError

    def launch_app(self, app_id):
        """Avvia l'applicazione identificata da `app_id` (ottenuto da
        list_apps). Ritorna True se il lancio e' andato a buon fine."""
        raise NotImplementedError

    def app_icon_png(self, app_id):
        """Icona dell'applicazione (`app_id` da list_apps) come byte di un
        PNG, o None se non se ne trova una. Serve al telefono per mostrarla
        in filigrana dietro i pulsanti, cosi' si riconosce a colpo d'occhio
        a che applicazione appartiene una dashboard. L'implementazione di
        default non ne trova nessuna: i pulsanti restano tinta unita."""
        return None

    # --- controllo dei player multimediali ---
    # L'implementazione di default dice "nessun player": e' quella che usano
    # Windows e macOS, dove il rilevamento non e' implementato (su Linux si
    # appoggia a MPRIS, lo standard D-Bus che usano browser e riproduttori).
    # Il telefono non mostra nessuna icona quando la lista e' vuota, quindi
    # la funzione si spegne da sola dove non e' supportata.

    def list_media_players(self):
        """Player multimediali attivi sul PC, come lista di dict
        {"id": str, "name": str, "title": str, "playing": bool}. `id` e'
        opaco e va passato invariato a media_player_pause/play."""
        return []

    def media_player_pause(self, player_id):
        """Mette in pausa il player indicato. True se il comando e' andato a
        buon fine."""
        return False

    def media_player_play(self, player_id):
        """Fa riprendere il player indicato. True se il comando e' andato a
        buon fine."""
        return False

    def list_audio_streams(self):
        """Flussi audio in uscita, uno per applicazione (su Linux uno per
        scheda del browser che sta suonando), come lista di dict
        {"id", "name": str, "muted": bool, "active": bool}. Serve dove i
        controlli dei player non arrivano: un browser pubblica un solo
        player anche con piu' schede che riproducono, ma i flussi audio
        restano distinti."""
        return []

    def set_audio_stream_muted(self, stream_id, muted):
        """Silenzia o riattiva il flusso indicato. True se il comando e'
        andato a buon fine."""
        return False

    def shutdown(self):
        """Rilascia risorse (processi in background, ecc.) alla chiusura del
        demone. Facoltativo: l'implementazione di default non fa nulla."""


# --- icone delle applicazioni -----------------------------------------------
# Servono al telefono per disegnarle in filigrana dietro i pulsanti. Sono
# funzioni di modulo perche' le usano piu' backend: cambia solo il modo di
# trovare il file (file .desktop su Linux, bundle su macOS, eseguibile su
# Windows), non cosa farne una volta trovato.

# Il telefono la ingrandisce dietro il pulsante: oltre questa misura si
# pagherebbe banda per pixel che non si vedono.
ICON_TARGET_PX = 256
# oltre questa soglia si prova a rimpicciolire: certe icone di sistema
# superano i 200 KB, che su una rete domestica sono comunque tanti per un
# dettaglio decorativo
ICON_MAX_BYTES = 60_000


def _icon_size_score(path):
    """Quanto e' adatto un file: si preferisce il PNG piu' vicino a
    ICON_TARGET_PX, e a parita' di tutto il vettoriale (che si converte alla
    misura esatta). Piu' basso e' meglio."""
    import re

    lower = path.lower()
    if lower.endswith(".svg"):
        return (1, 0)
    if not lower.endswith(".png"):
        return (2, 0)
    sizes = [int(n) for n in re.findall(r"(\d{2,4})", path)]
    best = min(sizes, key=lambda s: abs(s - ICON_TARGET_PX)) if sizes else 0
    return (0, abs(best - ICON_TARGET_PX))


def _best_icon_file(paths):
    import os

    existing = [p for p in paths if os.path.isfile(p)]
    if not existing:
        return None
    return min(existing, key=_icon_size_score)


def _convert_icon(path):
    """Converte/rimpicciolisce con ImageMagick, l'unico modo per leggere un
    SVG senza aggiungere dipendenze Python. Se non e' installato si rinuncia
    all'icona invece di mandare al telefono qualcosa che non sa disegnare."""
    try:
        result = subprocess.run(
            ["magick", path, "-resize", f"{ICON_TARGET_PX}x{ICON_TARGET_PX}", "png:-"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except Exception:
        return None
    return result.stdout or None


def _icon_file_to_png(path):
    """Byte PNG pronti per il telefono: il file cosi' com'e' se e' gia' un
    PNG di peso ragionevole, altrimenti convertito."""
    import os

    if path is None or not os.path.isfile(path):
        return None
    if path.lower().endswith(".png"):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if len(data) <= ICON_MAX_BYTES:
            return data
        return _convert_icon(path) or data
    return _convert_icon(path)


# --- Linux (implementato e verificato) --------------------------------------

# prefisso dei bus name MPRIS: ogni riproduttore (browser, player video/audio)
# che espone i controlli standard si registra come org.mpris.MediaPlayer2.<app>
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# integrazione browser di Plasma: espone come player le stesse schede che il
# browser pubblica gia' per conto suo, quindi lo stesso video comparirebbe due
# volte. A parita' di titolo/durata si tiene il player nativo (vedi
# _dedupe_media_players).
MPRIS_BROWSER_PROXY = "plasma-browser-integration"


def _unescape_gvariant(text):
    return text.replace("\\\\", "\\").replace("\\'", "'").replace('\\"', '"')


def _parse_mpris_properties(raw):
    """Estrae stato, titolo e durata dall'output di
    `gdbus call ... org.freedesktop.DBus.Properties.GetAll`.

    L'output e' un GVariant (`{'PlaybackStatus': <'Playing'>, 'Metadata':
    <{'xesam:title': <'...'>}>, ...}`), che non e' parsabile come letterale
    Python: si estraggono con delle espressioni regolari i soli tre campi che
    servono. Ritorna None se non e' nemmeno un elenco di proprieta' di un
    player (nessun PlaybackStatus)."""
    import re

    status = re.search(r"'PlaybackStatus': <'([A-Za-z]+)'>", raw)
    if status is None:
        return None
    title = re.search(r"'xesam:title': <'((?:[^'\\]|\\.)*)'>", raw)
    length = re.search(r"'mpris:length': <u?int64 (\d+)>", raw)
    return {
        "playing": status.group(1) == "Playing",
        "title": _unescape_gvariant(title.group(1)) if title else "",
        # la durata da sola non serve a niente, ma insieme al titolo
        # identifica lo stesso video esposto da piu' bus (vedi
        # _dedupe_media_players)
        "length": int(length.group(1)) if length else None,
    }


def _parse_sink_inputs(raw):
    """Estrae i flussi audio dall'output di `pactl -f json list sink-inputs`.
    A differenza di MPRIS qui c'e' del JSON vero, quindi niente regex.
    Ritorna una lista vuota se l'output non e' utilizzabile (pactl assente,
    versione senza supporto JSON, server audio non raggiungibile)."""
    import json

    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    streams = []
    for entry in entries:
        if not isinstance(entry, dict) or "index" not in entry:
            continue
        props = entry.get("properties") or {}
        name = (
            props.get("application.name")
            or props.get("media.name")
            or f"flusso {entry['index']}"
        )
        streams.append(
            {
                "id": entry["index"],
                "name": name,
                "muted": bool(entry.get("mute")),
                # "corked" = flusso sospeso: l'applicazione lo tiene aperto
                # ma non ci sta scrivendo audio (video in pausa, scheda che
                # ha finito di riprodurre)
                "active": not entry.get("corked", False),
            }
        )
    return streams


def _media_player_name(bus_name):
    """Nome leggibile ricavato dal bus name (es.
    "org.mpris.MediaPlayer2.brave.instance49328" -> "Brave"): evita una
    seconda chiamata D-Bus per la proprieta' Identity, che servirebbe solo a
    scrivere la stessa cosa."""
    rest = bus_name[len(MPRIS_PREFIX):] if bus_name.startswith(MPRIS_PREFIX) else bus_name
    return (rest.split(".")[0] or bus_name).capitalize()


def _normalize_title(title):
    """Titolo ridotto alla forma confrontabile fra bus diversi: i browser
    basati su Chromium antepongono il contatore delle notifiche ("(405) ")
    al titolo della scheda, l'integrazione di Plasma riporta invece il
    titolo pulito del video."""
    import re

    return re.sub(r"^\(\d+\)\s*", "", title.strip().lower())


def _same_media(a, b):
    """Due voci descrivono lo stesso video? Serve la stessa durata (che i
    bus riportano identica) e titoli compatibili — uno contenuto nell'altro,
    perche' il browser ci aggiunge il nome del sito (" - YouTube") che il
    proxy di Plasma non mette."""
    if a["length"] != b["length"]:
        return False
    title_a = _normalize_title(a["title"])
    title_b = _normalize_title(b["title"])
    if not title_a or not title_b:
        # senza titoli da confrontare ci si fida solo di una durata vera:
        # due player senza metadati restano distinti
        return a["length"] is not None
    return title_a in title_b or title_b in title_a


def _dedupe_media_players(players):
    """Toglie lo stesso video esposto da piu' bus: l'integrazione browser di
    Plasma ripubblica le schede che il browser espone gia' per conto suo, e
    sul telefono comparirebbero due icone per un solo video. Del doppione si
    tiene il player nativo, che e' quello che risponde meglio ai comandi."""
    kept = []
    for player in players:
        for i, other in enumerate(kept):
            if not _same_media(player, other):
                continue
            if MPRIS_BROWSER_PROXY in other["id"]:
                kept[i] = player
            break
        else:
            kept.append(player)
    return kept


class LinuxBackend(Backend):
    def __init__(self, runtime_dir):
        import os

        self._runtime_dir = runtime_dir
        self._record_proc = None
        self._ydotoold_proc = None
        self._ydotool_socket_path = os.path.join(
            runtime_dir, "stenografa-ydotool.sock"
        )

    def start_recording(self, wav_path):
        import subprocess

        self._record_proc = subprocess.Popen(
            [
                "pw-record",
                "--channels=1",
                "--rate=16000",
                "--format=s16",
                wav_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_recording(self):
        proc = self._record_proc
        self._record_proc = None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def copy_to_clipboard(self, text):
        subprocess.run(["wl-copy"], input=text, text=True, check=False)

    def read_clipboard(self):
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            return None
        # wl-paste ritorna un codice diverso da zero se gli appunti sono
        # vuoti o non contengono testo: in tal caso non c'e' nulla da
        # ripristinare in seguito
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout

    def _ensure_ydotoold(self):
        import os

        if self._ydotoold_proc is not None and self._ydotoold_proc.poll() is None:
            return True
        # file di socket "morto" (nessun processo dietro, es. da
        # un'esecuzione precedente del demone): va rimosso prima di
        # rilanciare ydotoold, altrimenti il bind fallisce
        if os.path.exists(self._ydotool_socket_path):
            try:
                os.remove(self._ydotool_socket_path)
            except OSError:
                pass
        try:
            self._ydotoold_proc = subprocess.Popen(
                [
                    "ydotoold",
                    f"--socket-path={self._ydotool_socket_path}",
                    "--socket-perm=0600",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        import os as _os

        for _ in range(20):
            if _os.path.exists(self._ydotool_socket_path):
                return True
            time.sleep(0.1)
        return False

    def simulate_keys(self, combo):
        import os

        from key_combo import parse_key_combo_linux

        codes = parse_key_combo_linux(combo)
        if not codes:
            return False
        if not self._ensure_ydotoold():
            return False
        env = os.environ.copy()
        env["YDOTOOL_SOCKET"] = self._ydotool_socket_path
        args = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        result = subprocess.run(
            ["ydotool", "key", *args],
            env=env,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def notify(self, title, body, urgency="normal"):
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "Stenografa", title, body],
            check=False,
        )

    def get_focused_window(self):
        import ast
        import json

        try:
            result = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.gnome.Shell",
                    "--object-path", "/org/gnome/Shell/Extensions/Windows",
                    "--method", "org.gnome.Shell.Extensions.Windows.List",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            (json_text,) = ast.literal_eval(result.stdout.strip())
            for w in json.loads(json_text):
                if w.get("focus"):
                    return w.get("id"), w.get("wm_class", ""), w.get("title", "")
        except Exception:
            pass
        return None

    # --- player multimediali (MPRIS su D-Bus, lo stesso standard che usano
    # KDE Connect e i widget multimediali del desktop) ---

    def list_media_players(self):
        players = []
        for bus_name in self._mpris_bus_names():
            raw = self._gdbus(
                [
                    "call", "--session",
                    "--dest", bus_name,
                    "--object-path", MPRIS_PATH,
                    "--method", "org.freedesktop.DBus.Properties.GetAll",
                    MPRIS_PLAYER_IFACE,
                ]
            )
            if raw is None:
                continue
            props = _parse_mpris_properties(raw)
            if props is None:
                continue
            players.append(
                {
                    "id": bus_name,
                    "name": self._media_player_display_name(bus_name),
                    "title": props["title"],
                    "playing": props["playing"],
                    "length": props["length"],
                }
            )
        deduped = _dedupe_media_players(players)
        # "length" serve solo alla deduplica: non ha motivo di viaggiare fino
        # al telefono
        for player in deduped:
            player.pop("length", None)
        return deduped

    def media_player_pause(self, player_id):
        return self._mpris_call(player_id, "Pause")

    def media_player_play(self, player_id):
        return self._mpris_call(player_id, "Play")

    def _media_player_display_name(self, bus_name):
        """Nome da mostrare accanto al titolo. Per l'integrazione browser di
        Plasma il bus name direbbe solo "plasma-browser-integration": in quel
        caso (e solo in quello) si paga una chiamata in piu' per chiedere
        l'Identity, che riporta il browser vero."""
        if MPRIS_BROWSER_PROXY not in bus_name:
            return _media_player_name(bus_name)
        import re

        raw = self._gdbus(
            [
                "call", "--session",
                "--dest", bus_name,
                "--object-path", MPRIS_PATH,
                "--method", "org.freedesktop.DBus.Properties.Get",
                "org.mpris.MediaPlayer2", "Identity",
            ]
        )
        identity = re.search(r"<'((?:[^'\\]|\\.)*)'>", raw or "")
        return _unescape_gvariant(identity.group(1)) if identity else "Browser"

    def _mpris_bus_names(self):
        raw = self._gdbus(
            [
                "call", "--session",
                "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus",
                "--method", "org.freedesktop.DBus.ListNames",
            ]
        )
        if raw is None:
            return []
        import re

        return sorted(re.findall(r"'(" + re.escape(MPRIS_PREFIX) + r"[^']+)'", raw))

    def _mpris_call(self, bus_name, method):
        if not bus_name or not bus_name.startswith(MPRIS_PREFIX):
            return False
        # timeout piu' corto della lettura: la pausa puo' precedere l'avvio
        # della registrazione (vedi pause_media_while_recording in daemon.py)
        # e un player che non risponde non deve far aspettare chi sta per
        # parlare
        raw = self._gdbus(
            [
                "call", "--session",
                "--dest", bus_name,
                "--object-path", MPRIS_PATH,
                "--method", f"{MPRIS_PLAYER_IFACE}.{method}",
            ],
            timeout=1,
        )
        return raw is not None

    # --- flussi audio (PipeWire/PulseAudio via pactl): rete di sicurezza
    # dove i player non bastano. Un browser pubblica un solo player MPRIS per
    # istanza, ma ogni scheda che suona ha il suo flusso audio, quindi da qui
    # si raggiunge anche quello che i controlli play/pausa non vedono ---

    def list_audio_streams(self):
        raw = self._run(["pactl", "-f", "json", "list", "sink-inputs"])
        return _parse_sink_inputs(raw)

    def set_audio_stream_muted(self, stream_id, muted):
        raw = self._run(
            ["pactl", "set-sink-input-mute", str(stream_id), "1" if muted else "0"],
            timeout=1,
        )
        return raw is not None

    @staticmethod
    def _run(args, timeout=2):
        """Esegue un comando e ritorna il suo stdout, None a qualunque
        problema: comando non installato, server non raggiungibile, flusso
        sparito fra la lettura e il comando. Sono tutti casi in cui la
        funzione deve semplicemente restare inerte."""
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
        except Exception:
            return None
        return result.stdout

    def _gdbus(self, args, timeout=2):
        """Esegue `gdbus` e ritorna il suo stdout, None a qualunque
        problema (comando assente, sessione D-Bus non raggiungibile, player
        sparito fra l'elenco e la lettura: tutti casi in cui semplicemente
        non ci sono controlli da mostrare)."""
        return self._run(["gdbus", *args], timeout=timeout)

    # elenco applicazioni: parsing dei file .desktop (specifica
    # freedesktop.org), la fonte standard su qualunque distribuzione Linux
    # per le app installate sia da pacchetto di sistema che Flatpak/Snap
    _DESKTOP_DIRS = (
        "/usr/share/applications",
        "/usr/local/share/applications",
        "~/.local/share/applications",
        "/var/lib/flatpak/exports/share/applications",
        "~/.local/share/flatpak/exports/share/applications",
        "/var/lib/snapd/desktop/applications",
    )

    def list_apps(self):
        import configparser
        import glob
        import os

        apps = {}
        for raw_dir in self._DESKTOP_DIRS:
            for path in glob.glob(os.path.join(os.path.expanduser(raw_dir), "*.desktop")):
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    parser.read(path, encoding="utf-8")
                except (OSError, UnicodeDecodeError, configparser.Error):
                    continue
                if "Desktop Entry" not in parser:
                    continue
                entry = parser["Desktop Entry"]
                if entry.get("Type", "Application") != "Application":
                    continue
                # niente voci nascoste dal menu (spesso helper interni, non
                # pensate per essere avviate direttamente dall'utente)
                if entry.getboolean("NoDisplay", fallback=False):
                    continue
                if entry.getboolean("Hidden", fallback=False):
                    continue
                name = entry.get("Name")
                if not name:
                    continue
                apps[path] = name  # stessa app in piu' directory: l'ultima vince
        return [
            {"id": path, "name": name}
            for path, name in sorted(apps.items(), key=lambda kv: kv[1].lower())
        ]

    def launch_app(self, app_id):
        # niente capture_output: l'applicazione avviata da "gio launch"
        # spesso eredita i file descriptor di stdout/stderr del processo
        # "gio" (che esce subito dopo l'avvio) e li tiene aperti finche'
        # resta in esecuzione lei stessa - con capture_output=True,
        # subprocess.run() legge dalle pipe finche' non riceve EOF, quindi
        # resterebbe bloccato per tutta la durata dell'app appena aperta
        # invece di tornare subito
        try:
            result = subprocess.run(
                ["gio", "launch", app_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    _ICON_THEME_DIRS = (
        "~/.local/share/icons",
        "/usr/share/icons",
        "/var/lib/flatpak/exports/share/icons",
        "~/.local/share/flatpak/exports/share/icons",
    )
    _ICON_FLAT_DIRS = (
        "/usr/share/pixmaps",
        "/var/lib/flatpak/exports/share/pixmaps",
    )

    def app_icon_png(self, app_id):
        """Il file .desktop dichiara `Icon=`, che puo' essere un percorso
        gia' pronto oppure un nome da cercare nei temi installati (specifica
        freedesktop.org). I temi non concordano sulla struttura delle
        cartelle — "48x48/apps/nome.png" per hicolor e Papirus, "apps/48/
        nome.png" per altri — quindi si cercano entrambe le forme e si
        sceglie poi il file migliore (vedi _best_icon_file)."""
        import configparser
        import glob
        import os

        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(app_id, encoding="utf-8")
        except (OSError, UnicodeDecodeError, configparser.Error):
            return None
        if "Desktop Entry" not in parser:
            return None
        name = (parser["Desktop Entry"].get("Icon") or "").strip()
        if not name:
            return None
        if os.path.isabs(name):
            return _icon_file_to_png(name)

        candidates = []
        for base in self._ICON_THEME_DIRS:
            base = os.path.expanduser(base)
            for pattern in (
                f"{base}/*/*/apps/{name}.*",
                f"{base}/*/apps/*/{name}.*",
            ):
                candidates.extend(glob.glob(pattern))
        for flat in self._ICON_FLAT_DIRS:
            candidates.extend(glob.glob(f"{flat}/{name}.*"))

        best = _best_icon_file(candidates)
        return _icon_file_to_png(best) if best else None

    def shutdown(self):
        import os

        if self._ydotoold_proc is not None:
            self._ydotoold_proc.terminate()
            try:
                self._ydotoold_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._ydotoold_proc.kill()
        if os.path.exists(self._ydotool_socket_path):
            os.remove(self._ydotool_socket_path)


# --- Windows (scritto seguendo le API documentate, MAI TESTATO) -------------


class WindowsBackend(Backend):
    """Non testato su Windows reale: verificare pywin32/pynput/sounddevice
    con una macchina/VM Windows prima di usarlo in produzione."""

    def __init__(self):
        self._stream = None
        self._sound_file = None

    def start_recording(self, wav_path):
        import sounddevice as sd
        import soundfile as sf

        self._sound_file = sf.SoundFile(
            wav_path, mode="w", samplerate=16000, channels=1, subtype="PCM_16"
        )

        def _callback(indata, frames, time_info, status):
            self._sound_file.write(indata)

        self._stream = sd.InputStream(
            samplerate=16000, channels=1, dtype="int16", callback=_callback
        )
        self._stream.start()

    def stop_recording(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._sound_file is not None:
            self._sound_file.close()
            self._sound_file = None

    def copy_to_clipboard(self, text):
        import pyperclip

        pyperclip.copy(text)

    def read_clipboard(self):
        try:
            import pyperclip

            text = pyperclip.paste()
            return text or None
        except Exception:
            return None

    def simulate_keys(self, combo):
        from key_combo import parse_key_combo_pynput

        keys = parse_key_combo_pynput(combo)
        if not keys:
            return False
        try:
            from pynput.keyboard import Controller

            controller = Controller()
            for k in keys:
                controller.press(k)
            for k in reversed(keys):
                controller.release(k)
            return True
        except Exception:
            return False

    def notify(self, title, body, urgency="normal"):
        try:
            from plyer import notification

            notification.notify(title=title, message=body, app_name="Stenografa")
        except Exception:
            pass

    def get_focused_window(self):
        try:
            import win32gui
            import win32process
            import psutil

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc_name = psutil.Process(pid).name()
            except Exception:
                proc_name = ""
            return hwnd, proc_name, title
        except Exception:
            return None

    def list_apps(self):
        import json

        # Get-StartApps enumera in un colpo solo sia le voci "classiche" del
        # menu Start (collegamenti .lnk) sia le app UWP/Store, ciascuna con
        # Name + AppID: piu' affidabile che fare parsing manuale dei .lnk
        # (serve pywin32 aggiuntivo) o leggere il registro (non copre le
        # UWP). AppID e' anche cio' che launch_app usa per il lancio.
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-StartApps | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):  # un solo risultato: non e' una lista JSON
            data = [data]
        apps = []
        for entry in data:
            name = entry.get("Name") if isinstance(entry, dict) else None
            app_id = entry.get("AppID") if isinstance(entry, dict) else None
            if name and app_id:
                apps.append({"id": app_id, "name": name})
        apps.sort(key=lambda a: a["name"].lower())
        return apps

    def launch_app(self, app_id):
        try:
            # niente capture_output: stesso rischio di blocco indefinito di
            # LinuxBackend.launch_app se l'app avviata ereditasse i file
            # descriptor di stdout/stderr di explorer.exe restando aperta
            subprocess.run(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            # explorer.exe ritorna spesso 1 anche quando il lancio riesce:
            # il codice di uscita non e' un indicatore affidabile su
            # Windows, quindi consideriamo riuscito il solo avvio del
            # processo senza eccezioni (stesso limite riconosciuto anche
            # per get_focused_window/simulate_keys su questo backend, mai
            # testato su una macchina Windows reale)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    # PowerShell fa da ponte verso .NET e verso il registro dei pacchetti:
    # e' l'unico modo per arrivare all'icona senza aggiungere dipendenze
    # Python (pywin32 basterebbe per gli .exe ma non per le app dello Store).
    # Il PNG torna in base64: lo stdout di PowerShell non e' un canale
    # affidabile per i byte grezzi.
    _ICON_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Drawing
$appId = $args[0]
$png = $null
if ($appId -like '*!*') {
    # app dello Store: il logo e' un file dentro la cartella del pacchetto
    $family = $appId.Split('!')[0]
    $pkg = Get-AppxPackage | Where-Object { $_.PackageFamilyName -eq $family } | Select-Object -First 1
    if ($pkg) {
        $logo = Get-ChildItem -Path $pkg.InstallLocation -Recurse -Include '*Logo*.png','*logo*.png' |
                Sort-Object Length -Descending | Select-Object -First 1
        if ($logo) { $png = [System.IO.File]::ReadAllBytes($logo.FullName) }
    }
} else {
    # voce classica del menu Start: si risale al .lnk e da questo all'exe
    $dirs = @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
              "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
    $target = $null
    foreach ($d in $dirs) {
        $lnk = Get-ChildItem -Path $d -Recurse -Filter '*.lnk' |
               Where-Object { $appId -like ('*' + $_.BaseName + '*') } | Select-Object -First 1
        if ($lnk) {
            $shell = New-Object -ComObject WScript.Shell
            $target = $shell.CreateShortcut($lnk.FullName).TargetPath
            if ($target) { break }
        }
    }
    if (-not $target -and (Test-Path $appId)) { $target = $appId }
    if ($target -and (Test-Path $target)) {
        $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($target)
        if ($icon) {
            $stream = New-Object System.IO.MemoryStream
            $icon.ToBitmap().Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
            $png = $stream.ToArray()
        }
    }
}
if ($png) { [Convert]::ToBase64String($png) }
"""

    def app_icon_png(self, app_id):
        import base64

        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive",
                    "-Command", self._ICON_PS, "-args", app_id,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        encoded = (result.stdout or "").strip()
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded)
        except ValueError:
            return None


# --- macOS (scritto seguendo le API documentate, MAI TESTATO) ---------------


class MacBackend(Backend):
    """Non testato su macOS reale. La simulazione dei tasti richiede il
    permesso "Accessibilita'" concesso manualmente all'interprete Python in
    Impostazioni di Sistema > Privacy e Sicurezza > Accessibilita': senza,
    fallisce silenziosamente. Il rilevamento app attiva usa solo il nome
    dell'applicazione (non il titolo finestra/scheda), perche' leggere il
    titolo richiederebbe anch'esso il permesso di Accessibilita'."""

    def __init__(self):
        self._stream = None
        self._sound_file = None

    def start_recording(self, wav_path):
        import sounddevice as sd
        import soundfile as sf

        self._sound_file = sf.SoundFile(
            wav_path, mode="w", samplerate=16000, channels=1, subtype="PCM_16"
        )

        def _callback(indata, frames, time_info, status):
            self._sound_file.write(indata)

        self._stream = sd.InputStream(
            samplerate=16000, channels=1, dtype="int16", callback=_callback
        )
        self._stream.start()

    def stop_recording(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._sound_file is not None:
            self._sound_file.close()
            self._sound_file = None

    def copy_to_clipboard(self, text):
        import pyperclip

        pyperclip.copy(text)

    def read_clipboard(self):
        try:
            import pyperclip

            text = pyperclip.paste()
            return text or None
        except Exception:
            return None

    def simulate_keys(self, combo):
        from key_combo import parse_key_combo_pynput

        keys = parse_key_combo_pynput(combo)
        if not keys:
            return False
        try:
            from pynput.keyboard import Controller

            controller = Controller()
            for k in keys:
                controller.press(k)
            for k in reversed(keys):
                controller.release(k)
            return True
        except Exception:
            return False

    def notify(self, title, body, urgency="normal"):
        try:
            from plyer import notification

            notification.notify(title=title, message=body, app_name="Stenografa")
        except Exception:
            # fallback: osascript e' presente su ogni macOS anche senza plyer
            try:
                script = (
                    f'display notification "{body}" with title "{title}"'
                )
                subprocess.run(["osascript", "-e", script], check=False)
            except Exception:
                pass

    def get_focused_window(self):
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            name = app.localizedName() or ""
            pid = app.processIdentifier()
            # niente API pubblica senza permesso di Accessibilita' per il
            # titolo della finestra specifica: usiamo il nome app due volte,
            # cosi' il match funziona comunque su "match in wm_class o title"
            return pid, name, name
        except Exception:
            return None

    _APP_DIRS = ("/Applications", "/System/Applications", "~/Applications")

    def list_apps(self):
        import glob
        import os
        import plistlib

        apps = {}
        for raw_dir in self._APP_DIRS:
            base = os.path.expanduser(raw_dir)
            # un livello di annidamento in piu' (es. "/Applications/Utility/
            # Foo.app"), senza scendere ricorsivamente ovunque
            paths = glob.glob(os.path.join(base, "*.app")) + glob.glob(
                os.path.join(base, "*", "*.app")
            )
            for path in paths:
                name = os.path.splitext(os.path.basename(path))[0]
                info_plist = os.path.join(path, "Contents", "Info.plist")
                try:
                    with open(info_plist, "rb") as f:
                        plist = plistlib.load(f)
                    name = plist.get("CFBundleName") or plist.get(
                        "CFBundleDisplayName"
                    ) or name
                except (OSError, plistlib.InvalidFileException):
                    pass
                apps[path] = name
        return [
            {"id": path, "name": name}
            for path, name in sorted(apps.items(), key=lambda kv: kv[1].lower())
        ]

    def launch_app(self, app_id):
        # niente capture_output: stesso rischio di blocco indefinito di
        # LinuxBackend.launch_app se l'app avviata ereditasse i file
        # descriptor di stdout/stderr di "open" restando aperta
        try:
            result = subprocess.run(
                ["open", app_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    def app_icon_png(self, app_id):
        """L'icona di un bundle .app e' un .icns dichiarato nell'Info.plist
        (o l'unico presente fra le risorse). Si converte con `sips`, che c'e'
        su ogni macOS: nessuna dipendenza da installare."""
        import glob
        import os
        import plistlib
        import tempfile

        resources = os.path.join(app_id, "Contents", "Resources")
        icon_name = None
        try:
            with open(os.path.join(app_id, "Contents", "Info.plist"), "rb") as f:
                plist = plistlib.load(f)
            icon_name = plist.get("CFBundleIconFile")
        except (OSError, plistlib.InvalidFileException):
            pass

        icns = None
        if icon_name:
            candidate = os.path.join(resources, icon_name)
            if not candidate.lower().endswith(".icns"):
                candidate += ".icns"
            if os.path.isfile(candidate):
                icns = candidate
        if icns is None:
            found = glob.glob(os.path.join(resources, "*.icns"))
            icns = found[0] if found else None
        if icns is None:
            return None

        # sips scrive solo su file, non su stdout
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "icon.png")
            try:
                subprocess.run(
                    ["sips", "-Z", str(ICON_TARGET_PX), "-s", "format", "png",
                     icns, "--out", out],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                with open(out, "rb") as handle:
                    return handle.read()
            except (OSError, subprocess.TimeoutExpired):
                return None


_backend_instance = None


def get_backend(runtime_dir=None):
    """Ritorna il backend per il sistema operativo corrente (singleton)."""
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance
    os_name = _current_os()
    if os_name == "linux":
        _backend_instance = LinuxBackend(runtime_dir)
    elif os_name == "windows":
        _backend_instance = WindowsBackend()
    elif os_name == "macos":
        _backend_instance = MacBackend()
    else:
        raise RuntimeError(f"sistema operativo non supportato: {platform.system()}")
    return _backend_instance

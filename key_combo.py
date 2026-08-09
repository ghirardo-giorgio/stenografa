"""
Parsing e validazione delle combinazioni di tasti (es. "ctrl+shift+v").

La rappresentazione testuale ("ctrl+c") e' l'unico formato che attraversa il
protocollo (layout.json, rete, MCP) ed e' indipendente dal sistema operativo:
la validazione in questo modulo (TOKEN_NAMES/is_valid_combo) serve a
daemon.py per accettare o rifiutare una combo a prescindere da quale backend
la eseguira' poi davvero. Le funzioni parse_key_combo_* convertono invece la
combo nella rappresentazione richiesta da un backend specifico (usate solo
da platform_backend.py).
"""

# nomi di tasti riconosciuti, indipendenti dal sistema operativo
TOKEN_NAMES = frozenset(
    {
        "ctrl", "control", "shift", "alt", "super", "meta", "win",
        "enter", "return", "esc", "escape", "tab", "space", "backspace",
        "delete", "del", "up", "down", "left", "right", "home", "end",
        "pageup", "pagedown",
        # "+"/"-" non possono essere token letterali (il primo confligge
        # col separatore usato per unire i tasti, es. "ctrl++"): "plus" e
        # "minus" sono la forma a parola, la stessa usata dalle stringhe
        # acceleratore di GTK (es. "<Primary>plus" per lo zoom).
        "plus", "minus",
        "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
        "f11", "f12",
        "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
        "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    }
)


# sinonimi non canonici che un LLM genera spesso (es. "page_up" come in
# pynput/JS, o "arrow_left" come in JS/HTML "ArrowLeft", invece dei nostri
# "pageup"/"left"): normalizzati qui una volta sola, cosi' TOKEN_NAMES/
# LINUX_KEYCODES/_PYNPUT_SPECIAL_NAMES non devono duplicare ogni variante.
_TOKEN_ALIASES = {
    "page_up": "pageup",
    "page_down": "pagedown",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "arrow_up": "up",
    "arrow_down": "down",
    "arrow_left": "left",
    "arrow_right": "right",
    "-": "minus",
}


def tokenize_combo(combo):
    """Divide 'ctrl+shift+v' in ['ctrl', 'shift', 'v'], minuscolo, con i
    sinonimi piu' comuni normalizzati alla forma canonica (vedi
    _TOKEN_ALIASES). Ritorna None se combo non e' una stringa valida, e'
    vuota o contiene token vuoti (es. 'ctrl++v')."""
    if not combo or not isinstance(combo, str):
        return None
    parts = [p.strip().lower() for p in combo.split("+")]
    if not parts or any(not p for p in parts):
        return None
    return [_TOKEN_ALIASES.get(p, p) for p in parts]


def is_valid_combo(combo):
    """True se ogni token della combo e' un tasto riconosciuto."""
    parts = tokenize_combo(combo)
    if not parts:
        return False
    return all(p in TOKEN_NAMES for p in parts)


# --- Linux: token -> keycode uinput (per ydotool). Vedi
# /usr/include/linux/input-event-codes.h
LINUX_KEYCODES = {
    "ctrl": 29, "control": 29,
    "shift": 42,
    "alt": 56,
    "super": 125, "meta": 125, "win": 125,
    "enter": 28, "return": 28,
    "esc": 1, "escape": 1,
    "tab": 15,
    "space": 57,
    "backspace": 14,
    "delete": 111, "del": 111,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109,
    # tasto "=" (KEY_EQUAL) senza shift: e' l'acceleratore a cui la
    # maggior parte delle app (browser inclusi) associa "zoom in" per
    # evitare la dipendenza dal layout di tastiera che richiederebbe
    # tenere premuto anche shift per ottenere il simbolo "+" letterale
    "plus": 13,
    "minus": 12,  # tasto "-" (KEY_MINUS), nessuna ambiguita' di shift
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
    "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
    "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
    "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    "0": 11, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8,
    "8": 9, "9": 10,
}


def parse_key_combo_linux(combo):
    """Converte una combo in una lista di keycode uinput (ydotool). Ritorna
    None se un token non e' riconosciuto."""
    parts = tokenize_combo(combo)
    if not parts:
        return None
    codes = [LINUX_KEYCODES.get(p) for p in parts]
    if any(c is None for c in codes):
        return None
    return codes


_PYNPUT_SPECIAL_NAMES = {
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "super": "cmd", "meta": "cmd", "win": "cmd",
    "enter": "enter", "return": "enter",
    "esc": "esc", "escape": "esc",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete", "del": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end",
    "pageup": "page_up", "pagedown": "page_down",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6",
    "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11",
    "f12": "f12",
}

# "plus"/"minus" non sono membri dell'enum Key di pynput (a differenza dei
# tasti sopra): sono caratteri normali, vanno passati come stringa letterale
# esattamente come le lettere/cifre singole
_PYNPUT_LITERAL_CHARS = {
    "plus": "+",
    "minus": "-",
}


def parse_key_combo_pynput(combo):
    """Converte una combo in una lista di tasti per pynput.keyboard
    (`Key` per i modificatori/tasti speciali, carattere singolo per
    lettere/cifre/plus/minus). Ritorna None se un token non e' riconosciuto
    o se pynput non e' installato nell'ambiente corrente."""
    parts = tokenize_combo(combo)
    if not parts:
        return None
    try:
        from pynput.keyboard import Key
    except ImportError:
        return None
    keys = []
    for p in parts:
        literal = _PYNPUT_LITERAL_CHARS.get(p)
        if literal is not None:
            keys.append(literal)
            continue
        special = _PYNPUT_SPECIAL_NAMES.get(p)
        if special is not None:
            keys.append(getattr(Key, special))
        elif len(p) == 1:
            keys.append(p)
        else:
            return None
    return keys

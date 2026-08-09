#!/home/oberon/.pyenv/shims/python3
"""
Server MCP per controllare le dashboard di pulsanti dell'app telefono
"RecordAndPaste" collegata al demone Stenografa.

Non parla mai direttamente con il telefono: si limita a inviare comandi al
demone (daemon.py) tramite il socket TCP locale di controllo (127.0.0.1,
porta 8767). E' il demone stesso a validare ogni comando, salvare il nuovo
layout su disco e trasmetterlo a tutti i telefoni connessi.

Il layout e' composto da una o piu' "dashboard" (schede), tra cui l'utente
passa sul telefono con uno swipe orizzontale. Ogni dashboard ha un nome e una
griglia rows x cols in cui ogni pulsante occupa esattamente una cella (row,
col), entrambe con indice a partire da 0. Il pulsante "record" (avvio/stop
registrazione) e' protetto: non puo' essere rimosso ne' spostato, e la
dashboard che lo contiene non puo' essere eliminata se e' l'unica ad averlo.
Gli altri pulsanti simulano una combinazione di tasti sul PC (es. "ctrl+c")
quando premuti dal telefono, e possono stare in qualunque dashboard — utile
per creare dashboard dedicate a scorciatoie di altre applicazioni.

Uso: registra questo script come server MCP stdio nel client (Claude Code /
Claude Desktop / LM Studio), puntando all'interprete Python del demone:

    /home/oberon/.pyenv/shims/python3 /home/oberon/Documents/Development/stenografa/mcp_server.py
"""
import json
import socket

from mcp.server.fastmcp import FastMCP

LOCAL_HOST = "127.0.0.1"
CONTROL_SOCKET_PORT = 8767

mcp = FastMCP(
    "stenografa-layout",
    instructions=(
        "Gestisce le dashboard di pulsanti mostrate a schermo intero "
        "nell'app telefono collegata al demone Stenografa; l'utente passa "
        "da una dashboard all'altra con uno swipe orizzontale. Ogni "
        "dashboard ha un nome e una propria griglia (rows x cols, indici da "
        "0) in cui ogni pulsante occupa una cella. Usa list_dashboards per "
        "vedere lo stato attuale (id delle dashboard, celle libere) prima "
        "di aggiungere o spostare pulsanti, create_dashboard per iniziarne "
        "una nuova (es. dedicata a un'altra applicazione) e set_grid_size "
        "se serve piu' spazio in una dashboard. Quando l'utente chiede piu' "
        "pulsanti insieme (es. 'aggiungi questi 6 comandi'), usa SEMPRE "
        "add_buttons con l'intera lista in una sola chiamata invece di "
        "chiamare add_button ripetutamente uno per uno. Con "
        "set_dashboard_match puoi far si' che l'app telefono passi da sola "
        "alla dashboard giusta quando l'utente ha in primo piano "
        "l'applicazione corrispondente sul PC (es. Visual Studio Code). "
        "duplicate_dashboard clona una dashboard esistente come punto di "
        "partenza, reorder_dashboard cambia l'ordine in cui appaiono "
        "swipando. set_language cambia la lingua della dettatura vocale "
        "(indipendente dal layout dei pulsanti). list_launchable_apps/"
        "launch_app avviano applicazioni installate sul PC (Linux/macOS/"
        "Windows): richiama sempre list_launchable_apps prima per trovare "
        "l'id giusto, non inventarlo — lo stesso id serve per creare un "
        "pulsante kind=\"launch\", che avvia quell'app dal telefono. Oltre "
        "alle scorciatoie a tasto singolo (kind=\"keys\") un pulsante puo' "
        "eseguire una sequenza di combinazioni (kind=\"macro\") o incollare "
        "un testo fisso (kind=\"text\"). set_vocabulary/"
        "set_dashboard_vocabulary migliorano la trascrizione dei termini "
        "tecnici ricorrenti."
    ),
)


def _control_request(payload):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((LOCAL_HOST, CONTROL_SOCKET_PORT))
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = s.recv(65536)
    except ConnectionRefusedError as exc:
        raise RuntimeError(
            "Il demone Stenografa non risulta in esecuzione "
            f"(nessuno in ascolto su {LOCAL_HOST}:{CONTROL_SOCKET_PORT})."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Errore di connessione al demone: {exc}") from exc

    reply = json.loads(data.decode("utf-8"))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "comando rifiutato dal demone"))
    return reply


@mcp.tool()
def list_dashboards() -> dict:
    """Elenca tutte le dashboard (id, nome, griglia rows x cols) e i loro
    pulsanti (id, label, posizione, tipo e combinazione di tasti). Chiamalo
    prima di aggiungere o spostare pulsanti per sapere quali dashboard
    esistono e quali celle sono libere."""
    return _control_request({"cmd": "list_layout"})["layout"]


@mcp.tool()
def create_dashboard(
    name: str,
    buttons: list[dict] | None = None,
    rows: int | None = None,
    cols: int | None = None,
    match: str | None = None,
    shortcuts: list[dict] | None = None,
    vocabulary: str | None = None,
) -> dict:
    """Crea una nuova dashboard con il nome indicato, utile ad esempio per
    raggruppare le scorciatoie di un'altra applicazione separate da quelle
    di Stenografa. L'utente potra' raggiungerla con uno swipe orizzontale
    sul telefono.

    Quando l'utente chiede di creare una dashboard E popolarla di
    scorciatoie in un'unica richiesta ("crea una dashboard per InvokeAI con
    i pulsanti Invoca, Annulla, ..."), passa direttamente `buttons` invece
    di fare due chiamate separate (create_dashboard poi add_buttons):
    l'operazione e' atomica, se anche un solo pulsante non e' valido non
    viene creata nemmeno la dashboard. `buttons` ha lo stesso schema di
    add_buttons — ogni elemento e' un oggetto con `label`, `row`, `col` e,
    per `kind="keys"` (default se omesso), `combo` (obbligatorio) piu'
    opzionalmente `color`/`icon` (vedi add_button per le linee guida su
    colore/icona e l'elenco delle icone disponibili); `kind` puo' anche
    essere "record" o "ai_command" (nessun combo/color/icon in quel caso).

    Se `rows`/`cols` non sono indicati, la griglia si dimensiona
    automaticamente sulla posizione massima (row/col) usata dai pulsanti in
    `buttons` (minimo 1x1); specificali solo se vuoi lasciare celle vuote in
    piu' per aggiunte future. Se non passi `buttons`, la dashboard viene
    creata vuota (comportamento di sempre: griglia 1x1, popolala poi con
    set_grid_size e add_button/add_buttons).

    IMPORTANTE: `name` e' solo l'etichetta mostrata sul telefono, NON
    abilita da sola lo switch automatico alla dashboard quando l'utente
    apre l'app corrispondente sul PC. Se l'utente chiede una dashboard "per
    l'app X" aspettandosi che compaia da sola quando apre X, valorizza
    SEMPRE anche `match` con un pattern che identifichi X (nome applicazione
    o, per le app web in un browser, un frammento del titolo della
    finestra/scheda — usa list_dashboards/osserva l'app attualmente in
    primo piano se non sei sicuro di cosa scrivere) invece di chiamare
    set_dashboard_match separatamente.

    Se l'utente ti da' una lista completa di scorciatoie di un'app e vuole
    che siano tutte riconoscibili dal pulsante "comando vocale IA" (kind=
    "ai_command") ma solo alcune (o nessuna) visibili come pulsanti sulla
    griglia, passale in `shortcuts` (lista di {"label", "combo"}, vedi
    set_dashboard_shortcuts) invece che tutte in `buttons`: risparmi spazio
    sulla griglia senza perdere il riconoscimento vocale.

    `vocabulary` (opzionale) e' l'elenco di termini specifici di questa
    app/contesto che Whisper deve trascrivere correttamente (vedi
    set_dashboard_vocabulary)."""
    payload = {"cmd": "create_dashboard", "name": name}
    if buttons is not None:
        payload["buttons"] = buttons
    if rows is not None:
        payload["rows"] = rows
    if cols is not None:
        payload["cols"] = cols
    if match is not None:
        payload["match"] = match
    if shortcuts is not None:
        payload["shortcuts"] = shortcuts
    if vocabulary is not None:
        payload["vocabulary"] = vocabulary
    return _control_request(payload)["layout"]


@mcp.tool()
def remove_dashboard(id: str) -> dict:
    """Rimuove la dashboard con l'id indicato (vedi list_dashboards) insieme
    a tutti i suoi pulsanti. Fallisce se e' l'unica dashboard rimasta o se
    contiene l'unico pulsante "record" del layout."""
    return _control_request({"cmd": "remove_dashboard", "id": id})["layout"]


@mcp.tool()
def rename_dashboard(id: str, name: str) -> dict:
    """Rinomina la dashboard con l'id indicato."""
    return _control_request(
        {"cmd": "rename_dashboard", "id": id, "name": name}
    )["layout"]


@mcp.tool()
def set_dashboard_match(id: str, match: str) -> dict:
    """Associa alla dashboard uno o piu' testi (case-insensitive) da cercare
    nel nome dell'applicazione o nel titolo della finestra col focus sul PC:
    quando corrisponde, l'app telefono passa automaticamente a questa
    dashboard (se l'utente ha attivato "segui app attiva" sul telefono).

    Esempi: "code" per Visual Studio Code (identificato dal nome
    dell'applicazione/wm_class); "invokeai" per un'app web riconoscibile
    solo dal titolo della scheda del browser (le app web non hanno un
    nome applicazione distinto, solo il titolo della finestra/scheda). Per
    piu' pattern alternativi separali con virgola, es. "code, codium"
    (corrisponde se l'app/titolo contiene uno qualsiasi dei due). Passa una
    stringa vuota per disattivare l'associazione.

    Richiede sul PC l'estensione GNOME Shell "Window Calls" attiva; se
    assente la funzionalita' resta silenziosamente inattiva (nessun
    errore, ma l'app telefono non ricevera' mai suggerimenti)."""
    return _control_request(
        {"cmd": "set_dashboard_match", "id": id, "match": match}
    )["layout"]


@mcp.tool()
def set_dashboard_shortcuts(
    dashboard_id: str, shortcuts: list[dict], mode: str = "replace"
) -> dict:
    """Salva un "vocabolario" di scorciatoie (etichetta -> combinazione di
    tasti) note al pulsante "comando vocale IA" (kind="ai_command", vedi
    add_button) di quella dashboard, SENZA mostrarle come pulsanti sulla
    griglia del telefono. Utile quando l'utente ti da' una lista completa
    di scorciatoie di un'applicazione (es. tutte le shortcut di un tool di
    disegno) e vuole che siano tutte riconoscibili a voce, ma solo poche
    (o nessuna) visibili come pulsanti da premere col dito.

    Quando l'utente detta un comando su un pulsante ai_command di questa
    dashboard, se il comando corrisponde (anche solo per significato) a una
    di queste etichette, o a quella di un pulsante "keys" gia' visibile
    nella stessa dashboard, viene eseguita la combinazione corrispondente
    con priorita' su un'interpretazione generica.

    `shortcuts` e' una lista di oggetti {"label": str, "combo": str} (stesso
    formato/validazione di `combo` in add_button, es. "ctrl+enter").

    `mode` decide cosa fare di quelle gia' salvate:
    - "replace" (default): sostituisce l'intero vocabolario precedente della
      dashboard. Usalo quando l'utente ti da' la lista definitiva o chiede
      di rifarla da capo.
    - "append": aggiunge le nuove tenendo le esistenti; una voce con la
      stessa etichetta (confronto senza distinzione di maiuscole) viene
      aggiornata invece di essere duplicata. Usalo quando l'utente chiede di
      "aggiungere anche" qualche scorciatoia a una lista gia' lunga, cosi'
      non devi rimandarla tutta."""
    return _control_request(
        {
            "cmd": "set_dashboard_shortcuts",
            "dashboard_id": dashboard_id,
            "shortcuts": shortcuts,
            "mode": mode,
        }
    )["layout"]


@mcp.tool()
def set_dashboard_vocabulary(dashboard_id: str, vocabulary: str) -> dict:
    """Salva i termini specifici di questa dashboard (nomi propri, gergo
    tecnico, nomi di strumenti dell'app a cui e' dedicata) che Whisper deve
    trascrivere correttamente. Quando la dettatura parte da un pulsante di
    questa dashboard, l'elenco viene passato al modello come contesto
    (`initial_prompt`): il modello tende cosi' a usare quelle grafie invece
    di quelle foneticamente simili che sbaglierebbe altrimenti (es.
    "InvokeAI" invece di "invoca i").

    `vocabulary` e' testo libero, in pratica un elenco di termini separati
    da virgola (es. "InvokeAI, denoising, checkpoint, LoRA, seed"), massimo
    800 caratteri: e' un suggerimento, non un vincolo, e liste troppo lunghe
    peggiorano la trascrizione invece di migliorarla — mettici solo i
    termini che vengono davvero sbagliati. Passa una stringa vuota per
    rimuoverlo. Si somma al vocabolario globale (vedi set_vocabulary)."""
    return _control_request(
        {
            "cmd": "set_dashboard_vocabulary",
            "dashboard_id": dashboard_id,
            "vocabulary": vocabulary,
        }
    )["layout"]


@mcp.tool()
def reorder_dashboard(id: str, position: int) -> dict:
    """Sposta la dashboard con l'id indicato alla posizione `position`
    nell'ordine delle dashboard (indice da 0 = prima, come appaiono
    swipando sul telefono). Se `position` supera il numero di dashboard
    disponibili viene semplicemente messa in fondo."""
    return _control_request(
        {"cmd": "reorder_dashboard", "id": id, "position": position}
    )["layout"]


@mcp.tool()
def duplicate_dashboard(id: str, name: str | None = None) -> dict:
    """Duplica la dashboard indicata (stessa griglia rows x cols e stessi
    pulsanti "keys", con nuovi id per non entrare in conflitto con
    l'originale) in una nuova dashboard, utile come punto di partenza per
    una dashboard simile. `name` e' opzionale (default: "<nome originale>
    (copia)"). Non copia: il pulsante "record" (eviterebbe un secondo
    microfono ridondante — se serve aggiungilo con add_button dopo) ne' il
    match dell'app attiva (eviterebbe due dashboard che rispondono alla
    stessa app)."""
    payload = {"cmd": "duplicate_dashboard", "id": id}
    if name is not None:
        payload["name"] = name
    return _control_request(payload)["layout"]


@mcp.tool()
def add_button(
    dashboard_id: str,
    label: str,
    row: int,
    col: int,
    combo: str | None = None,
    kind: str = "keys",
    color: str | None = None,
    icon: str | None = None,
    combos: list[str] | None = None,
    delay_ms: int | None = None,
    text: str | None = None,
    app_id: str | None = None,
    row_span: int | None = None,
    col_span: int | None = None,
) -> dict:
    """Aggiunge un nuovo pulsante nella cella (row, col) della griglia
    (indici da 0) della dashboard indicata. La cella deve essere libera e
    dentro i limiti della griglia attuale di quella dashboard (usa
    list_dashboards/set_grid_size se serve piu' spazio).

    `kind` e' "keys" (default), "macro", "text", "launch", "paste_last",
    "record" o "ai_command":
    - "keys": alla pressione sul telefono simula sul PC la combinazione di
      tasti indicata in `combo` (obbligatorio per questo tipo, es. "ctrl+c",
      "ctrl+v", "ctrl+shift+z", "alt+tab").
    - "macro": esegue in sequenza le combinazioni della lista `combos`
      (obbligatoria per questo tipo, es. ["ctrl+s", "alt+tab", "ctrl+v"]),
      con una pausa di `delay_ms` millisecondi fra un passo e il successivo
      (default 120, massimo 5000). Usalo quando l'utente descrive un flusso
      in piu' passi da fare con un solo tocco; se un passo fallisce la
      sequenza si interrompe (i passi successivi presuppongono lo stato
      lasciato dai precedenti). Massimo 20 passi.
    - "text": incolla sul PC il testo fisso indicato in `text`
      (obbligatorio per questo tipo, massimo 5000 caratteri) — prompt
      ricorrenti, firme, blocchi di codice, percorsi lunghi. Usa la stessa
      pipeline della dettatura: appunti, incolla adattivo (Ctrl+Shift+V nei
      terminali) e ripristino degli appunti se l'utente lo ha attivato.
    - "launch": avvia sul PC l'applicazione identificata da `app_id`
      (obbligatorio per questo tipo). L'id va SEMPRE ottenuto da
      list_launchable_apps subito prima — non inventarlo ne' riusarne uno
      vecchio: il demone rifiuta la creazione se non corrisponde a
      un'applicazione installata. Se ometti `label` viene usato il nome
      dell'applicazione.
    - "paste_last": re-incolla l'ULTIMA dettatura, senza registrarne una
      nuova. Serve quando il testo era finito nel posto sbagliato (cursore
      non dove doveva essere): l'utente rimette il cursore a posto e tocca
      questo pulsante invece di ridettare. Non richiede `combo`; se non
      c'e' ancora nessuna dettatura in memoria (lo storico si azzera al
      riavvio del demone) notifica l'errore sul PC senza incollare nulla.
      Se ometti `label` diventa "Incolla ultimo".
    - "record": pulsante "microfono", identico a quello della dashboard
      predefinita — avvia/ferma la dettatura vocale di Stenografa, poi
      incolla il testo trascritto. Non richiede `combo`; puoi aggiungerne
      uno in piu' dashboard (utile per non dover tornare sulla dashboard
      principale per dettare).
    - "ai_command": pulsante "microfono" alternativo — invece di incollare
      il testo trascritto, lo invia a un LLM locale (LM Studio, in
      esecuzione sul PC) che lo interpreta e ne esegue la combinazione di
      tasti corrispondente (es. l'utente dice "copia" e viene eseguito
      "ctrl+c"). Utile per dettare comandi invece di testo. Non richiede
      `combo`; se LM Studio non e' raggiungibile o il comando non viene
      riconosciuto, il pulsante notifica l'errore sul PC senza eseguire
      nulla. L'interpretazione da' priorita' alle scorciatoie (kind="keys")
      gia' presenti nella STESSA dashboard del pulsante ai_command premuto,
      PIU' quelle salvate con set_dashboard_shortcuts (non mostrate come
      pulsanti): se in quella dashboard esiste una scorciatoia "Invoca" con
      combo "ctrl+enter" (visibile o no) e l'utente detta "invoca" (anche
      solo per significato, non serve l'uguaglianza testuale esatta), viene
      eseguita proprio quella combinazione invece di una interpretazione
      generica. Per poche scorciatoie usate spesso, crea pulsanti "keys"
      con etichette parlanti nella stessa dashboard; per una lista lunga
      (es. tutte le scorciatoie di un tool) che occuperebbe troppa griglia,
      usa invece set_dashboard_shortcuts — in entrambi i casi il comando
      vocale le riconosce allo stesso modo.
    Ne' "record" ne' "ai_command" supportano `color`/`icon`: il loro
    aspetto segue lo stato della registrazione.

    `row_span`/`col_span` (opzionali, default 1) rendono il pulsante piu'
    alto/largo di una cella: usali per dare rilievo a quelli che si premono
    piu' spesso. L'area che occupa deve stare dentro la griglia e non
    sovrapporsi ad altri pulsanti.

    `color` (opzionale, formato "#RRGGBB") e `icon` (opzionale, uno dei nomi
    elencati sotto) determinano l'aspetto del pulsante sul telefono — per
    kind="keys", "macro", "text", "launch" e "paste_last" — sceglili in base al tipo di azione, cosi' l'utente
    riconosce a colpo d'occhio cosa fa ogni pulsante. Linee guida (adattale
    al contesto, non sono regole rigide):
    Usa la gamma dell'app, che sul telefono resta leggibile e coerente:
    Corallo "#e1543f" (accento), Ambra "#c8891e", Oliva "#7c8c3c",
    Smeraldo "#2f9e6e", Ceruleo "#2681a8", Indaco "#5b5fc7",
    Magenta "#a84ba5", Ardesia "#5d6a75" (neutro, e' anche il colore dei
    pulsanti senza colore proprio).
    - Corallo + icona "delete"/"delete_sweep"/"cancel"/"close" per azioni
      distruttive o che interrompono/cancellano qualcosa;
    - Ambra + icona "warning" per azioni che richiedono attenzione ma non
      sono immediatamente distruttive;
    - Smeraldo + icona "check"/"check_circle"/"play_arrow" per azioni di
      conferma o avvio;
    - Ceruleo o Ardesia per azioni informative o generiche (es.
      copia/incolla con "content_copy"/"content_paste");
    - Indaco/Magenta per azioni creative o di generazione (es.
      "bolt"/"flash_on" per un comando "genera" in un tool di AI
      generativa);
    - per strumenti di disegno/fotoritocco (es. Gimp, InvokeAI): "crop_free"
      (selezione rettangolo), "gesture" (lazo/selezione libera), "brush"
      (pennello), "gradient" (sfuma/blend), "healing" o "auto_fix_high"
      (timbro clone/correzione), "zoom_in" (zoom), "rotate_right"/
      "rotate_left" (rotazione), "straighten" (guida/riga), "colorize"
      (contagocce colore), "touch_app"/"near_me"/"pan_tool" (strumenti di
      selezione/spostamento generici), "crop", "opacity", "tune",
      "filter_alt", "grain", "line_weight", "flip", "exposure", "contrast".
    Se non specificati, il pulsante usa un aspetto neutro di default. Se
    `icon` non e' uno dei nomi elencati qui, viene ignorato silenziosamente
    (il pulsante viene comunque creato con l'icona di default) invece di far
    fallire la richiesta: non serve rifare la chiamata solo per un'icona
    sbagliata, ma usa un nome valido se vuoi che compaia davvero.

    Icone disponibili: keyboard, delete, delete_sweep, cancel, close,
    warning, refresh, autorenew, stop, play_arrow, pause, check,
    check_circle, content_copy, content_paste, content_cut, save, folder,
    image, photo, brush, palette, undo, redo, send, download, upload,
    settings, search, star, favorite, lock, lock_open, visibility, edit,
    add, remove, arrow_upward, arrow_downward, arrow_back, arrow_forward,
    mic, volume_up, volume_off, power_settings_new, sync, cloud, home,
    menu, more_horiz, info, help, layers, terminal, code, bolt, flash_on,
    clear_all, restart_alt, rocket_launch, open_in_new, apps,
    desktop_windows, text_snippet, notes, short_text, playlist_play,
    history.
    """
    payload = {
        "cmd": "add_button",
        "dashboard_id": dashboard_id,
        "label": label,
        "kind": kind,
        "row": row,
        "col": col,
    }
    if combo is not None:
        payload["combo"] = combo
    if color is not None:
        payload["color"] = color
    if icon is not None:
        payload["icon"] = icon
    if combos is not None:
        payload["combos"] = combos
    if delay_ms is not None:
        payload["delay_ms"] = delay_ms
    if text is not None:
        payload["text"] = text
    if app_id is not None:
        payload["app_id"] = app_id
    if row_span is not None:
        payload["row_span"] = row_span
    if col_span is not None:
        payload["col_span"] = col_span
    return _control_request(payload)["layout"]


@mcp.tool()
def add_buttons(dashboard_id: str, buttons: list[dict]) -> dict:
    """Aggiunge piu' pulsanti alla dashboard indicata in un'unica chiamata
    (piu' efficiente di chiamare add_button una volta per pulsante).
    L'operazione e' atomica: se anche un solo pulsante non e' valido
    (posizione fuori griglia, cella gia' occupata, combinazione di tasti
    errata, ecc.) non viene aggiunto nulla e l'errore indica quale elemento
    della lista ha fallito.

    Ogni elemento di `buttons` e' un oggetto con le stesse chiavi di
    add_button (tranne dashboard_id, che si specifica una sola volta):
    - label (str, obbligatorio; se omesso e kind="record" diventa "Registra",
      se omesso e kind="ai_command" diventa "Comando vocale", se omesso e
      kind="launch"
      diventa il nome dell'applicazione, se omesso e kind="paste_last"
      diventa "Incolla ultimo")
    - kind (str, opzionale, "keys" [default], "macro", "text", "launch",
      "paste_last", "record" o "ai_command": vedi add_button)
    - combo (str, obbligatorio se kind="keys", es. "ctrl+c")
    - combos (list[str], obbligatorio se kind="macro") e delay_ms (int,
      opzionale)
    - text (str, obbligatorio se kind="text")
    - app_id (str, obbligatorio se kind="launch", da list_launchable_apps)
    - row, col (int, obbligatori, indici da 0)
    - row_span, col_span (int, opzionali, default 1: quante celle occupa il
      pulsante, per farlo piu' grande degli altri)
    - color (str, opzionale, "#RRGGBB", non per i pulsanti microfono)
    - icon (str, opzionale, uno dei nomi elencati in add_button, non per i
      pulsanti microfono)

    Esempio:
    add_buttons(dashboard_id="vscode", buttons=[
        {"label": "Run", "combo": "f5", "row": 0, "col": 0,
         "color": "#1e88e5", "icon": "play_arrow"},
        {"label": "Stop", "combo": "shift+f5", "row": 2, "col": 1,
         "color": "#424242", "icon": "stop"},
        {"label": "Salva tutto e compila", "kind": "macro",
         "combos": ["ctrl+k", "ctrl+s", "ctrl+shift+b"], "row": 3, "col": 0,
         "icon": "playlist_play"},
    ])
    """
    return _control_request(
        {"cmd": "add_buttons", "dashboard_id": dashboard_id, "buttons": buttons}
    )["layout"]


@mcp.tool()
def edit_button(
    id: str,
    label: str | None = None,
    combo: str | None = None,
    combos: list[str] | None = None,
    delay_ms: int | None = None,
    text: str | None = None,
    app_id: str | None = None,
    row_span: int | None = None,
    col_span: int | None = None,
) -> dict:
    """Modifica un pulsante gia' esistente (vedi list_dashboards per l'id)
    senza ricrearlo: cambia l'etichetta e/o l'azione che esegue, mantenendo
    posizione, colore e icona.

    Ogni campo e' opzionale, quelli omessi restano invariati:
    - `label`: la scritta sul pulsante;
    - `combo`: la nuova combinazione di tasti, solo per kind="keys";
    - `combos` e/o `delay_ms`: la nuova sequenza e/o la pausa fra i passi,
      solo per kind="macro";
    - `text`: il nuovo snippet da incollare, solo per kind="text";
    - `app_id`: l'applicazione da avviare, solo per kind="launch" (id da
      list_launchable_apps; l'etichetta segue il nome dell'applicazione);
    - `row_span`/`col_span`: quante celle occupa il pulsante, per renderlo
      piu' grande degli altri (deve restare dentro la griglia e non
      sovrapporsi a nessuno).

    Passare un campo che non appartiene al tipo del pulsante (es. `combo` a
    una macro) e' un errore: usa `combos` per le macro. Per colore e icona
    usa set_button_style, per la posizione move_button.
    """
    payload = {"cmd": "edit_button", "id": id}
    if label is not None:
        payload["label"] = label
    if combo is not None:
        payload["combo"] = combo
    if combos is not None:
        payload["combos"] = combos
    if delay_ms is not None:
        payload["delay_ms"] = delay_ms
    if text is not None:
        payload["text"] = text
    if app_id is not None:
        payload["app_id"] = app_id
    if row_span is not None:
        payload["row_span"] = row_span
    if col_span is not None:
        payload["col_span"] = col_span
    return _control_request(payload)["layout"]


@mcp.tool()
def set_button_style(
    id: str, color: str | None = None, icon: str | None = None
) -> dict:
    """Cambia colore e/o icona di un pulsante gia' esistente (vedi
    list_dashboards per l'id). Il pulsante "record" non supporta uno stile
    personalizzato (il suo aspetto segue lo stato della registrazione).

    Stesse linee guida di colore/icona di add_button, con la gamma dell'app:
    Corallo "#e1543f" per azioni distruttive (+ "delete"), Ambra "#c8891e"
    per quelle che richiedono attenzione (+ "warning"), Smeraldo "#2f9e6e"
    per conferma/avvio (+ "check_circle"), Ceruleo "#2681a8" o Ardesia
    "#5d6a75" per azioni informative o generiche, Indaco "#5b5fc7" o
    Magenta "#a84ba5" per azioni creative o di generazione. Icone disponibili: keyboard, delete, delete_sweep, cancel,
    close, warning, refresh, autorenew, stop, play_arrow, pause, check,
    check_circle, content_copy, content_paste, content_cut, save, folder,
    image, photo, brush, palette, undo, redo, send, download, upload,
    settings, search, star, favorite, lock, lock_open, visibility, edit,
    add, remove, arrow_upward, arrow_downward, arrow_back, arrow_forward,
    mic, volume_up, volume_off, power_settings_new, sync, cloud, home,
    menu, more_horiz, info, help, layers, terminal, code, bolt, flash_on,
    clear_all, restart_alt, crop_free, gesture, colorize, healing, gradient,
    zoom_in, rotate_right, rotate_left, straighten, format_paint, highlight,
    touch_app, near_me, pan_tool, opacity, tune, filter_alt, crop, grain,
    auto_fix_high, line_weight, flip, exposure, contrast, rocket_launch,
    open_in_new, apps, desktop_windows, text_snippet, notes, short_text,
    playlist_play, history.

    A differenza di add_button/add_buttons, qui un nome icona non valido fa
    fallire la chiamata con un errore esplicito (non viene ignorato in
    silenzio): essendo una modifica mirata a un solo pulsante gia' esistente,
    un feedback immediato e' piu' utile che un fallback silenzioso.
    """
    payload = {"cmd": "set_button_style", "id": id}
    if color is not None:
        payload["color"] = color
    if icon is not None:
        payload["icon"] = icon
    return _control_request(payload)["layout"]


@mcp.tool()
def remove_button(id: str) -> dict:
    """Rimuove il pulsante con l'id indicato, cercandolo in tutte le
    dashboard (vedi list_dashboards). Se e' un pulsante "record" (microfono)
    e resta l'unico in tutto il layout, la rimozione fallisce (l'utente
    perderebbe il modo di avviare la registrazione): in tal caso vanno bene
    solo pulsanti "record" aggiuntivi, non l'ultimo rimasto."""
    return _control_request({"cmd": "remove_button", "id": id})["layout"]


@mcp.tool()
def move_button(id: str, row: int, col: int) -> dict:
    """Sposta un pulsante esistente (compresi quelli "record"/"ai_command")
    in un'altra cella (row, col) della stessa dashboard in cui si trova (non
    lo trasferisce in un'altra dashboard: per quello, rimuovilo e ricrealo
    altrove). Se la cella di destinazione e' gia' occupata da un altro
    pulsante, i due si scambiano di posto invece di fallire."""
    return _control_request(
        {"cmd": "move_button", "id": id, "row": row, "col": col}
    )["layout"]


@mcp.tool()
def set_grid_size(dashboard_id: str, rows: int, cols: int) -> dict:
    """Ridimensiona la griglia della dashboard indicata a rows x cols
    (minimo 1x1). Fallisce se un pulsante esistente in quella dashboard
    finirebbe fuori dai nuovi limiti: in tal caso spostalo o rimuovilo prima
    di ridurre la griglia."""
    return _control_request(
        {
            "cmd": "set_grid_size",
            "dashboard_id": dashboard_id,
            "rows": rows,
            "cols": cols,
        }
    )["layout"]


@mcp.tool()
def get_config() -> dict:
    """Ritorna la configurazione attuale del demone: `language` (lingua di
    dettatura, vedi set_language), `restore_clipboard` (vedi
    set_restore_clipboard), `translate_enabled`, `translate_target` e
    `translate_engine` (vedi set_translate_enabled/set_translate_target/
    set_translate_engine), `vocabulary` (vedi set_vocabulary),
    `confirm_before_paste` (vedi set_confirm_before_paste), `require_tls`
    (vedi set_require_tls) piu' due campi informativi di sola lettura:
    `tls_available` (se il demone ha un certificato con cui cifrare il
    canale) e `tls_fingerprint` (l'impronta SHA-1 che l'app telefono fissa
    al primo collegamento)."""
    return _control_request({"cmd": "get_config"})["config"]


@mcp.tool()
def set_language(language: str) -> dict:
    """Cambia la lingua usata da Whisper per trascrivere la dettatura
    vocale. `language` e' un codice ISO 639-1 di due lettere (es. "it",
    "en", "es", "fr", "de") oppure "auto" per il rilevamento automatico
    della lingua parlata (un po' piu' lento e occasionalmente meno preciso
    del passare la lingua esplicita). Alcuni codici comuni: it (italiano),
    en (inglese), es (spagnolo), fr (francese), de (tedesco), pt
    (portoghese), nl (olandese), ru (russo), zh (cinese), ja (giapponese),
    ko (coreano), ar (arabo), hi (hindi), pl (polacco), tr (turco); Whisper
    ne supporta molte altre. Il cambio ha effetto dalla prossima
    registrazione, non richiede di riavviare il demone."""
    return _control_request(
        {"cmd": "set_language", "language": language}
    )["config"]


@mcp.tool()
def set_restore_clipboard(enabled: bool) -> dict:
    """Attiva/disattiva il ripristino automatico degli appunti dopo la
    dettatura. Se attivo, il demone cattura il contenuto degli appunti
    all'inizio di ogni registrazione e, dopo aver incollato con successo il
    testo dettato, lo ripristina — cosi' quello che l'utente aveva copiato
    prima di dettare resta disponibile per un successivo Ctrl+V. Se
    l'incolla automatico fallisce il ripristino non avviene, cosi' il testo
    dettato resta negli appunti per un Ctrl+V manuale. Disattivo di
    default."""
    return _control_request(
        {"cmd": "set_restore_clipboard", "enabled": enabled}
    )["config"]


@mcp.tool()
def set_pause_media_while_recording(enabled: bool) -> dict:
    """Attiva/disattiva la pausa automatica dei video mentre si detta. Se
    attiva, all'avvio di ogni registrazione il demone mette in pausa i
    riproduttori in corso sul PC (browser compresi, via MPRIS) e li fa
    ripartire a dettatura finita: serve a non far finire l'audio del video
    dentro la trascrizione. Disattiva di default; funziona su Linux, dove il
    rilevamento dei player e' implementato.

    Indipendente da questa opzione, l'app telefono mostra comunque un
    pulsante play/pausa per ogni video in riproduzione."""
    return _control_request(
        {"cmd": "set_pause_media_while_recording", "enabled": enabled}
    )["config"]


@mcp.tool()
def set_translate_enabled(enabled: bool) -> dict:
    """Attiva/disattiva la traduzione automatica del testo dettato con un
    pulsante "record" (non si applica ai pulsanti "ai_command", che devono
    interpretare il comando nella lingua originale). Se attiva, il testo
    incollato e' nella lingua indicata da set_translate_target invece che
    nella lingua parlata. Vedi set_translate_target/set_translate_engine
    per la configurazione. Disattiva di default."""
    return _control_request(
        {"cmd": "set_translate_enabled", "enabled": enabled}
    )["config"]


@mcp.tool()
def set_translate_target(target: str) -> dict:
    """Lingua di destinazione della traduzione automatica (vedi
    set_translate_enabled), un codice ISO 639-1 di due lettere (es. "en",
    "it", "fr"). A differenza di set_language, "auto" non e' valido qui:
    bisogna sapere verso quale lingua tradurre. Se il target e' "en"
    (inglese) puoi scegliere il motore con set_translate_engine; per
    qualsiasi altra lingua viene sempre usato l'LLM locale (LM Studio), che
    e' l'unico dei due motori capace di tradurre verso lingue diverse
    dall'inglese."""
    return _control_request(
        {"cmd": "set_translate_target", "target": target}
    )["config"]


@mcp.tool()
def set_translate_engine(engine: str) -> dict:
    """Motore usato per tradurre quando il target e' l'inglese (vedi
    set_translate_target): "whisper" usa il task nativo "translate" del
    modello Whisper — veloce, non richiede LM Studio, ma puo' tradurre SOLO
    verso l'inglese (limite del modello). "llm" passa il testo gia'
    trascritto fedelmente a LM Studio (stesso motore del comando vocale IA)
    perche' lo traduca — un po' piu' lento e richiede LM Studio avviato con
    un modello caricato, ma e' l'unica opzione quando il target non e'
    l'inglese (in quel caso questa impostazione viene ignorata e si usa
    sempre "llm", vedi set_translate_target). Default "whisper"."""
    return _control_request(
        {"cmd": "set_translate_engine", "engine": engine}
    )["config"]


@mcp.tool()
def set_vocabulary(vocabulary: str) -> dict:
    """Vocabolario di dettatura globale: i termini che Whisper deve
    trascrivere correttamente in qualunque contesto (nome dell'utente, nomi
    di progetti, gergo che usa sempre). Stesso funzionamento e stessi limiti
    di set_dashboard_vocabulary (massimo 800 caratteri, elenco separato da
    virgola), ma vale per ogni dettatura e si somma a quello della dashboard
    da cui parte la registrazione. Passa una stringa vuota per
    rimuoverlo."""
    return _control_request(
        {"cmd": "set_vocabulary", "vocabulary": vocabulary}
    )["config"]


@mcp.tool()
def set_confirm_before_paste(enabled: bool) -> dict:
    """Attiva/disattiva la conferma prima dell'incolla. Se attiva, il testo
    dettato (gia' tradotto, se la traduzione e' accesa) non viene incollato
    subito: compare sul telefono in un pannello dove l'utente puo'
    rileggerlo, correggerlo e approvarlo, oppure annullarlo. Se non risponde
    entro qualche minuto la richiesta scade da sola senza incollare nulla
    (il testo resta comunque nello storico). Utile per dettature lunghe o
    quando la trascrizione va verificata prima di finire in un documento; da
    tenere spenta (default) per la dettatura rapida, dove aggiungerebbe un
    tocco ad ogni frase. Non si applica ai pulsanti "ai_command", che non
    incollano testo."""
    return _control_request(
        {"cmd": "set_confirm_before_paste", "enabled": enabled}
    )["config"]


@mcp.tool()
def set_require_tls(enabled: bool) -> dict:
    """Se attivo, il demone accetta dai telefoni SOLO connessioni cifrate
    (TLS) e rifiuta quelle in chiaro. Il demone genera da se' un certificato
    self-signed al primo avvio e l'app telefono ne fissa l'impronta al primo
    collegamento; con questa opzione spenta (default) accetta entrambe, per
    non tagliare fuori una versione precedente dell'app durante
    l'aggiornamento. Attivalo quando tutti i telefoni che usi sono
    aggiornati, soprattutto se il PC sta su una rete WiFi condivisa: sul
    canale in chiaro passano tutto il testo dettato e il token di
    autenticazione. Fallisce se il certificato non e' disponibile (openssl
    mancante sul PC), invece di rendere il demone irraggiungibile."""
    return _control_request(
        {"cmd": "set_require_tls", "enabled": enabled}
    )["config"]


@mcp.tool()
def restart_daemon() -> dict:
    """Riavvia il demone Stenografa (rieseguendo lo stesso processo, stesso
    PID): utile per far ripartire il modello/le connessioni dopo un
    problema, o per far ripartire il demone dopo un aggiornamento del suo
    codice, senza dover usare un terminale sul PC. La connessione di rete
    (fra cui questa stessa richiesta MCP) si interrompe per qualche
    secondo durante il riavvio; il layout e la configurazione salvati non
    vengono persi (restano su disco)."""
    return _control_request({"cmd": "restart_daemon"})


@mcp.tool()
def list_launchable_apps() -> list[dict]:
    """Elenca le applicazioni installate sul PC che possono essere avviate
    con launch_app: per ciascuna un `id` (opaco, va passato invariato a
    launch_app - NON costruirlo o indovinarlo) e un `name` leggibile.
    Funziona su Linux (dai file .desktop installati), macOS (dai bundle
    .app in /Applications) e Windows (dal menu Start, incluse le app UWP/
    Store). Richiamalo prima di launch_app per trovare l'id giusto: non
    riusare un id di una chiamata precedente in questa stessa conversazione,
    l'elenco puo' cambiare (nuove installazioni o disinstallazioni)."""
    return _control_request({"cmd": "list_apps"})["apps"]


@mcp.tool()
def launch_app(id: str) -> dict:
    """Avvia sul PC l'applicazione identificata da `id` (vedi
    list_launchable_apps, da richiamare prima per ottenere un id valido e
    aggiornato). Fallisce con un errore esplicito se `id` non compare piu'
    nell'elenco corrente (es. applicazione disinstallata nel frattempo)."""
    return _control_request({"cmd": "launch_app", "id": id})


@mcp.tool()
def reset_layout() -> dict:
    """Riporta il layout allo stato iniziale: un'unica dashboard "Stenografa"
    con griglia 1x1 e il solo pulsante "record" a schermo intero. Rimuove
    tutte le dashboard e i pulsanti personalizzati creati finora."""
    return _control_request({"cmd": "reset_layout"})["layout"]


if __name__ == "__main__":
    mcp.run(transport="stdio")

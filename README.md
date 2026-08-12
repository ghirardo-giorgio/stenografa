# Stenografa

Dettatura vocale con Whisper. Una scorciatoia da tastiera GNOME (o l'app
Flutter da telefono, vedi sotto) avvia/ferma la registrazione; al termine il
testo trascritto viene copiato negli appunti, incollato automaticamente nella
finestra col focus e mostrato in una notifica. Il demone non ha interfaccia
grafica: il riscontro arriva dalle notifiche desktop e dallo stato mostrato
nell'app telefono.

## Componenti

- `daemon.py` — demone (senza interfaccia grafica) in ascolto su un socket unix
  (`$XDG_RUNTIME_DIR/stenografa.sock`) e su un server TCP (porta 8765) per il
  controllo remoto da rete locale. Gestisce registrazione, trascrizione
  (`faster-whisper` su GPU, modello `medium`) e incolla automaticamente il
  risultato. Le operazioni specifiche del sistema operativo sono isolate in
  `platform_backend.py` (vedi "Compatibilita' Windows/macOS" sotto).
- `platform_backend.py` — layer di astrazione OS (audio, appunti, simulazione
  tasti, notifiche, rilevamento finestra col focus): Linux implementato e
  verificato, Windows/macOS scritti ma non ancora testati su una macchina
  reale.
- `llm_provider.py` — backend LLM intercambiabili (LM Studio, Ollama, OpenAI,
  API Claude, CLI Claude Code) usati da comando vocale IA, traduzione e
  traduzioni. Vedi "Scegliere il backend LLM" sotto.
- `setup_llm.py` — script interattivo che compone `~/.config/stenografa/llm.json`
  (quale backend usare, modello, credenziali) e ne verifica il funzionamento.
- `key_combo.py` — parsing/validazione delle combinazioni di tasti (es.
  `ctrl+c`), indipendente dal sistema operativo.
- `toggle.py` — invia il comando di avvio/stop al demone tramite il socket
  unix locale. Va collegato a una scorciatoia da tastiera.
- `RecordAndPaste` (`/home/oberon/Documents/Development/Flutter/RecordAndPaste`)
  — app Flutter per telefono: una griglia di pulsanti a schermo intero
  (registrazione, comandi vocali interpretati da IA, scorciatoie
  personalizzate) controllata da remoto sulla stessa rete WiFi, vedi sotto.
- `mcp_server.py` — server MCP (stdio) che espone come tool la gestione
  della griglia di pulsanti dell'app telefono e della configurazione del
  demone, cosi' un client MCP puo' comporla a partire da una richiesta in
  linguaggio naturale. Vedi sotto.
- `tests/` — suite pytest sulla logica pura del demone (validazione layout,
  configurazione, comando vocale IA); non richiede audio/GPU/rete reali.

Il demone parte automaticamente ad ogni login grazie a
`~/.config/autostart/stenografa.desktop` (se non lo vuoi, cancella quel
file). È già stato avviato anche per la sessione corrente.

## Configurare la scorciatoia da tastiera (GNOME)

1. Apri **Impostazioni > Tastiera > Scorciatoie personalizzate** (in GNOME 4x
   /50: Impostazioni > Tastiera, poi "Scorciatoie da tastiera" in fondo >
   "Scorciatoie personalizzate" > "+").
2. Nome: `Stenografa toggle`.
3. Comando: `/home/oberon/Documents/Development/stenografa/toggle.py`
4. Assegna la combinazione che preferisci (es. `Super+Spazio` o `Ctrl+Alt+R`).

Da quel momento premi la scorciatoia per iniziare a registrare, la premi di
nuovo per fermare: il testo trascritto finisce negli appunti (`Ctrl+V` per
incollarlo) e appare una notifica con l'anteprima.

## Uso manuale (senza scorciatoia)

```bash
# avvia il demone (se non già in esecuzione)
./daemon.py &

# avvia/ferma la registrazione
./toggle.py
```

Il demone impedisce a se stesso di partire due volte (lock su
`$XDG_RUNTIME_DIR/stenografa.lock`): se lo lanci mentre è già in esecuzione,
stampa un errore ed esce subito invece di aprire una seconda porta/socket in
conflitto con la prima istanza.

## Configurazione (lingua, traduzione, ripristino appunti)

`~/.config/stenografa/config.json` contiene, oltre al token dell'app
telefono, alcune impostazioni modificabili anche a caldo (dalle
impostazioni dell'app telefono o via MCP, senza riavviare il demone):

- **Lingua di dettatura** (`language`): un codice ISO 639-1 (`it`, `en`, ...)
  o `"auto"` per il rilevamento automatico. Whisper supporta quasi 100
  lingue; l'elenco mostrato nell'app/nel tool MCP (`SUPPORTED_LANGUAGES` in
  `daemon.py`) e' solo una selezione curata delle piu' comuni, non esaustiva.
- **Ripristina clipboard** (`restore_clipboard`, spento di default): se
  attivo, il contenuto degli appunti da prima della dettatura viene
  ripristinato subito dopo l'incolla automatico, cosi' non perdi quello che
  avevi copiato in precedenza. Il ripristino avviene **solo se l'incolla
  automatico e' riuscito**: se fallisce, il testo dettato resta negli appunti
  come alternativa per un Ctrl+V manuale.
- **Metti in pausa i video mentre detto** (`pause_media_while_recording`,
  spento di default): all'inizio della registrazione il demone mette in
  pausa i riproduttori in corso sul PC e li fa ripartire alla fine, cosi'
  il loro audio non finisce nella trascrizione. Quello che i player non
  espongono (piu' schede dello stesso browser che suonano insieme) viene
  silenziato via PipeWire e riattivato dopo. Vedi "Controlli dei video".
- **Traduzione automatica** (`translate_enabled`, spenta di default;
  `translate_target`; `translate_engine`): traduce il testo dettato con un
  pulsante microfono normale (`kind: "record"`, non si applica ad
  "ai_command") prima di incollarlo. Due motori possibili:
  - `translate_engine: "whisper"` — usa il task nativo "translate" del
    modello Whisper: veloce, non richiede LM Studio, ma **puo' tradurre solo
    verso l'inglese** (limite del modello, non del demone). Valido solo se
    `translate_target` e' `"en"`.
  - `translate_engine: "llm"` — passa il testo gia' trascritto fedelmente a
    LM Studio (stesso motore del comando vocale IA) perche' lo traduca in
    `translate_target`, che puo' essere una lingua qualsiasi (non solo
    inglese). Un po' piu' lento (richiede LM Studio avviato con un modello
    caricato).
  Se `translate_target` non e' `"en"`, l'engine "llm" viene sempre usato
  anche se e' salvato "whisper": Whisper da solo non puo' tradurre verso
  altre lingue.
- **Vocabolario di dettatura** (`vocabulary`, vuoto di default): elenco di
  termini (nomi propri, gergo tecnico, nomi di prodotto) passato a Whisper
  come `initial_prompt`, cioe' come se fosse il testo immediatamente
  precedente a quello da trascrivere: il modello lo usa come contesto e
  tende a preferire quelle grafie. E' un suggerimento, non un vincolo, e
  liste troppo lunghe peggiorano la trascrizione invece di migliorarla (da
  qui il limite di 800 caratteri): mettici solo i termini che vengono
  davvero sbagliati. Ogni dashboard puo' averne uno proprio (vedi
  "Vocabolario per dashboard" sotto), che si somma a questo.
- **Conferma prima di incollare** (`confirm_before_paste`, spenta di
  default): se attiva, il testo dettato non viene incollato subito ma
  compare sul telefono in un pannello dove puo' essere riletto, corretto e
  approvato — oppure annullato. Se non rispondi entro qualche minuto la
  richiesta scade da sola senza incollare nulla (il testo resta comunque
  nello storico). Utile per dettature lunghe o da verificare; di troppo per
  la dettatura rapida, dove aggiungerebbe un tocco ad ogni frase. Non si
  applica ai pulsanti "ai_command", che non incollano testo.
- **Richiedi TLS** (`require_tls`, spento di default): rifiuta le
  connessioni in chiaro dal telefono. Vedi "Cifratura del canale" sotto.

## Riavviare il demone dal telefono

Nelle impostazioni dell'app (sotto le altre opzioni, solo quando connessa)
c'e' un pulsante "Riavvia demone" (con conferma, per evitare tocchi
accidentali): rilancia lo stesso processo Python (stesso PID, via
`os.execv`) rileggendo il codice di `daemon.py` da disco, utile dopo un
aggiornamento del demone senza dover usare un terminale sul PC. Layout e
configurazione non vengono persi (restano su disco); la connessione si
interrompe per qualche secondo e l'app si riconnette da sola. Disponibile
anche via MCP (`restart_daemon`).

## App telefono (RecordAndPaste)

L'app Flutter in `/home/oberon/Documents/Development/Flutter/RecordAndPaste`
si collega al demone via TCP (porta 8765) sulla stessa rete WiFi. Nelle
impostazioni dell'app va inserito l'IP del PC, la porta e il token letto da
`~/.config/stenografa/config.json` (rigenerato cancellando quel file e
riavviando il demone). L'interfaccia dell'app è disponibile in italiano e
inglese (impostazione "Lingua interfaccia" nelle impostazioni, separata dalla
lingua di dettatura).

L'interfaccia e' composta da una o piu' **dashboard**, tra cui si passa con
uno swipe orizzontale (indicatore a puntini e nome in alto). Ogni dashboard
ha una propria griglia di pulsanti (righe x colonne). Esistono sette tipi
di pulsante:

- **microfono** (`kind: "record"`): avvia/ferma la dettatura vocale e incolla
  il testo trascritto. Tutte le sue istanze condividono la stessa
  registrazione (non ce n'e' una per dashboard). Deve sempre restarne almeno
  uno in tutto il layout (l'ultimo non e' rimovibile), ma se ne possono
  aggiungere altri in qualunque dashboard — utile per dettare senza dover
  tornare sulla dashboard principale.
- **comando vocale IA** (`kind: "ai_command"`): stesso funzionamento del
  microfono, ma invece di incollare il testo dettato lo invia a un modello
  linguistico locale (LM Studio) che lo traduce in una combinazione di tasti
  da eseguire — es. dici "copia" e viene eseguito `ctrl+c`. Vedi "Comando
  vocale IA" sotto. Non protetto: se ne possono avere quanti se ne vuole o
  nessuno.
- **scorciatoia** (`kind: "keys"`): simula una combinazione di tasti fissa
  (es. "copia" -> `ctrl+c`) quando premuto.
- **macro** (`kind: "macro"`): esegue in sequenza piu' combinazioni di tasti
  (`combos`, massimo 20) con una pausa configurabile fra l'una e l'altra
  (`delay_ms`, 120 ms di default) — i flussi che altrimenti richiederebbero
  tre tocchi separati. Se un passo fallisce la sequenza si interrompe e lo
  segnala: i passi successivi presuppongono lo stato lasciato dai
  precedenti, tirare dritto rischierebbe di eseguirli nel contesto
  sbagliato.
- **testo** (`kind: "text"`): incolla uno snippet fisso (`text`, massimo
  5000 caratteri) — prompt ricorrenti, firme, blocchi di codice, percorsi
  lunghi. Usa la stessa pipeline della dettatura: appunti, incolla adattivo
  nei terminali e ripristino degli appunti se attivo. Non entra nello
  storico delle dettature: non e' qualcosa che hai detto, e' qualcosa che
  hai gia' salvato in un pulsante.
- **avvia applicazione** (`kind: "launch"`): avvia sul PC l'applicazione
  indicata da `app_id`, scelta per nome da un elenco (vedi "Avvio
  applicazioni" sotto). L'id viene validato alla creazione del pulsante e di
  nuovo ad ogni pressione: se l'app viene disinstallata il pulsante notifica
  l'errore invece di tentare comunque il lancio.

Toccando l'icona a matita in alto a sinistra si entra in modalita' modifica:

- si tocca una cella vuota per creare li' un nuovo pulsante, scegliendo tra
  "Scorciatoia" (etichetta + combinazione di tasti, es. `ctrl+c`), "Macro"
  (etichetta + una combinazione per riga + pausa fra i passi), "Testo da
  incollare" (etichetta + testo), "Avvia applicazione" (etichetta + app
  scelta da un elenco ricercabile), "Microfono" (solo etichetta, di default
  "Registra"), "Comando vocale (IA)" (solo etichetta, di default "Comando
  vocale");
- si tocca la **X** in alto a destra su un pulsante per rimuoverlo (bloccato
  solo se e' l'unico pulsante microfono rimasto in tutto il layout);
- si trascina un punto qualsiasi della cella (non solo un'iconcina) per
  spostarlo in un'altra cella della stessa dashboard; se la si rilascia
  sopra una cella gia' occupata, i due pulsanti si scambiano di posto
  invece di rifiutare lo spostamento;
- le frecce +/- sotto e a destra della griglia aggiungono/tolgono righe e
  colonne — il contenuto delle celle si ridimensiona automaticamente se lo
  spazio verticale/orizzontale disponibile si riduce (es. ruotando il
  telefono in orizzontale con molte righe), senza andare in overflow;
- si tocca il nome della dashboard in alto per rinominarla, duplicarla,
  riordinarla (frecce sinistra/destra tra le dashboard) o eliminarla (non
  eliminabile se e' l'unica rimasta, o se contiene l'unico pulsante
  microfono del layout); la duplicazione copia le scorciatoie ma non i
  pulsanti microfono/IA ne' l'associazione "segue app attiva";
- con un tocco lungo su una scorciatoia (non su microfono/comando IA/
  IA, il cui aspetto segue lo stato della registrazione) si apre
  l'editor di colore/icona (stessa tavolozza/icone che puo' scegliere l'LLM
  via MCP, vedi sotto);
- nel dialogo "Impostazioni dashboard" (voce "Impostazioni" toccando il
  nome) si puo' associare uno o piu' testi (separati da virgola) da cercare
  nell'app/finestra col focus sul PC (vedi "Dashboard che segue l'app
  attiva" sotto) e un vocabolario di dettatura specifico (vedi
  "Vocabolario per dashboard" sotto).

### Tieni premuto per parlare (push-to-talk)

Nelle impostazioni dell'app, "Tieni premuto per parlare" cambia il
comportamento dei pulsanti microfono: invece di funzionare da interruttore
(tocca per iniziare, tocca per fermare) registrano finche' li tieni premuti.
Evita le registrazioni lasciate aperte per sbaglio, a costo di dover tenere
il dito sullo schermo durante la dettatura. La pressione e il rilascio
viaggiano come due comandi separati (`button_down`/`button_up`): il rilascio
viene inviato anche se il dito esce dalla cella, altrimenti il demone
resterebbe a registrare. Un secondo "premuto" mentre una registrazione e'
gia' in corso (es. da un altro telefono collegato) non la riavvia.

E' un'impostazione locale del telefono, non del demone: telefoni diversi
collegati allo stesso PC possono usarla o no indipendentemente. La
modalita' e' sospesa in modifica del layout, dove il tocco serve a
selezionare e trascinare i pulsanti.

**Rete di sicurezza per chi se ne dimentica**: chi non attiva "Tieni
premuto per parlare" resta in modalita' a interruttore, dove un tocco
accidentale puo' lasciare la registrazione avviata senza accorgersene. Il
demone impone comunque un limite massimo di `RECORDING_MAX_DURATION_SECONDS`
(3 minuti di default, in `daemon.py`): superato, ferma la registrazione da
solo, trascrive quello che c'e' fino a li' e avvisa con una notifica
desktop del motivo. Si applica solo alla registrazione "a interruttore"
(telefono in modalita' normale o scorciatoia da tastiera GNOME): in "tieni
premuto per parlare" non serve, dato che il rilascio del dito ferma sempre
tutto da solo, per quanto a lungo resti premuto.

### Vibrazione

Sempre nelle impostazioni dell'app (attiva di default): il telefono vibra
all'inizio e alla fine della registrazione e quando qualcosa va storto. Il
riscontro scritto del demone e' una notifica desktop, cioe' proprio dove non
stai guardando mentre tieni il telefono in mano.

### Storico delle dettature

L'icona a orologio in alto a sinistra (visibile solo se c'e' qualcosa da
mostrare) apre le ultime dettature della sessione, dalla piu' recente. Serve
soprattutto quando l'incolla automatico e' finito nella finestra sbagliata:
si tocca una voce e il demone la re-incolla nella finestra col focus adesso.

Lo storico vive **solo in memoria** sul PC (le ultime
`HISTORY_MAX_ENTRIES`, 20 di default) e non finisce mai su disco: e' testo
dettato dall'utente, spesso privato. Si azzera al riavvio del demone. Al
telefono arrivano solo le anteprime (300 caratteri) e un id: il re-incolla
avviene per id, quindi il testo integrale non viaggia mai in rete e il
telefono puo' solo scegliere fra testi che il demone ha gia' prodotto, non
farne incollare di arbitrari.

### Scegliere il backend LLM

Le funzioni che usano un modello linguistico — comando vocale IA, traduzione
con engine `llm` — passano tutte da un unico punto
(`llm_provider.py`), quindi il backend si sceglie una volta sola e vale per
tutte e tre. Cinque backend supportati:

| Backend | Chiave API | Note |
|---|---|---|
| **LM Studio** | no | Locale. Usa il modello gia' caricato (`model: "auto"`). |
| **Ollama** | no | Locale. Serve un modello scaricato (`ollama pull`). |
| **OpenAI** | si | Cloud. |
| **Anthropic (API Claude)** | si | Cloud. Senza chiave esplicita l'SDK usa `ANTHROPIC_API_KEY` o un profilo `ant auth login`. |
| **Claude Code (CLI)** | no | Usa il CLI gia' autenticato. Nessuna chiave, ma piu' lento (avvia un agente ad ogni chiamata). |

Si configura con lo script interattivo:

```bash
./setup_llm.py            # menu: scegli backend, modello, credenziali
./setup_llm.py --show     # mostra la configurazione attuale
./setup_llm.py --check    # prova il backend selezionato
```

Le impostazioni finiscono in `~/.config/stenografa/llm.json` (permessi 0600,
puo' contenere chiavi API). Il file contiene i parametri di **tutti** i
backend piu' il campo `provider` che dice quale usare: cambiare backend e' una
parola sola e le credenziali degli altri restano dove sono. Il demone rilegge
il file ad ogni chiamata, quindi **il cambio ha effetto subito, senza
riavviarlo**.

Le chiavi API si possono tenere fuori dal disco: ogni backend cloud ha un
campo `api_key_env` con il nome di una variabile d'ambiente, che ha la
precedenza sul valore salvato nel file.

Aggiungere un backend significa scrivere una sottoclasse di `BaseProvider` in
`llm_provider.py` e registrarla in `PROVIDERS`, senza toccare `daemon.py`.

**Quale scegliere.** I backend locali rispondono in poche centinaia di
millisecondi e non mandano nulla fuori dal PC: sono la scelta giusta per il
comando vocale, dove la latenza si sente ad ogni pressione. I backend cloud
danno interpretazioni migliori sui comandi ambigui, al prezzo
della latenza di rete (e, per OpenAI/Anthropic, del consumo di credito).
Claude Code non richiede credenziali ma paga l'avvio di un agente completo ad
ogni chiamata, quindi conviene sulle traduzioni piu' che sul comando
vocale.

### Comando vocale IA

Un pulsante `kind: "ai_command"` invia il testo trascritto al **backend LLM
configurato** (vedi "Scegliere il backend LLM" sotto), che lo interpreta e
restituisce la combinazione di tasti da eseguire.

Se il backend non e' raggiungibile, o il comando dettato non corrisponde a
nessuna combinazione valida, il pulsante non esegue nulla e mostra una
notifica di errore sul PC — non incolla mai il testo grezzo come fallback,
per non eseguire scorciatoie a caso.

**Comandi ambigui: scelta sul telefono**: quando la frase dettata puo'
corrispondere a piu' azioni diverse (es. "copia" fra `ctrl+c`, `ctrl+shift+c`
e "duplica"), il demone **non indovina e non esegue nulla**: propone i
candidati e aspetta. Il telefono apre un pannello sopra la dashboard corrente
con le opzioni come pulsanti a griglia; al tocco viene eseguita quella
scelta, e il pannello si chiude. Meglio un tocco in piu' che l'esecuzione a
caso di una scorciatoia potenzialmente distruttiva.

Il pannello e' **effimero**: non crea dashboard, non tocca `layout.json` e
non c'e' quindi nessuna dashboard da "ripristinare" dopo la scelta. Si chiude
da solo dopo `AI_COMMAND_CHOICE_TIMEOUT` secondi (90 di default, in
`daemon.py`) senza eseguire niente, e viene ritirato anche se nel frattempo
parte una nuova dettatura o se la scelta e' gia' stata fatta da un altro
telefono collegato. Il demone accetta solo combinazioni fra quelle che ha
proposto, quindi il pannello non e' una via per fargli eseguire scorciatoie
arbitrarie.

Quanti candidati proporre lo decide il modello: se il comando e' chiaro ne
restituisce **uno solo** e viene eseguito subito come prima, senza alcun
tocco aggiuntivo. Il massimo di opzioni mostrate e' `AI_COMMAND_MAX_OPTIONS`
(6).

**Priorita' alle scorciatoie della dashboard**: l'interpretazione tiene conto
di quali pulsanti "scorciatoia" (`kind: "keys"`) esistono nella stessa
dashboard del pulsante `ai_command` premuto. Es. se la dashboard "Invoke AI"
contiene un pulsante "Invoca" con combo `ctrl+enter`, dettare "invoca" (anche
solo per significato, non serve l'uguaglianza testuale esatta) esegue proprio
quella combinazione invece di un'interpretazione generica. E' quindi utile
aggiungere in quella dashboard pulsanti "keys" con etichette parlanti anche
se non li tocchi mai col dito: danno al comando vocale un vocabolario
preciso per quel contesto.

**Vocabolario di scorciatoie nascoste**: se hai una lista lunga di
scorciatoie di un'app (es. tutti gli strumenti di Gimp) e vuoi che il
comando vocale le riconosca tutte senza occupare la griglia con decine di
pulsanti che non premerai mai col dito, salvale nel campo `shortcuts` della
dashboard — via MCP `set_dashboard_shortcuts(dashboard_id, shortcuts)`
(sostituisce l'intera lista, non la somma) o passando `shortcuts` gia' a
`create_dashboard`. Sono etichetta+combinazione di tasti come i pulsanti
"keys", ma non compaiono mai sul telefono: contano solo per
l'interpretazione IA, con la stessa priorita' delle scorciatoie visibili.

**Aprire applicazioni a voce**: oltre alle combinazioni di tasti, il comando
vocale riconosce le richieste di avviare un'applicazione ("apri gimp",
"lancia il browser"). Il modello **non riceve mai l'elenco** delle
applicazioni installate (centinaia di voci) e non puo' inventare un id: si
limita a nominare un'app, e il demone risolve quel nome contro le
applicazioni davvero installate. Se corrisponde a piu' d'una (es. "steam"
fra "Steam" e "Steam Tinker Launch") le propone come opzioni nello stesso
pannello di scelta dei comandi ambigui; una corrispondenza esatta ha la
precedenza su quelle parziali, cosi' dire "steam" non chiede una scelta che
l'utente ha gia' fatto.

**Macro generate a voce**: se il comando descrive un task composto da piu'
azioni in sequenza ("in gimp crea un nuovo file, aggiungi un livello e
selezionalo tutto"), il modello puo' generare al volo una **macro** —
piu' combinazioni di tasti eseguite in ordine, esattamente come un pulsante
`kind: "macro"` — invece di limitarsi a una singola combinazione.

Questa capacita' e' **vincolata alla dashboard da cui parte il comando**: il
prompt inviato al modello include il nome della dashboard corrente (es.
"GIMP") e gli impone esplicitamente che ogni combinazione, singola o dentro
una macro, deve valere per QUELL'applicazione e nessun'altra — se il task non
ha senso per quell'app, il modello deve rispondere con un array vuoto. Senza
una dashboard nota (caso che in pratica non si presenta mai, dato che un
pulsante `ai_command` vive sempre dentro una dashboard) la capacita' di
generare macro non viene nemmeno descritta al modello: una macro proposta
comunque verrebbe scartata, non eseguita.

Ogni combinazione della macro passa la stessa validazione di un pulsante
`kind: "macro"` (sintassi, massimo `MACRO_MAX_STEPS` passi): se anche una
sola non e' valida, l'intera macro viene scartata invece di eseguirne solo
una parte — capita spesso per azioni che in realta' richiedono il mouse (es.
"impostala di colore rosso" in Gimp non ha una scorciatoia da tastiera: il
modello a volte inventa un tasto inesistente, che la validazione respinge
prima di premere qualunque cosa). Se il task e' realmente ambiguo fra piu'
interpretazioni (comprese macro diverse), vale lo stesso pannello di scelta
sul telefono descritto sopra.

Mentre l'IA sta interpretando il comando, tutti i pulsanti microfono/IA/
IA del layout mostrano lo stato "in elaborazione" (viola, icona
`psychology`): e' uno stato condiviso, dato che la registrazione e' unica
per tutto il demone.

### Vocabolario per dashboard

Oltre al vocabolario di dettatura globale (vedi "Configurazione" sopra),
ogni dashboard puo' avere il proprio elenco di termini — il gergo dell'app a
cui e' dedicata. Quando la dettatura parte da un pulsante di quella
dashboard, i due si sommano e vengono passati a Whisper come contesto: e' il
motivo per cui una dashboard "InvokeAI" puo' far trascrivere correttamente
"denoising" o "checkpoint" senza che quei termini disturbino le dettature
fatte altrove.

Si imposta dal dialogo "Impostazioni dashboard" sul telefono oppure via MCP
con `set_dashboard_vocabulary`. Concettualmente e' il gemello del campo
`shortcuts`: un vocabolario invisibile e specifico per contesto, solo che
riguarda la trascrizione invece dell'interpretazione IA.

### Dashboard che segue l'app attiva

Toccando l'icona a mirino sotto quella della matita si attiva/disattiva
"segui app attiva" (spenta di default): quando attiva, il telefono passa da
solo alla dashboard associata all'applicazione col focus sul PC — es. una
dashboard "VS Code" con `match = "code"` si apre da sola quando porti in
primo piano Visual Studio Code. L'associazione si imposta nel dialogo
"Impostazioni dashboard" (campo "Rileva app") oppure via MCP con
`set_dashboard_match`. Il testo (o i testi, separati da virgola: es.
`"code, codium"`) e' cercato (case-insensitive) sia nel nome
dell'applicazione sia nel titolo della finestra, utile anche per le app web
riconoscibili solo dal titolo della scheda del browser.

Il rilevamento richiede l'estensione GNOME Shell **Window Calls**
(`window-calls@domandoman.xyz`) attiva sul PC; se assente o disattivata la
funzione resta inerte senza generare errori (nessuna dashboard riceve mai
un suggerimento). Il demone interroga la finestra col focus ogni ~1.5s
tramite `org.gnome.Shell.Extensions.Windows.List` via D-Bus (solo Linux, vedi
limiti Windows/macOS sotto).

### Cifratura del canale (TLS)

Il protocollo di rete trasporta tutto il testo dettato e permette di
simulare tasti sul PC: in chiaro, su una WiFi condivisa, sarebbe leggibile
(e il token numerico a 5 cifre, comodo ma corto, catturabile) da chiunque
sia sulla stessa rete. Il rate-limiting sull'autenticazione copre il
tentativo di forza bruta, non l'ascolto passivo.

Il demone genera quindi con `openssl` un certificato self-signed al primo
avvio (`~/.config/stenografa/cert.pem` + `key.pem`, quest'ultimo `0600`) e
accetta connessioni TLS. Non essendoci nessuna autorita' a garantirlo, l'app
telefono usa il **"trust on first use"**: fissa l'impronta del certificato
al primo collegamento e da li' in poi rifiuta un certificato diverso,
mostrando un avviso con l'impronta nuova invece di collegarsi. Il
certificato viene riusato fra i riavvii: se cambiasse ogni volta, il
pinning scatterebbe di continuo e finirebbe per addestrare l'utente ad
accettare qualunque certificato.

**La stessa porta (8765) accetta entrambi i tipi di connessione**: il demone
riconosce il TLS dal primo byte del pacchetto (un record di handshake TLS
inizia sempre con `0x16 0x03`, il protocollo in chiaro con la `{` di un
oggetto JSON) senza consumarlo. Serve a non tagliare fuori una versione
precedente dell'app durante l'aggiornamento. Quando tutti i telefoni che usi
sono aggiornati, attiva **"Richiedi collegamento cifrato"**
(`require_tls`, dalle impostazioni dell'app o via MCP `set_require_tls`): da
quel momento le connessioni in chiaro vengono rifiutate con un motivo
esplicito. L'opzione non e' attivabile se il certificato non esiste
(`openssl` mancante sul PC), altrimenti renderebbe il demone irraggiungibile
da qualunque telefono senza modo di tornare indietro dall'app.

L'impronta e' SHA-1 e non SHA-256 perche' e' l'unica che `X509Certificate`
di Dart espone senza dipendenze aggiuntive; per sostituire un certificato
gia' fissato servirebbe una seconda preimmagine, non una collisione, quindi
resta adeguata allo scopo. Le impostazioni dell'app mostrano se il canale in
uso e' cifrato e con quale impronta, da confrontare con quella riportata da
`get_config` sul PC.

### Invio automatico dopo la dettatura

Un pulsante `kind: "record"` con `auto_enter` acceso preme **Invio** subito
dopo aver incollato: in una chat il messaggio dettato parte da solo, senza
toccare la tastiera del PC. Sul telefono si accende e si spegne con la
spunta sul pulsante stesso, perche' e' una scelta che cambia di continuo —
in chat serve, in un editor sarebbe un guaio.

Vale solo per la dettatura di quel pulsante: non per il comando vocale IA
(che non incolla testo), non per il re-incolla di una voce passata e non per
gli snippet fissi. Con "conferma prima di incollare" attiva, l'Invio parte
dopo l'approvazione. Fra l'incolla e l'Invio c'e' una breve pausa: certe
chat web elaborano l'incolla in modo asincrono e un Invio immediato
partirebbe a campo ancora vuoto.

### Incolla ultimo

Un pulsante `kind: "paste_last"` re-incolla l'ultima dettatura senza
registrarne una nuova: serve quando il testo e' finito nel posto sbagliato
perche' il cursore non era dove doveva. Rimetti il cursore a posto, tocchi
il pulsante e il testo viene incollato di nuovo (stessa pipeline della
dettatura: appunti, incolla adattivo, ripristino appunti se attivo). Non
crea una nuova voce nello storico — e' lo stesso testo, non una dettatura
in piu'. Se non c'e' ancora nulla in memoria (lo storico si azzera al
riavvio del demone) il pulsante notifica l'errore senza incollare.

### Controlli dei video

Il telefono mostra un pulsante play/pausa per ogni riproduttore attivo sul
PC, cosi' si ferma un video prima di dettare senza tornare alla tastiera —
altrimenti il microfono ne capta l'audio. Il rilevamento usa **MPRIS** (lo
standard D-Bus di browser e riproduttori), interrogato con `gdbus`: nessuna
dipendenza aggiuntiva. Lo stesso video pubblicato da piu' bus (capita con
l'integrazione browser di Plasma) viene mostrato una volta sola.

I browser basati su Chromium pubblicano **un solo player per finestra**:
con due schede che suonano insieme la seconda non e' controllabile. Per
quel caso l'automatismo `pause_media_while_recording` fa un secondo
passaggio dai flussi audio di PipeWire (`pactl`), che invece sono uno per
scheda: quello che non si e' potuto mettere in pausa viene silenziato per
la durata della dettatura e riattivato alla fine.

Il server audio ricorda il mute **per applicazione**: se un flusso
silenziato finisce prima del ripristino, l'applicazione resterebbe muta
anche dopo. Il demone se ne difende riattivando per nome dell'applicazione
e tenendo d'occhio chi ha silenziato per una ventina di secondi dopo la
dettatura. Su Windows e macOS il rilevamento non e' implementato: nessun
pulsante e nessun silenziamento.

### Pulsanti piu' grandi di una cella

`row_span`/`col_span` (default 1) dicono quante celle occupa un pulsante:
servono a dare rilievo a quelli che si premono piu' spesso. Si impostano
alla creazione (`add_button`) o dopo (`edit_button`, ed e' cosi' che li
cambia l'app dal telefono). L'area deve stare dentro la griglia e non
sovrapporsi ad altri pulsanti; ridurre la griglia con un pulsante esteso
fuori dai nuovi limiti viene rifiutato, come per un pulsante normale. Nel
layout salvato i valori pari a 1 non vengono scritti, quindi i layout di
chi non usa questa funzione restano identici a prima.

### Icone delle applicazioni

Il telefono disegna in filigrana, dietro i pulsanti, l'icona vera
dell'applicazione a cui la dashboard si riferisce (il suo pulsante "avvia
applicazione", oppure l'app che corrisponde al suo `match`). Le icone
arrivano dal PC — su Linux dai file `.desktop` e dai temi installati, con
gli SVG convertiti in PNG da ImageMagick quando serve; su macOS
dall'`.icns` del bundle via `sips`; su Windows dall'eseguibile o dagli
asset del pacchetto via PowerShell — e viaggiano su richiesta
(`get_app_icon`), con cache da entrambe le parti.

### Avvio applicazioni

Un pulsante `kind: "launch"` avvia un'applicazione installata. In modalita'
modifica si sceglie da un elenco ricercabile che il telefono chiede al
demone (`list_apps`): l'id vero (percorso `.desktop` su Linux, `AppID` su
Windows, bundle `.app` su macOS) non viene mai digitato a mano. Lo stesso
elenco alimenta il comando vocale IA e i tool MCP `list_launchable_apps`/
`launch_app`, ed e' tenuto in cache per 15 secondi: enumerarlo costa
(centinaia di file `.desktop`) e non ha senso rifarlo ad ogni pressione.

Il layout (tutte le dashboard) e' salvato sul PC in
`~/.config/stenografa/layout.json` e sopravvive a riavvii di demone e
telefono. I layout creati con una versione precedente di questa app (una
sola griglia, senza dashboard) vengono migrati automaticamente in un'unica
dashboard "Stenografa" al primo avvio del demone aggiornato.

## Controllo da MCP

`mcp_server.py` espone la stessa gestione delle dashboard (e alcune
impostazioni del demone) come server MCP (stdio), cosi' un client MCP (es.
Claude Code/Claude Desktop) puo' comporle a partire da una richiesta in
linguaggio naturale ("crea una dashboard con le scorciatoie per InvokeAI").
Non parla mai col telefono direttamente: passa dal socket di controllo
locale del demone (`$XDG_RUNTIME_DIR/stenografa-control.sock`, accessibile
solo dall'utente locale), che a sua volta salva e trasmette il nuovo stato a
tutti i telefoni connessi.

Tool disponibili:

- `list_dashboards()` — elenco dashboard e pulsanti.
- `create_dashboard(name, buttons=None, rows=None, cols=None, match=None)` —
  se richiesta creazione + popolamento in un colpo solo ("crea una dashboard
  per InvokeAI con i pulsanti Invoca, Annulla..."), passa direttamente
  `buttons` (stesso schema di `add_buttons`) invece di due chiamate separate
  (create_dashboard poi add_buttons): operazione atomica, la griglia si
  auto-dimensiona sui pulsanti se `rows`/`cols` non sono indicati.
  `remove_dashboard(id)`, `rename_dashboard(id, name)`,
  `reorder_dashboard(id, position)` (sposta la dashboard in una nuova
  posizione tra le altre), `duplicate_dashboard(id, name=None)` (copia
  scorciatoie ma non pulsanti microfono/IA ne' l'associazione "match").
- `set_dashboard_match(id, match)` — associa la dashboard all'app/finestra
  da rilevare per lo switch automatico sul telefono (piu' pattern separati
  da virgola).
- `set_dashboard_shortcuts(dashboard_id, shortcuts, mode)` — salva un
  vocabolario di scorciatoie (lista di `{label, combo}`) note al comando
  vocale IA ma non mostrate come pulsanti. `mode="replace"` (default)
  sostituisce l'intera lista precedente, `mode="append"` aggiunge tenendo
  le esistenti (una voce con la stessa etichetta viene aggiornata, non
  duplicata): comodo per aggiungerne due a una lista di quaranta senza
  rimandarla tutta.
- `set_dashboard_vocabulary(dashboard_id, vocabulary)` — termini che Whisper
  deve trascrivere correttamente quando la dettatura parte da quella
  dashboard (vedi "Vocabolario per dashboard" sopra).
- `add_button(dashboard_id, label, row, col, combo, kind, color, icon,
  combos, delay_ms, text, app_id)` — `kind` e' `"keys"` (default, richiede
  `combo`), `"macro"` (richiede `combos`, opzionale `delay_ms`), `"text"`
  (richiede `text`), `"launch"` (richiede `app_id`, da
  `list_launchable_apps`), `"paste_last"` (re-incolla l'ultima
  dettatura), `"record"`/`"ai_command"` (pulsanti
  microfono aggiuntivi, nessun campo in piu').
- `add_buttons(dashboard_id, buttons)` — aggiunge piu' pulsanti in una sola
  chiamata atomica, ognuno con lo stesso schema di `add_button`; usalo
  quando servono piu' pulsanti insieme invece di chiamare `add_button`
  ripetutamente.
- `edit_button(id, label, combo, combos, delay_ms, text, app_id)` — cambia
  etichetta e/o azione di un pulsante esistente senza ricrearlo, quindi
  senza perderne posizione, colore e icona.
- `set_button_style(id, color, icon)` — cambia aspetto a un pulsante "keys"
  esistente (non applicabile a "record"/"ai_command").
- `remove_button(id)`, `move_button(id, row, col)` (solo entro la stessa
  dashboard; se la cella e' occupata i due pulsanti si scambiano di posto),
  `set_grid_size(dashboard_id, rows, cols)`.
- `get_config()` — lingua di dettatura corrente, stato di "ripristina
  clipboard" e configurazione della traduzione automatica.
- `set_language(language)` — cambia la lingua di dettatura (codice ISO
  639-1 o `"auto"`).
- `set_pause_media_while_recording(enabled)` — mette in pausa (e silenzia)
  i video mentre si detta; vedi "Controlli dei video".
- `set_restore_clipboard(enabled)` — attiva/disattiva il ripristino degli
  appunti dopo l'incolla automatico.
- `set_translate_enabled(enabled)`, `set_translate_target(target)` (codice
  ISO 639-1, mai `"auto"`), `set_translate_engine(engine)` (`"whisper"`,
  solo verso inglese, o `"llm"`, qualsiasi lingua via LM Studio) — vedi
  "Configurazione" sopra.
- `set_vocabulary(vocabulary)`, `set_confirm_before_paste(enabled)`,
  `set_require_tls(enabled)` — vedi "Configurazione" sopra.
- `restart_daemon()` — riavvia il demone (stesso processo, stesso PID),
  utile dopo un aggiornamento del suo codice.
- `reset_layout()` — azzera tutto a un'unica dashboard col solo pulsante
  "Registra".
- `list_launchable_apps()` — elenco delle applicazioni installate sul PC
  (Linux/macOS/Windows, vedi sotto), ciascuna con un `id` opaco e un `name`
  leggibile.
- `launch_app(id)` — avvia l'applicazione con quell'`id` (va sempre ottenuto
  da una chiamata recente a `list_launchable_apps`, mai inventato o riusato
  da una sessione precedente: l'elenco puo' cambiare nel frattempo).

`list_launchable_apps`/`launch_app` servono a far avviare un'app al client
MCP stesso ("apri VSCode") su richiesta in linguaggio naturale; lo stesso
elenco alimenta i pulsanti `kind: "launch"` e il comando vocale IA (vedi
"Avvio applicazioni" sopra). L'enumerazione e' cross-platform ma con una
fonte diversa per OS:
- **Linux**: parsing dei file `.desktop` (specifica freedesktop.org) in
  `/usr/share/applications`, `~/.local/share/applications` e le directory
  equivalenti di Flatpak/Snap, scartando le voci `Type` diverso da
  `Application` o marcate `NoDisplay`/`Hidden`; il lancio usa `gio launch`
  (espande correttamente il campo `Exec`, incluse le app sandboxate).
- **macOS**: bundle `.app` in `/Applications`, `/System/Applications` e
  `~/Applications`, col nome letto da `CFBundleName` nell'`Info.plist`; il
  lancio usa `open`.
- **Windows**: `Get-StartApps` (PowerShell) elenca in un colpo solo sia le
  voci classiche del menu Start sia le app UWP/Store con un `AppID`
  univoco; il lancio usa `explorer.exe shell:AppsFolder\<AppID>`. Non
  testato su una macchina Windows reale, come il resto del backend Windows
  (vedi "Compatibilità Windows/macOS").

Il demone valida sempre `id` contro l'elenco aggiornato di `list_apps()`
prima di avviare qualunque cosa (stesso principio delle scorciatoie
`ai_command`: il client MCP puo' solo *selezionare* un'app da un elenco
noto, mai eseguire un id/percorso arbitrario) — se l'app non compare piu'
(es. disinstallata) `launch_app` fallisce con un errore esplicito invece di
tentare comunque il lancio.

Le combinazioni di tasti supportate sono nomi comuni separati da `+` (es.
`ctrl+c`, `ctrl+shift+z`, `alt+tab`, `f5`, `pageup`/`pagedown`, `plus`/
`minus` per lo zoom es. `ctrl+plus`/`ctrl+minus` — "+"/"-" non possono
essere token letterali perche' "+" e' anche il separatore, "plus"/"minus"
e' la stessa forma a parola usata dalle stringhe acceleratore di GTK).
Sinonimi comuni generati spesso da un LLM vengono normalizzati invece che
rifiutati: `page_up`/`page_down`/`pgup`/`pgdn` per `pageup`/`pagedown`,
`arrow_up`/`arrow_down`/`arrow_left`/`arrow_right` (come in JS "ArrowLeft")
per `up`/`down`/`left`/`right`, `-` per `minus`. I pulsanti "record"/
"ai_command" non sono mai
restilizzabili (il loro aspetto segue lo stato della registrazione), e non
si puo' rimuovere l'ultimo pulsante "record" del layout ne' la dashboard
che lo contiene se e' l'unica ad averne uno (i pulsanti "ai_command" non
hanno questa protezione: non sono l'unico modo di avviare una
registrazione).

In `add_buttons`, `create_dashboard` e `set_dashboard_shortcuts`, se piu' di
un elemento della lista non e' valido l'errore li elenca tutti insieme (non
solo il primo trovato): utile su liste lunghe (es. tutte le scorciatoie di
un'app), dove correggerle una alla volta a chiamate successive sarebbe
inutilmente lento.

`color` (formato `#RRGGBB`) e `icon` (uno di un set fisso di ~80 nomi Material
Design — incluse icone per strumenti di disegno/fotoritocco tipo Gimp/
InvokeAI, elencati nel docstring di `add_button`) sono opzionali: l'LLM li
sceglie in base al tipo di azione (es. rosso + icona cestino per un comando
distruttivo, verde per conferma/avvio) cosi' l'utente riconosce a colpo
d'occhio le scorciatoie. Senza specificarli il pulsante usa un aspetto
neutro di default. In `add_button`/`add_buttons`/`create_dashboard` un nome
icona non riconosciuto (l'LLM ne inventa a volte uno plausibile ma
inesistente) non fa fallire la creazione: viene semplicemente ignorato e il
pulsante usa l'icona di default, cosi' un batch di piu' pulsanti non viene
scartato per intero per un solo nome sbagliato. Solo `set_button_style`
(modifica mirata a un pulsante gia' esistente) resta strict e segnala
l'errore.

Per registrarlo in Claude Code:

```bash
claude mcp add stenografa -- /home/oberon/.pyenv/shims/python3 \
  /home/oberon/Documents/Development/stenografa/mcp_server.py
```

Oppure aggiungendo a mano la voce in `.mcp.json` / config del client MCP:

```json
{
  "mcpServers": {
    "stenografa": {
      "command": "/home/oberon/.pyenv/shims/python3",
      "args": ["/home/oberon/Documents/Development/stenografa/mcp_server.py"]
    }
  }
}
```

## Compatibilità Windows/macOS

Le operazioni specifiche del sistema operativo (audio, appunti, simulazione
tasti, notifiche, rilevamento finestra attiva) sono isolate in
`platform_backend.py` dietro un'interfaccia comune (`Backend`), cosi'
`daemon.py` resta identico su tutti i sistemi operativi.

| | Linux | Windows | macOS |
|---|---|---|---|
| Stato | implementato e verificato (unico ambiente testato finora) | scritto, **mai testato** su una macchina reale | scritto, **mai testato** su una macchina reale |
| Audio | `pw-record` (PipeWire) | `sounddevice`/`soundfile` | `sounddevice`/`soundfile` |
| Appunti | `wl-copy`/`wl-paste` | `pyperclip` | `pyperclip` |
| Simulazione tasti | `ydotool`/`ydotoold` | `pynput` | `pynput` |
| Notifiche | `notify-send` | `plyer` | `plyer` (fallback `osascript`) |
| Finestra col focus | estensione GNOME Shell "Window Calls" via D-Bus | `pywin32` + `psutil` | `AppKit.NSWorkspace` (solo nome app, non titolo finestra) |
| Elenco/avvio app | file `.desktop` + `gio launch` | `Get-StartApps` (PowerShell) + `explorer.exe shell:AppsFolder\` | bundle `.app` + `open` |
| Dipendenze extra | nessuna oltre a quelle gia' installate | `requirements-windows.txt` | `requirements-macos.txt` |

Limiti noti da verificare quando si prova su Windows/macOS:

- **macOS** richiede che l'utente conceda manualmente il permesso
  "Accessibilita'" all'interprete Python in Impostazioni di Sistema >
  Privacy e Sicurezza, altrimenti la simulazione dei tasti fallisce
  silenziosamente (non e' un permesso concedibile da codice). Il rilevamento
  della finestra col focus usa solo il nome dell'applicazione in primo piano
  (non il titolo finestra/scheda), perche' leggere il titolo richiederebbe lo
  stesso permesso tramite le API AXUIElement.
- **Windows**: nessun problema di permessi noto a priori, ma la
  combinazione pynput + focus tramite `GetForegroundWindow` non e' mai stata
  verificata in pratica. `explorer.exe shell:AppsFolder\<AppID>` (usato da
  `launch_app`) restituisce spesso un codice di uscita 1 anche quando il
  lancio riesce: il codice di ritorno non e' considerato affidabile e
  `launch_app` ritorna `True` semplicemente se il processo parte senza
  eccezioni — da verificare che non nasconda mai un fallimento reale.
  Richiede inoltre il **Microsoft Visual C++ Redistributable x64** installato
  a livello di sistema (non installabile via pip): senza, l'import di
  `faster_whisper` fallisce con `FileNotFoundError` su `ctranslate2.dll`
  (mancano `vcruntime140.dll`/`vcruntime140_1.dll`/`msvcp140.dll`).
  `winget install --id Microsoft.VCRedist.2015+.x64 -e`
- Su entrambi, testare tramite Wine/Lutris non e' affidabile per verificare
  incolla-in-altre-app e notifiche reali: serve una macchina Windows/macOS
  reale (o una VM con VirtualBox per Windows; non esiste virtualizzazione
  macOS su hardware non Apple).

## Robustezza

- **Istanza singola**: lock su `$XDG_RUNTIME_DIR/stenografa.lock`, vedi
  sopra.
- **Rate-limiting sull'autenticazione remota**: il token numerico a 5 cifre
  (comodo da digitare a mano, ma debole) e' protetto da un limite di 5
  tentativi falliti per indirizzo IP ogni 60 secondi. Contro l'ascolto
  passivo sulla stessa rete serve invece TLS: vedi "Cifratura del canale"
  sopra.
- **Vocabolari chiusi**: in nessun punto il telefono o l'LLM possono far
  eseguire al demone qualcosa di arbitrario. Il pannello di scelta accetta
  solo le opzioni che il demone stesso ha proposto; `launch_app` e i
  pulsanti di avvio validano l'id contro l'elenco aggiornato delle app
  installate; il comando vocale puo' nominare un'app ma non costruirne
  l'id; il re-incolla dello storico avviene per id, non per testo.
- **Test automatici**: `tests/` (pytest) copre la validazione del layout e
  dei tipi di pulsante, la configurazione (lingua, vocabolario, ripristino
  appunti), il comando vocale IA (combinazioni e avvio app), lo storico, la
  conferma dell'incolla e il riconoscimento TLS/chiaro, usando backend finti
  — non serve audio/GPU/rete reali per lanciarli:

  ```bash
  cd /home/oberon/Documents/Development/stenografa
  python3 -m pytest
  ```

## Accelerazione GPU e uso della VRAM

La trascrizione gira su CUDA tramite `faster-whisper` (CTranslate2), con il
modello quantizzato `int8_float16`. Misure reali su RTX 4060 Ti:

| | |
|---|---|
| VRAM occupata dal modello `medium` | ~1.0 GB (picco ~1.1 GB in trascrizione) |
| Caricamento in VRAM | ~4 s, ma avviene **mentre stai già parlando** |
| Trascrizione di 5 s di audio | ~0.3-0.6 s |

Due accorgimenti tengono bassa l'impronta sulla GPU:

- **Caricamento posticipato**: finché non detti nulla, la VRAM è libera. Il
  modello si carica all'inizio della registrazione, in parallelo, quindi
  all'atto pratico non aspetti.
- **Scarico automatico**: dopo 5 minuti di inattività il modello viene tolto
  dalla VRAM (restano ~100 MB di contesto CUDA finché il demone è vivo).

Per regolare il compromesso, modifica in cima a `daemon.py`:

- `MODEL_NAME` — `small` (~0.5 GB) o `base` (~0.3 GB) per occupare meno,
  `large-v3` (~3 GB) per la massima precisione.
- `MODEL_IDLE_TIMEOUT` — secondi prima dello scarico; abbassalo se vuoi la
  GPU libera più in fretta, alzalo se detti spesso.
- `MODEL_DEVICE` — metti `"cpu"` (con `MODEL_COMPUTE_TYPE = "int8"`) per
  tornare al funzionamento senza GPU.

## Note

- Il demone non ha interfaccia grafica: tutto il riscontro passa da notifiche
  desktop (`notify-send` su Linux) e dallo stato mostrato nell'app telefono.
- Il filtro VAD scarta il silenzio prima della trascrizione: evita le
  allucinazioni tipiche di Whisper sulle pause (se non parli, ricevi
  "Nessun testo rilevato" invece di una frase inventata).
- **Incolla adattivo nei terminali**: nella maggior parte degli emulatori di
  terminale (GNOME Terminal, Konsole, xterm, kitty, Windows Terminal/cmd/
  PowerShell, Terminal.app/iTerm...) Ctrl+V non incolla — e' spesso un
  carattere di controllo o semplicemente non mappato. Se la finestra col
  focus al momento dell'incolla automatico sembra un terminale (in base al
  nome dell'applicazione, non al titolo — per evitare falsi positivi da
  testo a caso nel titolo), il demone usa Ctrl+Shift+V al suo posto, sia
  per il testo dettato che per quello tradotto. Richiede lo stesso
  rilevamento della finestra attiva usato per "segui app attiva" (su Linux,
  l'estensione GNOME Shell "Window Calls"); se non disponibile, ricade
  sempre su Ctrl+V.
- Se cambi microfono o hai più dispositivi audio, `pw-record` usa la
  sorgente predefinita di PipeWire (quella impostata nelle Impostazioni
  audio di GNOME).
- Per fermare il demone: `./toggle.py` non basta, usa `pkill -f daemon.py`
  (o trova il PID esatto con `pgrep -af daemon.py`: il pattern deve
  corrispondere alla riga di comando esatta con cui e' stato lanciato,
  relativa o assoluta) oppure invia `quit` al socket. Attenzione:
  `~/.config/autostart/stenografa.desktop` lo rilancia al prossimo login (e
  puo' gia' essere stato rilanciato da GNOME se il demone era stato avviato
  a login e poi terminato manualmente durante la sessione) — il lock su
  `stenografa.lock` evita comunque che due istanze girino insieme in
  conflitto.

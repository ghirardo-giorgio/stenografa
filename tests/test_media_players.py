"""Test dei controlli play/pausa dei video in riproduzione sul PC: lettura
dei player via MPRIS (parsing dell'output di gdbus e deduplica), elenco
mostrato sul telefono e pausa automatica durante la dettatura."""
import daemon as daemon_module
import platform_backend as pb


# output vero di
# `gdbus call --session --dest org.mpris.MediaPlayer2.brave.instance49328
#  --object-path /org/mpris/MediaPlayer2
#  --method org.freedesktop.DBus.Properties.GetAll
#  org.mpris.MediaPlayer2.Player`
BRAVE_PROPS = (
    "({'CanControl': <true>, 'CanGoNext': <false>, 'CanGoPrevious': <false>, "
    "'CanPause': <true>, 'CanPlay': <true>, 'CanSeek': <true>, "
    "'MaximumRate': <1.0>, 'Metadata': <{'mpris:length': <int64 1495181000>, "
    "'mpris:trackid': <objectpath '/com/brave/MediaPlayer2/TrackList/Track02'>, "
    "'xesam:album': <''>, 'xesam:artist': <['']>, "
    "'xesam:title': <'Un video - YouTube'>}>, 'MinimumRate': <1.0>, "
    "'PlaybackStatus': <'Playing'>, 'Position': <int64 47856279>, "
    "'Rate': <0.0>, 'Volume': <1.0>},)"
)


# --- parsing delle proprieta' MPRIS ---


def test_parse_mpris_properties_reads_status_title_and_length():
    props = pb._parse_mpris_properties(BRAVE_PROPS)

    assert props["playing"] is True
    assert props["title"] == "Un video - YouTube"
    assert props["length"] == 1495181000


def test_parse_mpris_properties_recognizes_paused():
    props = pb._parse_mpris_properties(
        BRAVE_PROPS.replace("'PlaybackStatus': <'Playing'>", "'PlaybackStatus': <'Paused'>")
    )

    assert props["playing"] is False


def test_parse_mpris_properties_handles_quotes_in_the_title():
    raw = BRAVE_PROPS.replace(
        "'xesam:title': <'Un video - YouTube'>",
        r"'xesam:title': <'L\'ultimo video'>",
    )

    assert pb._parse_mpris_properties(raw)["title"] == "L'ultimo video"


def test_parse_mpris_properties_ignores_something_that_is_not_a_player():
    assert pb._parse_mpris_properties("({'Identity': <'Brave'>},)") is None


def test_media_player_name_comes_from_the_bus_name():
    assert (
        pb._media_player_name("org.mpris.MediaPlayer2.brave.instance49328")
        == "Brave"
    )


# --- lettura dei flussi audio (pactl) ---


SINK_INPUTS_JSON = """
[
  {
    "index": 969,
    "driver": "PipeWire",
    "corked": false,
    "mute": false,
    "properties": {
      "application.name": "Brave",
      "media.name": "Playback"
    }
  },
  {
    "index": 970,
    "driver": "PipeWire",
    "corked": true,
    "mute": true,
    "properties": {
      "media.name": "Notifica"
    }
  }
]
"""


def test_parse_sink_inputs_reads_id_name_and_flags():
    streams = pb._parse_sink_inputs(SINK_INPUTS_JSON)

    assert streams[0] == {
        "id": 969,
        "name": "Brave",
        "muted": False,
        "active": True,
    }
    # senza application.name si ripiega su media.name; "corked" = sospeso
    assert streams[1]["name"] == "Notifica"
    assert streams[1]["active"] is False
    assert streams[1]["muted"] is True


def test_parse_sink_inputs_survives_unusable_output():
    """pactl assente, versione senza JSON, server audio non raggiungibile:
    la funzione resta inerte invece di far fallire la dettatura."""
    assert pb._parse_sink_inputs(None) == []
    assert pb._parse_sink_inputs("") == []
    assert pb._parse_sink_inputs("non e' json") == []
    assert pb._parse_sink_inputs("[]") == []


# --- deduplica dello stesso video esposto da piu' bus ---


def _player(bus, title="Un video", length=100, playing=True):
    return {
        "id": bus,
        "name": "X",
        "title": title,
        "playing": playing,
        "length": length,
    }


def test_dedupe_prefers_the_native_player_over_the_plasma_proxy():
    """Con l'integrazione browser di Plasma attiva lo stesso video compare
    due volte: sul telefono deve restare una sola icona, quella del player
    del browser."""
    players = [
        _player("org.mpris.MediaPlayer2.plasma-browser-integration"),
        _player("org.mpris.MediaPlayer2.brave.instance49328"),
    ]

    deduped = pb._dedupe_media_players(players)

    assert [p["id"] for p in deduped] == [
        "org.mpris.MediaPlayer2.brave.instance49328"
    ]


def test_dedupe_recognizes_the_same_video_with_differently_written_titles():
    """Caso reale: Brave antepone il contatore delle notifiche e aggiunge il
    nome del sito, l'integrazione di Plasma riporta il titolo pulito. Stesso
    video, due titoli diversi: la durata identica li riconcilia."""
    players = [
        _player(
            "org.mpris.MediaPlayer2.brave.instance49328",
            title="(405) Russia Got TERRIBLE News Today - YouTube",
            length=1495181000,
        ),
        _player(
            "org.mpris.MediaPlayer2.plasma-browser-integration",
            title="Russia Got TERRIBLE News Today",
            length=1495181000,
        ),
    ]

    deduped = pb._dedupe_media_players(players)

    assert [p["id"] for p in deduped] == [
        "org.mpris.MediaPlayer2.brave.instance49328"
    ]


def test_dedupe_keeps_two_videos_with_the_same_length_but_other_titles():
    """La durata da sola non basta: due video diversi possono durare
    uguale."""
    players = [
        _player("org.mpris.MediaPlayer2.a", title="Prima lezione", length=600),
        _player("org.mpris.MediaPlayer2.b", title="Seconda lezione", length=600),
    ]

    assert len(pb._dedupe_media_players(players)) == 2


def test_dedupe_keeps_different_videos():
    players = [
        _player("org.mpris.MediaPlayer2.brave.instance1", title="Primo", length=1),
        _player("org.mpris.MediaPlayer2.vlc", title="Secondo", length=2),
    ]

    assert len(pb._dedupe_media_players(players)) == 2


def test_dedupe_keeps_players_without_metadata_apart():
    """Senza titolo ne' durata non c'e' modo di dire che sono lo stesso
    video: meglio due icone che nasconderne una legittima."""
    players = [
        _player("org.mpris.MediaPlayer2.a", title="", length=None),
        _player("org.mpris.MediaPlayer2.b", title="", length=None),
    ]

    assert len(pb._dedupe_media_players(players)) == 2


# --- elenco mostrato sul telefono ---


class FakeMediaBackend:
    """Backend finto con player e flussi audio pilotabili dal test."""

    def __init__(self, players=(), streams=()):
        self.players = [dict(p) for p in players]
        self.streams = [dict(s) for s in streams]
        self.paused = []
        self.played = []
        self.mute_calls = []

    def list_media_players(self):
        return [dict(p) for p in self.players]

    def list_audio_streams(self):
        return [dict(s) for s in self.streams]

    def set_audio_stream_muted(self, stream_id, muted):
        for stream in self.streams:
            if stream["id"] == stream_id:
                stream["muted"] = muted
                self.mute_calls.append((stream_id, muted))
                return True
        # flusso sparito fra la lettura e il comando (scheda chiusa)
        return False

    def _set_playing(self, player_id, playing):
        for player in self.players:
            if player["id"] == player_id:
                player["playing"] = playing
                return True
        return False

    def media_player_pause(self, player_id):
        self.paused.append(player_id)
        return self._set_playing(player_id, False)

    def media_player_play(self, player_id):
        self.played.append(player_id)
        return self._set_playing(player_id, True)


def _visible(app):
    return [p["id"] for p in app._visible_media_players()]


def test_players_are_listed_whether_they_play_or_not(daemon_app):
    """Anche un video fermo ha il suo pulsante: serve proprio a farlo
    ripartire, ed e' quello che si trova aprendo l'app a video gia' in
    pausa."""
    daemon_app.backend = FakeMediaBackend(
        [
            {"id": "brave", "name": "Brave", "title": "A", "playing": True},
            {"id": "vlc", "name": "Vlc", "title": "B", "playing": False},
        ]
    )

    assert _visible(daemon_app) == ["brave", "vlc"]


def test_a_player_paused_from_the_phone_stays_in_the_list(daemon_app):
    """L'icona deve restare per far ripartire il video, e mostrare subito
    che ora e' in pausa."""
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )

    daemon_app._handle_player_action("brave", "pause")

    assert daemon_app.backend.paused == ["brave"]
    assert _visible(daemon_app) == ["brave"]
    assert daemon_app._media_players[0]["playing"] is False


def test_playing_again_from_the_phone_resumes_it(daemon_app):
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )
    daemon_app._handle_player_action("brave", "pause")

    daemon_app._handle_player_action("brave", "play")

    assert daemon_app.backend.played == ["brave"]
    assert daemon_app._media_players[0]["playing"] is True


def test_a_player_that_disappears_leaves_the_list(daemon_app):
    """Scheda chiusa, player uscito: niente piu' pulsante."""
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )
    daemon_app._handle_player_action("brave", "pause")

    daemon_app.backend.players = []

    assert _visible(daemon_app) == []


def test_a_player_still_claiming_to_play_right_after_the_pause(daemon_app):
    """Regressione: Brave continua a dichiararsi "in riproduzione" per un
    istante dopo aver ricevuto la pausa. L'elenco trasmesso subito dopo il
    tocco deve mostrare comunque la pausa, altrimenti l'icona resta
    indietro rispetto al dito."""

    class SlowToUpdateBackend(FakeMediaBackend):
        def media_player_pause(self, player_id):
            self.paused.append(player_id)
            return True  # eseguita, ma lo stato riportato non cambia subito

    daemon_app.backend = SlowToUpdateBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )

    daemon_app._handle_player_action("brave", "pause")

    # l'elenco appena trasmesso mostra gia' la pausa, senza aspettare che il
    # player si accorga di essersi fermato
    assert daemon_app._media_players[0]["playing"] is False


def test_players_are_broadcast_only_when_they_change(daemon_app):
    sent = []
    daemon_app._broadcast = lambda msg: sent.append(msg)
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )

    daemon_app._refresh_media_players()
    daemon_app._refresh_media_players()

    assert len(sent) == 1
    assert sent[0]["type"] == "players"


# --- pausa automatica durante la dettatura ---


def test_recording_pauses_and_resumes_the_video_when_enabled(daemon_app):
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(
        [
            {"id": "brave", "name": "Brave", "title": "A", "playing": True},
            {"id": "vlc", "name": "Vlc", "title": "B", "playing": False},
        ]
    )

    daemon_app._pause_media_for_recording()

    # il video in pausa da prima non viene toccato: farlo ripartire alla fine
    # sarebbe una sorpresa, non era il demone ad averlo fermato
    assert daemon_app.backend.paused == ["brave"]

    daemon_app._resume_media_after_recording()

    assert daemon_app.backend.played == ["brave"]
    assert daemon_app._paused_for_recording == []


def test_recording_does_not_touch_the_video_when_disabled(daemon_app):
    daemon_app.pause_media_while_recording = False
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )

    daemon_app._pause_media_for_recording()

    assert daemon_app.backend.paused == []


def test_returning_to_idle_resumes_the_paused_video(daemon_app):
    """La ripresa e' agganciata al ritorno a "idle", cosi' vale per ogni
    esito della dettatura (testo incollato, comando IA, errore)."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )
    daemon_app._pause_media_for_recording()

    daemon_app._set_state(daemon_module.STATE_IDLE)

    assert daemon_app.backend.played == ["brave"]


# --- silenziamento dei flussi audio che i player non coprono ---


def _stream(stream_id, name="Brave", muted=False, active=True):
    return {"id": stream_id, "name": name, "muted": muted, "active": active}


def test_streams_are_muted_while_dictating_and_restored_after(daemon_app):
    """Il caso che i player non coprono: due schede dello stesso browser che
    riproducono insieme. Il browser pubblica un solo player, ma i flussi
    audio sono due, e da li' si zittiscono entrambe."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(
        players=[{"id": "brave", "name": "Brave", "title": "A", "playing": True}],
        streams=[_stream(1), _stream(2)],
    )

    daemon_app._pause_media_for_recording()

    assert [s["muted"] for s in daemon_app.backend.streams] == [True, True]

    daemon_app._resume_media_after_recording()

    assert [s["muted"] for s in daemon_app.backend.streams] == [False, False]


def test_a_stream_already_muted_by_the_user_is_left_alone(daemon_app):
    """Riattivarlo alla fine sarebbe una sorpresa: non l'aveva silenziato il
    demone."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1, muted=True)])

    daemon_app._pause_media_for_recording()
    daemon_app._resume_media_after_recording()

    assert daemon_app.backend.mute_calls == []
    assert daemon_app.backend.streams[0]["muted"] is True


def test_an_idle_stream_is_not_muted(daemon_app):
    """Un flusso sospeso non sta suonando: non c'e' niente da zittire."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1, active=False)])

    daemon_app._pause_media_for_recording()

    assert daemon_app.backend.mute_calls == []


def test_a_stream_that_disappears_does_not_break_the_restore(daemon_app):
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1), _stream(2)])
    daemon_app._pause_media_for_recording()

    # la scheda viene chiusa mentre si detta
    daemon_app.backend.streams = [
        s for s in daemon_app.backend.streams if s["id"] != 1
    ]

    daemon_app._resume_media_after_recording()

    assert daemon_app.backend.streams[0]["muted"] is False
    assert daemon_app._muted_for_recording == []


def test_a_stream_reopened_with_a_new_id_is_unmuted_too(daemon_app):
    """Regressione (audio sparito davvero): il server audio ricorda il mute
    per applicazione. Se il video finisce mentre si detta e il browser apre
    un flusso nuovo, quello nasce muto e riattivare il vecchio id non serve
    a niente — l'utente resta senza audio."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1, name="Brave")])
    daemon_app._pause_media_for_recording()

    # il flusso di prima sparisce e ne compare uno nuovo, gia' muto perche'
    # il server audio si ricorda l'applicazione
    daemon_app.backend.streams = [_stream(2, name="Brave", muted=True)]

    daemon_app._resume_media_after_recording()

    assert daemon_app.backend.streams[0]["muted"] is False


def test_an_app_coming_back_muted_after_the_dictation_is_rescued(daemon_app):
    """Stesso rischio, ma il flusso nuovo compare qualche secondo dopo la
    fine della dettatura: ci pensa la finestra di guardia."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1, name="Brave")])
    daemon_app._pause_media_for_recording()
    daemon_app.backend.streams = []
    daemon_app._resume_media_after_recording()

    # il browser riapre il flusso poco dopo, e nasce muto
    daemon_app.backend.streams = [_stream(9, name="Brave", muted=True)]
    daemon_app._unmute_guarded_apps()

    assert daemon_app.backend.streams[0]["muted"] is False


def test_the_guard_expires_and_stops_touching_the_audio(daemon_app):
    """Passata la finestra, un mute e' una scelta dell'utente e va
    rispettata."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1, name="Brave")])
    daemon_app._pause_media_for_recording()
    daemon_app._resume_media_after_recording()
    daemon_app._muted_apps_guard["until"] = 0  # come se fosse passato il tempo

    daemon_app.backend.streams = [_stream(9, name="Brave", muted=True)]
    daemon_app._unmute_guarded_apps()

    assert daemon_app.backend.streams[0]["muted"] is True
    assert daemon_app._muted_apps_guard is None


def test_an_untouched_app_is_never_unmuted_by_the_guard(daemon_app):
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1, name="Brave")])
    daemon_app._pause_media_for_recording()
    daemon_app._resume_media_after_recording()

    # un'altra applicazione, che l'utente ha silenziato per conto suo
    daemon_app.backend.streams.append(_stream(5, name="Spotify", muted=True))
    daemon_app._unmute_guarded_apps()

    assert daemon_app.backend.streams[-1]["muted"] is True


def test_audio_is_not_touched_when_the_option_is_off(daemon_app):
    daemon_app.pause_media_while_recording = False
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1)])

    daemon_app._pause_media_for_recording()

    assert daemon_app.backend.mute_calls == []


def test_returning_to_idle_restores_the_audio(daemon_app):
    """Vale per ogni esito della dettatura, errori compresi."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(streams=[_stream(1)])
    daemon_app._pause_media_for_recording()

    daemon_app._set_state(daemon_module.STATE_IDLE)

    assert daemon_app.backend.streams[0]["muted"] is False


def test_video_paused_by_the_phone_survives_a_dictation(daemon_app):
    """L'automatismo non deve far ripartire quello che l'utente aveva
    fermato apposta dal telefono."""
    daemon_app.pause_media_while_recording = True
    daemon_app.backend = FakeMediaBackend(
        [{"id": "brave", "name": "Brave", "title": "A", "playing": True}]
    )
    daemon_app._handle_player_action("brave", "pause")

    daemon_app._pause_media_for_recording()
    daemon_app._set_state(daemon_module.STATE_IDLE)

    assert daemon_app.backend.played == []


# --- associazione dashboard -> applicazione (per l'icona di sfondo) ---


def test_match_app_by_name_prefers_whole_words():
    """Regressione: "code" come sottostringa pescava "Nobara Codec Wizard"
    al posto di Visual Studio Code, e la dashboard si ritrovava l'icona di
    un'applicazione che non c'entrava."""
    apps = [
        {"id": "/a/nobara-codec-wizard.desktop", "name": "Nobara Codec Wizard"},
        {"id": "/a/code.desktop", "name": "Visual Studio Code"},
    ]

    assert daemon_module._match_app_by_name(apps, ["code"]) == "/a/code.desktop"


def test_match_app_by_name_prefers_the_exact_name():
    apps = [
        {"id": "/a/gimp-dev.desktop", "name": "GIMP Development Version"},
        {"id": "/a/gimp.desktop", "name": "GIMP"},
    ]

    assert daemon_module._match_app_by_name(apps, ["gimp"]) == "/a/gimp.desktop"


def test_match_app_by_name_gives_up_instead_of_guessing():
    """Un'app web (InvokeAI nel browser) non ha un'applicazione installata:
    meglio nessuna icona che quella sbagliata."""
    apps = [{"id": "/a/firefox.desktop", "name": "Firefox"}]

    assert daemon_module._match_app_by_name(apps, ["invoke"]) is None

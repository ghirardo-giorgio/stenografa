"""Test della gestione lato demone di list_apps/launch_app (i comandi del
socket di controllo usati dai tool MCP list_launchable_apps/launch_app):
qui solo _handle_launch_app, che valida l'id contro l'elenco aggiornato di
Backend.list_apps() prima di avviare qualunque cosa - stesso principio di
sicurezza usato per le scorciatoie ai_command (l'MCP/LLM puo' solo
selezionare da un elenco noto, mai eseguire un id arbitrario)."""


class FakeBackend:
    def __init__(self, apps, launch_ok=True):
        self._apps = apps
        self._launch_ok = launch_ok
        self.launched = []

    def list_apps(self):
        return self._apps

    def launch_app(self, app_id):
        self.launched.append(app_id)
        return self._launch_ok


def test_handle_launch_app_launches_known_id(daemon_app):
    daemon_app.backend = FakeBackend(
        [{"id": "/path/firefox.desktop", "name": "Firefox"}]
    )

    result = daemon_app._handle_launch_app("/path/firefox.desktop")

    assert result == {"ok": True}
    assert daemon_app.backend.launched == ["/path/firefox.desktop"]


def test_handle_launch_app_rejects_unknown_id(daemon_app):
    daemon_app.backend = FakeBackend(
        [{"id": "/path/firefox.desktop", "name": "Firefox"}]
    )

    result = daemon_app._handle_launch_app("/path/not-in-the-list.desktop")

    assert result["ok"] is False
    assert "non trovata" in result["error"]
    assert daemon_app.backend.launched == []  # mai lanciato


def test_handle_launch_app_rejects_missing_id(daemon_app):
    daemon_app.backend = FakeBackend([])

    result = daemon_app._handle_launch_app(None)

    assert result["ok"] is False
    assert "mancante" in result["error"]


def test_handle_launch_app_reports_launch_failure(daemon_app):
    daemon_app.backend = FakeBackend(
        [{"id": "/path/gimp.desktop", "name": "GIMP"}], launch_ok=False
    )

    result = daemon_app._handle_launch_app("/path/gimp.desktop")

    assert result["ok"] is False
    assert "non riuscito" in result["error"]

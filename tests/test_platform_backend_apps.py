"""Test dell'enumerazione/avvio applicazioni installate (list_apps/
launch_app), usati dai tool MCP list_launchable_apps/launch_app. Qui solo
la logica di parsing dei file .desktop del backend Linux (l'unico
eseguibile in questo ambiente di test) - i backend Windows/macOS restano
verificabili solo manualmente, come il resto di platform_backend.py."""
import platform_backend


def _write_desktop(path, **fields):
    lines = ["[Desktop Entry]"]
    for key, value in fields.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_list_apps_parses_valid_desktop_files(tmp_path, monkeypatch):
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    _write_desktop(
        apps_dir / "firefox.desktop",
        Type="Application",
        Name="Firefox",
        Exec="firefox %u",
    )
    _write_desktop(
        apps_dir / "gimp.desktop",
        Type="Application",
        Name="GIMP",
        Exec="gimp %U",
    )

    backend = platform_backend.LinuxBackend("/tmp")
    monkeypatch.setattr(backend, "_DESKTOP_DIRS", (str(apps_dir),))

    apps = backend.list_apps()

    names = sorted(a["name"] for a in apps)
    assert names == ["Firefox", "GIMP"]
    assert all(a["id"].endswith(".desktop") for a in apps)


def test_list_apps_skips_non_application_and_hidden_entries(tmp_path, monkeypatch):
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    _write_desktop(
        apps_dir / "link.desktop", Type="Link", Name="Un link", URL="http://x"
    )
    _write_desktop(
        apps_dir / "nodisplay.desktop",
        Type="Application",
        Name="Helper interno",
        NoDisplay="true",
    )
    _write_desktop(
        apps_dir / "hidden.desktop",
        Type="Application",
        Name="Nascosta",
        Hidden="true",
    )
    _write_desktop(
        apps_dir / "noname.desktop", Type="Application", Exec="qualcosa"
    )
    _write_desktop(
        apps_dir / "ok.desktop", Type="Application", Name="App valida"
    )

    backend = platform_backend.LinuxBackend("/tmp")
    monkeypatch.setattr(backend, "_DESKTOP_DIRS", (str(apps_dir),))

    apps = backend.list_apps()

    assert [a["name"] for a in apps] == ["App valida"]


def test_list_apps_deduplicates_across_directories(tmp_path, monkeypatch):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _write_desktop(dir_a / "app.desktop", Type="Application", Name="App")
    _write_desktop(dir_b / "app.desktop", Type="Application", Name="App")

    backend = platform_backend.LinuxBackend("/tmp")
    monkeypatch.setattr(backend, "_DESKTOP_DIRS", (str(dir_a), str(dir_b)))

    apps = backend.list_apps()

    assert len(apps) == 2  # percorsi diversi restano voci distinte


def test_list_apps_ignores_malformed_desktop_file(tmp_path, monkeypatch):
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    (apps_dir / "broken.desktop").write_text("questo non e' un file .desktop valido {{{")
    _write_desktop(apps_dir / "ok.desktop", Type="Application", Name="App valida")

    backend = platform_backend.LinuxBackend("/tmp")
    monkeypatch.setattr(backend, "_DESKTOP_DIRS", (str(apps_dir),))

    apps = backend.list_apps()

    assert [a["name"] for a in apps] == ["App valida"]


def test_list_apps_missing_directory_is_ignored(tmp_path, monkeypatch):
    backend = platform_backend.LinuxBackend("/tmp")
    monkeypatch.setattr(
        backend, "_DESKTOP_DIRS", (str(tmp_path / "non-esiste"),)
    )

    assert backend.list_apps() == []


def test_launch_app_uses_gio_launch(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeResult()

    monkeypatch.setattr(platform_backend.subprocess, "run", fake_run)

    backend = platform_backend.LinuxBackend("/tmp")
    ok = backend.launch_app("/usr/share/applications/firefox.desktop")

    assert ok is True
    assert calls == [["gio", "launch", "/usr/share/applications/firefox.desktop"]]


def test_launch_app_reports_failure(monkeypatch):
    class FakeResult:
        returncode = 1

    monkeypatch.setattr(
        platform_backend.subprocess, "run", lambda *a, **kw: FakeResult()
    )

    backend = platform_backend.LinuxBackend("/tmp")
    assert backend.launch_app("/nonexistent.desktop") is False

"""Test del canale cifrato verso l'app telefono: generazione del
certificato, impronta mostrata all'utente per il pinning e riconoscimento
automatico fra connessione TLS e in chiaro sulla stessa porta."""
import shutil
import socket
import threading

import pytest

import daemon as daemon_module


openssl_required = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="openssl non disponibile: il demone resta in chiaro",
)


@openssl_required
def test_generates_certificate_and_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(daemon_module, "CERT_PATH", tmp_path / "cert.pem")
    monkeypatch.setattr(daemon_module, "KEY_PATH", tmp_path / "key.pem")

    context, fingerprint = daemon_module._build_tls_context()

    assert context is not None
    # formato "AA:BB:..." identico a quello che l'app telefono calcola dal
    # certificato presentato, cosi' le due stringhe si confrontano a vista
    assert fingerprint is not None
    parts = fingerprint.split(":")
    assert len(parts) == 20  # SHA-1
    assert all(len(p) == 2 for p in parts)


@openssl_required
def test_certificate_is_reused_between_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(daemon_module, "CERT_PATH", tmp_path / "cert.pem")
    monkeypatch.setattr(daemon_module, "KEY_PATH", tmp_path / "key.pem")

    _first, fingerprint1 = daemon_module._build_tls_context()
    _second, fingerprint2 = daemon_module._build_tls_context()

    # se cambiasse ad ogni riavvio il pinning sul telefono scatterebbe
    # ogni volta, addestrando l'utente ad accettare qualunque certificato
    assert fingerprint1 == fingerprint2


def test_missing_certificate_leaves_daemon_in_the_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "CERT_PATH", tmp_path / "assente.pem")
    monkeypatch.setattr(daemon_module, "KEY_PATH", tmp_path / "assente.key")
    monkeypatch.setattr(daemon_module, "_generate_tls_cert", lambda: False)

    assert daemon_module._build_tls_context() == (None, None)


def _peek_kind(app, payload):
    """Passa `payload` a _wrap_if_tls attraverso una coppia di socket e
    ritorna la socket restituita (None se la connessione e' stata chiusa)."""
    server, client = socket.socketpair()
    with server, client:
        threading.Thread(target=lambda: client.sendall(payload), daemon=True).start()
        return app._wrap_if_tls(server)


def test_plaintext_client_is_served_as_before(daemon_app):
    daemon_app.require_tls = False
    daemon_app.tls_context = None

    conn = _peek_kind(daemon_app, b'{"cmd":"auth","token":"12345"}\n')

    assert conn is not None


def test_plaintext_client_is_refused_when_tls_required(daemon_app):
    daemon_app.require_tls = True
    daemon_app.tls_context = None

    conn = _peek_kind(daemon_app, b'{"cmd":"auth","token":"12345"}\n')

    assert conn is None


def test_tls_handshake_without_certificate_is_dropped(daemon_app):
    """Primo byte di un ClientHello TLS (0x16 0x03) ma il demone non ha un
    certificato: si chiude invece di rispondere in chiaro a un client che si
    aspetta un canale cifrato."""
    daemon_app.require_tls = False
    daemon_app.tls_context = None

    conn = _peek_kind(daemon_app, b"\x16\x03\x01\x00\x50")

    assert conn is None


@openssl_required
def test_real_tls_client_is_wrapped_and_readable(daemon_app, tmp_path, monkeypatch):
    """Handshake vero (non solo il riconoscimento dei primi byte): un client
    TLS deve arrivare fino all'invio del comando di autenticazione."""
    import ssl

    monkeypatch.setattr(daemon_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(daemon_module, "CERT_PATH", tmp_path / "cert.pem")
    monkeypatch.setattr(daemon_module, "KEY_PATH", tmp_path / "key.pem")
    daemon_app.tls_context, _fingerprint = daemon_module._build_tls_context()
    daemon_app.require_tls = True

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def client():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # il certificato e' self-signed: il pinning (lato app telefono)
        # sostituisce la verifica standard, qui non serve verificarlo
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", port)) as raw:
            with context.wrap_socket(raw) as tls:
                tls.sendall(b'{"cmd":"auth","token":"12345"}\n')

    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    conn, _addr = listener.accept()

    wrapped = daemon_app._wrap_if_tls(conn)

    assert wrapped is not None
    assert b'"cmd":"auth"' in wrapped.recv(64)
    wrapped.close()
    listener.close()
    thread.join(timeout=5)


def test_require_tls_is_refused_without_certificate(daemon_app):
    """Accettarlo renderebbe il demone irraggiungibile da qualunque
    telefono, senza modo di tornare indietro dall'app."""
    daemon_app.tls_context = None

    ok, error = daemon_app._set_require_tls(True)

    assert not ok
    assert "certificato" in error

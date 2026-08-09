"""Test dell'handshake di autenticazione con l'app telefono, in particolare
del campo "reason" che accompagna un rifiuto: l'app deve poter distinguere
un token davvero sbagliato (da correggere a mano) da un rifiuto temporaneo
(connessione non cifrata, troppi tentativi ravvicinati), su cui invece deve
continuare a riprovare da sola."""
import json
import socket

import daemon as daemon_module


def _exchange(app, payload, handler="client"):
    """Esegue l'handshake su una coppia di socket e ritorna il primo
    messaggio che il demone manda indietro (None se non risponde)."""
    server, client = socket.socketpair()
    with server, client:
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        if handler == "client":
            app._handle_network_client(server, ("192.168.1.50", 5555))
        else:
            app._wrap_if_tls(server)
        try:
            data = client.recv(4096)
        except OSError:
            return None
    if not data:
        return None
    return json.loads(data.decode("utf-8").splitlines()[0])


def test_wrong_token_is_reported_as_bad_token(daemon_app):
    daemon_app.auth_token = "12345"

    reply = _exchange(daemon_app, b'{"cmd":"auth","token":"99999"}\n')

    assert reply["type"] == "auth" and reply["ok"] is False
    assert reply["reason"] == "bad_token"


def test_plaintext_refusal_is_reported_as_tls_required(daemon_app):
    """Il caso che lasciava l'app "non connessa" fino alla riapertura: il
    telefono ricade in chiaro dopo un handshake TLS fallito e il demone lo
    rifiuta. Non e' il token ad essere sbagliato, quindi va ritentato."""
    daemon_app.require_tls = True
    daemon_app.tls_context = None

    reply = _exchange(
        daemon_app, b'{"cmd":"auth","token":"12345"}\n', handler="wrap"
    )

    assert reply["ok"] is False
    assert reply["reason"] == "tls_required"


def test_rate_limited_refusal_is_marked_temporary(daemon_app):
    daemon_app.auth_token = "12345"
    for _ in range(daemon_module.AUTH_MAX_ATTEMPTS):
        daemon_app._record_auth_failure("192.168.1.50")

    # anche con il token giusto: la finestra di rate limit non e' scaduta
    reply = _exchange(daemon_app, b'{"cmd":"auth","token":"12345"}\n')

    assert reply["ok"] is False
    assert reply["reason"] == "rate_limited"


def test_correct_token_is_accepted(daemon_app):
    daemon_app.auth_token = "12345"
    daemon_app.state = daemon_module.STATE_IDLE

    reply = _exchange(daemon_app, b'{"cmd":"auth","token":"12345"}\n')

    assert reply["ok"] is True
    assert "reason" not in reply

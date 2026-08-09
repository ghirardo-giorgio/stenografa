#!/home/oberon/.pyenv/shims/python3
"""
Invia un comando di toggle al demone stenografa (daemon.py) tramite socket
TCP su loopback. Pensato per essere collegato a una scorciatoia da tastiera
personalizzata di GNOME (Impostazioni > Tastiera > Scorciatoie personalizzate).
"""
import socket
import subprocess
import sys

LOCAL_HOST = "127.0.0.1"
TOGGLE_SOCKET_PORT = 8766


def main():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect((LOCAL_HOST, TOGGLE_SOCKET_PORT))
            s.sendall(b"toggle")
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        subprocess.run(
            [
                "notify-send", "-u", "critical", "-a", "Stenografa",
                "Stenografa non attivo",
                "Il demone non risulta in esecuzione. Avvialo con daemon.py.",
            ],
            check=False,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

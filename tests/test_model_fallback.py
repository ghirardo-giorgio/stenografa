"""Ripiego della trascrizione sulla CPU quando la GPU non e' utilizzabile
(scheda assente, driver mancanti, oppure VRAM gia' occupata da altro).

Senza, la dettatura fallisce e basta: e' successo davvero, con
"CUDA failed with error out of memory" mentre un altro modello occupava la
scheda."""
import daemon


class FakeWhisperModel:
    """Finto WhisperModel: fallisce su cuda, si carica su cpu."""

    creati = []

    def __init__(self, name, device="cpu", compute_type="int8", fail_on=("cuda",)):
        FakeWhisperModel.creati.append((name, device, compute_type))
        if device in fail_on:
            raise RuntimeError("CUDA failed with error out of memory")
        self.name = name
        self.device = device


def _installa_finto_modello(monkeypatch, fail_on=("cuda",)):
    """faster_whisper viene importato dentro _load_locked, quindi si sostituisce
    il modulo prima che la funzione lo importi."""
    import sys
    import types

    FakeWhisperModel.creati = []
    modulo = types.ModuleType("faster_whisper")

    def costruttore(name, device="cpu", compute_type="int8"):
        return FakeWhisperModel(name, device, compute_type, fail_on=fail_on)

    modulo.WhisperModel = costruttore
    monkeypatch.setitem(sys.modules, "faster_whisper", modulo)


def test_falls_back_to_cpu_when_the_gpu_is_unusable(monkeypatch):
    _installa_finto_modello(monkeypatch)
    avvisi = []
    manager = daemon.ModelManager(on_fallback=avvisi.append)

    with manager._lock:
        manager._load_locked()

    # prima ci ha provato sulla GPU, poi e' ripiegato
    assert FakeWhisperModel.creati[0][1] == daemon.MODEL_DEVICE
    assert FakeWhisperModel.creati[1][1] == daemon.MODEL_FALLBACK_DEVICE
    assert manager.device == daemon.MODEL_FALLBACK_DEVICE
    # e l'ha detto: senza avviso sembrerebbe solo che il PC si e' impallato
    assert len(avvisi) == 1
    assert "out of memory" in avvisi[0]


def test_no_fallback_when_the_gpu_works(monkeypatch):
    _installa_finto_modello(monkeypatch, fail_on=())
    avvisi = []
    manager = daemon.ModelManager(on_fallback=avvisi.append)

    with manager._lock:
        manager._load_locked()

    assert manager.device == daemon.MODEL_DEVICE
    assert len(FakeWhisperModel.creati) == 1
    assert avvisi == []


def test_the_fallback_keeps_the_same_model(monkeypatch):
    """Cambiare anche modello significherebbe cambiare la qualita' della
    trascrizione proprio nel momento in cui qualcosa e' gia' andato storto."""
    _installa_finto_modello(monkeypatch)
    manager = daemon.ModelManager()
    with manager._lock:
        manager._load_locked()
    nome_gpu = FakeWhisperModel.creati[0][0]
    nome_cpu = FakeWhisperModel.creati[1][0]
    assert nome_cpu == nome_gpu


def test_unloading_lets_the_gpu_be_tried_again(monkeypatch):
    """Se si era ripiegato perche' la VRAM era occupata, dopo lo scarico per
    inattivita' vale la pena riprovare: nel frattempo puo' essersi liberata."""
    _installa_finto_modello(monkeypatch)
    manager = daemon.ModelManager()
    with manager._lock:
        manager._load_locked()
    assert manager.device == daemon.MODEL_FALLBACK_DEVICE

    manager._last_used = 0.0  # come se fosse passato il timeout
    assert manager.unload_if_idle()
    assert manager.device == daemon.MODEL_DEVICE


def test_the_dictation_still_fails_if_even_the_cpu_cannot_load(monkeypatch):
    """Non si inventa un terzo ripiego: l'errore arriva a chi ha dettato."""
    _installa_finto_modello(monkeypatch, fail_on=("cuda", "cpu"))
    manager = daemon.ModelManager()
    import pytest

    with pytest.raises(RuntimeError):
        with manager._lock:
            manager._load_locked()


def test_the_daemon_reports_which_device_is_in_use(daemon_app):
    assert daemon_app._config_snapshot()["model_device"] == daemon.MODEL_DEVICE
    daemon_app.model.device = daemon.MODEL_FALLBACK_DEVICE
    assert (
        daemon_app._config_snapshot()["model_device"]
        == daemon.MODEL_FALLBACK_DEVICE
    )

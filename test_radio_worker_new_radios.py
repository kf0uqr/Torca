"""
Wiring tests for the newly-added radio support (IC-7610, X6200, FTX-1):

1. RadioWorker._build_config() actually passes model= through to
   rigplane's backend config -- a real pre-existing bug (confirmed live
   before this fix: constructing SerialBackendConfig the way this app
   used to, with no model=, always produced an Icom7610SerialRadio
   regardless of which radio was selected, since rigplane's own backend
   factory defaults an unset serial model to "IC-7610"). Fatal for a
   genuinely different protocol family (FTX-1's Yaesu CAT) and a silent
   behavior swap for every other radio.
2. radio_worker.py's own patch to rigplane's Yaesu CAT backend
   (_RIGS_DIR) is actually applied on import, and a real rigplane
   backend factory call for "FTX-1" produces a working YaesuCatRadio
   instance rather than raising RigLoadError.

Does NOT re-test connection_dialog.py's own UI logic (see
test_connection_dialog_new_radios.py for that) or rigplane's own
protocol/CI-V correctness -- this only confirms TORCA's own plumbing
sends the right things to rigplane.

Run directly: ./bin/python3 test_radio_worker_new_radios.py
"""

import sys

from radio_worker import RadioWorker
from rigctld_launcher import ManagedRigctld


def test_build_config_passes_model_for_serial():
    print("--- _build_config() passes model= for serial connections ---")
    worker = RadioWorker({
        "radio_model": "IC-7610",
        "connection_type": "usb",
        "serial_port": "/dev/ttyUSB0",
        "baud_rate": 115200,
        "addr": 0x98,
    })
    cfg = worker._build_config()
    assert cfg.model == "IC-7610", cfg.model
    print("  PASSED\n")


def test_build_config_passes_model_for_lan():
    print("--- _build_config() passes model= for network connections ---")
    worker = RadioWorker({
        "radio_model": "IC-9700",
        "connection_type": "network",
        "host": "127.0.0.1",
        "port": 50011,
        "username": "u",
        "password": "p",
        "addr": 0xA2,
    })
    cfg = worker._build_config()
    assert cfg.model == "IC-9700", cfg.model
    print("  PASSED\n")


def test_serial_model_routes_to_correct_backend_class():
    """Confirms the fix actually changes real routing behavior, not
    just that a dataclass field got set -- constructs a real rigplane
    backend for each radio_model and checks the class, the same check
    that would have caught the original bug (everything defaulting to
    Icom7610SerialRadio)."""
    print("--- corrected model= routes to the correct rigplane backend class ---")
    from rigplane.backends.factory import create_radio

    expectations = {
        "IC-7300": "Ic7300SerialRadio",
        "IC-9700": "Ic9700SerialRadio",
        "IC-705": "Ic705SerialRadio",
        "IC-7610": "Icom7610SerialRadio",
        "X6200": "Ic705SerialRadio",  # shares the IC-705 CI-V personality, see x6200.toml
        "ID-5100": "Ic705SerialRadio",  # generic transport class, see id5100.toml/factory.py
    }
    for radio_model, expected_class in expectations.items():
        worker = RadioWorker({
            "radio_model": radio_model,
            "connection_type": "usb",
            "serial_port": "/dev/ttyUSB0",
            "baud_rate": 115200,
            "addr": None,
        })
        cfg = worker._build_config()
        radio = create_radio(cfg)
        assert type(radio).__name__ == expected_class, (radio_model, type(radio).__name__)
    print("  PASSED\n")


def test_ftx1_routes_to_yaesu_cat_radio():
    print("--- FTX-1 routes to YaesuCatRadio (not the Icom CI-V factory branch) ---")
    from rigplane.backends.factory import create_radio

    worker = RadioWorker({
        "radio_model": "FTX-1",
        "connection_type": "usb",
        "serial_port": "/dev/ttyUSB0",
        "baud_rate": 38400,
        "addr": None,
    })
    cfg = worker._build_config()
    assert cfg.model == "FTX-1", cfg.model
    radio = create_radio(cfg)
    assert type(radio).__name__ == "YaesuCatRadio", type(radio).__name__
    print("  PASSED\n")


def test_trusdx_is_recognized_as_a_managed_rigctld_radio():
    """truSDX has no native rigplane backend (see RADIO_PROFILES' own
    comment in constants.py) -- a plain USB connection for it must be
    recognized as needing a locally-managed rigctld (_start_managed_
    rigctld, called from _main() before _build_config()), the same way
    every other USB radio is NOT."""
    print("--- truSDX (USB) is recognized as a managed-rigctld radio ---")
    worker = RadioWorker({
        "radio_model": "truSDX",
        "connection_type": "usb",
        "serial_port": "/dev/ttyUSB0",
        "baud_rate": 115200,
        "addr": None,
    })
    assert worker._is_managed_rigctld_radio()

    other = RadioWorker({
        "radio_model": "IC-7300",
        "connection_type": "usb",
        "serial_port": "/dev/ttyUSB0",
        "baud_rate": 115200,
        "addr": 0x94,
    })
    assert not other._is_managed_rigctld_radio()
    print("  PASSED\n")


def test_tx_push_target_routes_to_passthrough_bridge():
    """_tx_push_target() (used by send_tx_audio_pcm/start_tx_audio_stream,
    the APRS/PSK31/RTTY send paths) must resolve to a PassthroughAudio-
    Bridge's own play_tx_pcm() for a cable-connected radio like truSDX --
    self.radio (a RigctldRadioAdapter) has no push_audio_tx_pcm/push_tx
    method at all, so falling through to the normal find_method_name()
    resolution would wrongly return None even with a working TX audio
    device configured."""
    print("--- _tx_push_target() routes to a PassthroughAudioBridge's play_tx_pcm ---")
    from audio import PassthroughAudioBridge

    worker = RadioWorker({
        "radio_model": "truSDX",
        "connection_type": "usb",
        "serial_port": "/dev/ttyUSB0",
        "baud_rate": 115200,
        "addr": None,
    })
    bridge = object.__new__(PassthroughAudioBridge)
    bridge._out_stream = object()  # "open"
    worker.audio_bridge = bridge
    worker.radio = object()  # a bare radio with no push_audio_tx_pcm/push_tx at all

    push = worker._tx_push_target()
    assert push == bridge.play_tx_pcm, push

    bridge._out_stream = None  # "not open" -- no valid TX destination
    assert worker._tx_push_target() is None
    print("  PASSED\n")


def test_rigctld_radio_adapter_provides_get_set_frequency():
    """RigctldRadioAdapter (rigctld_adapter.py) is what _main() actually
    constructs for a managed-rigctld radio (bypassing _build_config()/
    create_radio() entirely, same as the "remote" connection_type
    bypasses them for RemoteWebRadio) -- confirms it patches over the
    real gap in rigplane's own RigctldClientRadio: get_frequency/
    set_frequency, called directly and unconditionally all over
    radio_worker.py (tuning knob, band buttons, the fast poll loop),
    which RigctldClientRadio itself doesn't implement (only the
    shorter get_freq/set_freq it uses internally) -- confirmed live
    this was a real, functional bug (AttributeError on every single
    frequency read/write), not just a naming nitpick."""
    print("--- RigctldRadioAdapter provides get_frequency/set_frequency ---")
    import asyncio

    from rigctld_adapter import RigctldRadioAdapter

    async def go():
        m = ManagedRigctld("/dev/null", 115200, [1])  # Hamlib Dummy -- no real serial device needed
        await m.start()
        try:
            radio = RigctldRadioAdapter(host=m.host, port=m.port, model="truSDX")
            async with radio:
                await radio.set_frequency(14074000, receiver=0)
                freq = await radio.get_frequency(receiver=0)
                assert freq == 14074000, freq
        finally:
            await m.stop()

    asyncio.run(go())
    print("  PASSED\n")


def test_yaesu_rigs_dir_patch_applied():
    print("--- rigplane's Yaesu CAT rig-profile directory patch is applied ---")
    import rigplane.backends.yaesu_cat.radio as yaesu_module

    assert yaesu_module._RIGS_DIR.exists(), yaesu_module._RIGS_DIR
    assert (yaesu_module._RIGS_DIR / "ftx1.toml").exists()
    print("  PASSED\n")


def main():
    ok = True
    try:
        test_build_config_passes_model_for_serial()
        test_build_config_passes_model_for_lan()
        test_serial_model_routes_to_correct_backend_class()
        test_ftx1_routes_to_yaesu_cat_radio()
        test_trusdx_is_recognized_as_a_managed_rigctld_radio()
        test_tx_push_target_routes_to_passthrough_bridge()
        test_rigctld_radio_adapter_provides_get_set_frequency()
        test_yaesu_rigs_dir_patch_applied()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        ok = False
    if ok:
        print("ALL NEW-RADIO WIRING TESTS PASSED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

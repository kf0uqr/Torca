"""
RigctldRadioAdapter: a thin compatibility subclass over rigplane's
RigctldClientRadio (backends/rigctld_client/radio.py).

radio_worker.py calls radio.get_frequency(receiver=...)/
radio.set_frequency(freq_hz, receiver=...) directly and unconditionally
in many places (the tuning knob, band buttons, the fast poll loop, ...)
-- these are treated as guaranteed-present "tier-1" Radio protocol
methods everywhere else in this app, since every native rigplane backend
(Icom CI-V, Yaesu CAT) implements them. RigctldClientRadio is an
intentionally partial, still-experimental backend (see its own
_SUPPORTED_COMMANDS set) that only implements the shorter get_freq/
set_freq names it needs for its own internal polling -- confirmed live
this mismatch is fatal, not cosmetic: every frequency read/write against
a plain RigctldClientRadio instance raised AttributeError, spamming the
error log and leaving band-select/tuning completely non-functional.

Everything else this app calls directly (get_mode/set_mode, set_ptt) is
inherited unchanged -- their signatures already match what
radio_worker.py calls, confirmed by reading RigctldClientRadio's own
method signatures.
"""

from rigplane.backends.rigctld_client.radio import RigctldClientRadio


class RigctldRadioAdapter(RigctldClientRadio):
    async def get_frequency(self, receiver: int = 0, bypass_cache: bool = False) -> int:
        return await self.get_freq(receiver=receiver)

    async def set_frequency(self, freq_hz: int, receiver: int = 0) -> None:
        await self.set_freq(int(freq_hz), receiver=receiver)

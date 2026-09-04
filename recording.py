"""Audio recording: capture (Recorder), persistent index, and export.

No Qt here at all -- Recorder is fed raw PCM chunks from AudioBridge's
extra RX/TX callbacks (audio.py), which fire on non-GUI threads (the
asyncio loop for RX, the PortAudio input-stream thread for TX). Keeping
this file Qt-free means those callbacks can call straight into it
without any cross-thread signal marshaling for the actual audio writes
-- only status/UI updates need to hop to the GUI thread, and that's
recording_window.py's job (it polls Recorder's own plain attributes
from a QTimer rather than Recorder emitting anything itself).

Storage follows the same convention as memories_window.py's
MEMORIES_PATH: everything under ~/.icom_radio_app_cache/, a flat JSON
index (recordings.json) plus a subdirectory for the actual binary
files (recordings/*.wav).
"""

import json
import pathlib
import shutil
import subprocess
import threading
import uuid
import wave

import numpy as np

from constants import AUDIO_SAMPLE_WIDTH, AUDIO_TX_PCM_SAMPLE_RATE

RECORDINGS_DIR = pathlib.Path.home() / ".icom_radio_app_cache" / "recordings"
RECORDINGS_INDEX_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "recordings.json"


def load_recordings():
    """Same defensive load pattern as memories_window.py's own
    _load_memories -- corrupt/missing index just means "no recordings
    yet", never a crash."""
    if RECORDINGS_INDEX_PATH.exists():
        try:
            data = json.loads(RECORDINGS_INDEX_PATH.read_text())
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
    return []


def save_recordings(entries):
    RECORDINGS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORDINGS_INDEX_PATH.write_text(json.dumps(entries, indent=2))


def new_recording_path():
    """Allocates a fresh, collision-free .wav path under RECORDINGS_DIR
    (a random filename, not anything derived from the operator-editable
    "name" field -- that's index-only metadata, never touches the
    filesystem, so renaming a recording later can't collide with or
    orphan the file)."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.wav"
    return RECORDINGS_DIR / filename, filename


def _resample_pcm16_mono(pcm_bytes, src_rate, dst_rate):
    """Simple linear-interpolation resample of mono PCM16 audio --
    good enough for voice-quality TX audio being folded into a mixed
    RX+TX recording (see Recorder's own docstring for why only TX ever
    needs this). numpy is already a hard dependency (constants.py's
    other users, sgp4 orbit math) -- no new library needed just for
    this."""
    if src_rate == dst_rate or not pcm_bytes:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if samples.size == 0:
        return pcm_bytes
    duration = samples.size / src_rate
    dst_count = max(1, round(duration * dst_rate))
    src_x = np.arange(samples.size)
    dst_x = np.linspace(0, samples.size - 1, dst_count)
    resampled = np.interp(dst_x, src_x, samples.astype(np.float64))
    return resampled.astype(np.int16).tobytes()


def _extract_channel(interleaved_bytes, channel):
    """Pulls just Main (left) or Sub (right) out of raw interleaved
    stereo PCM16 -- see AudioBridge.add_extra_rx_raw_callback's own
    docstring for the L=Main/R=Sub convention this matches. Drops a
    trailing incomplete sample pair, if any, rather than raising."""
    samples = np.frombuffer(interleaved_bytes, dtype=np.int16)
    usable = samples.size - (samples.size % 2)
    if usable < 2:
        return b""
    stereo = samples[:usable].reshape(-1, 2)
    index = 0 if channel == "main" else 1
    return stereo[:, index].tobytes()


class Recorder:
    """Streams RX/TX PCM chunks straight into one open .wav file --
    never buffers a whole recording in memory, so an hours-long
    satellite pass costs no more RAM than a few seconds would.

    source: "rx" / "tx" / "both" (RX+TX combined into one mono track).
    rx_channel: "main" / "sub" / "both" / None -- only meaningful when
        source involves RX; "both" only valid together with source=="rx"
        (RX+TX "both" is a single mono track, see set_tx_active's own
        docstring for why Main/Sub picking doesn't apply there).
    rx_sample_rate: the connected radio's own AudioBridge.sample_rate.
    rx_is_stereo: AudioBridge.is_rx_stereo() at record-start time --
        whether raw RX chunks are genuinely interleaved stereo (only
        then does "main"/"sub" extraction make sense; a mono-radio
        capture just writes the raw bytes straight through regardless
        of what rx_channel was requested).

    Thread safety: on_rx_raw() is called from the asyncio loop thread,
    on_tx()/set_tx_active() from the PortAudio input-stream thread --
    genuinely concurrent with each other, both writing into the same
    wave.Wave_write handle, hence the lock around every method that
    touches it.
    """

    def __init__(self, path, source, rx_channel, rx_sample_rate, rx_is_stereo):
        self.source = source
        self.rx_channel = rx_channel
        self.path = path
        self._rx_sample_rate = rx_sample_rate
        self._rx_is_stereo = rx_is_stereo
        self._lock = threading.Lock()
        self._tx_in_progress = False
        self._frames_written = 0
        self._closed = False

        if source == "rx" and rx_channel == "both":
            self._channels = 2
            self._rate = rx_sample_rate
        elif source == "tx":
            self._channels = 1
            self._rate = AUDIO_TX_PCM_SAMPLE_RATE
        else:
            # rx (main/sub/mono-radio), or "both" (RX+TX mixed mono) --
            # both land on RX's own native rate; see the module
            # docstring / _resample_pcm16_mono for why TX is the one
            # that gets resampled, not RX.
            self._channels = 1
            self._rate = rx_sample_rate

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(self._channels)
        self._wav.setsampwidth(AUDIO_SAMPLE_WIDTH)
        self._wav.setframerate(self._rate)

    def on_rx_raw(self, pcm_bytes):
        """Feed from AudioBridge.add_extra_rx_raw_callback(). No-op if
        this recording's source doesn't involve RX at all."""
        if self.source not in ("rx", "both"):
            return
        with self._lock:
            if self._closed:
                return
            if self.source == "both" and self._tx_in_progress:
                # RX+TX mono mode: TX audio takes over the single track
                # for the duration of a transmission -- see
                # set_tx_active's own docstring for why this needs no
                # explicit timestamp/silence-padding logic.
                return
            if self.rx_channel == "both":
                data = pcm_bytes  # interleaved stereo, passthrough
            elif self.rx_channel in ("main", "sub") and self._rx_is_stereo:
                data = _extract_channel(pcm_bytes, self.rx_channel)
            else:
                data = pcm_bytes  # mono radio, or a stereo-only request on a mono stream
            self._write_locked(data)

    def on_tx(self, pcm_bytes):
        """Feed from AudioBridge.add_extra_tx_callback(). Only ever
        invoked while genuinely transmitting (the hook site in audio.py
        already guarantees that). No-op if this recording's source
        doesn't involve TX at all."""
        if self.source not in ("tx", "both"):
            return
        with self._lock:
            if self._closed:
                return
            data = pcm_bytes
            if self._rate != AUDIO_TX_PCM_SAMPLE_RATE:
                data = _resample_pcm16_mono(pcm_bytes, AUDIO_TX_PCM_SAMPLE_RATE, self._rate)
            self._write_locked(data)

    def set_tx_active(self, active: bool):
        """Only meaningful for source=="both" (RX+TX mono) -- flips
        which source's chunks land in the track. Both RX and TX arrive
        already real-time-paced (RX is a continuous stream regardless
        of squelch; TX is a continuous stream of fixed-size blocks for
        the duration of PTT), so simply switching which one gets
        written, right as PTT toggles, produces a chronologically
        correct single track with no overlap and no silence gaps to
        pad -- no separate timestamp bookkeeping needed."""
        with self._lock:
            self._tx_in_progress = active

    def _write_locked(self, data):
        if not data:
            return
        self._wav.writeframes(data)
        self._frames_written += len(data) // (AUDIO_SAMPLE_WIDTH * self._channels)

    def close(self) -> float:
        """Closes the wave file and returns the final duration in
        seconds, computed from frames actually written (not wall-clock
        time since start(), so it's exact even if a caller closes late
        for some reason)."""
        with self._lock:
            if self._closed:
                return 0.0
            self._closed = True
            duration = self._frames_written / self._rate if self._rate else 0.0
            self._wav.close()
        return duration


def export_recording(src_wav_path, dest_path):
    """Copies (WAV) or transcodes (anything else, via system ffmpeg)
    src_wav_path to dest_path. Raises RuntimeError with a clear message
    if ffmpeg isn't on PATH or the transcode fails -- callers should
    catch this and show it, not let it propagate as a raw traceback."""
    dest_path = pathlib.Path(dest_path)
    if dest_path.suffix.lower() == ".wav":
        shutil.copyfile(src_wav_path, dest_path)
        return
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH -- install ffmpeg to export to formats other than WAV."
        )
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src_wav_path), str(dest_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg export failed: {result.stderr.strip()[-500:]}")

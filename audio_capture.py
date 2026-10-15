"""Audio capture: list devices, find the VB-Cable loopback, capture to 16 kHz mono blocks."""

import queue
import threading
import time

import numpy as np

TARGET_RATE = 16000


def list_input_devices() -> list[dict]:
    """All recording (input) endpoints visible to PortAudio."""
    import sounddevice as sd

    try:
        query = sd.query_devices()
    except Exception as e:
        print(f"[audio] Could not query devices: {e}")
        return []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1

    devices = []
    for i, d in enumerate(query):
        if d["max_input_channels"] > 0:
            devices.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "default_rate": int(d["default_samplerate"]),
                "hostapi": sd.query_hostapis(d["hostapi"])["name"],
                "is_default": i == default_in,
            })
    return devices


def find_device(name_substring: str) -> int | None:
    """Case-insensitive substring match against device names; first match wins."""
    sub = name_substring.lower()
    for d in list_input_devices():
        if sub in d["name"].lower():
            return d["index"]
    return None


class AudioCapture(threading.Thread):
    """Captures a playback-cable device, downmixes to mono, resamples to 16 kHz,
    and pushes variable-size float32 blocks to a queue (consumers pull via read())."""

    def __init__(self, device_spec: str | int, out_q: "queue.Queue[np.ndarray]",
                 capture_rate: int = 0):
        super().__init__(daemon=True, name="AudioCapture")
        self.device_spec = device_spec
        self.out_q = out_q
        self.capture_rate = capture_rate
        self.stop_event = threading.Event()
        self._raw_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
        self._resampler = None
        self.device_name = str(device_spec)

    def _open_stream(self):
        import sounddevice as sd
        import soxr

        if isinstance(self.device_spec, int):
            index, name = self.device_spec, sd.query_devices(self.device_spec)["name"]
        else:
            index = find_device(self.device_spec)
            if index is None:
                raise RuntimeError(
                    f'No input device matching "{self.device_spec}". '
                    "Run: python main.py --list-devices")
            name = sd.query_devices(index)["name"]
        self.device_name = name

        info = sd.query_devices(index)
        rate = self.capture_rate or int(info["default_samplerate"])
        channels = min(2, int(info["max_input_channels"]))
        self._resampler = soxr.ResampleStream(rate, TARGET_RATE, 1, dtype="float32")

        stream = sd.InputStream(
            device=index, samplerate=rate, channels=channels,
            dtype="float32", blocksize=0, callback=self._callback)
        return stream, name, rate, channels

    def _callback(self, indata, frames, time_info, status):
        if status:
            pass  # occasional overflow is fine; we drop nothing, PortAudio flags it
        try:
            self._raw_q.put_nowait(indata[:, :].copy())
        except queue.Full:
            pass  # consumer stalled; drop oldest-ish behavior: drop this block

    def run(self):
        import sounddevice as sd

        while not self.stop_event.is_set():
            try:
                stream, name, rate, channels = self._open_stream()
            except Exception as e:
                print(f"[audio] Cannot open device ({e}); retrying in 2s")
                if self.stop_event.wait(2.0):
                    return
                continue

            print(f"[audio] Capturing '{name}' @ {rate} Hz x{channels}ch")
            try:
                with stream:
                    while not self.stop_event.is_set():
                        try:
                            block = self._raw_q.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        mono = block.mean(axis=1, dtype=np.float32)
                        out = self._resampler.resample_chunk(mono)
                        if len(out):
                            try:
                                self.out_q.put_nowait(np.asarray(out, dtype=np.float32))
                            except queue.Full:
                                pass
            except Exception as e:
                print(f"[audio] Capture error ({e}); reopening in 2s")
                if self.stop_event.wait(2.0):
                    return

    def stop(self):
        self.stop_event.set()

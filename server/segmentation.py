"""Voice activity segmentation: Silero VAD (ONNX) + an utterance state machine.

Consumes arbitrary-size 16 kHz mono float32 blocks and emits complete
utterances (numpy arrays) once silence closes them (or a max-length cap hits).
"""

import threading

import numpy as np

try:
    import onnxruntime as ort
except ImportError as e:
    raise ImportError("onnxruntime is required (installed with faster-whisper)") from e

WINDOW = 512       # raw samples per VAD frame (32 ms @16 kHz)
CONTEXT = 64       # samples of previous audio prepended to each frame
MODEL_INPUT = WINDOW + CONTEXT  # silero v6 frames: [1, 576]


class SileroVAD:
    """Streaming wrapper around the Silero VAD ONNX model bundled with
    faster-whisper (silero_vad_v6.onnx: inputs input/h/c, chunk = 512 + 64 context)."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self._model = get_vad_model()  # lru_cached instance with a .session
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def prob(self, chunk: np.ndarray) -> float:
        """chunk: float32 [512] @16 kHz. Returns speech probability."""
        x = np.concatenate([self._context, chunk.astype(np.float32)]).reshape(1, MODEL_INPUT)
        out, self._h, self._c = self._model.session.run(
            None, {"input": x, "h": self._h, "c": self._c})
        self._context = chunk[-CONTEXT:].astype(np.float32)
        return float(np.asarray(out).ravel()[0])


class VadSegmenter(threading.Thread):
    """Accumulates 16 kHz blocks, runs the VAD, emits closed utterances.

    While speech is ongoing, a *partial* segment (everything said so far) is
    emitted every `partial_interval_s` so subtitles can appear mid-sentence;
    the *final* segment closes the utterance on silence or the length cap.
    Items are dicts: {"audio", "uid", "partial"} (plus {"eof": True} at end of
    source in file mode)."""

    def __init__(self, audio_q, seg_q, vad_cfg: dict, paused_event: threading.Event):
        super().__init__(daemon=True, name="VadSegmenter")
        self.audio_q = audio_q
        self.seg_q = seg_q
        self.cfg = vad_cfg
        self.paused = paused_event
        self.stop_event = threading.Event()
        self.source_done = threading.Event()  # set by file mode: flush when queue drains
        self._buf = np.zeros(0, dtype=np.float32)
        self._vad = None
        self._uid = 0
        self._reset_segment_state()

    def _reset_segment_state(self):
        self._prebuf: list[np.ndarray] = []
        self._cur: list[np.ndarray] = []
        self._speech_frames = 0
        self._silence_frames = 0
        self._speaking = False
        self._since_partial = 0

    def run(self):
        self._vad = SileroVAD()
        cfg = self.cfg

        # thresholds are re-read every iteration so a live profile switch
        # (chat <-> game) takes effect on the next audio block
        while not self.stop_event.is_set():
            on, off = cfg["on_threshold"], cfg["off_threshold"]
            min_speech = max(1, int(cfg["min_speech_ms"] / 1000 * TARGET_FPS))
            min_silence = max(1, int(cfg["min_silence_ms"] / 1000 * TARGET_FPS))
            max_frames = int(cfg["max_utterance_s"] * TARGET_FPS)
            pad_frames = int(cfg["padding_ms"] / 1000 * TARGET_FPS)
            partial_min = max(1, int(cfg.get("partial_min_s", 1.2) * TARGET_FPS))
            partial_every = max(1, int(cfg.get("partial_interval_s", 2.0) * TARGET_FPS))
            try:
                block = self.audio_q.get(timeout=0.25)
            except Exception:
                if self.source_done.is_set() and self.audio_q.empty():
                    self._flush(pad_frames)
                    return
                continue

            if self.paused.is_set():
                self._vad.reset()
                self._reset_segment_state()
                self._drain(self.audio_q)
                continue

            self._buf = np.concatenate([self._buf, block]) if len(self._buf) else block
            n = len(self._buf) // WINDOW
            for i in range(n):
                win = self._buf[i * WINDOW:(i + 1) * WINDOW]
                p = self._vad.prob(win)
                self._step(win, p, on, off, min_speech, min_silence, max_frames,
                           pad_frames, partial_min, partial_every)
            self._buf = self._buf[n * WINDOW:]

    def _step(self, win, p, on, off, min_speech, min_silence, max_frames, pad_frames,
              partial_min, partial_every):
        if p >= on:
            self._silence_frames = 0
            self._cur.append(win)
            if not self._speaking:
                self._speech_frames += 1
                if self._speech_frames >= min_speech:
                    self._speaking = True
            else:
                self._speech_frames += 1
            if len(self._cur) >= max_frames:
                # constant talker: cap the utterance mid-speech and start a new one
                self._emit(pad_frames)
                return
            # streaming: publish a partial while the person keeps talking
            if self._speaking and len(self._cur) >= partial_min:
                self._since_partial += 1
                if self._since_partial >= partial_every:
                    self._emit_partial()
        else:
            if self._speaking:
                self._silence_frames += 1
                self._cur.append(win)
                if (self._silence_frames >= min_silence
                        or len(self._cur) >= max_frames):
                    self._emit(pad_frames)
            else:
                if self._speech_frames > 0:
                    # tentative blip that faded out: discard
                    self._speech_frames = 0
                    self._cur = []
                self._prebuf.append(win)
                if len(self._prebuf) > pad_frames:
                    self._prebuf.pop(0)

    def _audio_so_far(self) -> np.ndarray:
        frames = list(self._prebuf) + list(self._cur)
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)

    def _emit_partial(self):
        self._since_partial = 0
        audio = self._audio_so_far()
        if len(audio) < WINDOW * 5:
            return
        try:
            self.seg_q.put_nowait({"audio": audio, "uid": self._uid, "partial": True})
        except Exception:
            pass  # worker backed up: skip this partial

    def _emit(self, pad_frames):
        tail_trim = max(0, self._silence_frames - pad_frames)
        frames = list(self._prebuf) + self._cur
        if tail_trim:
            frames = frames[:-tail_trim] if tail_trim < len(frames) else []
        audio = (np.concatenate(frames) if frames
                 else np.zeros(0, dtype=np.float32))
        uid = self._uid
        self._uid += 1
        self._vad.reset()
        self._reset_segment_state()
        if len(audio) >= WINDOW * 5:  # <160 ms of audio is never speech worth decoding
            try:
                self.seg_q.put_nowait({"audio": audio, "uid": uid, "partial": False})
            except Exception:
                pass  # worker backed up: drop this utterance

    def _flush(self, pad_frames):
        """File mode: emit whatever is pending, then an EOF sentinel."""
        if self._cur:
            self._silence_frames = 0
            self._emit(pad_frames)
        try:
            self.seg_q.put_nowait({"eof": True})
        except Exception:
            pass

    @staticmethod
    def _drain(q):
        while True:
            try:
                q.get_nowait()
            except Exception:
                return

    def stop(self):
        self.stop_event.set()


TARGET_FPS = 1000 / 32  # VAD frames per second (32 ms windows) — used above

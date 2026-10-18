"""Per-speaker sessions: one VadSegmenter per Discord user.

The sink (the /audio WS handler via ShipperSink) pushes 16 kHz mono blocks
keyed by Discord user; each user gets their own audio queue + VAD segmenter
thread, and every segment they emit is tagged with the speaker before landing
in the shared seg_q the PipelineWorker drains.
"""

import queue
import threading

import numpy as np

from segmentation import VadSegmenter

TARGET_RATE = 16000


class _TaggedSegQueue(queue.Queue):
    """Presents a queue-like target to VadSegmenter (which only uses
    put_nowait) and forwards every emitted segment into the shared seg_q with
    the speaker's id and name attached."""

    def __init__(self, shared_q: queue.Queue, speaker_id, speaker_name: str):
        super().__init__()
        self.shared_q = shared_q
        self.speaker_id = speaker_id
        self.speaker_name = speaker_name

    def put_nowait(self, item, block=True, timeout=None):  # segmenter call shape
        item["speaker_id"] = self.speaker_id
        item["speaker"] = self.speaker_name
        try:
            self.shared_q.put_nowait(item)
        except queue.Full:
            pass  # worker backed up: drop this segment (partials recover)

    def put(self, item, block=True, timeout=None):
        self.put_nowait(item)

    def empty(self):
        return self.shared_q.empty()


class SpeakerSession:
    def __init__(self, user_id, name: str, audio_q: queue.Queue,
                 segmenter: VadSegmenter):
        self.user_id = user_id
        self.name = name
        self.audio_q = audio_q
        self.segmenter = segmenter


class SpeakerManager:
    """Owns one session per voice-channel member that has spoken."""

    def __init__(self, cfg: dict, seg_q: queue.Queue, paused_event: threading.Event):
        self.cfg = cfg
        self.seg_q = seg_q
        self.paused = paused_event
        self._lock = threading.Lock()
        self._sessions: dict[object, SpeakerSession] = {}
        self._counter = 0  # synthetic ids for fake speakers (test mode)

    @staticmethod
    def _display_label(name: str) -> str:
        """batch:<group> sessions transcribe UNLABELED: the group's members
        are mixed into one stream, so no individual speaker can be
        attributed. The full key stays on the session for identity (status,
        per-group language pins)."""
        return "" if name.startswith("batch:") else name

    def ensure(self, user_id, name: str) -> queue.Queue:
        """Get (creating if needed) the audio queue a speaker's blocks go to."""
        with self._lock:
            s = self._sessions.get(user_id)
            if s is None:
                audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
                seg_target = _TaggedSegQueue(self.seg_q, user_id,
                                             self._display_label(name))
                segmenter = VadSegmenter(audio_q, seg_target, self.cfg["vad"],
                                         self.paused)
                segmenter.start()
                s = SpeakerSession(user_id, name, audio_q, segmenter)
                self._sessions[user_id] = s
                print(f"[speakers] Session started: {name}")
            elif s.name != name:
                s.name = name
                s.segmenter.seg_q.speaker_name = self._display_label(name)
            return s.audio_q

    def push(self, user_id, name: str, block: np.ndarray):
        q = self.ensure(user_id, name)
        if q.full():
            try:
                q.get_nowait()  # this speaker's VAD stalled: drop the OLDEST
                # block so live audio keeps flowing (drop-newest would mute
                # the present while replaying a stale buffer)
            except queue.Empty:
                pass
        try:
            q.put_nowait(block)
        except queue.Full:
            pass

    def remove(self, user_id, flush: bool = True):
        """Stop a speaker's segmenter. flush=True (test mode / leaving) waits
        for the VAD to emit its pending tail; live speech never 'ends' but a
        leaving member's partial utterance is worth closing out."""
        with self._lock:
            s = self._sessions.pop(user_id, None)
        if s is None:
            return
        if flush:
            s.segmenter.source_done.set()
            s.segmenter.join(timeout=5.0)
            if s.segmenter.is_alive():
                s.segmenter.stop()
        else:
            s.segmenter.stop()
        print(f"[speakers] Session ended: {s.name}")

    def fake_speaker(self, name: str):
        """Test mode: a speaker that never came from Discord."""
        self._counter += 1
        return (f"fake-{self._counter}", self.ensure(f"fake-{self._counter}", name))

    def names(self) -> list[str]:
        with self._lock:
            return sorted({s.name for s in self._sessions.values()})

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def stop_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.segmenter.stop()

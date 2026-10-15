"""Dev demo: play OWCS audio at real-time pace through the pipeline into the
live overlay, screenshotting the screen so you can see the subtitles as a
viewer would. Not part of the app itself.

Usage: venv/Scripts/python demo_subtitles.py [audio_or_video] [seconds] [--open-video]
"""

import os
import queue as _queue
import sys
import threading
import time

sys.path.insert(0, ".")

from main import PipelineWorker, add_cuda_dll_dirs, fmt_result  # noqa: E402
add_cuda_dll_dirs()

import av  # noqa: E402
import numpy as np  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from config import load_config  # noqa: E402
from overlay import OverlayService  # noqa: E402
from segmentation import VadSegmenter  # noqa: E402
from transcriber import Transcriber  # noqa: E402
from translator import NllbTranslator  # noqa: E402

AUDIO = sys.argv[1] if len(sys.argv) > 1 else "owcs_jjonak_slice.wav"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else 100.0
OPEN_VIDEO = "--open-video" in sys.argv
TARGET_RATE = 16000


def load_audio(path, max_s):
    r = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
    chunks = []
    with av.open(path) as c:
        for frame in c.decode(audio=0):
            for rf in r.resample(frame):
                a = rf.to_ndarray()
                if a.ndim > 1:
                    a = a.mean(axis=0)
                chunks.append(a.astype(np.float32) / 32768.0)
            if sum(len(x) for x in chunks) > max_s * TARGET_RATE:
                break
    return np.concatenate(chunks)[: int(max_s * TARGET_RATE)]


def main():
    cfg = load_config({"language": "ko"})
    audio = load_audio(AUDIO, SECONDS)
    print(f"[demo] {len(audio) / TARGET_RATE:.0f}s of audio, real-time playback")

    from main import pick_device_and_compute
    device, compute = pick_device_and_compute(cfg)

    audio_q, seg_q, result_q = _queue.Queue(), _queue.Queue(), _queue.Queue()
    paused = threading.Event()

    worker = PipelineWorker(cfg, seg_q, result_q, paused)
    worker.transcriber = Transcriber(cfg["whisper_model"], device, compute,
                                     cpu_threads=int(cfg["cpu_threads"]),
                                     beam_size=int(cfg["beam_size"]))
    worker.translator = NllbTranslator(
        cfg["nllb_ct2_repo"], cfg["nllb_tokenizer_repo"],
        device, compute, cfg["target_lang"])
    worker.start()

    segmenter = VadSegmenter(audio_q, seg_q, cfg["vad"], paused)
    segmenter.start()

    app = QApplication(sys.argv)
    service = OverlayService(result_q, cfg, paused,
                             on_result=lambda r: print(fmt_result(r)))

    def feed():
        if OPEN_VIDEO:
            print("[demo] opening video in default player — subtitles start now")
            os.startfile(os.path.abspath(AUDIO))
        block = TARGET_RATE  # one second per push, real-time pace
        for i in range(0, len(audio), block):
            audio_q.put(audio[i:i + block])
            time.sleep(1.0)
        segmenter.source_done.set()

    threading.Thread(target=feed, daemon=True).start()

    def snap(t):
        QApplication.primaryScreen().grabWindow(0).save(f"subtitles_{t}s.png")
        service.win.grab().save(f"overlay_{t}s.png")
        print(f"[demo] screenshot at {t}s, overlay geometry: {service.win.geometry()}")

    for t in (35, 60, 85):
        QTimer.singleShot(int(t * 1000), lambda t=t: snap(t))
    QTimer.singleShot(int((SECONDS + 5) * 1000), app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

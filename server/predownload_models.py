r"""Pre-download both models so first start is instant.

    python predownload_models.py                 # download from Hugging Face
    python predownload_models.py --copy-from "D:\Translation Discord"
                                                # copy from the desktop app
"""

import argparse
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from config import load_config  # noqa: E402

OLD_DEFAULT = Path(r"D:\Translation Discord")


def dir_size(p: Path) -> str:
    n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return f"{n / 1e9:.1f} GB" if n > 0.5e9 else f"{n / 1e6:.0f} MB"


def copy_models(src: Path):
    dst = APP_DIR / "models"
    src = src / "models" if (src / "models").exists() else src
    if not src.exists():
        sys.exit(f"nothing to copy at {src}")
    print(f"[models] Copying {src} -> {dst} ({dir_size(src)})…")
    shutil.copytree(src, dst, dirs_exist_ok=True)
    print(f"[models] Done ({dir_size(dst)}).")


def download_models():
    from main import add_cuda_dll_dirs
    add_cuda_dll_dirs()
    cfg = load_config()
    print(f"[models] Downloading Whisper '{cfg['whisper_model']}' "
          f"(~2 GB, first run only)…")
    from transcriber import Transcriber
    t = Transcriber(cfg["whisper_model"], "cpu", "int8", cpu_threads=2)
    del t
    print(f"[models] Downloading NLLB '{cfg['nllb_ct2_repo']}' (~1.4 GB)…")
    from translator import download_nllb_ct2
    download_nllb_ct2(cfg["nllb_ct2_repo"])
    print(f"[models] Done ({dir_size(APP_DIR / 'models')}).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy-from", metavar="DIR",
                    help=f"copy models/ from another install (default "
                         f"{OLD_DEFAULT} if it exists)")
    args = ap.parse_args()
    if args.copy_from:
        copy_models(Path(args.copy_from))
    else:
        download_models()

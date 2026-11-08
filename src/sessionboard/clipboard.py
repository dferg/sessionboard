"""Put text on the clipboard with whatever this machine has; report how."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

_CANDIDATES = (
    ("pbcopy", ["pbcopy"]),
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
    ("clip.exe", ["clip.exe"]),
)


def copy_local(text: str) -> str | None:
    """Try the native clipboard tools; return the tool name used or None."""
    for name, cmd in _CANDIDATES:
        if name == "pbcopy" and platform.system() != "Darwin":
            continue
        if name in ("xclip", "xsel") and not os.environ.get("DISPLAY"):
            continue
        if name == "wl-copy" and not os.environ.get("WAYLAND_DISPLAY"):
            continue
        if not shutil.which(name):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return name
        except Exception:
            continue
    return None


def over_ssh() -> bool:
    return bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))

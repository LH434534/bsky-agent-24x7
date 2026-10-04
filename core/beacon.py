"""Heartbeat beacon — proves the worker is alive *and* making progress."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class Beacon:
    """Writes pid + timestamp + tick counter every `every` seconds.

    A watchdog distinguishes three states:
      fresh heartbeat advancing  → healthy
      heartbeat present, stale   → hung (event loop blocked, deadlock, stuck socket)
      heartbeat gone entirely    → process died
    """

    def __init__(self, name: str = "worker", every: float = 15.0,
                 path: Path | None = None):
        self.name = name
        self.every = every
        self.path = Path(path or DATA / f"hb_{name}.json")
        self.tick = 0
        self._t: threading.Thread | None = None
        self._stop = threading.Event()

    def bump(self, meta: dict | None = None) -> None:
        self.tick += 1
        try:
            self.path.write_text(json.dumps({
                "pid": os.getpid(), "ts": time.time(), "tick": self.tick,
                "ppid": os.getppid(), **(meta or {})}))
        except Exception:
            pass

    def start(self) -> "Beacon":
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.bump()
            time.sleep(self.every)

    def stop(self) -> None:
        self._stop.set()


def read(name: str) -> dict | None:
    p = DATA / f"hb_{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def age(name: str) -> float:
    d = read(name)
    return 1e9 if not d else time.time() - float(d.get("ts", 0))


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_of(name: str) -> int:
    d = read(name)
    return int(d.get("pid", 0)) if d else 0

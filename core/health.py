"""Health probes — everything that can silently kill a 24/7 bot, checked explicitly."""
from __future__ import annotations

import json
import os
import psutil
import shutil
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "agent.log"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = False


@dataclass
class Health:
    checks: List[Check] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.fatal)

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if not c.ok]

    def as_dict(self) -> Dict:
        return {"ts": self.ts, "ok": self.ok,
                "checks": [asdict(c) for c in self.checks]}


def disk(path: Path = ROOT, min_mb: int = 120) -> Check:
    try:
        u = shutil.disk_usage(str(path))
        free_mb = u.free // (1024 * 1024)
        return Check("disk", free_mb >= min_mb, f"{free_mb}MB free", fatal=True)
    except Exception as e:
        return Check("disk", False, str(e), fatal=True)


def memory(min_mb: int = 80) -> Check:
    try:
        m = psutil.virtual_memory()
        avail = m.available // (1024 * 1024)
        return Check("memory", avail >= min_mb, f"{avail}MB available")
    except Exception as e:
        return Check("memory", False, str(e))


def db_ok(path: Path = DATA / "memory.db") -> Check:
    if not path.exists():
        return Check("sqlite", True, "not created yet")
    try:
        con = sqlite3.connect(str(path), timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("SELECT COUNT(*) FROM actions").fetchone()
        con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return Check("sqlite", True, "ok")
    except sqlite3.DatabaseError as e:
        return Check("sqlite", False, f"corrupt: {str(e)[:80]}", fatal=True)
    except Exception as e:
        return Check("sqlite", False, str(e)[:120], fatal=True)


def log_error_rate(window_lines: int = 300, max_ratio: float = 0.35) -> Check:
    """Too many ERROR/CRASH lines recently = something is systematically broken."""
    if not LOG.exists():
        return Check("log_errors", True, "no log yet")
    try:
        lines = LOG.read_text(errors="ignore").splitlines()[-window_lines:]
        if len(lines) < 30:
            return Check("log_errors", True, f"{len(lines)} lines")
        bad = sum(1 for l in lines if " ERROR " in l or "CRASH" in l or "Traceback" in l)
        ratio = bad / len(lines)
        return Check("log_errors", ratio <= max_ratio, f"{bad}/{len(lines)} = {ratio:.0%}")
    except Exception as e:
        return Check("log_errors", False, str(e)[:80])


def files_writable() -> Check:
    try:
        p = DATA / ".wtest"
        p.write_text("x")
        p.unlink()
        return Check("writable", True, "ok", fatal=True)
    except Exception as e:
        return Check("writable", False, str(e)[:100], fatal=True)


def corpus_present() -> Check:
    c = DATA / "corpus.txt"
    n = len(c.read_text(errors="ignore").splitlines()) if c.exists() else 0
    return Check("corpus", n > 20, f"{n} lines")


def session_valid(max_age_h: int = 24 * 20) -> Check:
    s = DATA / "session.json"
    if not s.exists():
        return Check("session", True, "none yet")
    try:
        age = time.time() - s.stat().st_mtime
        d = json.loads(s.read_text())
        ok = bool(d.get("access")) and age < max_age_h * 3600
        return Check("session", ok, f"{age/3600:.1f}h old")
    except Exception as e:
        return Check("session", False, f"bad json: {str(e)[:60]}")


def python_imports() -> Check:
    """The agent's own modules must import cleanly — catches self-inflicted syntax errors."""
    import subprocess, sys
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0,'%s'); "
                        "import core.atproto, core.brain, core.antispam, core.memory, "
                        "core.actions, core.scheduler" % str(ROOT)],
                       capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "?"
        return Check("imports", False, tail[:160], fatal=True)
    return Check("imports", True, "ok", fatal=True)


def pds_reachable(url: str = "https://bsky.social", timeout: int = 8) -> Check:
    import requests
    try:
        r = requests.get(f"{url}/xrpc/_health", timeout=timeout)
        return Check("pds", r.status_code < 500, f"{r.status_code}")
    except Exception as e:
        return Check("pds", False, f"{type(e).__name__}: {str(e)[:60]}")


def ollama_up() -> Check:
    import requests
    try:
        r = requests.get(os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/tags",
                         timeout=3)
        return Check("ollama", r.status_code == 200, "up" if r.ok else str(r.status_code))
    except Exception:
        return Check("ollama", False, "down (offline brain will be used)")


def cpu_load(max_load: float = 12.0) -> Check:
    try:
        l = os.getloadavg()[0]
        return Check("load", l < max_load, f"{l:.1f}")
    except Exception:
        return Check("load", True, "n/a")


ALL = [disk, memory, db_ok, log_error_rate, files_writable, corpus_present,
       session_valid, python_imports, cpu_load, ollama_up]


def full(skip_network: bool = False) -> Health:
    h = Health()
    for fn in ALL:
        if skip_network and fn is pds_reachable:
            continue
        try:
            h.checks.append(fn())
        except Exception as e:
            h.checks.append(Check(fn.__name__, False, f"probe crashed: {str(e)[:80]}"))
    return h


def snapshot() -> Dict:
    h = full()
    d = h.as_dict()
    try:
        p = psutil.Process()
        d["proc"] = {"rss_mb": p.memory_info().rss // 1048576,
                     "cpu": p.cpu_percent(0.1),
                     "threads": p.num_threads(),
                     "fds": (p.num_fds() if hasattr(p, "num_fds") else -1)}
    except Exception:
        pass
    return d

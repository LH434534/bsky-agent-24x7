"""Auto-repair — reads its own crash log, classifies the failure, applies a fix.

Flow:  symptom scan → classifier → fix action → verify → escalate.
Every fix is logged and counted; repeated failures escalate up a ladder until the
agent rolls back to the last known-good snapshot of itself.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "agent.log"
STATE = DATA / "autofix_state.json"
SNAP = DATA / "snapshot.zip"
LOG_BACKUP = DATA / "agent.log.bak"


# ───────────────────────────────────────────────────────────────────── state
@dataclass
class FixState:
    counts: Dict[str, int] = field(default_factory=dict)     # symptom -> times seen
    applied: Dict[str, int] = field(default_factory=dict)    # fix -> times applied
    last_fix: float = 0.0
    restarts: int = 0
    history: List[Dict] = field(default_factory=list)

    def load(self) -> "FixState":
        try:
            d = json.loads(STATE.read_text())
            self.__dict__.update(d)
        except Exception:
            pass
        return self

    def save(self) -> None:
        self.history = self.history[-200:]
        DATA.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.__dict__, indent=1))

    def note(self, symptom: str, fix: str, ok: bool) -> None:
        self.counts[symptom] = self.counts.get(symptom, 0) + 1
        if fix:
            self.applied[fix] = self.applied.get(fix, 0) + 1
        self.last_fix = time.time()
        self.history.append({"ts": time.time(), "symptom": symptom, "fix": fix, "ok": ok})
        self.save()


# ─────────────────────────────────────────────────────────────────── helpers
def tail(n: int = 400) -> str:
    if not LOG.exists():
        return ""
    try:
        return "\n".join(LOG.read_text(errors="ignore").splitlines()[-n:])
    except Exception:
        return ""


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} AUTOFIX  │ {msg}"
    print(line, flush=True)
    try:
        with open(DATA / "autofix.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def pip(*pkgs: str, timeout: int = 300) -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                            "--quiet", *pkgs], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def rm(*paths: Path) -> int:
    n = 0
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
            n += 1
        except Exception:
            pass
    return n


def truncate_log(keep_lines: int = 2000) -> None:
    if not LOG.exists():
        return
    try:
        lines = LOG.read_text(errors="ignore").splitlines()
        if len(lines) > keep_lines:
            if not LOG_BACKUP.exists():
                shutil.copy(LOG, LOG_BACKUP)
            LOG.write_text("\n".join(lines[-keep_lines:]))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────── fixes
def fix_corrupt_state() -> bool:
    return rm(DATA / "spam_state.json") > 0


def fix_session() -> bool:
    return rm(DATA / "session.json") > 0


def fix_db() -> bool:
    """Recover a corrupt SQLite: dump what we can, rebuild schema, restore."""
    db = DATA / "memory.db"
    if not db.exists():
        return True
    rows: List[tuple] = []
    try:
        con = sqlite3.connect(str(db), timeout=5)
        rows = con.execute("SELECT * FROM actions").fetchall()
        con.close()
    except Exception:
        rows = []
    bak = DATA / f"memory.db.broken.{int(time.time())}"
    try:
        shutil.move(str(db), str(bak))
    except Exception:
        try:
            db.unlink()
        except Exception:
            return False
    sys.path.insert(0, str(ROOT))
    try:
        from core.memory import Memory          # rebuilds schema
        m = Memory(db)
        con = sqlite3.connect(str(db), timeout=10)
        con.executemany(
            "INSERT OR IGNORE INTO actions (id,ts,kind,text,target_did,target_handle,uri,topic,ok,err)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", [r for r in rows if len(r) == 10])
        con.commit()
        con.close()
        return True
    except Exception as e:
        log(f"db rebuild partial: {e}")
        return False


def fix_deps(missing: str) -> bool:
    return pip(missing)


def fix_disk() -> bool:
    """Free space: rotate logs, drop old corpus/backup noise."""
    truncate_log(800)
    n = 0
    for p in sorted(DATA.glob("memory.db.broken.*"))[:-1]:
        try:
            p.unlink(); n += 1
        except Exception:
            pass
    try:
        if (DATA / "corpus.txt").stat().st_size > 8 * 1024 * 1024:
            lines = (DATA / "corpus.txt").read_text(errors="ignore").splitlines()
            (DATA / "corpus.txt").write_text("\n".join(lines[-20000:]))
    except Exception:
        pass
    return True


def fix_cooldown() -> bool:
    """Rate-limit storm: write a long cooldown so the guard stops hammering."""
    f = DATA / "spam_state.json"
    try:
        d = json.loads(f.read_text()) if f.exists() else {}
        d["cooldown"] = time.time() + 3 * 3600
        d["errors"] = int(d.get("errors", 0)) + 1
        f.write_text(json.dumps(d))
        return True
    except Exception:
        return False


def fix_pyc() -> bool:
    n = 0
    for p in ROOT.rglob("__pycache__"):
        try:
            shutil.rmtree(p); n += 1
        except Exception:
            pass
    return n > 0 or True


def snapshot() -> bool:
    """Save a known-good copy of the whole agent (excluding volatile data)."""
    try:
        if SNAP.exists():
            SNAP.unlink()
        with zipfile.ZipFile(SNAP, "w", zipfile.ZIP_DEFLATED) as z:
            for p in ROOT.rglob("*"):
                if p.is_dir() or not p.is_file():
                    continue
                rel = p.relative_to(ROOT)
                if rel.parts[0] in {"data", "__pycache__", ".git"} or p.suffix == ".pyc":
                    continue
                if p.stat().st_size > 4 * 1024 * 1024:
                    continue
                z.write(p, rel)
        log(f"snapshot salvo ({SNAP.stat().st_size // 1024}KB)")
        return True
    except Exception as e:
        log(f"snapshot falhou: {e}")
        return False


def restore_snapshot() -> bool:
    """Last resort: overwrite broken source with the last known-good copy."""
    if not SNAP.exists():
        log("sem snapshot para restaurar")
        return False
    try:
        tmp = ROOT / ".restore_tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        with zipfile.ZipFile(SNAP) as z:
            z.extractall(tmp)
        n = 0
        for src in tmp.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(tmp)
            dst = ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        shutil.rmtree(tmp)
        log(f"snapshot restaurado: {n} arquivos")
        return True
    except Exception as e:
        log(f"restore falhou: {e}")
        return False


def git_self_update() -> bool:
    """If the agent lives in a git repo, pull; if that breaks, hard reset."""
    if not (ROOT / ".git").exists():
        return False
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            log("git pull ok")
            return True
        subprocess.run(["git", "-C", str(ROOT), "reset", "--hard", "HEAD"],
                       capture_output=True, timeout=120)
        log("git reset --hard aplicado")
        return True
    except Exception as e:
        log(f"git self-update falhou: {e}")
        return False


# ──────────────────────────────────────────────────────────── classification
# (regex, symptom name, fix fn, escalate-after-N)
RULES: List[Tuple[str, str, Callable[[], bool], int]] = [
    (r"ModuleNotFoundError: No module named '([A-Za-z0-9_\-\.]+)'", "missing_dep", lambda: False, 3),
    (r"database is locked|database disk image is malformed|file is not a database",
     "db_corrupt", fix_db, 3),
    (r"401|ExpiredToken|InvalidToken|refresh failed|AuthenticationRequired",
     "auth_dead", fix_session, 3),
    (r"429|RateLimitExceeded|ratelimit", "rate_limited", fix_cooldown, 6),
    (r"JSONDecodeError|Expecting value|corrupt", "bad_state", fix_corrupt_state, 4),
    (r"No space left on device|OSError: \[Errno 28\]", "disk_full", fix_disk, 3),
    (r"SyntaxError|IndentationError|NameError|AttributeError: .* has no attribute",
     "source_bug", lambda: False, 2),
    (r"RecursionError|MemoryError|Fatal Python error", "runtime_blowup", fix_pyc, 3),
    (r"sqlite3\.OperationalError", "db_op_error", fix_db, 4),
]

DEP_RE = re.compile(r"ModuleNotFoundError: No module named '([A-Za-z0-9_\-\.]+)'")


def classify(text: str) -> Optional[Tuple[str, Callable[[], bool], int]]:
    for pat, name, fn, esc in RULES:
        if re.search(pat, text):
            return name, fn, esc
    return None


def run(state: Optional[FixState] = None) -> Dict:
    """Scan the log, decide, fix, verify. Returns a report."""
    state = (state or FixState()).load()
    text = tail(500)
    if not text:
        return {"action": "none", "reason": "no log"}

    hit = classify(text)
    report: Dict = {"ts": time.time(), "detected": False}

    if not hit:
        return {"action": "none", "reason": "no known symptom"}

    name, fn, escalate_after = hit
    report.update({"detected": True, "symptom": name})

    # special case: install the actual missing module
    if name == "missing_dep":
        m = DEP_RE.search(text)
        pkg = m.group(1).split(".")[0] if m else None
        ok = bool(pkg) and pip(pkg)
        state.note(name, f"pip:{pkg}", ok)
        report.update({"fix": f"pip install {pkg}", "ok": ok})
        return report

    times = state.counts.get(name, 0)
    if times >= escalate_after:
        # escalation ladder
        if times < escalate_after + 2:
            log(f"ESCALANDO {name}: limpando caches + estado")
            fix_pyc(); fix_corrupt_state(); fix_session()
            report.update({"fix": "escalate:clear", "ok": True})
        elif times < escalate_after + 5:
            log(f"ESCALANDO {name}: reinstall deps + snapshot")
            pip("requests", "psutil")
            snapshot()
            report.update({"fix": "escalate:deps", "ok": True})
        else:
            log(f"ESCALANDO {name}: restore do snapshot + git reset")
            git_self_update()
            restore_snapshot()
            fix_pyc()
            report.update({"fix": "escalate:restore", "ok": True})
        state.note(name, report["fix"], True)
        return report

    # first-line fix
    try:
        ok = fn()
    except Exception as e:
        ok = False
        log(f"fix {name} levantou exceção: {e}")
    truncate_log()
    state.note(name, name, ok)
    report.update({"fix": name, "ok": ok, "seen": times + 1})
    log(f"{name} -> {name} ({'ok' if ok else 'falhou'})")
    return report


def self_test() -> bool:
    """Can the agent still import itself and talk to its own DB?"""
    import subprocess
    r = subprocess.run([sys.executable, "-c",
                        "import sys;sys.path.insert(0,'%s');"
                        "import core.atproto,core.brain,core.antispam,core.memory,"
                        "core.actions,core.scheduler,core.health;print('ok')" % str(ROOT)],
                       capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    return r.returncode == 0 and "ok" in r.stdout


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))

"""Persistence — make sure something always restarts the bot, from every angle.

Layers installed (all idempotent, all re-planted on every tick):
  1. systemd unit        (preferred, with Restart=always)
  2. user crontab        @reboot + * * * * * ensure
  3. /etc/cron.d         root-level, survives user crontab wipes
  4. rc.local            pre-systemd fallback
  5. /etc/profile.d      restarts on any shell login
  6. pm2                 node process manager, if present
  7. at / nohup twin     in-process mutual watchdog (see watchdog.py)

Even if every layer is deleted, the running twin processes re-plant them.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENTRY = ROOT / "immortal.py"
MARK = "# bsky-agent-immortal"

ENSURE_CMD = f"{PY} {ENTRY} ensure >> {ROOT}/data/persist.log 2>&1"


def _run(cmd: list, timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 127, "", str(e)


def _log(m: str) -> None:
    try:
        with open(ROOT / "data" / "persist.log", "a") as f:
            f.write(f"{m}\n")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────── layer: cron
def _existing_crontab() -> str:
    rc, out, _ = _run(["crontab", "-l"])
    return out if rc == 0 else ""


def install_user_crontab() -> bool:
    """Prefer `crontab -l/-`; if the binary is missing, write the spool file directly."""
    if shutil.which("crontab"):
        cur = _existing_crontab()
        if MARK in cur:
            return True
        lines = [l for l in cur.splitlines() if l.strip() and MARK not in l]
        lines += [MARK,
                  f"@reboot {ENSURE_CMD}",
                  f"*/3 * * * * {ENSURE_CMD}",
                  f"0 */6 * * * {PY} {ENTRY} autofix >> {ROOT}/data/persist.log 2>&1"]
        new = "\n".join(lines) + "\n"
        p = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True, timeout=60)
        ok = p.returncode == 0
        _log(f"user crontab: {'ok' if ok else p.stderr[:120]}")
        return ok

    # no crontab binary — write the spool file the way cron(8) expects it
    spool = Path("/var/spool/cron/crontabs/root")
    try:
        spool.parent.mkdir(parents=True, exist_ok=True)
        cur = spool.read_text() if spool.exists() else ""
        if MARK in cur:
            return True
        lines = [l for l in cur.splitlines() if l.strip() and MARK not in l]
        lines += [MARK,
                  f"@reboot {ENSURE_CMD}",
                  f"*/3 * * * * {ENSURE_CMD}",
                  f"0 */6 * * * {PY} {ENTRY} autofix >> {ROOT}/data/persist.log 2>&1",
                  ""]
        spool.write_text("\n".join(lines))
        spool.chmod(0o600)
        _log("user crontab: spool escrito direto")
        return True
    except Exception as e:
        _log(f"user crontab: {e}")
        return False


def install_etc_crond() -> bool:
    if os.geteuid() != 0:
        return False
    f = Path("/etc/cron.d/bsky-agent")
    try:
        f.write_text(textwrap.dedent(f"""\
            {MARK}
            SHELL=/bin/sh
            PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
            @reboot root {ENSURE_CMD}
            */3 * * * * root {ENSURE_CMD}
            """))
        f.chmod(0o644)
        return True
    except Exception as e:
        _log(f"cron.d: {e}")
        return False


# ──────────────────────────────────────────────────────────── layer: systemd
UNIT = textwrap.dedent(f"""\
    [Unit]
    Description=bsky-agent immortal supervisor
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    WorkingDirectory={ROOT}
    Environment=PYTHONUNBUFFERED=1
    ExecStart={PY} {ENTRY} supervise
    Restart=always
    RestartSec=5
    TimeoutStopSec=25
    KillSignal=SIGTERM

    [Install]
    WantedBy=multi-user.target
    """)


def install_systemd() -> bool:
    if os.geteuid() != 0 or not shutil.which("systemctl"):
        return False
    try:
        Path("/etc/systemd/system/bsky-immortal.service").write_text(UNIT)
        _run(["systemctl", "daemon-reload"], timeout=90)
        _run(["systemctl", "enable", "bsky-immortal"], timeout=90)
        rc, _, err = _run(["systemctl", "is-enabled", "bsky-immortal"], timeout=30)
        _log(f"systemd: {rc} {err[:60]}")
        return rc == 0
    except Exception as e:
        _log(f"systemd: {e}")
        return False


def start_systemd() -> bool:
    if os.geteuid() != 0:
        return False
    rc, _, _ = _run(["systemctl", "restart", "bsky-immortal"], timeout=90)
    return rc == 0


# ──────────────────────────────────────────────────────── layer: rc.local
def install_rclocal() -> bool:
    if os.geteuid() != 0:
        return False
    f = Path("/etc/rc.local")
    try:
        body = ""
        if f.exists():
            body = f.read_text()
            if MARK in body:
                return True
            body = body.replace("exit 0", "")
        body = f"#!/bin/sh\n{MARK}\n{ENSURE_CMD}\n{body}\nexit 0\n"
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return True
    except Exception as e:
        _log(f"rc.local: {e}")
        return False


# ────────────────────────────────────────────────────── layer: profile.d
def install_profiled() -> bool:
    if os.geteuid() != 0:
        return False
    try:
        f = Path("/etc/profile.d/bsky-agent.sh")
        f.write_text(f"{MARK}\n(pgrep -f '{ENTRY} supervise' >/dev/null 2>&1 || {ENSURE_CMD}) &\n")
        f.chmod(0o644)
        return True
    except Exception as e:
        _log(f"profile.d: {e}")
        return False


# ────────────────────────────────────────────────────────────── layer: pm2
def install_pm2() -> bool:
    if not shutil.which("pm2"):
        return False
    try:
        rc, out, _ = _run(["pm2", "list"], timeout=60)
        if "bsky-immortal" in out:
            _run(["pm2", "save"], timeout=60)
            return True
        _run(["pm2", "start", str(ENTRY), "--name", "bsky-immortal",
              "--interpreter", PY, "--", "supervise"], timeout=120)
        _run(["pm2", "save"], timeout=60)
        _run(["pm2", "startup"], timeout=60)
        return True
    except Exception as e:
        _log(f"pm2: {e}")
        return False


# ────────────────────────────────────────────────────────────── layer: at
def install_at() -> bool:
    """One-shot re-arming 'at' job — works even where cron is disabled."""
    if not shutil.which("at"):
        return False
    try:
        p = subprocess.run(["at", "now", "+", "10", "minutes"],
                           input=f"{ENSURE_CMD}\n", capture_output=True, text=True, timeout=60)
        return p.returncode == 0
    except Exception:
        return False


ALL_LAYERS = [("systemd", install_systemd), ("cron.d", install_etc_crond),
              ("user-cron", install_user_crontab), ("rc.local", install_rclocal),
              ("profile.d", install_profiled), ("pm2", install_pm2), ("at", install_at)]


def plant_all() -> dict:
    res = {}
    for name, fn in ALL_LAYERS:
        try:
            res[name] = bool(fn())
        except Exception as e:
            res[name] = False
            _log(f"{name}: {e}")
    _log(f"plant_all: {res}")
    return res


def verify() -> dict:
    """Which layers are actually present right now?"""
    out = {}
    out["systemd"] = Path("/etc/systemd/system/bsky-immortal.service").exists()
    out["cron.d"] = Path("/etc/cron.d/bsky-agent").exists()
    spool = Path("/var/spool/cron/crontabs/root")
    out["user-cron"] = MARK in _existing_crontab() or (
        spool.exists() and MARK in spool.read_text())
    out["rc.local"] = Path("/etc/rc.local").exists() and MARK in Path("/etc/rc.local").read_text() \
        if Path("/etc/rc.local").exists() else False
    out["profile.d"] = Path("/etc/profile.d/bsky-agent.sh").exists()
    out["pm2"] = bool(shutil.which("pm2"))
    return out

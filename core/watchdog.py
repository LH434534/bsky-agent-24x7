"""Immortal supervisor — mutual-watchdog, self-healing, self-reinstalling.

Three processes, each watching the next in a ring:

      ┌──────────── supervisor ────────────┐
      │  spawns worker (main.py run)       │
      │  spawns twin (immortal.py twin)    │
      │  re-plants cron/systemd/pm2/...    │
      └────────────────────────────────────┘
            │ monitors              ▲ monitored by
            ▼                       │
        worker  (does the actual Bluesky work)
                                    │
        twin ─── monitors supervisor, restarts it if it dies

If supervisor dies   → twin respawns it
If twin dies         → supervisor respawns it
If worker dies/hangs → supervisor restarts it
If ALL die           → cron/systemd/rc.local/profile.d respawn the supervisor
If crash loops       → autofix ladder: clear state → reinstall deps → restore snapshot
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
PY = sys.executable
ENTRY = ROOT / "immortal.py"
MAIN = ROOT / "main.py"

from core.beacon import Beacon, read, age, alive, pid_of      # noqa: E402
from core import autofix                                       # noqa: E402
from core import persist                                       # noqa: E402
from core import health                                        # noqa: E402


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} IMMORTAL│ {m}"
    print(line, flush=True)
    try:
        with open(DATA / "immortal.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def spawn(*args: str, env_extra: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_extra or {})
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen([PY, *args], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


@dataclass
class Immortal:
    role: str = "supervisor"
    worker_grace: float = 180.0       # heartbeat older than this = hung
    twin_grace: float = 120.0
    check_every: float = 20.0
    replant_every: float = 600.0
    max_restarts: int = 60
    _worker: subprocess.Popen | None = None
    _twin: subprocess.Popen | None = None
    _restarts: int = 0
    _last_replant: float = 0.0
    _beacon: Beacon | None = None
    _running: bool = True

    # ────────────────────────────────────────────────────────────── lifecycle
    def kill_stale_workers(self) -> int:
        """Kill orphaned workers from a previous supervisor life so we never double-post."""
        me = os.getpid()
        n = 0
        try:
            out = subprocess.run(["pgrep", "-f", f"{MAIN} run"],
                                 capture_output=True, text=True, timeout=20).stdout
            for line in out.split():
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if pid == me:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    n += 1
                except Exception:
                    pass
        except Exception:
            pass
        if n:
            log(f"{n} worker(s) órfão(s) eliminados")
        return n

    def start_worker(self) -> None:
        self._worker = spawn(str(MAIN), "run")
        log(f"worker spawned pid={self._worker.pid}")

    def start_twin(self) -> None:
        self._twin = spawn(str(ENTRY), "twin", env_extra={"IMMORTAL_PARENT": str(os.getpid())})
        log(f"twin spawned pid={self._twin.pid}")

    def kill_worker(self) -> None:
        p = self._worker
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=15)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # ────────────────────────────────────────────────────────────── decisions
    def worker_healthy(self) -> tuple[bool, str]:
        p = self._worker
        if p is None:
            return False, "no handle"
        if p.poll() is not None:
            return False, f"exited rc={p.returncode}"
        a = age("worker")
        if a > self.worker_grace:
            return False, f"heartbeat stale {a:.0f}s"
        return True, "ok"

    def twin_healthy(self) -> tuple[bool, str]:
        p = self._twin
        if p is not None and p.poll() is None:
            return True, "ok"
        a = age("twin")
        pid = pid_of("twin")
        if a < self.twin_grace and alive(pid):
            return True, "ok (external)"
        return False, f"dead (age={a:.0f}s)"

    def supervisor_healthy(self) -> tuple[bool, str]:
        """Used by the twin role."""
        a = age("supervisor")
        pid = pid_of("supervisor")
        if a < self.twin_grace and alive(pid):
            return True, "ok"
        return False, f"dead (age={a:.0f}s)"

    def crash_repair(self) -> None:
        self._restarts += 1
        log(f"restart #{self._restarts} — rodando autofix")
        try:
            rep = autofix.run()
            log(f"autofix: {json.dumps(rep, ensure_ascii=False)[:200]}")
        except Exception:
            log(f"autofix crashed: {traceback.format_exc()[-300:]}")
        # escalate on repeated failures
        if self._restarts % 5 == 0:
            log("ciclo de falhas: limpando pyc + estado")
            autofix.fix_pyc()
            autofix.fix_corrupt_state()
        if self._restarts % 12 == 0:
            log("ciclo de falhas grave: restaurando snapshot")
            autofix.restore_snapshot()
        if self._restarts >= self.max_restarts:
            log("max restarts atingido — resetando contador e fazendo health full")
            self._restarts = 0
            h = health.full()
            for c in h.failed:
                log(f"health FAIL {c.name}: {c.detail}")

    # ────────────────────────────────────────────────────────────────── loops
    def supervise(self) -> None:
        self._beacon = Beacon("supervisor", every=10).start()
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "_running", False))
        signal.signal(signal.SIGINT, lambda *a: setattr(self, "_running", False))
        log(f"supervisor online pid={os.getpid()}")
        self.kill_stale_workers()
        self.start_worker()
        self.start_twin()
        persist.plant_all()

        while self._running:
            time.sleep(self.check_every)
            try:
                ok, why = self.worker_healthy()
                if not ok:
                    log(f"worker unhealthy: {why}")
                    self.kill_worker()
                    self.crash_repair()
                    time.sleep(5)
                    self.start_worker()

                ok, why = self.twin_healthy()
                if not ok:
                    log(f"twin unhealthy: {why} — respawnando")
                    self.start_twin()

                if time.time() - self._last_replant > self.replant_every:
                    self._last_replant = time.time()
                    persist.plant_all()
                    h = health.full()
                    bad = [c for c in h.failed if c.fatal]
                    if bad:
                        log("health fatal: " + ", ".join(f"{c.name}={c.detail}" for c in bad))
                        autofix.run()
            except Exception:
                log(f"supervisor loop: {traceback.format_exc()[-400:]}")

        self.kill_worker()
        log("supervisor saindo")

    def twin(self) -> None:
        """Watch the supervisor from a separate process/session."""
        self._beacon = Beacon("twin", every=10).start()
        log(f"twin online pid={os.getpid()}")
        while True:
            time.sleep(self.check_every)
            try:
                ok, why = self.supervisor_healthy()
                if not ok:
                    log(f"supervisor morto ({why}) — relançando")
                    autofix.run()
                    spawn(str(ENTRY), "supervise")
                    time.sleep(30)
                if int(time.time()) % 900 < 25:
                    persist.plant_all()
            except Exception:
                log(f"twin loop: {traceback.format_exc()[-300:]}")
                time.sleep(30)

    def worker(self) -> None:
        """Run the actual agent with a beacon so the supervisor can detect hangs."""
        beacon = Beacon("worker", every=15).start()
        log(f"worker online pid={os.getpid()}")
        sys.argv = [str(MAIN), "run"]
        try:
            import main as agent_main
            agent_main.cmd_run(type("A", (), {"log": "INFO"})())
        except Exception:
            log(f"worker crash: {traceback.format_exc()[-800:]}")
            raise
        finally:
            beacon.stop()


def already_running() -> bool:
    ok, _ = (lambda: (age("supervisor") < 90 and alive(pid_of("supervisor")), ""))()
    return ok


def ensure() -> int:
    """Idempotent entry point for cron/@reboot/rc.local. Safe to run every minute."""
    if already_running():
        return 0
    a = age("supervisor")
    log(f"ensure: supervisor ausente (age={a:.0f}s) — relançando")
    persist.plant_all()
    spawn(str(ENTRY), "supervise")
    return 1

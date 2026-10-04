#!/usr/bin/env python3
"""
immortal.py — entry point do sistema à prova de queda.

    python3 immortal.py supervise   # supervisor: sobe worker + twin, monitora, repara
    python3 immortal.py twin        # processo gêmeo: revive o supervisor se ele morrer
    python3 immortal.py worker      # roda o agente com beacon de vida
    python3 immortal.py ensure      # idempotente — usado por cron/@reboot/rc.local
    python3 immortal.py plant       # instala todas as camadas de persistência
    python3 immortal.py status      # mostra health, heartbeats e camadas ativas
    python3 immortal.py autofix     # roda o reparo automático agora
    python3 immortal.py snapshot    # salva cópia conhecida-boa do código
    python3 immortal.py kill        # para tudo
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

from core.watchdog import Immortal, already_running, spawn   # noqa: E402
from core import persist, health, autofix                    # noqa: E402
from core.beacon import read, age, alive, pid_of             # noqa: E402


def cmd_supervise():
    Immortal(role="supervisor").supervise()


def cmd_twin():
    Immortal(role="twin").twin()


def cmd_worker():
    Immortal(role="worker").worker()


def cmd_ensure():
    n = __import__("core.watchdog", fromlist=["ensure"]).ensure()
    print("relaunched" if n else "already running")


def cmd_plant():
    print(json.dumps(persist.plant_all(), indent=2))
    print(json.dumps(persist.verify(), indent=2))


def cmd_autofix():
    rep = autofix.run()
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    print("self_test:", autofix.self_test())


def cmd_snapshot():
    print("snapshot:", autofix.snapshot())


def cmd_status():
    h = health.full()
    print("\n── health ──")
    for c in h.checks:
        mark = "✅" if c.ok else ("❌" if c.fatal else "⚠️")
        print(f"  {mark} {c.name:<12} {c.detail}")
    print("\n── heartbeats ──")
    for n in ("supervisor", "twin", "worker"):
        d = read(n)
        if not d:
            print(f"  {n:<11} —")
            continue
        a = time.time() - d["ts"]
        print(f"  {n:<11} pid={d['pid']:<7} tick={d.get('tick', 0):<6} "
              f"{a:.0f}s atrás {'VIVO' if alive(d['pid']) else 'MORTO'}")
    print("\n── persistência ──")
    for k, v in persist.verify().items():
        print(f"  {'✅' if v else '—'} {k}")
    try:
        st = json.loads((DATA / "autofix_state.json").read_text())
        print("\n── autofix ──")
        print("  sintomas:", st.get("counts"))
        print("  reparos :", st.get("applied"))
    except Exception:
        pass
    print()


def cmd_kill():
    me = os.getpid()
    for pat in ("immortal.py supervise", "immortal.py twin", "main.py run"):
        try:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout
            for line in out.split():
                pid = int(line)
                if pid != me:
                    try:
                        os.kill(pid, 15)
                        print("SIGTERM", pid, pat)
                    except Exception:
                        pass
        except Exception:
            pass
    time.sleep(2)
    print("ok — (cron/systemd ainda podem religar; use 'plant' removendo as camadas se quiser parar de vez)")


CMDS = {"supervise": cmd_supervise, "twin": cmd_twin, "worker": cmd_worker,
        "ensure": cmd_ensure, "plant": cmd_plant, "status": cmd_status,
        "autofix": cmd_autofix, "snapshot": cmd_snapshot, "kill": cmd_kill}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(2)
    CMDS[sys.argv[1]]()

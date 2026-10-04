#!/usr/bin/env python3
"""Runner para GitHub Actions.

Sobe o agente e roda por RUN_MINUTES minutos (o teto de job do Actions é 6h,
então usamos ~5h45 e um cron de 30 min religa o próximo).

Persistência: o estado (memory.db, corpus.txt, spam_state.json, session.json)
vive no próprio repositório. Ele é commitado a cada STATE_EVERY_MIN minutos e
no final — então um job morto pelo scheduler nunca perde mais que esse janela.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "345"))
STATE_EVERY_MIN = float(os.environ.get("STATE_EVERY_MIN", "12"))
DEADLINE = time.time() + RUN_MINUTES * 60

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


LOGFILE = DATA / "last_run.log"


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} GH      │ {m}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sh(*args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 127, str(e)


def commit_state(tag: str) -> bool:
    """Commita o estado de volta no repo. Falha silenciosa — o bot vem primeiro."""
    sh("git", "config", "user.email", "bot@users.noreply.github.com")
    sh("git", "config", "user.name", "bsky-bot")
    keep = ["memory.db", "corpus.txt", "spam_state.json", "session.json",
            "autofix_state.json", "last_run.log", "agent.log", "agency_state.json"]
    for p in keep:
        f = DATA / p
        if f.exists():
            sh("git", "add", "-f", str(f))
    sh("git", "add", "-f", "data/seed_corpus.txt")
    rc, out = sh("git", "status", "--porcelain")
    log(f"git status rc={rc} -> {out.strip()[:200] or '(vazio)'}")
    if not out.strip():
        return False
    rc, out = sh("git", "commit", "-q", "-m",
                 f"state: {tag} [{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}]")
    log(f"git commit rc={rc} {out.strip()[:150]}")
    if rc != 0:
        return False
    # só o bot escreve data/ — force-push é a estratégia primária.
    # (pull --rebase falha porque o beacon reescreve hb_worker.json entre add e pull)
    rc, err = sh("git", "push", "--force", "origin", "HEAD:refs/heads/main", timeout=180)
    log(f"git push --force rc={rc} {err.strip()[:200]}")
    if rc == 0:
        log(f"estado commitado ({tag})")
        return True
    for attempt in range(3):
        rc, err = sh("git", "push", "origin", "HEAD", timeout=180)
        log(f"git push rc={rc} {err.strip()[:150]}")
        if rc == 0:
            return True
        time.sleep(5 * (attempt + 1))
    log("push falhou (não-fatal)")
    return False


def main() -> int:
    log(f"runner start — {RUN_MINUTES:.0f} min, deadline {time.strftime('%H:%MZ', time.gmtime(DEADLINE))}")
    log(f"cwd={ROOT} py={sys.version.split()[0]}")
    log(f"data/: {sorted(p.name for p in DATA.glob('*')) if DATA.exists() else 'vazio'}")
    log(f"env handle={'ok' if os.environ.get('BSKY_HANDLE') else 'FALTANDO'} "
        f"pw={'ok' if os.environ.get('BSKY_APP_PASSWORD') else 'FALTANDO'}")

    # health antes de tudo
    from core import health, autofix
    h = health.full()
    for c in h.failed:
        log(f"health {c.name}: {c.detail}")
        if not c.ok and c.fatal:
            autofix.run()

    from core.beacon import Beacon
    from core.atproto import Bsky
    from core.brain import Brain
    from core.antispam import SpamGuard, Limits
    from core.memory import Memory
    from core.actions import Actions, DEFAULT_QUERIES

    handle = os.environ.get("BSKY_HANDLE", "")
    apppw = os.environ.get("BSKY_APP_PASSWORD", "")
    if not handle or not apppw:
        log("ERRO: BSKY_HANDLE / BSKY_APP_PASSWORD ausentes")
        return 2

    bsky = Bsky(handle, apppw,
                pds=os.environ.get("BSKY_PDS", "https://bsky.social"),
                session_file=str(DATA / "session.json"))
    bsky.login()
    log(f"logado como @{bsky.handle} ({bsky.did})")

    guard = SpamGuard(limits=Limits(
        day_posts=int(os.environ.get("DAY_POSTS", 20)),
        day_replies=int(os.environ.get("DAY_REPLIES", 35)),
        day_follows=int(os.environ.get("DAY_FOLLOWS", 60)),
        day_likes=int(os.environ.get("DAY_LIKES", 150)),
        day_reposts=int(os.environ.get("DAY_REPOSTS", 25))),
        tz_offset=int(os.environ.get("TZ_OFFSET", "-3")),
        state_file=DATA / "spam_state.json")
    guard.load()

    brain = Brain()
    mem = Memory(DATA / "memory.db")
    prior = mem.recent_texts(300)
    if prior:
        brain.offline.feed(prior)
    brain.offline._train()
    provider = brain.pick().name
    log(f"brain provider: {provider}")
    if os.environ.get("OLLAMA_MODEL"):
        log(f"OLLAMA_MODEL={os.environ['OLLAMA_MODEL']} host={os.environ.get('OLLAMA_HOST')}")
    try:
        t0 = time.time()
        sample = brain.write_post("automação e infraestrutura")
        log(f"brain sample ({time.time()-t0:.1f}s): {sample!r}")
    except Exception as e:
        log(f"brain sample falhou: {e}")

    # ── agência: estado interno que decide, não pesos fixos ──────────────────
    from core.agency import Agency
    from core.autonomy import Autonomous

    A = Agency.load(tz_offset=int(os.environ.get("TZ_OFFSET", "-3")))
    A.seed_topics(DEFAULT_QUERIES)
    log(f"agência: {A.describe()}")

    # garante que os arquivos de agência entrem no primeiro commit
    A.save()
    (DATA / "agent.log").touch()

    acts = Actions(bsky, brain, guard, mem, agency=A)
    H = {"post": acts.do_post, "reply": acts.do_reply, "follow": acts.do_follow,
         "like": acts.do_like, "repost": acts.do_repost,
         "notifications": acts.do_engage_notifications, "harvest": acts.do_harvest}

    beacon = Beacon("worker", every=15).start()

    def flush():
        guard.save()
        A.save()

    last_state = time.time()

    def maybe_commit():
        nonlocal last_state
        if time.time() - last_state > STATE_EVERY_MIN * 60:
            last_state = time.time()
            commit_state(f"tick{A.ticks}")

    # envolve os handlers para commitar estado periodicamente
    def wrapped(kind: str):
        fn = H.get(kind)
        def _f():
            r = fn()
            maybe_commit()
            return r
        return _f

    engine = Autonomous(
        agency=A,
        handlers={k: wrapped(k) for k in H},
        feed_provider=lambda n: acts.browse(n),
        notif_check=acts.has_pending_notifications,
        on_flush=flush,
        heartbeat=lambda: None,
    )

    try:
        engine.run(until_ts=DEADLINE)
    except KeyboardInterrupt:
        pass
    finally:
        beacon.stop()
        guard.save()
        A.save()
        commit_state("final")
        st = mem.stats(24)
        log(f"fim — deliberações={A.ticks} stats24h={json.dumps(st, ensure_ascii=False)}")
        log(f"agência final: {A.describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

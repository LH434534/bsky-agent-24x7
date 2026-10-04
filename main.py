#!/usr/bin/env python3
"""
bsky-agent — autonomous Bluesky agent with anti-ban behaviour shaping.

    python main.py run          # 24/7 loop
    python main.py once post    # single action
    python main.py status       # caps, counters, health
    python main.py post "texto" # manual post (screened)
    python main.py harvest      # fill the offline corpus, no writes
    python main.py doctor       # connectivity + credentials self-test
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.atproto import Bsky                      # noqa: E402
from core.brain import Brain                       # noqa: E402
from core.antispam import SpamGuard, Limits        # noqa: E402
from core.memory import Memory                     # noqa: E402
from core.actions import Actions                   # noqa: E402
from core.scheduler import Loop, Weights           # noqa: E402

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


# ───────────────────────────────────────────────────────────────────── config
def load_env() -> None:
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)-6s │ %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(DATA / "agent.log", encoding="utf-8")])


def build(tz_offset: int = -3, queries: list | None = None,
          persona_topics: list | None = None):
    load_env()
    handle = os.environ.get("BSKY_HANDLE", "")
    apppw = os.environ.get("BSKY_APP_PASSWORD", "")
    if not handle or not apppw:
        print("ERRO: defina BSKY_HANDLE e BSKY_APP_PASSWORD em .env", file=sys.stderr)
        sys.exit(2)

    bsky = Bsky(handle, apppw,
                pds=os.environ.get("BSKY_PDS", "https://bsky.social"),
                session_file=str(DATA / "session.json"))
    bsky.login()

    guard = SpamGuard(
        limits=Limits(
            day_posts=int(os.environ.get("DAY_POSTS", 20)),
            day_replies=int(os.environ.get("DAY_REPLIES", 35)),
            day_follows=int(os.environ.get("DAY_FOLLOWS", 60)),
            day_likes=int(os.environ.get("DAY_LIKES", 150)),
            day_reposts=int(os.environ.get("DAY_REPOSTS", 25)),
        ),
        tz_offset=tz_offset,
        state_file=DATA / "spam_state.json")
    guard.load()

    brain = Brain()
    mem = Memory(DATA / "memory.db")

    # feed the offline brain with what we already said
    prior = mem.recent_texts(300)
    if prior:
        brain.offline.feed(prior)

    acts = Actions(bsky, brain, guard, mem, queries=queries, persona_topics=persona_topics)
    return bsky, brain, guard, mem, acts


def handlers(acts: Actions) -> dict:
    return {
        "post": acts.do_post,
        "reply": acts.do_reply,
        "follow": acts.do_follow,
        "like": acts.do_like,
        "repost": acts.do_repost,
        "notifications": acts.do_engage_notifications,
        "harvest": acts.do_harvest,
    }


# ─────────────────────────────────────────────────────────────────────── cmd
def cmd_run(a) -> None:
    setup_logging(a.log)
    from core.beacon import Beacon
    Beacon("worker", every=15).start()      # prova de vida para o supervisor
    tz = int(os.environ.get("TZ_OFFSET", "-3"))
    bsky, brain, guard, mem, acts = build(tz)
    brain.offline._train()

    w = Weights()
    loop = Loop(weights=w,
                tick_min=float(os.environ.get("TICK_MIN", 60)),
                tick_max=float(os.environ.get("TICK_MAX", 300)))

    def flush():
        guard.save()

    print(f"\n  agente online  »  @{bsky.handle}  ({bsky.did})")
    print(f"  brain provider »  {brain.pick().name}")
    print(f"  tz local       »  UTC{tz:+d}   quiet hours {guard.limits.quiet_hours}")
    print("  ctrl-c para parar\n")
    try:
        loop.run(handlers(acts), on_stop=flush)
    finally:
        guard.save()
        print(json.dumps(mem.stats(), indent=2, ensure_ascii=False))


def cmd_once(a) -> None:
    setup_logging(a.log)
    bsky, brain, guard, mem, acts = build()
    brain.offline._train()
    ok = Loop().once(a.action, handlers(acts))
    guard.save()
    print(f"{a.action}: {'ok' if ok else 'skipped/failed'}")
    sys.exit(0 if ok else 1)


def cmd_status(a) -> None:
    setup_logging("ERROR")
    load_env()
    guard = SpamGuard(tz_offset=int(os.environ.get("TZ_OFFSET", "-3")),
                      state_file=DATA / "spam_state.json")
    guard.load()
    mem = Memory(DATA / "memory.db")
    print("\n── spam guard ──")
    print(json.dumps(guard.stats(), indent=2, ensure_ascii=False))
    print("\n── última 24h ──")
    print(json.dumps(mem.stats(24), indent=2, ensure_ascii=False))
    topics = mem.hot_topics(8)
    if topics:
        print("\n── tópicos ──\n ", ", ".join(topics))
    print()


def cmd_post(a) -> None:
    setup_logging(a.log)
    bsky, brain, guard, mem, acts = build()
    ok = acts._post_text(a.text)
    guard.save()
    print("postado" if ok else "rejeitado pelo filtro anti-spam")
    sys.exit(0 if ok else 1)


def cmd_harvest(a) -> None:
    setup_logging(a.log)
    bsky, brain, guard, mem, acts = build()
    for _ in range(int(a.rounds)):
        acts.do_harvest()
        time.sleep(random.uniform(3, 8))
    brain.offline._train()
    print("corpus:", (DATA / "corpus.txt").stat().st_size if (DATA / "corpus.txt").exists() else 0,
          "bytes")


def cmd_doctor(a) -> None:
    setup_logging("ERROR")
    load_env()
    print("\n── doctor ──")
    print("  .env handle      :", os.environ.get("BSKY_HANDLE", "NÃO DEFINIDO"))
    print("  .env app password:", "ok" if os.environ.get("BSKY_APP_PASSWORD") else "NÃO DEFINIDO")
    brain = Brain()
    for p in brain.providers + [brain.offline]:
        try:
            ok = p.available()
        except Exception as e:
            ok = False
        print(f"  brain {p.name:<12}: {'ATIVO' if ok else '-'}")
    try:
        bsky = Bsky(os.environ.get("BSKY_HANDLE", ""),
                    os.environ.get("BSKY_APP_PASSWORD", ""),
                    session_file=str(DATA / "session.json"))
        bsky.login()
        prof = bsky.profile(bsky.handle)
        print(f"  login            : OK  @{prof.get('handle')} "
              f"({prof.get('followersCount')} seguidores / seguindo {prof.get('followsCount')})")
        print("\n  tudo certo. rode:  python main.py run\n")
    except Exception as e:
        print(f"  login            : FALHOU — {str(e)[:200]}")
        print("\n  confira handle/app password ou a rede.\n")
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser("bsky-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="loop 24/7")
    p1 = sub.add_parser("once")
    p1.add_argument("action", choices=["post", "reply", "follow", "like",
                                       "repost", "notifications", "harvest"])
    sub.add_parser("status")
    p2 = sub.add_parser("post")
    p2.add_argument("text")
    p3 = sub.add_parser("harvest")
    p3.add_argument("--rounds", default=4)
    sub.add_parser("doctor")

    a = ap.parse_args()
    {"run": cmd_run, "once": cmd_once, "status": cmd_status, "post": cmd_post,
     "harvest": cmd_harvest, "doctor": cmd_doctor}[a.cmd](a)


if __name__ == "__main__":
    main()

"""End-to-end test: mock AT Protocol in a thread, then drive the real agent."""
import io, json, os, sys, threading, time, contextlib
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BSKY_PDS"] = "http://127.0.0.1:8899"
os.environ["BSKY_HANDLE"] = "his08.bsky.social"
os.environ["BSKY_APP_PASSWORD"] = "test"
os.environ["TZ_OFFSET"] = "-3"

from tests.mock_atproto import H                      # noqa: E402

srv = HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
os.environ["BSKY_PDS"] = f"http://127.0.0.1:{PORT}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

import logging
from core.atproto import Bsky                          # noqa: E402
from core.brain import Brain                           # noqa: E402
from core.antispam import SpamGuard, Limits            # noqa: E402
from core.memory import Memory                         # noqa: E402
from core.actions import Actions                       # noqa: E402

DATA = ROOT / "data"
for f in ("session.json", "spam_state.json", "memory.db", "corpus.txt"):
    p = DATA / f
    if p.exists():
        p.unlink()

logging.basicConfig(level=logging.INFO, format="  %(levelname)-5s %(name)-6s │ %(message)s")

print("\n═══ 1. login ═══")
b = Bsky("his08.bsky.social", "test", pds=os.environ["BSKY_PDS"],
         session_file=str(DATA / "session.json"))
b.login()
print("  did:", b.did)

print("\n═══ 2. facets (rich text) ═══")
f = b.parse_facets("olha isso https://ex.com/a e @dev.one.bsky.social sobre #automacao")
print(" ", json.dumps(f, ensure_ascii=False)[:220])

print("\n═══ 3. brain (offline engine) ═══")
brain = Brain()
brain.offline.feed([" ".join(["automation systems shipping dev infra python docker"] * 3)])
brain.offline._train()
print("  provider:", brain.pick().name)
print("  post   :", brain.write_post("automação"))
print("  reply  :", brain.write_reply("docker broke again", "dev"))
print("  topics :", brain.pick_topics(3))

print("\n═══ 4. anti-spam screen ═══")
guard = SpamGuard(limits=Limits(), tz_offset=-3, state_file=DATA / "spam_state.json")
mem = Memory(DATA / "memory.db")
cases = [
    ("normal post sobre automação e infra estrutura", True),
    ("FREE AIRDROP 100x join telegram dm me", False),
    ("As an AI, I think this is crucial to note", False),
    ("#a #b #c #d many hashtags here", False),
    ("FOLLOW ME BACK FOLLOW BACK", False),
]
for t, expect in cases:
    ok, why = guard.screen(t)
    flag = "PASS" if ok == expect else "FAIL"
    print(f"  [{flag}] ok={ok:<5} why={why:<32} :: {t[:40]}")

print("\n═══ 5. behaviours (contra o mock) ═══")
acts = Actions(b, brain, guard, mem)
for name, fn in (("post", acts.do_post), ("reply", acts.do_reply),
                 ("follow", acts.do_follow), ("like", acts.do_like),
                 ("repost", acts.do_repost), ("notifications", acts.do_engage_notifications),
                 ("harvest", acts.do_harvest)):
    try:
        r = fn()
        print(f"  {name:<14} -> {r}")
    except Exception as e:
        print(f"  {name:<14} -> CRASH {type(e).__name__}: {e}")

print("\n═══ 6. memória / limites ═══")
print(" ", json.dumps(mem.stats(24), ensure_ascii=False))
print(" ", json.dumps(guard.stats(), ensure_ascii=False))
guard.save()

print("\n═══ 7. idempotência (2ª rodada deve ser bloqueada pelos gaps) ═══")
for name, fn in (("post", acts.do_post), ("reply", acts.do_reply), ("follow", acts.do_follow)):
    print(f"  {name:<8} -> {fn()}  (esperado False = rate limit funcionando)")

srv.shutdown()
print("\n✅ e2e completo\n")

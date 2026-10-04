"""24h simulation against the mock: verifies caps hold, no crashes, loop keeps choosing."""
import os, pathlib, random, sys, threading, time, json
from http.server import HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BSKY_PDS"] = "http://127.0.0.1:8899"

from tests.mock_atproto import H                       # noqa: E402
srv = HTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
os.environ["BSKY_PDS"] = f"http://127.0.0.1:{PORT}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

import logging                                          # noqa: E402
logging.basicConfig(level=logging.WARNING)

from core.atproto import Bsky                           # noqa: E402
from core.brain import Brain                            # noqa: E402
from core.antispam import SpamGuard, Limits             # noqa: E402
from core.memory import Memory                          # noqa: E402
from core.actions import Actions                        # noqa: E402
from core.scheduler import Loop, Weights                # noqa: E402

D = ROOT / "data"
for f in ("memory.db", "spam_state.json", "session.json"):
    p = D / f
    if p.exists():
        p.unlink()

b = Bsky("his08.bsky.social", "test", pds=os.environ["BSKY_PDS"], session_file=str(D / "session.json"))
b.login()
br = Brain(); br.offline._train()
g = SpamGuard(limits=Limits(day_posts=8, day_replies=10, day_follows=6, day_likes=20, day_reposts=4,
                            gap_posts=1, gap_replies=1, gap_follows=1, gap_likes=1, gap_reposts=1,
                            quiet_hours=(99, 99)),
              tz_offset=-3, state_file=D / "spam_state.json")
m = Memory(D / "memory.db")
a = Actions(b, br, g, m)
H_ = {"post": a.do_post, "reply": a.do_reply, "follow": a.do_follow, "like": a.do_like,
      "repost": a.do_repost, "notifications": a.do_engage_notifications, "harvest": a.do_harvest}

TICKS = 40
counts = {k: 0 for k in H_}
did = {k: 0 for k in H_}
loop = Loop(weights=Weights())
random.seed(7)
t0 = time.time()
for i in range(TICKS):
    act = loop._pick()
    counts[act] += 1
    try:
        r = H_[act]()
        if r:
            did[act] += 1
    except Exception as e:
        print(f"  CRASH {act}: {type(e).__name__}: {e}")
g.save()

print(f"\n  {TICKS} ticks em {time.time()-t0:.1f}s")
print("  escolhido / executado:")
for k in sorted(H_):
    print(f"    {k:<14} {counts[k]:>4} / {did[k]:>4}")
st = m.stats(24)
print("\n  contadores 24h:", json.dumps(st, ensure_ascii=False))
caps = {"post": 8, "reply": 10, "follow": 6, "like": 20, "repost": 4}
viol = {k: (st[k], caps[k]) for k in caps if st[k] > caps[k]}
print("  violações de caps:", viol or "nenhuma ✅")
print("  erros:", st["errors_24h"])
srv.shutdown()
sys.exit(1 if viol else 0)

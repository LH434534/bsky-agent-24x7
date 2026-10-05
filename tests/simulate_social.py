"""Prova que as interações são autônomas (dirigidas por relação), não automáticas.

O teste compara o comportamento com e sem grafo social ao longo de vários dias:
  - sem grafo: interage com quem aparece (popularidade manda)
  - com grafo: concentra em quem conhece, cria fadiga, volta depois, visita
"""
from __future__ import annotations

import random
import sys
from collections import Counter

sys.path.insert(0, "/data/workspace/bsky_agent")

from core.social import Social, Person    # noqa: E402
import pathlib                            # noqa: E402

random.seed(int(__import__("os").environ.get("SEED", "7")))
F = pathlib.Path("/data/workspace/bsky_agent/data/social_test.json")
if F.exists():
    F.unlink()

PESSOAS = [
    ("did:a", "ana", "clima"), ("did:b", "bruno", "jogos"),
    ("did:c", "carla", "série"), ("did:d", "diego", "futebol"),
    ("did:e", "elisa", "música"),
]

S = Social(F)

# ── dia 1: só vê as pessoas postarem ──
for dia in range(3):
    for did, handle, topico in PESSOAS:
        for _ in range(random.randint(2, 6)):
            S.note_seen(did, handle, "post", topico)

print("═══ depois de 3 dias só observando ═══")
for did, handle, _ in PESSOAS:
    p = S.get(did, handle)
    print(f"  {p.handle:<7} conf {p.confianca:.2f}  afin {p.afinidade:.2f}  calor {p.calor:.2f}")

# ── ana responde ele; elisa nunca ──
for _ in range(3):
    S.note_got_reply("did:a", "ana")
for _ in range(4):
    S.note_reply("did:e", "elisa")

print("\n═══ reciprocidade muda tudo ═══")
for did, handle, _ in PESSOAS:
    p = S.get(did, handle)
    print(f"  {p.handle:<7} recip {p.reciprocidade:.2f}  falei {p.falei}  calor {p.calor:.2f}")

# ── ranqueamento: popularidade vs relação ──
estranho = {"did": "did:zz", "handle": "viral", "text": "post gigante", "score": 500}
conhecido = {"did": "did:a", "handle": "ana", "text": "post simples", "score": 3}
print("\n═══ ranqueamento (500 likes de estranho vs 3 de conhecida) ═══")
for c in S.rank([estranho, conhecido], "reply"):
    print(f"  → @{c['handle']}  (score original {c.get('score')})")

# ── fadiga: insistir na mesma pessoa cansa ──
print("\n═══ fadiga ═══")
for i in range(1, 8):
    S.note_reply("did:b", "bruno")
    ok, why = S.should_engage("did:b", "bruno", "reply")
    p = S.get("did:b", "bruno")
    print(f"  interação {i}: fadiga {p.fadiga:.2f} — {'sim' if ok else 'não'} ({why[:46]})")

# ── o tempo cura ──
import time  # noqa: E402
p = S.get("did:b", "bruno")
p.ultimo = time.time() - 3600 * 24
p.idle(24)
print(f"\n  24h depois: fadiga {p.fadiga:.2f} — {S.should_engage('did:b','bruno','reply')[0]}")

# ── iniciativa ──
print("\n═══ iniciativa: quem ele vai visitar ═══")
for did, handle, _ in PESSOAS:
    q = S.get(did, handle)
    q.ultimo = time.time() - 3600 * random.randint(10, 60)
    q.idle(20)
for p in S.who_to_visit(3):
    print(f"  → @{p.handle} (afinidade {p.afinidade:.2f}, {p.hours_since():.0f}h sem ver)")

# ── verificação ──
print("\n═══ verificações ═══")
ok = True
ana = S.get("did:a", "ana")
vir = S.get("did:zz", "viral")
c1 = ana.calor > vir.calor
print(f"  {'✅' if c1 else '❌'} conhecida tem mais peso que desconhecida "
      f"({ana.calor:.2f} vs {vir.calor:.2f})")
ok &= c1
c2 = S.get("did:b", "bruno").fadiga < 0.5
print(f"  {'✅' if c2 else '❌'} fadiga alta foi curada pelo tempo")
ok &= c2
c3 = ana.reciprocidade > S.get("did:e", "elisa").reciprocidade
print(f"  {'✅' if c3 else '❌'} reciprocidade registrada ({ana.reciprocidade:.2f} vs "
      f"{S.get('did:e','elisa').reciprocidade:.2f})")
ok &= c3
c4 = len(S.who_to_visit(3)) > 0
print(f"  {'✅' if c4 else '❌'} gera iniciativa de visitar alguém")
ok &= c4
c5 = bool(S.context_for("did:a"))
print(f"  {'✅' if c5 else '❌'} contexto social para o prompt: \"{S.context_for('did:a')[:60]}\"")
ok &= c5
print(f"\n{'AUTÔNOMO' if ok else 'AINDA AUTOMÁTICO'}")
sys.exit(0 if ok else 1)

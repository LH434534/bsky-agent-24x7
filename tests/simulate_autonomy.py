"""Teste do sistema inteiro autônomo: parâmetros, sistema imune e maestro.

Verifica que NADA é constante:
  1. parâmetros se ajustam com evidência (não ficam fixos)
  2. sistema imune aprende qual remédio funciona e para de repetir o que falha
  3. maestro muda de postura conforme a saúde
  4. maestro detecta monólogo e reequilibra
  5. maestro pode escolher silêncio
  6. 429 faz os intervalos subirem na hora
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, "/data/workspace/bsky_agent")
from core.adaptive import Adaptive      # noqa: E402
from core.immune import Immune, Remedy  # noqa: E402
from core.conductor import Conductor, Vitals   # noqa: E402

random.seed(int(__import__("os").environ.get("SEED", "5")))
D = Path("/data/workspace/bsky_agent/data")
for f in ("adaptive_state.json", "immune_state.json", "conductor_state.json"):
    if (D / f).exists():
        (D / f).unlink()

ok = True


def check(label: str, cond: bool, detail: str = "") -> bool:
    global ok
    print(f"  {'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail else ""))
    ok = ok and cond
    return cond


print("═══ 1. parâmetros aprendem ═══")
AD = Adaptive(D / "adaptive_state.json")
antes_posts = AD.get("gap_posts")
# 30 tentativas, quase todas falhando → deve recuar
for _ in range(30):
    AD.note("gap_posts", random.random() < 0.2)
mudou = AD.tune(force=True)
depois_posts = AD.get("gap_posts")
print(f"  gap_posts: {antes_posts:.0f} → {depois_posts:.0f}")
check("intervalo recua quando apanha", depois_posts > antes_posts,
      f"+{depois_posts-antes_posts:.0f}s")

antes2 = AD.get("gap_likes")
for _ in range(30):
    AD.note("gap_likes", True)
AD.tune(force=True)
print(f"  gap_likes: {antes2:.0f} → {AD.get('gap_likes'):.0f}")
check("intervalo afrouxa quando tudo dá certo", AD.get("gap_likes") <= antes2)

print("\n═══ 2. 429 muda tudo na hora ═══")
AD2 = Adaptive(D / "adaptive_state.json")
g0 = AD2.get("gap_posts")
msg = AD2.on_rate_limit()
print(f"  {msg[:80]}")
check("429 sobe os intervalos imediatamente", AD2.get("gap_posts") > g0)

print("\n═══ 3. sistema imune aprende ═══")
IM = Immune(D / "immune_state.json")
# remédios falsos: um funciona, outro nunca
IM.remedies = {
    "bom": Remedy("bom", lambda: True),
    "ruim": Remedy("ruim", lambda: False),
}
for _ in range(6):
    IM.respond("Traceback qualquer coisa", health_check=lambda: True)
ep = IM.episodes
print(f"  episódios: {len(ep)}  curas: {sum(1 for e in ep if e.ok)}")
bons = [e for e in ep if e.remedy == "bom"]
check("imune prefere o remédio que funciona", len(bons) > 0,
      f"{len(bons)}/{len(ep)} usaram 'bom'")
check("imune registra o que falhou", IM.remedies["ruim"].failed > 0)

print("\n═══ 4. imune para de martelar ═══")
IM2 = Immune(D / "immune_state.json")
IM2.remedies = {"inutil": Remedy("inutil", lambda: False)}
IM2.episodes = []
for _ in range(5):
    IM2.respond("ConnectionError timeout", health_check=lambda: False)
pode, porque = IM2.should_intervene("rede")
print(f"  depois de 5 falhas: {pode} ({porque[:60]})")
check("entra em quarentena após falhas repetidas", not pode)

print("\n═══ 5. maestro muda de postura ═══")
C = Conductor(state_file=D / "conductor_state.json")
for i in range(30):
    C.note("post", True, 3.0)
p, why = C.assess()
print(f"  tudo bem → {p} ({why})")
check("fica ativo quando vai bem", p == "ativo", why)

C2 = Conductor(state_file=D / "conductor_state.json")
for i in range(10):
    C2.note_error("429 rate limit")
p2, why2 = C2.assess()
print(f"  10 erros → {p2} ({why2})")
check("recua quando apanha", p2 in ("recuperando", "cauteloso"), why2)

print("\n═══ 6. maestro detecta monólogo ═══")
C3 = Conductor(agency=None, state_file=D / "conductor_state.json")
for i in range(20):
    C3.v.acoes.append((time.time(), "post", True))
C3.v.tentativas = 40
fala_antes = C3.A.inner.vontade_falar if C3.A else None
C3._rebalance()
print(f"  foco mudou: {'reply' in C3.focus_shift}")
check("corta a vontade de postar quando só monologa",
      "reply" in C3.focus_shift)

print("\n═══ 7. maestro escolhe silêncio ═══")
C4 = Conductor(state_file=D / "conductor_state.json")
C4.v.ultima_acao = time.time() - 3600 * 4      # 4h sem fazer nada útil
p4, why4 = C4.assess()
C4.set_posture(p4, why4)
print(f"  4h parado → {p4} ({why4})")
check("decide ficar quieto", p4 == "silencioso")
check("silêncio bloqueia ação", not C4.should_act())

print("\n═══ 8. maestro segura a rédea ═══")
C5 = Conductor(state_file=D / "conductor_state.json")
C5.posture = "recuperando"
vetos = sum(1 for _ in range(100) if not C5.should_act())
print(f"  postura recuperando: {vetos}/100 ações vetadas")
check("veta a maioria das ações quando doente", vetos > 50)

print(f"\n{'SISTEMA AUTÔNOMO' if ok else 'AINDA AUTOMÁTICO'}")
sys.exit(0 if ok else 1)

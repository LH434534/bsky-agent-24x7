"""Teste do motor de crescimento: medir, atribuir, cronometrar, escolher onde.

Verifica que não é "postar mais" e sim "postar onde rende":
  1. classifica estrutura do texto corretamente
  2. saturação de respostas mata a oportunidade (reply enterrado não serve)
  3. conta pequena / post velho / spam = zero
  4. aprende que tipo de post rendeu mais, e ainda assim explora
  5. relógio da audiência aprende a melhor hora
  6. o sistema inteiro escolhe alvo por visibilidade, não por ordem de chegada
"""
from __future__ import annotations

import datetime
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/data/workspace/bsky_agent")
from core.growth import Growth, Outcome, classify_hook, shape   # noqa: E402

random.seed(int(__import__("os").environ.get("SEED", "11")))
F = Path("/data/workspace/bsky_agent/data/growth_test.json")
if F.exists():
    F.unlink()
G = Growth(F)
ok = True


def check(label, cond, detail=""):
    global ok
    print(f"  {'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail else ""))
    ok = ok and cond


print("═══ 1. classificação de estrutura ═══")
cases = [
    ("alguém mais acha que o frio tá estranho?", "pergunta"),
    ("todo mundo erra sobre isso, na verdade é o contrário", "contrarian"),
    ("3 motivos pra você largar isso hoje", "lista"),
    ("reparei que ninguém fala do barulho da cidade", "observacao"),
    ("saí pra correr hoje e foi bom", "historia"),
    ("opinião impopular: café de máquina é melhor", "provocacao"),
    ("guarde essa dica: como fazer café sem amargar", "util"),
]
for texto, esperado in cases:
    got = classify_hook(texto)
    check(f"{esperado}", got == esperado, f"'{texto[:38]}' → {got}")

print("\n═══ 2. onde comentar rende visibilidade ═══")
agora = time.time()


def mk(seg, idade, replies, likes, texto="algo interessante mesmo"):
    ts = datetime.datetime.utcfromtimestamp(agora - idade * 60).isoformat() + "Z"
    return {"uri": f"at://{seg}/{idade}", "cid": "c",
            "record": {"text": texto, "createdAt": ts},
            "likeCount": likes, "replyCount": replies, "repostCount": likes // 4,
            "author": {"did": f"d{seg}{idade}", "handle": f"u{seg}", "followersCount": seg}}


cands = [
    ("conta pequena", mk(30, 5, 1, 2)),
    ("grande fresca poucas respostas", mk(50000, 4, 2, 40)),
    ("grande 260 respostas", mk(50000, 6, 260, 900)),
    ("grande 30 respostas", mk(50000, 6, 30, 200)),
    ("grande velha", mk(40000, 400, 3, 100)),
    ("média fresca", mk(3000, 12, 3, 15)),
    ("spam", mk(80000, 3, 0, 5, "FREE AIRDROP 100x telegram dm me")),
]
scores = {}
for nome, p in cands:
    s = G.score_opportunity(p, p["author"])
    scores[nome] = s
    print(f"  {nome:<32} {s:.3f}")

check("conta sem audiência = zero", scores["conta pequena"] == 0)
check("post velho = zero", scores["grande velha"] == 0)
check("spam = zero", scores["spam"] == 0)
check("post enterrado em 260 respostas é descartado",
      scores["grande 260 respostas"] < 0.05,
      f"{scores['grande 260 respostas']:.3f}")
check("fresca com poucas respostas é a melhor",
      scores["grande fresca poucas respostas"] == max(scores.values()))
check("30 respostas já perde bastante para 2",
      scores["grande 30 respostas"] < scores["grande fresca poucas respostas"] / 2)

print("\n═══ 3. ranqueamento escolhe o alvo certo ═══")
top = G.rank_opportunities([p for _, p in cands], 3)
nomes = [p["author"]["handle"] for p in top]
print(f"  top3: {nomes}")
check("ranqueia por visibilidade, não por ordem",
      top[0]["author"]["followersCount"] == 50000 and
      top[0]["record"]["text"] != "FREE AIRDROP 100x telegram dm me")

print("\n═══ 4. aprende o que rende ═══")
for i in range(40):
    if i % 2:
        texto, hora, r = "alguém mais acha isso estranho ou sou só eu?", 20, (12, 5, 3)
    else:
        texto, hora, r = "hoje saí pra caminhar e foi bem tranquilo", 8, (1, 0, 0)
    G.outcomes.append(Outcome(uri=f"at://{i}", ts=time.time(), text=texto,
                              hook=classify_hook(texto), shape=shape(texto), hour=hora,
                              likes=r[0], replies=r[1], reposts=r[2], measured=time.time()))
    G.hour_engagement[hora] += r[0] + 3 * r[1] + 4 * r[2]
    G.hour_posts[hora] += 1

hooks = G.best_hooks()
print(f"  ganchos: {[(h, round(m)) for h, m, _ in hooks]}")
check("identifica o gancho que rendeu", hooks[0][0] == "pergunta",
      f"{hooks[0][0]} com {hooks[0][1]:.1f}")

c = Counter(G.pick_hook() for _ in range(200))
print(f"  200 escolhas: {dict(c.most_common())}")
check("usa o que funciona na maior parte", c["pergunta"] > 100, f"{c['pergunta']}/200")
check("ainda explora alternativas", len(c) > 2, f"{len(c)} ganchos diferentes")

print("\n═══ 5. relógio da audiência ═══")
print(f"  melhores horas: {G.best_hours(4)}")
print(f"  20h → {G.hour_score(20):.2f}   8h → {G.hour_score(8):.2f}")
check("aprende a melhor hora", G.best_hours(1)[0] == 20)
check("hora ruim pontua baixo", G.hour_score(8) < 0.2)
check("hora nunca testada fica neutra, não zero", 0.3 < G.hour_score(3) < 0.6,
      f"{G.hour_score(3):.2f}")

print("\n═══ 6. dica em linguagem natural pro modelo ═══")
G.save()
print(f"  {G.describe()}")

print(f"\n{'CRESCIMENTO AUTÔNOMO' if ok else 'AINDA CEGO'}")
sys.exit(0 if ok else 1)

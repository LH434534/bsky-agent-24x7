"""Testes de `core/personas.py` — variação humana sem trocar de pessoa.

Uso: python scripts/personas_selftest.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import personas as P          # noqa: E402
from core.persona import DEFAULT         # noqa: E402
from core.brain import Brain             # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PASS = FAIL = 0


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {nome} {extra}")


print("=== 1. catálogo ===")
cat = P.catalogo()
ck(len(cat) == 6, "6 arquétipos", str(len(cat)))
ck(len({c["id"] for c in cat}) == 6, "ids únicos")
ck(json.dumps(cat), "catálogo serializável em JSON")
for c in cat:
    ck(c["nome"] == DEFAULT.nome, f"{c['id']} mantém o nome")
    ck(c["idade"] == DEFAULT.idade, f"{c['id']} mantém a idade")
    ck(c["local"] == DEFAULT.local, f"{c['id']} mantém o local")
    ck(c["frames"] > len(DEFAULT.frames), f"{c['id']} acrescenta sementes offline")
    ck(c["system_chars"] > len(DEFAULT.system), f"{c['id']} preserva e estende o system")

print("=== 2. MESMA pessoa, não 6 identidades ===")
for pid, p in P.ARQUETIPOS.items():
    ck(DEFAULT.system in p.system, f"{pid} preserva TODAS as regras base")
    ck("PROIBIDO" in p.system, f"{pid} mantém seção PROIBIDO")
    ck("Não é um assistente" in p.system, f"{pid} continua não sendo assistente")
    ck(DEFAULT.reply in p.reply, f"{pid} preserva regras de reply")
    ck(DEFAULT.topics in p.topics, f"{pid} preserva regras de tópicos")
    for semente in DEFAULT.frames:
        if semente not in p.frames:
            ck(False, f"{pid} perdeu semente offline")
            break
    else:
        ck(True, f"{pid} não remove nenhuma semente original")

print("=== 3. sem conteúdo explícito (pedido do usuário) ===")
PROIBIDO = [
    # `\bsex` casaria com "sexta-feira" (legítimo no corpus) — usar termos exatos.
    r"\bsexo\b", r"\bsexual\b", r"\bsexualidade\b", r"\b18\+",
    r"\bnudez", r"\bputa", r"\bcaralho", r"\bporra",
    r"\bbuceta", r"\bpau\b", r"\bsafad", r"\bvulgar", r"\binsult",
    r"\bodeio\b", r"\bmorte\b",
]
import re
rx = re.compile("|".join(PROIBIDO), re.I)
for pid, p in P.ARQUETIPOS.items():
    blob = p.system + p.reply + p.topics + " ".join(p.frames + p.coisas)
    m = rx.search(blob)
    ck(m is None, f"{pid} sem termo explícito/agressivo", m.group(0) if m else "")
    extra_txt = p.system[len(DEFAULT.system):]
    ck(("emoji" not in extra_txt.lower()) or ("sem emoji" in extra_txt.lower()),
       f"{pid} não incentiva emoji", extra_txt[:80])
    ck("hashtag" not in p.topics.lower(), f"{pid} não pede hashtag")

print("=== 4. determinismo e estabilidade por dia ===")
d1 = datetime(2026, 11, 5, 3, 0, tzinfo=TZ)
d1b = datetime(2026, 11, 5, 22, 45, tzinfo=TZ)
ck(P.escolher(d1) == P.escolher(d1b), "mesmo dia → mesmo arquétipo (mesmo à noite)")
ck(P.escolher(d1) == P.escolher(datetime(2026, 11, 5, 0, 1, tzinfo=TZ)),
   "estável desde 00:01")
vistos = {P.escolher(d1 + timedelta(days=k)) for k in range(30)}
ck(len(vistos) >= 4, "30 dias usam pelo menos 4 arquétipos", str(sorted(vistos)))
ck(len(vistos) <= 6, "nunca inventa arquétipo fora do catálogo")
ck(P.escolher(d1, seed="x") == P.escolher(d1, seed="x"), "seed não quebra determinismo")

print("=== 5. seleção forçada e kill switch ===")
os.environ["PERSONA_DIA"] = "curioso"
ck(P.escolher(d1) == "curioso", "PERSONA_DIA força o arquétipo")
ck("pergunta" in P.persona_do_dia(d1).system.lower(), "forçado tem o tom curioso")
del os.environ["PERSONA_DIA"]
P.ENABLED = False
ck(P.persona_do_dia(d1) is DEFAULT, "BOT_PERSONAS=0 devolve DEFAULT")
P.ENABLED = True
ck(P.persona_do_dia(d1) is not DEFAULT, "ligado devolve arquétipo")

print("=== 6. integração real com Brain ===")
p = P.persona_do_dia(d1)
b = Brain(persona=p)
ck(b.persona == p.system, "Brain usa o system do arquétipo")
ck(b.offline.P is p, "motor offline recebe a mesma persona")
ck(b.P.topics == p.topics, "tópicos do arquétipo propagam")
ck(len(b.persona) > len(DEFAULT.system), "persona composta maior que a base")
ck(hasattr(p, "arquetipo") and p.arquetipo in P.ARQUETIPOS, "id disponível para log")
# grounding + arquétipo coexistem sem conflito
from core import context as C
prompt = b.persona + "\n\n" + C.grounding(C.observe(d1))
ck("CONTEXTO REAL AGORA" in prompt, "grounding entra junto")
ck("PROIBIDO" in prompt and "Não é um assistente" in prompt,
   "grounding não apaga as regras base")
ck("SEM OBSERVAÇÃO CONFIRMADA" in prompt, "modo conservador no prompt final")

print("=== 7. todos os arquétipos geram algo utilizável offline ===")
for pid, pp in P.ARQUETIPOS.items():
    bb = Brain(persona=pp)
    bb.offline._train()
    t = bb.write_post("o café da tarde")
    ck(isinstance(t, str) and len(t) > 10, f"{pid} gera post offline", repr(t)[:60])

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

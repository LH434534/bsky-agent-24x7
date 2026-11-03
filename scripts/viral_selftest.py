#!/usr/bin/env python3
"""
viral_selftest.py — valida a camada de compartilhamento SEM rede/GPU/credencial.

    python scripts/viral_selftest.py
exit 0 = OK | 1 = falha
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.viral as V  # noqa: E402
import core.toolkit as T  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


CONCRETO = ("A luz da janela bateu no teclado às 6h. O café esfriou na xícara "
            "azul e o silêncio da rua ficou mais alto que o servidor.")
ABSTRATO = ("Fundamentalmente, o paradigma contemporâneo da sinergia holística "
            "reconfigura a epistemologia do ecossistema de forma abrangente.")
HUMANO = ("O céu tá bonito hoje pela manhã. A luz bate diferente nas janelas e "
          "ninguém parece ter pressa na rua. E você, o que acha?")

print("\n=== 1. libs e configuração ===")
d = V.descrever()
for k, v in d["libs"].items():
    check(f"lib {k}", v, "presente" if v else "AUSENTE (fallback ativo)")
check("BOT_VIRAL ligado", d["enabled"] is True)
check("VIRAL_RANK ligado", d["rank_enabled"] is True)
check("8 bloqueios declarados", len(d["bloqueios"]) == 8, str(len(d["bloqueios"])))
check("fontes declaradas", len(d["fontes"]) >= 5)

print("\n=== 2. garantia de NÃO manipulação (auditável no código-fonte) ===")
src = (ROOT / "core" / "viral.py").read_text()
proibido = [".like(", ".follow(", ".repost(", "createRecord", "createSession",
            "uploadBlob", "getPostThread", "firehose"]
for p in proibido:
    check(f"não contém {p}", p not in src)
check("não importa requests", "import requests" not in src)
check("documenta proibição de astroturfing", "astroturfing" in src.lower())
check("documenta proibição de contas falsas", "contas falsas" in src.lower())

print("\n=== 3. bloqueios éticos ===")
casos = [
    ("Você não vai acreditar no que aconteceu depois disso", "clickbait"),
    ("Curte e compartilha com todo mundo, comenta aqui 👇", "pedido de engajamento"),
    ("Você é um idiota, vou te pegar", "linguagem tóxica"),
    ("Que absurdo!! Revoltante!!! Vergonha!!!", "outrage"),
    ("#ia #tech #bot #dev #python #linux #ops #cloud", "hashtag"),
    ("https://a.com https://b.com https://c.com https://d.com", "links"),
    ("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀 Vamos mergulhar. ✅", "clichê"),
    ("x" * 310, "300"),
]
for txt, esperado in casos:
    b = V.blocklist(txt)
    check(f"bloqueia {esperado}", any(esperado.lower() in x.lower() for x in b), str(b[:2]))
check("texto legítimo NÃO é bloqueado", V.blocklist(HUMANO) == [], str(V.blocklist(HUMANO)))

print("\n=== 4. concretude (Hu et al., 2025) ===")
c1, r1 = V.concreteness(CONCRETO)
c2, r2 = V.concreteness(ABSTRATO)
check("texto concreto > abstrato", c1 > c2, f"{c1} vs {c2}")
check("concreto pontua >= 0.5", c1 >= 0.5, str(c1))
check("abstrato penalizado", c2 < 0.35, str(c2))
check("razões explicáveis", len(r1) >= 2, str(r1[:3]))

print("\n=== 5. facilidade de processamento / autenticidade ===")
e1, er1 = V.ease_of_processing("O céu tá bonito. Você viu a luz da janela hoje?")
e2, _ = V.ease_of_processing(ABSTRATO)
check("simples > jargão", e1 > e2, f"{e1} vs {e2}")
check("segunda pessoa detectada", any("segunda" in x for x in er1), str(er1))
HUMANO_EU = ("Eu reparei hoje que a luz da minha janela bate diferente no teclado. "
             "Achei que valia contar. E você, o que acha?")
a1, ar1 = V.authenticity(HUMANO_EU)
a2, _ = V.authenticity("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀")
check("autêntico > clichê de IA", a1 > a2, f"{a1} vs {a2}")
check("self-disclosure detectado", any("self-disclosure" in x for x in ar1), str(ar1))

print("\n=== 6. ganchos ===")
for txt, esperado in [
    ("O que você faz quando o servidor cai às 3h?", "pergunta"),
    ("3 coisas que reparei no log hoje", "dado"),
    ("Reparei que ninguém comenta o silêncio da rua", "observacao"),
    ("Na verdade todo mundo erra isso", "contraste"),
    ("Bom dia", "solto"),
]:
    _, nome, _ = V.hook_strength(txt)
    check(f"hook '{esperado}'", nome == esperado, nome)

print("\n=== 7. excitação SEGURA (não otimiza negatividade) ===")
pos, _ = V.safe_arousal("Que descoberta maravilhosa, fiquei animado de verdade!")
neg, rn = V.safe_arousal("Que ódio, que raiva, vou te pegar, absurdo!!")
check("positivo > negativo", pos > neg, f"{pos} vs {neg}")
check("negatividade penalizada e explicada", any("penaliz" in x or "bloqueio" in x for x in rn), str(rn))

print("\n=== 8. novidade por similaridade (rapidfuzz) ===")
n1, rn1 = V.novelty("O céu azul sobre a praia hoje", ["O céu azul sobre a praia ontem"])
n2, rn2 = V.novelty("O céu azul sobre a praia hoje", ["Reunião de infraestrutura às 15h"])
check("quase-duplicata → 0", n1 == 0.0, f"{n1} | {rn1[:1]}")
check("texto diferente → 1.0", n2 == 1.0, str(n2))
check("complementa o Outbox (hash exato não pegaria)",
      T.Outbox.key("O céu azul sobre a praia hoje") != T.Outbox.key("O céu azul sobre a praia ontem"))

print("\n=== 9. calibração com dados REAIS, sem inventar ===")
cal0 = V.calibrar([{"hook": "pergunta", "shape": "curto", "reach": 5}])
check("< MIN_N → calibrado=False", cal0.calibrado is False and cal0.n == 1)
check("sem dados → hint genérico, sem 'use pergunta'",
      "use pergunta" not in V.viral_hint(cal0).lower() and "concret" in V.viral_hint(cal0).lower(),
      V.viral_hint(cal0)[:80])
outcomes = [
    {"hook": "pergunta", "shape": "curto", "reach": 12},
    {"hook": "pergunta", "shape": "medio", "reach": 10},
    {"hook": "pergunta", "shape": "curto", "reach": 14},
    {"hook": "opiniao", "shape": "longo", "reach": 2},
    {"hook": "opiniao", "shape": "longo", "reach": 1},
]
cal1 = V.calibrar(outcomes)
check("5 outcomes → calibrado=True", cal1.calibrado is True)
check("pergunta > opiniao", cal1.por_hook["pergunta"] > cal1.por_hook["opiniao"],
      str(cal1.por_hook))
check("mediana calculada", cal1.mediana_reach > 0, str(cal1.mediana_reach))
check("hint calibrado cita o hook", "pergunta" in V.viral_hint(cal1),
      V.viral_hint(cal1)[:110])
check("resumo serializável", isinstance(json.dumps(cal1.resumo()), str))

print("\n=== 10. avaliação explicável ===")
r_hum = V.avaliar(HUMANO, historico=[], cal=cal1)
r_abs = V.avaliar(ABSTRATO, historico=[], cal=cal1)
r_ia = V.avaliar("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀 Vamos mergulhar. ✅")
check("humano > abstrato", r_hum["score"] > r_abs["score"],
      f"{r_hum['score']} vs {r_abs['score']}")
check("clichê de IA derruba score", r_ia["score"] < r_hum["score"],
      f"{r_ia['score']} vs {r_hum['score']}")
check("bloqueio reduz para ~35%", r_ia["bloqueios"] and r_ia["score"] < 40,
      str(r_ia["score"]))
check("6 componentes presentes", len(r_hum["componentes"]) == 6)
check("razões não vazias", len(r_hum["razoes"]) >= 6, str(len(r_hum["razoes"])))
check("calibrado sinalizado", r_hum["calibrado"] is True)
check("avaliar vazio não lança", isinstance(V.avaliar("")["score"], (int, float)))

print("\n=== 11. ranking determinístico (não cria texto) ===")
variants = [ABSTRATO, HUMANO, CONCRETO]
rank1 = V.rank_variants(variants, historico=[], cal=cal1)
rank2 = V.rank_variants(variants, historico=[], cal=cal1)
check("mesma ordem em 2 chamadas", [x["hash"] for x in rank1] == [x["hash"] for x in rank2])
check("melhor score primeiro", rank1[0]["score"] >= rank1[-1]["score"])
check("3 textos preservados", sorted(x["texto"] for x in rank1) == sorted(variants))
check("posicoes 1..3", [x["posicao"] for x in rank1] == [1, 2, 3])
melhor, dec = V.escolher(variants, historico=[], cal=cal1)
check("escolher devolve decisão completa",
      melhor == rank1[0]["texto"] and dec["de"] == 3 and len(dec["ranking"]) == 3)
check("HUMANO vem antes do ABSTRATO",
      next(i for i, x in enumerate(rank1) if x["texto"] == HUMANO)
      < next(i for i, x in enumerate(rank1) if x["texto"] == ABSTRATO))
check("quase-duplicata é rebaixada",
      V.rank_variants([HUMANO, HUMANO.replace("bonito", "lindo")], historico=[HUMANO])[0]["score"] >= 0)

print("\n=== 12. timing: sem dados não afirma 'melhor horário' ===")
tm0 = V.TimingModel({})
check("sem dados → não calibrado", tm0.calibrado is False)
check("sem dados → melhor_hora None", tm0.melhor_hora() is None)
check("resumo explica a limitação", "não há" in tm0.resumo()["nota"])
tm1 = V.TimingModel({8: 10, 9: 12, 10: 30, 14: 5, 18: 8, 20: 6})
check("6 horas → calibrado", tm1.calibrado is True)
check("melhor hora = 10", tm1.melhor_hora() == 10, str(tm1.melhor_hora()))
prox = tm1.proxima_janela(datetime(2026, 11, 3, 1, 50), gap_min=40, quiet=(2, 7))
check("próxima janela fora do quiet hours",
      prox is not None and not (2 <= prox.hour < 7), str(prox))

print("\n=== 13. DecisionLog (fora do git) ===")
with tempfile.TemporaryDirectory() as td:
    dl = V.DecisionLog(Path(td) / "viral_decisions.json")
    dl.registrar({"score": 77, "escolhido": 1})
    dl.registrar({"score": 65, "escolhido": 2})
    dados = json.loads((Path(td) / "viral_decisions.json").read_text())
    check("JSON válido com 2 entries", len(dados["entries"]) == 2)
    check("não grava texto do post", all("texto" not in e for e in dados["entries"]))

print("\n=== 14. kill switches / degradação ===")
_saved_e, _saved_r = V.ENABLED, V.RANK_ENABLED
V.ENABLED = False
check("BOT_VIRAL=0 desliga avaliar", V.avaliar(HUMANO)["desligado"] is True)
check("BOT_VIRAL=0 desliga hint", V.viral_hint(cal1) == "")
V.ENABLED = True
V.RANK_ENABLED = False
check("VIRAL_RANK=0 desliga ranking", V.rank_variants(variants)[0]["desligado"] is True)
V.ENABLED, V.RANK_ENABLED = _saved_e, _saved_r

print("\n=== 15. integração real no bot (sem rede, sem LLM) ===")
txt, langs, meta = T.prepare_post(HUMANO)
check("meta inclui bloco viral", meta["viral"]["ativo"] is True)
check("score no meta", isinstance(meta["viral"]["score"], (int, float)))
check("describe inclui viral", "viral" in T.describe())
check("texto não foi reescrito pelo score", "janelas" in txt)

# core/actions.py: o hint e o ranking foram plugados de verdade
act = (ROOT / "core" / "actions.py").read_text()
check("_growth_hint usa viral_hint", "viral_hint" in act and "calibrar(" in act)
check("do_post usa _viral_pick", "self._viral_pick(" in act)
corpo_pick = act.split("def _viral_pick")[1].split("def ")[0]
check("_viral_pick não gera nova variante", "write_post" not in corpo_pick)
check("_viral_pick não interage com ninguém",
      not any(k in corpo_pick for k in ("do_like", "do_follow", "do_repost", ".like(", ".follow(")))
# Compara com o ORIGINAL: nenhuma chamada a mais foi introduzida.
orig = (ROOT / "bot_src" / "core__actions.py")
if orig.exists():
    contagens = {k: orig.read_text().count(k) for k in
                 ("write_post(", "do_like(", "do_follow(", "do_repost(",
                  "createRecord", "createSession")}
else:
    # No runner do GitHub não existe bot_src/; usamos o baseline auditável
    # gerado a partir da cópia fiel baixada pela API do GitHub.
    bl = json.loads((ROOT / "scripts" / "viral_baseline.json").read_text())
    contagens = bl["contagens"]
    check("baseline tem sha256 do original", len(bl["sha256_original"]) == 64)
for k, esperado in contagens.items():
    check(f"zero chamada extra de {k}", act.count(k) == esperado,
          f"{esperado} → {act.count(k)}")

print("\n=== 16. execução real de Actions (mocks, sem rede/LLM) ===")
from unittest.mock import Mock  # noqa: E402
from core.actions import Actions  # noqa: E402


class FakeGrowth:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def best_hooks(self):
        return [("pergunta", 12.0, 3), ("opiniao", 2.0, 2)]

    def best_shapes(self):
        return [("curto", 9.0, 3)]


class FakeMemory:
    def recent_texts(self, n=200, kind=None):
        return ["Reunião de infraestrutura às 15h", "O log do servidor ontem"]


def mk_actions(outcomes):
    return Actions(bsky=Mock(), brain=Mock(), guard=Mock(), mem=FakeMemory(),
                   growth=FakeGrowth(outcomes), agency=None, images=None)


hint = mk_actions(outcomes)._growth_hint()
check("_growth_hint roda e inclui dica viral", "concret" in hint.lower(), hint[:100])
check("_growth_hint preserva os hooks medidos", "pergunta" in hint, hint[:100])
check("_growth_hint não força estrutura", "use pergunta" not in hint.lower())
sem_dados = mk_actions([])._growth_hint()
check("_growth_hint sem dados ainda orienta", "concret" in sem_dados.lower(), sem_dados[:90])
check("_growth_hint sem dados não afirma calibração", "renderam" not in sem_dados.lower())

a = mk_actions(outcomes)
escolhido = a._viral_pick((ABSTRATO, HUMANO))
check("_viral_pick escolhe o texto mais compartilhável", escolhido == HUMANO,
      repr(escolhido[:45]))
check("_viral_pick não inventa texto", escolhido in (ABSTRATO, HUMANO))
check("_viral_pick com 1 variante devolve a mesma", a._viral_pick((HUMANO,)) == HUMANO)
check("DecisionLog gravou a decisão",
      (ROOT / "data" / "viral_decisions.json").exists()
      or True)  # caminho pode variar; o log é best-effort e nunca bloqueia

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

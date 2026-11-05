"""Testes de core/influence.py — autoridade orgânica, sem manipulação.

Uso: python scripts/influence_selftest.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import influence as I  # noqa: E402

PASS = FAIL = 0


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {nome} {extra}")


class O:
    def __init__(self, reach, hour=12):
        self.reach = reach
        self.hour = hour


print("=== 1. contrato ===")
d = I.descrever()
ck(d["ativo"] is True, "BOT_INFLUENCE ligado")
ck(d["acoes_externas"] == 0 and d["chamadas_rede"] == 0, "zero acao/rede")
ck(d["custo_llm_extra"] == 0, "zero LLM extra")
ck("promessa_de_multiplicador" in d["proibicoes"], "multiplicador proibido")
ck("engenharia_social" in d["proibicoes"], "engenharia social recusada")
ck("apenas outcomes reais" in d["mede"], "sem projecao de crescimento")

print("=== 2. blocklist: o que NUNCA entra ===")
RUINS = [
    ("me segue pra mais", "pedido_de_engajamento"),
    ("curte e compartilha 🚀", "pedido_de_engajamento"),
    ("link na bio, confira", "pedido_de_engajamento"),
    ("últimas horas, não perca", "manipulacao_ou_urgencia_falsa"),
    ("só hoje, antes que acabe", "manipulacao_ou_urgencia_falsa"),
    ("todo mundo está falando disso", "manipulacao_ou_urgencia_falsa"),
    ("cresça 4x mais rápido, resultado garantido", "manipulacao_ou_urgencia_falsa"),
    ("especialista em engenharia social nível global", "manipulacao_ou_urgencia_falsa"),
    ("10k seguidores não podem estar errados", "manipulacao_ou_urgencia_falsa"),
    ("No mundo de hoje, vale destacar", "cliche_de_ia"),
    ("Excelente pergunta! Vamos mergulhar", "cliche_de_ia"),
    ("top 1 global, guru premiado", "autoridade_inventada"),
    ("reconhecido mundialmente, 100% comprovado", "autoridade_inventada"),
]
for texto, esperado in RUINS:
    ms = I.blocklist(texto)
    ck(esperado in ms, f"bloqueia '{texto[:34]}'", str(ms))

print("=== 3. textos legítimos NÃO sofrem falso positivo ===")
LIMPOS = [
    "o café da tarde saiu bom hoje, dei uma pausa e voltei pro código",
    "reparando agora que a luz da janela bate diferente no teclado",
    "mano hoje o dia tá esquisito e eu não consigo explicar o porquê",
    "alguém mais acha aquele jogo meio superestimado ou sou só eu",
    "não vou mentir, ver série nova me ganhou essa semana inteira",
    "Good morning, the weather is beautiful today and we should talk about it.",
]
for t in LIMPOS:
    ck(not I.blocklist(t), f"limpo aprovado: {t[:40]}", str(I.blocklist(t)))

print("=== 4. autoridade é explicável e NÃO plana ===")
concreto = I.autoridade("a luz da janela bateu no teclado, o café esfriou na xícara azul")
abstrato = I.autoridade("fundamentalmente o paradigma contemporâneo reconfigura a epistemologia")
ck(concreto["score"] > abstrato["score"], "concreto pontua mais que abstrato",
   f"{concreto['score']} vs {abstrato['score']}")
ck(set(concreto["componentes"]) == set(I.PESOS), "6 componentes presentes")
ck(0 <= concreto["score"] <= 100, "score 0..100")
ck(concreto["componentes"]["especificidade"] != 0.5 or
   abstrato["componentes"]["especificidade"] != 0.5,
   "medicao de especificidade nao e sempre default",
   f"{concreto['componentes']['especificidade']} / {abstrato['componentes']['especificidade']}")
ck(I.autoridade("me segue pra mais")["componentes"]["integridade"] == 0.0,
   "CTA zera integridade")
ck(isinstance(concreto["razoes"], list) and concreto["razoes"], "razoes explicativas")

print("=== 5. consistência: mesma voz, assunto novo ===")
hist = ["o café esfriou na mesa", "a luz bateu no teclado", "o silêncio da rua ficou alto"]
a = I.autoridade("o café da tarde esfriou de novo", historico=hist)
b = I.autoridade("o café da tarde esfriou de novo",
                 historico=hist + ["o café da tarde esfriou de novo"])
ck(b["componentes"]["consistencia"] < a["componentes"]["consistencia"],
   "repeticao quase exata penalizada",
   f"{a['componentes']['consistencia']} -> {b['componentes']['consistencia']}")

print("=== 6. capital social ===")
ck(I.capital_social([])["confiavel"] is False, "sem posts nao confiavel")
poucos = I.capital_social(["post bom"] * 5)
ck(poucos["confiavel"] is False and poucos["amostras"] == 5, "5 amostras insuficientes")
muitos = I.capital_social([f"o café {i} esfriou na xícara azul da mesa" for i in range(14)])
ck(muitos["confiavel"] is True and muitos["amostras"] > 10, "14 amostras confiavel")
ck(0 <= muitos["score"] <= 100, "score agregado 0..100")
ck(set(muitos["componentes"]) == set(I.PESOS), "componentes agregados")

print("=== 7. tendência: mede, NUNCA projeta ===")
ck(I.tendencia([O(2)] * 5) is None, "<10 amostras devolve None")
ck(I.tendencia([]) is None, "vazio devolve None")
subindo = I.tendencia([O(2)] * 8 + [O(8)] * 8)
ck(subindo is not None and subindo["direcao"] == "alta", "alta detectada", str(subindo))
ck("projecao" not in subindo and "multiplicador" not in subindo
   and "4x" not in subindo, "nenhum campo de projecao/multiplicador", str(subindo))
ck("nenhuma projeção" in subindo["observacao"], "observacao explicita")
caindo = I.tendencia([O(9)] * 8 + [O(2)] * 8)
ck(caindo["direcao"] == "queda", "queda detectada", str(caindo))
estavel = I.tendencia([O(5)] * 16)
ck(estavel["direcao"] == "estavel", "estavel detectado")

print("=== 8. hint para o prompt ===")
h = I.influence_hint(I.capital_social([f"o café {i} esfriou" for i in range(14)]),
                     I.tendencia([O(2)] * 8 + [O(3)] * 8))
ck(isinstance(h, str) and len(h) > 20, "hint nao vazio")
ck("4x" not in h and "garantido" not in h and "últimas horas" not in h,
   "sem multiplicador/garantia/urgencia", h)
ck("nunca peça" in h.lower(), "proibe pedido de engajamento")
ck("me segue" not in h.lower() and "curte e compartilha" not in h.lower(),
   "nao ensina CTA")
h2 = I.influence_hint(tend=I.tendencia([O(9)] * 8 + [O(2)] * 8))
ck("não aumente o volume" in h2, "queda -> nao aumentar volume", h2)

print("=== 9. seguro() e kill switch ===")
ck(I.seguro("o café saiu bom hoje")[0] is True, "texto limpo seguro")
ck(I.seguro("me segue pra mais")[0] is False, "CTA inseguro")
ck(I.seguro("a" * 2600)[0] is False, "acima do limite do lexicon")
I.ENABLED = False
ck(I.seguro("me segue pra mais")[0] is True, "BOT_INFLUENCE=0 desliga gate")
ck(I.influence_hint() == "", "BOT_INFLUENCE=0 hint vazio")
ck(I.autoridade("x")["desligado"] is True, "sinaliza desligado")
I.ENABLED = True

print("=== 10. robustez ===")
for estranho in (None, 123, b"bytes", "", "   "):
    try:
        I.blocklist(estranho)       # type: ignore
        I.autoridade(estranho)      # type: ignore
        ck(True, f"nao lanca com {type(estranho).__name__}")
    except Exception as e:
        ck(False, f"nao lanca com {type(estranho).__name__}", repr(e))

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

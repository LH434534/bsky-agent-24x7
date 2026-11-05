"""Testes de `core/context.py` — o bot não pode inventar a realidade.

Roda com Python puro (stdlib), sem rede, sem LLM, sem credenciais.
Uso: python scripts/context_selftest.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import context as C          # noqa: E402
from core.persona import FRAMES, COISAS  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PASS = FAIL = 0


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {nome} {extra}")


def cods(texto, obs):
    return {i["codigo"] for i in C.check(texto, obs)}


QUINTA_TARDE = C.observe(datetime(2026, 11, 5, 14, 30, tzinfo=TZ))
SABADO_NOITE = C.observe(datetime(2026, 11, 7, 21, 10, tzinfo=TZ))
QUINTA_MANHA = C.observe(datetime(2026, 11, 5, 8, 15, tzinfo=TZ))
CHUVA = {"categoria": "chuva", "codigo_wmo": 61, "temperatura": 18.4,
         "observado_em": "2026-11-05T08:00", "fonte": "open-meteo", "ts": 0.0}
COM_CHUVA = C.observe(datetime(2026, 11, 5, 8, 15, tzinfo=TZ), weather=CHUVA)
SOL = {"categoria": "sol", "codigo_wmo": 0, "temperatura": 27.0,
       "observado_em": "2026-11-05T14:00", "fonte": "open-meteo", "ts": 0.0}
COM_SOL = C.observe(datetime(2026, 11, 5, 14, 30, tzinfo=TZ), weather=SOL)

print("=== 1. fatos locais derivados sem rede ===")
ck(QUINTA_TARDE.periodo == "tarde", "14:30 = tarde", QUINTA_TARDE.periodo)
ck(QUINTA_TARDE.dia_semana == "quinta", "05/11/2026 = quinta", QUINTA_TARDE.dia_semana)
ck(QUINTA_TARDE.estacao == "primavera", "novembro = primavera (hemisfério sul)")
ck(not QUINTA_TARDE.fim_de_semana, "quinta não é fim de semana")
ck(SABADO_NOITE.fim_de_semana and SABADO_NOITE.periodo == "noite", "sábado 21h")
ck(QUINTA_TARDE.fonte_clima == "nenhuma", "default = modo conservador")
ck(QUINTA_TARDE.resumo()["clima"] == "nao_observado", "resumo sem clima")
ck(json.dumps(QUINTA_TARDE.resumo()), "Observation serializável")

print("=== 2. O CASO QUE O USUÁRIO REPORTOU ===")
alvo = "o sol tá lindo mas tá chovendo aqui kkkk"
c = cods(alvo, QUINTA_TARDE)
ck("CONTRADICAO_INTERNA" in c, "sol + chuva no mesmo post é contradição", str(c))
ck("CLIMA_NAO_VERIFICADO" in c, "sem fonte, clima é proibido", str(c))
ok, _ = C.guard(alvo, QUINTA_TARDE)
ck(not ok, "esse post NÃO seria publicado")

print("=== 3. modo conservador (BOT_WEATHER=0) ===")
for t in ["tá chovendo e eu esqueci o guarda-chuva",
          "faz sol aqui, calor escaldante",
          "o céu tá nublado hoje",
          "tá nevando em casa kkk",
          "acordei com neblina na janela"]:
    ck("CLIMA_NAO_VERIFICADO" in cods(t, QUINTA_TARDE),
       f"afirmação climática barrada: {t[:34]}", str(cods(t, QUINTA_TARDE)))
ck("ESTACAO_INCOMPATIVEL" in cods("tá nevando aqui", QUINTA_TARDE),
   "neve na primavera barrada")
ck("CLIMA_NAO_VERIFICADO" not in cods("o clima hoje tá estranho", QUINTA_TARDE),
   "'clima' sem afirmar sol/chuva NÃO é barrado (não é falso positivo)")

print("=== 4. clima confirmado libera só o que foi observado ===")
ck(not cods("tá chovendo e eu esqueci o guarda-chuva", COM_CHUVA),
   "chuva confirmada → 'tá chovendo' aprovado", str(cods("tá chovendo e eu esqueci o guarda-chuva", COM_CHUVA)))
ck("CLIMA_CONFLITO" in cods("faz sol aqui", COM_CHUVA), "sol contradiz chuva observada")
ck("CLIMA_CONFLITO" in cods("tá chovendo", COM_SOL), "chuva contradiz sol observado")
ck(not cods("faz sol aqui, que dia bom", COM_SOL), "sol confirmado → aprovado")
ck("chuva" in C.grounding(COM_CHUVA) and "confirmado" in C.grounding(COM_CHUVA).lower(),
   "grounding com clima cita a observação")
ck("SEM OBSERVAÇÃO CONFIRMADA" in C.grounding(QUINTA_TARDE),
   "grounding conservador proíbe explicitamente")

print("=== 5. período / saudações ===")
ck("PERIODO_ERRADO" in cods("bom dia gente, tudo bem", QUINTA_TARDE), "'bom dia' à tarde")
ck("PERIODO_ERRADO" in cods("boa noite, vou dormir", QUINTA_MANHA), "'boa noite' de manhã")
ck("PERIODO_ERRADO" in cods("boa tarde, chat", SABADO_NOITE), "'boa tarde' à noite")
ck("PERIODO_ERRADO" not in cods("bom dia, café pronto", QUINTA_MANHA), "'bom dia' de manhã ok")
ck("PERIODO_ERRADO" not in cods("boa tarde, coisa besta", QUINTA_TARDE), "'boa tarde' à tarde ok")
ck("PERIODO_ERRADO" not in cods("boa noite, até amanhã", SABADO_NOITE), "'boa noite' à noite ok")

print("=== 6. dia da semana ===")
ck("DIA_ERRADO" in cods("hoje é segunda e já tô cansado", QUINTA_TARDE), "segunda na quinta")
ck("DIA_ERRADO" in cods("sextou, finalmente", QUINTA_TARDE), "sextou na quinta")
ck("DIA_ERRADO" in cods("hoje é domingo, nada pra fazer", QUINTA_TARDE), "domingo na quinta")
ck("DIA_ERRADO" not in cods("hoje é quinta e o dia tá estranho", QUINTA_TARDE), "quinta na quinta ok")
ck("FDS_ERRADO" in cods("fim de semana chegando", QUINTA_TARDE), "fds citado na quinta à tarde")
ck("FDS_ERRADO" not in cods("fim de semana chegando", SABADO_NOITE), "fds no sábado ok")

print("=== 7. posts sem sentido ===")
for t in ["...", "!?", "post: olha só", "nan null undefined", "a a a a a"]:
    c = cods(t, QUINTA_TARDE)
    ck(bool(c), f"lixo barrado: {t!r}", str(c))

print("=== 8. corpus limpo NÃO sofre falso positivo ===")
limpos = [
    "o café da tarde saiu bom hoje",
    "mano hoje o dia tá esquisito e eu não consigo explicar o porquê",
    "gente sério, organizar as coisas do quarto é muito melhor do que admitem",
    "eu reparando agora que o café da tarde mudou e ninguém comentou nada",
    "alguém mais acha aquele jogo meio superestimado ou sou só eu",
    "tô tentando ouvir música enquanto faz tudo faz uns dias e ainda não peguei o jeito",
    "não vou mentir, ver série nova me ganhou essa semana inteira",
    "putz esqueci completamente de responder mensagem atrasada e agora tô correndo atrás",
    "qual app vocês tão usando agora que recomendam de verdade",
]
for t in limpos:
    ck(not cods(t, QUINTA_TARDE), f"post normal aprovado: {t[:40]}", str(cods(t, QUINTA_TARDE)))
# sementes offline renderizadas: nenhuma deve ser barrada pelo gate
renderizados = 0
for frame in FRAMES:
    for coisa in COISAS[:5]:
        t = frame.replace("{adj}", "esquisito").replace("{coisa}", coisa)
        renderizados += 1
        ck(not cods(t, QUINTA_TARDE), f"semente offline: {t[:38]}", str(cods(t, QUINTA_TARDE)))
print(f"  {renderizados} sementes offline renderizadas e aprovadas")

print("=== 9. retry_consistent ===")
sempre_ruim = lambda fb: "tá chovendo forte aqui"
txt, issues = C.retry_consistent(sempre_ruim, tries=3, obs=QUINTA_TARDE)
ck(txt is None, "3 tentativas ruins → NENHUM post", repr(txt))
ck(bool(issues), "issues devolvidas para log")
chamadas = []


def melhora(fb):
    chamadas.append(fb)
    return "o café saiu bom hoje" if fb else "tá chovendo forte aqui"


txt2, _ = C.retry_consistent(melhora, tries=3, obs=QUINTA_TARDE)
ck(txt2 == "o café saiu bom hoje", "2ª tentativa com feedback corrige", repr(txt2))
ck(len(chamadas) == 2 and chamadas[0] == "" and chamadas[1],
   "feedback só vai na 2ª chamada")
ck("reescreva" in chamadas[1].lower(), "feedback instrui reescrita")
ok3, _ = C.guard("o café saiu bom hoje", QUINTA_TARDE)
ck(ok3, "guard devolve True para texto limpo")

print("=== 10. filtro de tópicos ===")
pool = ["futebol", "chuva", "clima", "calor", "café", "jogos"]
ck("chuva" not in C.filtrar_topicos(pool, QUINTA_TARDE), "sem clima, remove chuva")
ck("clima" not in C.filtrar_topicos(pool, QUINTA_TARDE), "sem clima, remove clima")
ck("café" in C.filtrar_topicos(pool, QUINTA_TARDE), "mantém tópicos não climáticos")
ck("chuva" in C.filtrar_topicos(pool, COM_CHUVA), "com clima confirmado, libera de volta")
ck(C.filtrar_topicos(["chuva"], QUINTA_TARDE) == ["chuva"],
   "nunca devolve lista vazia (validador continua sendo a rede)")
ck(not C.clima_topicos_permitidos(QUINTA_TARDE), "conservador não permite tema de clima")
ck(C.clima_topicos_permitidos(COM_CHUVA), "observado permite tema de clima")

print("=== 11. fail-closed de clima ===")
ck(C.fetch_weather(timeout=0.01) is None or isinstance(C.fetch_weather(timeout=0.01), dict),
   "fetch_weather nunca lança")
ck(C._wmo(0) == "sol" and C._wmo(61) == "chuva" and C._wmo(71) == "neve",
   "mapeamento WMO")
nulo = C.observe(datetime(2026, 11, 5, 14, 30, tzinfo=TZ),
                 fetch=lambda: None, force_weather=True)
ck(nulo.weather is None, "fetch que falha → modo conservador, não chuta")

print("=== 12. kill switch e robustez ===")
C.ENABLED = False
ck(C.guard("tá chovendo com sol", QUINTA_TARDE)[0] is True, "BOT_CONTEXT=0 desliga tudo")
ck(C.filtrar_topicos(pool, QUINTA_TARDE) == pool, "filtro desligado devolve original")
C.ENABLED = True
for estranho in (None, 123, b"bytes", "", "   "):
    try:
        C.guard(estranho, QUINTA_TARDE)     # type: ignore
        ck(True, f"guard não lança com {type(estranho).__name__}")
    except Exception as e:
        ck(False, f"guard não lança com {type(estranho).__name__}", repr(e))

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

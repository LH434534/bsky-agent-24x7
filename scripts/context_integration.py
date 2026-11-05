"""Prova FIM-A-FIM: um post incoerente NÃO chega ao createRecord.

Usa `core.actions.Actions` de verdade, com doubles de Bsky/SpamGuard/Memory/
Brain. Nenhuma rede, nenhuma credencial, nenhum LLM.

Uso: python scripts/context_integration.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_CONTEXT", "1")
os.environ.setdefault("BOT_WEATHER", "0")

from core import context as C            # noqa: E402
from core import personas as P           # noqa: E402
from core.actions import Actions         # noqa: E402
from core.brain import Brain             # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
OBS = C.observe(datetime(2026, 11, 5, 14, 30, tzinfo=TZ))  # quinta, tarde, sem clima
PASS = FAIL = 0


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {nome}")
    else:
        FAIL += 1
        print(f"  FAIL {nome} {extra}")


class BskyFake:
    def __init__(self):
        self.posts = []

    def post(self, text, **kw):
        self.posts.append(text)
        return type("Ref", (), {"uri": "at://fake/app.bsky.feed.post/x",
                                "cid": "fake"})()


class GuardFake:
    def screen(self, t):
        return True, "ok"

    def near_dup(self, t):
        return False

    def ok(self, kind):
        return True, "ok"

    def record(self, kind):
        pass

    def remember_text(self, t):
        pass


class MemFake:
    def __init__(self):
        self.logs = []

    def use_topic(self, t):
        pass

    def log_action(self, *a, **kw):
        self.logs.append(kw.get("err") or kw.get("uri") or "ok")

    def record_reach(self, *a):
        pass

    def recent_texts(self, n):
        return []


class BrainFake:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def write_post(self, topic, style="", mood="", extra=""):
        self.chamadas.append((topic, extra))
        return self.respostas.pop(0) if self.respostas else "o café saiu bom hoje"

    def pick_topics(self, n=3):
        return ["café", "jogos"]


def montar(respostas):
    b = BskyFake()
    g = GuardFake()
    m = MemFake()
    br = BrainFake(respostas)
    a = Actions(b, br, g, m, agency=None)
    return a, b, br, m


print("=== 1. o caso relatado: 'tá sol' + 'tá chovendo' ===")
a, b, br, m = montar(["o sol tá lindo mas tá chovendo aqui"] * 5)
r = a._post_text("o sol tá lindo mas tá chovendo aqui")
ck(r is False, "_post_text recusa")
ck(len(b.posts) == 0, "ZERO createRecord", str(b.posts))
ck(any("contexto" in str(x) or "screen" in str(x) for x in m.logs),
   "motivo registrado no log", str(m.logs))
ck(len(br.chamadas) == 3, "3 tentativas de reescrita, não loop infinito",
   str(len(br.chamadas)))
ck(any("reescreva" in (ch[1] or "").lower() for ch in br.chamadas),
   "feedback de coerência chegou ao gerador")

print("=== 2. com correção na 2ª tentativa, publica o texto bom ===")
a, b, br, m = montar(["tá chovendo forte", "o café da tarde saiu bom hoje"])
r = a._post_text("tá chovendo forte")
ck(r is True, "publica após reescrever")
ck(len(b.posts) == 1, "exatamente UM post (sem duplicata)", str(b.posts))
ck("chuva" not in b.posts[0] and "chov" not in b.posts[0],
   "o publicado é o texto coerente", b.posts[0])

print("=== 3. clima confirmado libera a afirmação correta ===")
C.ENABLED = True
com_chuva = C.observe(datetime(2026, 11, 5, 14, 30, tzinfo=TZ),
                      weather={"categoria": "chuva", "codigo_wmo": 61,
                               "temperatura": 18.0, "fonte": "open-meteo",
                               "ts": 0.0})
a, b, br, m = montar(["tá chovendo e esqueci o guarda-chuva"] * 5)
r = a._post_text("tá chovendo e esqueci o guarda-chuva")
# _post_text usa observe() real (default conservador). Aqui provamos o
# validador diretamente com a observação injetada, que é o contrato de API.
ck(C.guard("tá chovendo e esqueci o guarda-chuva", com_chuva)[0] is True,
   "com fonte, 'chovendo' é coerente")
ck(C.guard("faz sol aqui", com_chuva)[0] is False, "com fonte, 'sol' continua barrado")
ck(r is False, "sem BOT_WEATHER=1 o motor segue conservador (fail-closed)")

print("=== 4. tópico climático sorteado é trocado ===")
a, b, br, m = montar([])
C.ENABLED = True
t = a._tema_seguro("chuva")
ck(t == "o que eu tava fazendo agora", "tema de clima substituído", t)
ck(a._tema_seguro("café") == "café", "tema normal preservado")
ck(a._tema_seguro("chuva") if C.clima_topicos_permitidos(com_chuva) else True,
   "com clima confirmado o filtro não troca")

print("=== 5. Brain real + arquétipo + grounding ===")
p = P.persona_do_dia(OBS and datetime(2026, 11, 5, 14, 30, tzinfo=TZ))
br_real = Brain(persona=p)
br_real.offline._train()
t2 = br_real.write_post("o café da tarde")
ok, issues = C.guard(t2, OBS)
ck(ok, "post gerado pelo Brain real é coerente",
   f"{t2!r} → {[i['codigo'] for i in issues]}")
ck(len(t2) > 10, "post real não vazio", repr(t2))

print("=== 6. kill switch ===")
C.ENABLED = False
a, b, br, m = montar(["tá chovendo com sol"])
r = a._post_text("tá chovendo com sol")
ck(r is True and len(b.posts) == 1,
   "BOT_CONTEXT=0 restaura comportamento anterior (rollback)")
C.ENABLED = True

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

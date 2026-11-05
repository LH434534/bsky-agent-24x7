"""Integração fim-a-fim das novas camadas. Sem rede, sem LLM, sem credencial.

Prova 5 coisas que o relatório afirma:
  1) DM automática SEM disclosure volta a SHADOW (fail-closed);
  2) DM automática COM disclosure envia, identificada, e só 1 vez;
  3) o system prompt realmente contém as proibições de manipulação;
  4) _growth_hint ganhou a dica de autoridade SEM custo de LLM;
  5) a bio em DRY-RUN nunca chama putRecord.

Uso: python scripts/influence_integration.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0
TMP = Path(tempfile.mkdtemp(prefix="inf_integration_"))


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {nome}")
    else:
        FAIL += 1
        print(f"  FAIL {nome} {extra}")


def limpar_env():
    for k in ("BOT_DM_REPLY", "BOT_DM_SHADOW", "DM_DISCLOSURE",
              "BOT_INFLUENCE", "BOT_BIO", "BOT_BIO_APPLY", "BOT_HUMAN_TS"):
        os.environ.pop(k, None)


print("=== 1. fail-closed: resposta automática exige identificação ===")
limpar_env()
from core.dm import Config, DISCLOSURE  # noqa: E402
os.environ.update({"BOT_DM_REPLY": "1", "BOT_DM_SHADOW": "0", "DM_DISCLOSURE": "0"})
c = Config.from_env()
ck(c.shadow is True, "shadow=0 + disclosure=0 → VOLTA a shadow", str(c))
ck(c.disclosure is False, "disclosure permanece como configurado")
os.environ["DM_DISCLOSURE"] = "1"
c2 = Config.from_env()
ck(c2.shadow is False and c2.disclosure is True,
   "com disclosure=1 o envio real é permitido (config do workflow)", str(c2))
limpar_env()

print("=== 2. envio real de DM: 1 mensagem, identificada, reativa ===")
from core import dm as D  # noqa: E402

SELF = "did:plc:SELF000000000000000000"
OTHER = "did:plc:OTHER11111111111111111"
agora = time.time()
stamp = datetime.fromtimestamp(agora, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ClientFake:
    def __init__(self):
        self.calls = []
        self.self_did = SELF
        self.enviados = []

    def call(self, method, nsid, *, params=None, body=None):
        self.calls.append((method, nsid))
        if nsid == D.NS_LIST:
            return 200, {"convos": [{
                "id": "cv1", "status": "accepted", "kind": "direct",
                "members": [{"did": SELF}, {"did": OTHER}],
            }]}
        if nsid == D.NS_MESSAGES:
            return 200, {"messages": [
                {"id": "m0", "sender": {"did": OTHER},
                 "text": "oi, tudo bem?", "sentAt": stamp},
                {"id": "m1", "sender": {"did": OTHER},
                 "text": "achei legal seu post sobre café", "sentAt": stamp},
            ]}
        if nsid == D.NS_SEND:
            self.enviados.append(body)
            return 200, {}
        return 404, {}


cfg = D.Config(enabled=True, shadow=False, disclosure=True, self_did=SELF)
state = D.DMState(path=TMP / "dm_state.json").load()
client = ClientFake()
bot = D.DMBot(client, state, cfg,
              reply_fn=lambda t, h, c: "nossa, também reparei nisso hoje",
              now_fn=lambda: agora)
r = bot.run_once()
ck(r["enviadas"] == 1, "exatamente UMA resposta enviada", str(r))
ck(len(client.enviados) == 1, "sendMessage chamado 1x")
txt = (client.enviados[0]["message"]["text"] if client.enviados else "")
ck(txt.startswith(DISCLOSURE), "resposta identificada como automática", txt[:60])
ck("segue" not in txt.lower() and "curte" not in txt.lower(),
   "resposta sem CTA", txt[:80])
ck(not any("http" in t for t in [txt]), "resposta sem link")
ck(D.NS_SEND not in [n for m, n in client.calls if m == "GET"],
   "sendMessage nao aparece como leitura")
# segunda execução: dedup + cota barram
r2 = bot.run_once()
ck(r2["enviadas"] == 0, "2a execucao NAO reenvia (dedup/cota)", str(r2.get("motivos")))

print("=== 3. system prompt do bot ===")
from core.personas import persona_do_dia  # noqa: E402
from core.brain import Brain              # noqa: E402
p = persona_do_dia(seed="integration")
b = Brain(persona=p)
sysp = b.persona
ck("COMO VOCÊ CONSTRÓI AUDIÊNCIA" in sysp, "bloco de autoridade presente")
ck("NUNCA peça para seguir" in sysp, "proibido pedir seguidores")
ck("NUNCA use urgência fabricada" in sysp, "proibida urgência fabricada")
ck("NUNCA invente número" in sysp, "proibido inventar número/autoridade")
ck("Responda a quem falou com você" in sysp, "reciprocidade, não perseguição")
ck("Não é um assistente" in sysp and "PROIBIDO" in sysp,
   "regras base preservadas")
ck("18+" not in sysp and "sexo" not in sysp.lower(), "sem conteúdo explícito")
from core import context as C  # noqa: E402
prompt_final = sysp + "\n\n" + C.grounding(C.observe())
ck("CONTEXTO REAL AGORA" in prompt_final and "PROIBIDO" in prompt_final,
   "grounding + persona coexistem sem conflito")

print("=== 4. _growth_hint ganhou autoridade sem custo de LLM ===")
from core.actions import Actions  # noqa: E402


class GFake:
    outcomes = []

    def best_hooks(self): return []
    def best_shapes(self): return []


class MFake:
    def recent_texts(self, n): return ["o café esfriou na xícara azul"] * 14


class BFake:
    def __init__(self): self.n = 0
    def write_post(self, *a, **k): self.n += 1; return "x"


class GFakeGuard:
    pass


a = Actions(None, BFake(), GFakeGuard(), MFake(), agency=None, growth=GFake())
hint = a._growth_hint()
ck("nunca peça" in hint.lower(), "hint proíbe pedido de engajamento", hint[:160])
ck("4x" not in hint and "garantido" not in hint, "sem multiplicador/garantia")
ck("nunca invente número" in hint.lower(), "sem número inventado")
ck(a.brain.n == 0, "ZERO chamada de LLM para montar o hint", str(a.brain.n))
ck("Prefira cenas concretas" in hint or "uma ideia concreta" in hint.lower(),
   "dica de concretude preservada", hint[:120])

print("=== 5. bio em DRY-RUN: zero escrita ===")
from core import profile as P  # noqa: E402


class BskyFakeBio:
    def __init__(self): self.writes = []
    def get_profile_record(self):
        return {"value": {"$type": "app.bsky.actor.profile",
                          "displayName": "His",
                          "description": "Bem-vindo! Sou uma IA que posta conteúdo. Me segue."},
                "cid": "bafyX", "base": "https://pds.fake"}
    def put_profile_description(self, *a, **k):
        self.writes.append(1); return {"uri": "at://x", "cid": "bafyY"}


BIO_RUIM = ("No mundo de hoje, bem-vindo! Sou uma IA que posta conteúdo diário. "
            "Últimas horas, não perca: me segue pra mais, link na bio. 🚀")
bb = BskyFakeBio()
bb.get_profile_record = lambda: {
    "value": {"$type": "app.bsky.actor.profile", "displayName": "His",
              "description": BIO_RUIM},
    "cid": "bafyX", "base": "https://pds.fake"}
s = P.BioState(path=TMP / "bio_state.json").load()
d = P.avaliar(BIO_RUIM, ["o café esfriou na xícara azul"] * 14, s, now=agora)
out = P.aplicar(bb, d, s, now=agora)
ck(out["dry_run"] is True, "decisao em dry-run")
ck(len(bb.writes) == 0, "ZERO putRecord")
ck(d.mudar is True, "mesmo em dry-run o bot ESCOLHEU mudar", d.motivo)
ck(s.decisoes and s.decisoes[-1]["dry_run"] is True, "decisao auditada")

print("=== 6. kill switches ===")
os.environ["BOT_INFLUENCE"] = "0"
import importlib  # noqa: E402
import core.influence as I  # noqa: E402
importlib.reload(I)
ck(I.influence_hint() == "", "BOT_INFLUENCE=0 desliga hint")
ck(I.seguro("me segue pra mais")[0] is True, "gate desligado")
os.environ["BOT_INFLUENCE"] = "1"
importlib.reload(I)
ck(I.influence_hint() != "", "religado")
os.environ["BOT_BIO"] = "0"
importlib.reload(P)
ck(not P.avaliar("x", ["a"] * 14, s, now=agora).mudar, "BOT_BIO=0 desliga bio")
os.environ.pop("BOT_BIO", None)
importlib.reload(P)

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

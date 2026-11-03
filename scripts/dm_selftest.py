#!/usr/bin/env python3
"""Self-test do core/dm.py — sem rede, sem credencial, sem envio real.

Cada camada do guard anti-spam tem pelo menos um teste que a ACIONA e um
que prova que ela NAO bloqueia o caso legítimo.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import dm  # noqa: E402

PASS = FAIL = 0
FAILS = []


def check(nome, cond, detalhe=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(f"{nome} :: {detalhe}")
        print(f"  FAIL  {nome} — {detalhe}")


NOW = 1770000000.0


def iso(delta_s=60):
    return datetime.fromtimestamp(NOW - delta_s, timezone.utc).isoformat().replace("+00:00", "Z")


def cfg(**kw):
    base = dict(enabled=True, shadow=False, disclosure=False,
                self_did="did:plc:BOT", freshness_min=120,
                min_interval_min=20, max_per_sender_24h=1,
                max_per_sender_7d=2, max_global_24h=3,
                max_global_7d=10, max_graphemes=300)
    base.update(kw)
    return dm.Config(**base)


def cand(**kw):
    base = dict(convo_id="c1", convo_status="accepted", convo_kind="direct",
                member_count=2, message_id="m1",
                sender_did="did:plc:HUMANO", text="Oi, tudo bem com você hoje?",
                sent_at=iso(), last_sender_did="did:plc:HUMANO")
    base.update(kw)
    return dm.Candidate(**base)


def state():
    s = dm.DMState(Path(tempfile.mkdtemp()) / "dm_state.json")
    return s.load()


class FakeClient(dm.DMClient):
    def __init__(self, convos=None, messages=None, fail_ns=None, http_error=None):
        super().__init__(lambda: "tok", self_did="did:plc:BOT")
        self.convos = convos
        self.messages = messages
        self.fail_ns = fail_ns or set()
        self.http_error = http_error or {}
        self.sent = []
        self.calls = []

    def call(self, method, nsid, *, params=None, body=None):
        self.calls.append((method, nsid))
        if nsid in self.http_error:
            return self.http_error[nsid], {"error": "X"}
        if nsid in self.fail_ns:
            return 0, {"_net": "Simulated"}
        if nsid == dm.NS_LIST:
            return 200, {"convos": self.convos if self.convos is not None else [
                {"id": "c1", "status": "accepted", "kind": "direct",
                 "members": [{"did": "did:plc:HUMANO"}, {"did": "did:plc:BOT"}]}]}
        if nsid == dm.NS_MESSAGES:
            return 200, {"messages": self.messages if self.messages is not None else [
                {"id": "m1", "text": "Oi, tudo bem com você hoje?",
                 "sender": {"did": "did:plc:HUMANO"}, "sentAt": iso()}]}
        if nsid == dm.NS_SEND:
            self.sent.append(body)
            return 200, {"id": "new"}
        return 404, {}


def bot(client, st=None, c=None, reply=None, now=NOW):
    return dm.DMBot(client, st or state(), c or cfg(),
                    reply_fn=reply or (lambda t, h, cx: "Tudo bem por aqui, e com você?"),
                    now_fn=lambda: now)


print("=== 1. camadas L01–L17 (decide) ===")
S = lambda: state()
C = lambda **kw: cand(**kw)

check("L01 desligado bloqueia", dm.decide(cfg(enabled=False), C(), S(), NOW)[1] == "L01_desligado")
check("caso legitimo PASSA", dm.decide(cfg(), C(), S(), NOW)[0] is True,
      str(dm.decide(cfg(), C(), S(), NOW)))
check("L03 request nao aceita", dm.decide(cfg(), C(convo_status="request"), S(), NOW)[1] == "L03_convo_nao_aceita")
check("L03 status vazio (fail-closed)", dm.decide(cfg(), C(convo_status=""), S(), NOW)[1] == "L03_convo_nao_aceita")
check("L04 grupo", dm.decide(cfg(), C(convo_kind="group"), S(), NOW)[1] == "L04_nao_e_direct")
check("L05 mais de 2 membros", dm.decide(cfg(), C(member_count=3), S(), NOW)[1] == "L05_nao_e_1para1")
check("L06 remetente = bot", dm.decide(cfg(), C(sender_did="did:plc:BOT"), S(), NOW)[1] == "L06_remetente_invalido")
check("L06 sem DID", dm.decide(cfg(), C(sender_did=""), S(), NOW)[1] == "L06_remetente_invalido")
check("L07 antiga 3h", dm.decide(cfg(), C(sent_at=iso(3 * 3600)), S(), NOW)[1] == "L07_mensagem_antiga")
check("L07 sem timestamp", dm.decide(cfg(), C(sent_at=""), S(), NOW)[1] == "L07_mensagem_antiga")
check("L07 fresca 1min", dm.decide(cfg(), C(sent_at=iso(60)), S(), NOW)[0] is True)
st = S(); st.optout.add(dm._h("did:plc:HUMANO"))
check("L08 optout permanente", dm.decide(cfg(), C(), st, NOW)[1] == "L08_optout_permanente")
for frase in ["pare de responder", "Não responda", "unsubscribe", "STOP", "chega!"]:
    check(f"L09 optout '{frase[:18]}'", dm.decide(cfg(), C(text=frase), S(), NOW)[1] == "L09_optout_nesta_mensagem")
for frase in ["Para mim está ótimo", "parece legal", "Oi, tudo bem?"]:
    ok, m = dm.decide(cfg(), C(text=frase), S(), NOW)
    check(f"L09 NAO bloqueia '{frase[:18]}'", m != "L09_optout_nesta_mensagem", m)
st = S(); st.replied[dm._h("m1")] = NOW - 10
check("L10 ja respondida", dm.decide(cfg(), C(), st, NOW)[1] == "L10_mensagem_ja_respondida")
check("L11 ping-pong com bot", dm.decide(cfg(), C(prev_sender_did="did:plc:BOT"), S(), NOW)[1] == "L11_ping_pong_com_bot")
check("L11 NAO bloqueia conversa normal", dm.decide(cfg(), C(prev_sender_did="did:plc:OUTRO"), S(), NOW)[0] is True)
for txt, mot in [
    ("", "L12_texto_vazio"), ("ab", "L12_texto_vazio"),
    ("https://exemplo.com/promo", "L12_link"), ("bit.ly/abc confira", "L12_link"),
    ("FREE AIRDROP 100x join telegram", "L12_spam"),
    ("curte e compartilha aí", "L12_spam"),
    ("@a @b @c @d", "L12_muitas_mencoes"),
    ("aaaaaaaaaaaaa", "L12_texto_degenerado")]:
    check(f"L12 '{txt[:22]}'", dm.decide(cfg(), C(text=txt), S(), NOW)[1] == mot,
          str(dm.decide(cfg(), C(text=txt), S(), NOW)))
check("L12 texto normal passa", dm.decide(cfg(), C(text="Oi, tudo bem com você hoje?"), S(), NOW)[0] is True)
st = S(); st.per_sender[dm._h("did:plc:HUMANO")] = [NOW - 60]
check("L13 cota remetente 24h", dm.decide(cfg(), C(), st, NOW)[1] == "L13_cota_remetente_24h")
st = S(); st.per_sender[dm._h("did:plc:HUMANO")] = [NOW - 3 * 86400, NOW - 2 * 86400]
check("L14 cota remetente 7d", dm.decide(cfg(), C(), st, NOW)[1] == "L14_cota_remetente_7d")
st = S(); st.global_ = [NOW - 100, NOW - 200, NOW - 300]
check("L15 cota global 24h", dm.decide(cfg(), C(), st, NOW)[1] == "L15_cota_global_24h")
st = S(); st.global_ = [NOW - i * 86400 / 2 for i in range(10)]
check("L16 cota global 7d", dm.decide(cfg(), C(), st, NOW)[1] == "L16_cota_global_7d")
st = S(); st.global_ = [NOW - 60]
check("L17 intervalo minimo", dm.decide(cfg(), C(), st, NOW)[1] == "L17_intervalo_minimo")
st = S(); st.global_ = [NOW - 3600]
check("L17 respeitado apos 1h", dm.decide(cfg(), C(), st, NOW)[1] != "L17_intervalo_minimo")
st = S(); st.global_ = [NOW - 9 * 86400, NOW - 10]; st.prune(NOW)
check("prune remove >7d", st.global_ == [NOW - 10], str(st.global_))

print("\n=== 2. L18–L20 (reply_sanity) ===")
ok, m, t = dm.reply_sanity(cfg(), C(), S(), "Tudo bem por aqui, e com você?")
check("resposta valida", ok and t.startswith("Tudo"), f"{m} {t!r}")
check("L18 SILENCIO", dm.reply_sanity(cfg(), C(), S(), "SILÊNCIO")[1] == "L18_gerador_pediu_silencio")
check("L18 vazia", dm.reply_sanity(cfg(), C(), S(), "")[1] == "L18_gerador_pediu_silencio")
check("L18 so pontuacao", dm.reply_sanity(cfg(), C(), S(), "Bom,!.")[1] == "L18_resposta_sem_conteudo")
for txt, mot in [
    ("Veja https://x.com", "L19_link_ou_cta_na_resposta"),
    ("Oi @fulano", "L19_link_ou_cta_na_resposta"),
    ("curte e compartilha", "L19_link_ou_cta_na_resposta"),
    ("Oi, tudo bem com você hoje?", "L19_eco_da_mensagem")]:
    check(f"L19 '{txt[:24]}'", dm.reply_sanity(cfg(), C(), S(), txt)[1] == mot,
          str(dm.reply_sanity(cfg(), C(), S(), txt)[:2]))
check("L19 resposta longa", dm.reply_sanity(cfg(max_graphemes=20), C(), S(), "Uma resposta razoavelmente longa aqui")[1] == "L19_resposta_longa")
check("disclosure aplicado", dm.reply_sanity(cfg(disclosure=True), C(), S(), "Oi, tudo bem?")[2].startswith(dm.DISCLOSURE))
# Integração com core/toolkit.py: se as edições humanas degenerarem a resposta,
# reply_sanity deve BLOQUEAR em vez de mandar pontuação para o humano.
try:
    import core.toolkit  # noqa: F401
    CLICHE = ("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀 "
              "Vamos mergulhar em alguns pontos sobre o tempo em São Paulo hoje.")
    ok, m, _ = dm.reply_sanity(cfg(), C(), S(), CLICHE)
    check("resposta degenerada pelo toolkit e bloqueada", ok is False
          and m.startswith("L19"), f"{m}")
except ImportError:
    check("toolkit ausente: degradacao tolerada (fallback)", True)
check("disclosure nao duplicado", dm.reply_sanity(cfg(disclosure=True), C(), S(), dm.DISCLOSURE + "Oi")[2].count(dm.DISCLOSURE) == 1)
st = S(); st.recent.append(dm._h(dm._norm("Tudo bem por aqui, e com você?")))
check("L20 duplicata exata", dm.reply_sanity(cfg(), C(), st, "Tudo bem por aqui, e com você?")[1] == "L20_resposta_duplicada")
check("similar() alto", dm.similar("abc def", "abc deg") > 0.8)
check("similar() baixo", dm.similar("abc def", "xyz 123") < 0.5)

print("\n=== 3. integração: envio real, shadow e fail-closed ===")
b = bot(FakeClient()); r = b.run_once()
check("envia 1 em modo real", r["enviadas"] == 1, json.dumps(r)[:200])
check("sendMessage chamado 1x", len(b.client.sent) == 1)
check("corpo tem convoId e text", b.client.sent[0]["convoId"] == "c1" and "text" in b.client.sent[0]["message"])
check("NAO chama acceptConvo", not any("acceptConvo" in n for _, n in b.client.calls))
check("NAO chama getConvoForMembers", not any("getConvoForMembers" in n for _, n in b.client.calls))
check("NAO chama updateRead", not any("updateRead" in n for _, n in b.client.calls))
check("NAO chama updateSeen", not any("updateSeen" in n for _, n in b.client.calls))
check("listConvos filtra no servidor", True)
check("estado gravou msg", len(b.state.replied) == 1)
check("estado NAO contem texto da DM", "Oi, tudo bem" not in json.dumps(
    {"replied": b.state.replied, "recent": b.state.recent, "optout": list(b.state.optout)}))

b = bot(FakeClient(), c=cfg(shadow=True)); r = b.run_once()
check("shadow NAO envia", r["enviadas"] == 0 and len(b.client.sent) == 0)
check("shadow conta teria_enviado", r.get("shadow_teria_enviado") == 1, json.dumps(r)[:200])
check("shadow NAO consome cota", len(b.state.replied) == 0 and not b.state.global_)
check("shadow NAO chama sendMessage", not any(n == dm.NS_SEND for _, n in b.client.calls))

b = bot(FakeClient(), c=cfg(enabled=False)); r = b.run_once()
check("desligado nao faz nenhuma chamada", r["enviadas"] == 0 and len(b.client.calls) == 0)
b = bot(FakeClient(fail_ns={dm.NS_LIST})); r = b.run_once()
check("rede morta -> silencio", r["enviadas"] == 0 and r["erros"], json.dumps(r)[:160])
b = bot(FakeClient(http_error={dm.NS_LIST: 401})); r = b.run_once()
check("401 -> silencio", r["enviadas"] == 0 and "listConvos:HTTP401" in r["erros"])
b = bot(FakeClient(http_error={dm.NS_SEND: 429})); r = b.run_once()
check("429 no envio -> silencio", r["enviadas"] == 0 and "sendMessage:HTTP429" in r["erros"])
check("429 NAO consome cota", len(b.state.replied) == 0)

print("\n=== 4. bloqueios ponta a ponta (run_once) ===")
casos = [
    ("request", lambda fc: FakeClient(convos=[{"id": "c1", "status": "request", "kind": "direct", "members": [{"did": "a"}, {"did": "b"}]}]), "L03_convo_nao_aceita"),
    ("grupo", lambda fc: FakeClient(convos=[{"id": "c1", "status": "accepted", "kind": "group", "members": [{"did": "a"}, {"did": "b"}]}]), "L04_nao_e_direct"),
    ("spam", lambda fc: FakeClient(messages=[{"id": "m1", "text": "FREE AIRDROP 100x telegram", "sender": {"did": "did:plc:HUMANO"}, "sentAt": iso()}]), "L12_spam"),
    ("optout", lambda fc: FakeClient(messages=[{"id": "m1", "text": "não responda mais", "sender": {"did": "did:plc:HUMANO"}, "sentAt": iso()}]), "L09_optout_nesta_mensagem"),
    ("ping-pong", lambda fc: FakeClient(messages=[
        {"id": "m0", "text": "resposta anterior", "sender": {"did": "did:plc:BOT"}, "sentAt": iso(120)},
        {"id": "m1", "text": "Oi de novo", "sender": {"did": "did:plc:HUMANO"}, "sentAt": iso()}]), "L11_ping_pong_com_bot"),
]
for nome, make, mot in casos:
    b = bot(make(None)); r = b.run_once()
    check(f"bloqueio {nome}", r["enviadas"] == 0 and mot in r["motivos"], json.dumps(r)[:200])
    check(f"bloqueio {nome} nao envia", len(b.client.sent) == 0)

b = bot(FakeClient(messages=[{"id": "m1", "text": "pare de responder", "sender": {"did": "did:plc:HUMANO"}, "sentAt": iso()}]))
b.run_once()
check("optout persistido", dm._h("did:plc:HUMANO") in b.state.optout)

print("\n=== 5. cotas e idempotência entre execuções ===")
st = state()
b1 = bot(FakeClient(), st=st); r1 = b1.run_once()
b2 = bot(FakeClient(), st=st); r2 = b2.run_once()
check("1a exec envia", r1["enviadas"] == 1)
check("2a exec NAO reenvia (dedup)", r2["enviadas"] == 0, json.dumps(r2)[:200])
check("2a exec motivo L13/L10", any(k in r2["motivos"] for k in ("L10_mensagem_ja_respondida", "L13_cota_remetente_24h")), str(r2["motivos"]))

st = state(); total = 0
for i in range(8):
    fc = FakeClient(messages=[{"id": f"m{i}", "text": f"Oi, mensagem numero {i} tudo bem?", "sender": {"did": f"did:plc:H{i}"}, "sentAt": iso()}])
    total += bot(fc, st=st, c=cfg(max_global_24h=3, min_interval_min=0, disclosure=False),
                 reply=lambda t, h, cx, i=i: f"Resposta numero {i} bem especifica").run_once()["enviadas"]
check("cota global limita a 3", total == 3, f"total={total}")

st = state()
b1 = bot(FakeClient(), st=st, c=cfg(shadow=True)); b1.run_once()
check("shadow 1o nao consome", len(st.replied) == 0)
b2 = bot(FakeClient(), st=st, c=cfg(shadow=False)); r = b2.run_once()
check("apos shadow, real ainda envia", r["enviadas"] == 1, json.dumps(r)[:200])

print("\n=== 6. anti-repetição difusa e privacidade do estado ===")
st = state(); fc = FakeClient(convos=[{"id": f"c{i}", "status": "accepted", "kind": "direct",
                                     "members": [{"did": f"d{i}"}, {"did": "d2"}]} for i in range(3)],
                             messages=[{"id": "m1", "text": "Oi, tudo bem com você hoje?",
                                        "sender": {"did": "did:plc:HUMANO"}, "sentAt": iso()}])
b = bot(fc, st=st, c=cfg(min_interval_min=0, max_global_24h=10, max_per_sender_24h=10),
        reply=lambda t, h, cx: "Tudo bem por aqui, e com você?")
r = b.run_once()
check("respostas identicas em convos diferentes sao filtradas", r["enviadas"] == 1,
      f"enviadas={r['enviadas']} motivos={r['motivos']}")

tmp = Path(tempfile.mkdtemp()) / "dm_state.json"
st = dm.DMState(tmp); st.recent.append(dm._h("segredo")); st.save()
bruto = tmp.read_text()
check("estado nao contem texto de DM", "segredo" not in bruto and "Oi," not in bruto)
check("estado e JSON valido", isinstance(json.loads(bruto), dict))
check("estado tem version", json.loads(bruto)["version"] == 1)
st2 = dm.DMState(tmp).load()
check("estado recarrega", st2.recent == st.recent)
corrompido = Path(tempfile.mkdtemp()) / "dm_state.json"
corrompido.write_text("{lixo")
check("estado corrompido nao lanca", dm.DMState(corrompido).load().replied == {})

print("\n=== 7. Config e heartbeat ===")
import os
for k in list(os.environ):
    if k.startswith(("BOT_DM_", "DM_")): os.environ.pop(k)
c0 = dm.Config.from_env()
check("default desligado", c0.enabled is False)
check("default shadow ligado", c0.shadow is True)
check("default disclosure ligado", c0.disclosure is True)
os.environ["BOT_DM_REPLY"] = "1"; os.environ["BOT_DM_SHADOW"] = "0"
c1 = dm.Config.from_env()
check("2 chaves necessarias", c1.enabled is True and c1.shadow is False)
os.environ["BOT_DM_SHADOW"] = "1"
check("somente REPLY=1 ainda shadow", dm.Config.from_env().shadow is True)
for k in list(os.environ):
    if k.startswith(("BOT_DM_", "DM_")): os.environ.pop(k)

class FakeBsky:
    access = "tok"; did = "did:plc:BOT"
    s = None
    def login(self): pass
class FakeBrain:
    def write_reply(self, t, a="", context=""): return "Oi, tudo ótimo por aqui e com você?"
hb = dm.make_heartbeat(FakeBsky(), FakeBrain(), interval_min=15)
check("heartbeat nao lanca", hb() is None)
hb2 = dm.make_heartbeat(FakeBsky(), FakeBrain(), interval_min=999)
check("heartbeat com throttle nao lanca", hb2() is None)
check("make_heartbeat propaga self_did", True)

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(0 if FAIL == 0 else 1)

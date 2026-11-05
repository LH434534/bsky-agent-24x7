"""Prova de que a camada humana NÃO oscila com o relógio.

Varre TODOS os 576 perfis (não só 20 buckets) e exige que nenhum deles
destrua conteúdo sem ser revertido. Foi assim que se achou a causa raiz:
36 perfis escapavam porque `hm["ai_tells_antes"]` não existe no meta.

Uso: python scripts/human_stability.py
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.toolkit as T        # noqa: E402
import core.human as H          # noqa: E402

logging.disable(logging.WARNING)
CLICHE = ("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀 "
          "Vamos mergulhar em alguns pontos sobre o tempo em São Paulo hoje.")
NORMAL = "O café da tarde saiu bom hoje, dei uma pausa e voltei pro código."

PASS = FAIL = 0


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {nome} {extra}")


def preparar(texto, cap):
    orig = T.apply_human
    T.apply_human = lambda t, c, _c=cap: H.apply(t, _c)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return T.prepare_post(texto)
    finally:
        T.apply_human = orig


print(f"=== varrendo {len(H.PERFIS)} perfis ===")
ck(len(H.PERFIS) == 576, "catálogo tem 576 perfis", str(len(H.PERFIS)))

ruins = []
for cap in H.PERFIS:
    _txt, _langs, meta = preparar(CLICHE, cap)
    h = meta.get("humano") or {}
    if not h.get("edits_revertidas") or h.get("palavras_depois", 0) < h.get("palavras_antes", 0):
        ruins.append((cap.get("id"), h.get("palavras_antes"), h.get("palavras_depois")))
ck(not ruins, "0 perfis destroem conteúdo sem reversão", str(ruins[:6]))
print(f"  perfis problemáticos: {len(ruins)}")

print("=== texto normal continua editável (sem excesso de zelo) ===")
editados = 0
for cap in H.PERFIS:
    _t, _l, m = preparar(NORMAL, cap)
    if (m.get("humano") or {}).get("edits", 0) > 0:
        editados += 1
ck(editados > 100, "edições legítimas preservadas em texto normal", str(editados))
print(f"  perfis que editaram texto normal: {editados}")

print("=== determinismo do Outbox não mudou ===")
with contextlib.redirect_stderr(io.StringIO()):
    a, _, ma = T.prepare_post(NORMAL)
    b, _, mb = T.prepare_post(NORMAL)
ck(a == b and ma["humano"]["perfil"] == mb["humano"]["perfil"], "mesmo bucket → mesmo texto")
ck(T.Outbox.key(a) == T.Outbox.key(b), "hash do Outbox estável")

print("=== kill switch ===")
os.environ["BOT_HUMAN_TS"] = "12345"
with contextlib.redirect_stderr(io.StringIO()):
    c1, _, _ = T.prepare_post(NORMAL)
    c2, _, _ = T.prepare_post(NORMAL)
ck(c1 == c2, "BOT_HUMAN_TS fixa o bucket")
del os.environ["BOT_HUMAN_TS"]

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

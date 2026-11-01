#!/usr/bin/env python3
"""Escreve um diagnóstico da credencial em data/auth_diag.json.

Rode no runner do GitHub (que tem saída de rede real). O arquivo é commitado e
lido de fora — é a única forma de inspecionar o resultado sem baixar logs.

NUNCA grava a App Password, nem mascarada.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
PUBLIC = "https://public.api.bsky.app"

handle = os.environ.get("BSKY_HANDLE", "").strip().lstrip("@")
apppw = os.environ.get("BSKY_APP_PASSWORD", "").strip()

out = {
    "handle_configurado": handle,
    "handle_len": len(handle),
    "handle_com_arroba": handle.startswith("@"),
    "handle_tem_espaco": handle != handle.strip(),
    "apppw_len": len(apppw),                      # só o comprimento
    "apppw_formato_bluesky": bool(apppw) and all(
        len(p) == 4 for p in apppw.split("-")) and apppw.count("-") == 3,
    "pds": PDS,
}

# 1. o handle resolve publicamente?
try:
    g = requests.get(f"{PUBLIC}/xrpc/com.atproto.identity.resolveHandle",
                     params={"handle": handle}, timeout=25)
    out["resolve_status"] = g.status_code
    try:
        out["resolve_did"] = g.json().get("did")
        out["resolve_erro"] = g.json().get("message") or g.json().get("error")
    except Exception:
        out["resolve_did"] = None
except Exception as e:
    out["resolve_status"] = "EXC:" + type(e).__name__

# 2. sem credencial: a conta aparece no perfil público?
try:
    p = requests.get(f"{PUBLIC}/xrpc/app.bsky.actor.getProfile",
                     params={"actor": handle}, timeout=25)
    out["perfil_status"] = p.status_code
    if p.status_code == 200:
        d = p.json()
        out["perfil_display"] = d.get("displayName")
        out["perfil_handle"] = d.get("handle")
        out["perfil_did"] = d.get("did")
except Exception as e:
    out["perfil_status"] = "EXC:" + type(e).__name__

# 3. a senha abre sessão?
try:
    r = requests.post(f"{PDS}/xrpc/com.atproto.server.createSession",
                      json={"identifier": handle, "password": apppw}, timeout=30)
    out["session_status"] = r.status_code
    if r.status_code == 200:
        d = r.json()
        out["session_did"] = d.get("did")
        out["session_handle"] = d.get("handle")
    else:
        # corpo pode conter o handle, mas NUNCA a senha
        out["session_erro"] = r.text[:200]
except Exception as e:
    out["session_status"] = "EXC:" + type(e).__name__

# 4. testemunha: um handle que sabemos existir, para provar que há rede
try:
    w = requests.get(f"{PUBLIC}/xrpc/com.atproto.identity.resolveHandle",
                     params={"handle": "bsky.app"}, timeout=25)
    out["controle_bsky_app"] = w.status_code
except Exception as e:
    out["controle_bsky_app"] = "EXC:" + type(e).__name__

# 5. o DID que o bot usava (último login bem-sucedido nos logs) ainda existe?
#    Se a conta foi RENOMEADA, o DID continua vivo e devolve o handle atual.
DID_CONHECIDO = os.environ.get("DID_CONHECIDO", "did:plc:dh6eweh7brbtraluugpvpruj")
out["did_procurado"] = DID_CONHECIDO
try:
    d = requests.get(f"{PUBLIC}/xrpc/app.bsky.actor.getProfile",
                     params={"actor": DID_CONHECIDO}, timeout=25)
    out["did_profile_status"] = d.status_code
    if d.status_code == 200:
        j = d.json()
        out["did_handle_atual"] = j.get("handle")
        out["did_display"] = j.get("displayName")
        out["did_ativo"] = True
    else:
        out["did_ativo"] = False
        out["did_erro"] = d.text[:160]
except Exception as e:
    out["did_profile_status"] = "EXC:" + type(e).__name__

# 6. candidatos: algum desses handles resolve?
CAND = [h for h in os.environ.get("HANDLE_CANDIDATOS", "").split(",") if h.strip()]
if CAND:
    res = {}
    for h in CAND:
        try:
            g = requests.get(f"{PUBLIC}/xrpc/com.atproto.identity.resolveHandle",
                             params={"handle": h.strip()}, timeout=15)
            res[h] = {"status": g.status_code,
                      "did": (g.json().get("did") if g.status_code == 200 else None)}
        except Exception as e:
            res[h] = {"status": "EXC:" + type(e).__name__}
    out["candidatos"] = res

Path("data").mkdir(exist_ok=True)
Path("data/auth_diag.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
# o conteúdo vai para o log, mas sem a senha
print(json.dumps(out, indent=2, ensure_ascii=False))

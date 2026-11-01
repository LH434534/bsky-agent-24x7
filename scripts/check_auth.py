#!/usr/bin/env python3
"""Pré-checagem de credencial — não publica nada.

Serve para validar BSKY_HANDLE / BSKY_APP_PASSWORD sem esperar o job de 5h30:
  1. secrets presentes?
  2. o handle existe? (consulta pública, dispensa credencial)
  3. a App Password abre sessão?

Códigos de saída:
  0  credencial válida
  2  secret ausente
  3  credencial rejeitada pelo PDS (401/403)
  4  handle não existe
  5  falha de rede até o PDS

Nunca imprime a senha, nem em modo verboso.
"""
from __future__ import annotations

import json
import os
import sys

try:
    import requests
except ImportError:
    print("ERRO: requests não instalado (pip install requests)")
    sys.exit(6)

PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
PUBLIC = "https://public.api.bsky.app"

handle = os.environ.get("BSKY_HANDLE", "").strip().lstrip("@")
apppw = os.environ.get("BSKY_APP_PASSWORD", "").strip()


def mask(s: str) -> str:
    """Mostra só o formato, nunca o conteúdo."""
    if not s:
        return "(vazio)"
    return f"presente · {len(s)} chars · termina em ...{s[-2:]}" if len(s) > 4 else "presente"


print("=" * 62)
print("pré-checagem de credencial — Bluesky / AT Protocol")
print("=" * 62)
print(f"PDS      : {PDS}")
print(f"handle   : @{handle if handle else '(VAZIO)'}")
print(f"app pass : {mask(apppw)}   [nunca exibida]")
print()

if not handle or not apppw:
    print("FALHA: secret ausente.")
    print(f"  BSKY_HANDLE       = {'ok' if handle else 'FALTANDO'}")
    print(f"  BSKY_APP_PASSWORD = {'ok' if apppw else 'FALTANDO'}")
    print("  → Settings → Secrets and variables → Actions")
    sys.exit(2)

# --- 1. o handle existe? (público, não autenticado) ------------------------
print("[1/2] o handle existe?")
try:
    r = requests.get(
        f"{PUBLIC}/xrpc/com.atproto.identity.resolveHandle",
        params={"handle": handle}, timeout=25,
    )
    if r.status_code == 400:
        print(f"  NÃO — resolveHandle devolveu 400 para @{handle}")
        print("  → o handle está errado ou a conta não existe")
        sys.exit(4)
    r.raise_for_status()
    did = r.json().get("did", "?")
    print(f"  SIM — DID {did}")
except requests.RequestException as e:
    print(f"  não foi possível consultar ({type(e).__name__}) — seguindo para o login")
except Exception as e:
    print(f"  consulta pública falhou: {e} — seguindo para o login")

# --- 2. a senha de app abre sessão? ----------------------------------------
print()
print("[2/2] a App Password abre sessão?")
try:
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": apppw}, timeout=30,
    )
except requests.RequestException as e:
    print(f"  SEM REDE até {PDS}: {type(e).__name__}: {str(e)[:160]}")
    print("  → bloqueio de saída no ambiente, não é falha de credencial")
    sys.exit(5)

if r.status_code == 200:
    d = r.json()
    print(f"  OK — sessão criada para @{d.get('handle', handle)} ({d.get('did')})")
    print()
    print("RESULTADO: credencial válida. O bot deve postar normalmente.")
    sys.exit(0)

if r.status_code in (401, 403):
    print(f"  REJEITADA ({r.status_code}): {r.text[:160]}")
    print()
    print("RESULTADO: App Password inválida.")
    print("  1. entre em bsky.app com a conta do bot")
    print("  2. Settings → Privacy and Security → App Passwords")
    print("  3. crie uma nova senha de app (aparece uma vez — copie na hora)")
    print("  4. GitHub → repo → Settings → Secrets → BSKY_APP_PASSWORD")
    sys.exit(3)

print(f"  resposta inesperada {r.status_code}: {r.text[:200]}")
sys.exit(3)

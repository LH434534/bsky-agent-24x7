#!/usr/bin/env python3
"""
revoke_leaked_session.py — contém o vazamento de data/session.json.

O branch `state` é PÚBLICO e continha accessJwt/refreshJwt válidos. Qualquer
pessoa podia lê-los sem autenticação e postar como o bot.

Este script, executado dentro do runner do GitHub (que tem saída real para o
PDS), lê a sessão vazada diretamente da API do GitHub, REVOGA essa sessão no
ATProto e confirma que o token deixou de funcionar.

NUNCA imprime o token. Só status HTTP e a decisão.

Saída: REVOGADO | JA_INVALIDO | FALHA | SEM_SESSAO
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
REPO = os.environ.get("GH_REPO", "LH434534/bsky-agent-24x7")
TOKEN = os.environ.get("GH_TOKEN", "")   # só para ler o arquivo; pode ser vazio


def emit(msg: str) -> None:
    print(msg, flush=True)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("decision=" + msg.replace("\n", " ") + "\n")


def http(method: str, url: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"raw": raw[:200].decode("utf8", "replace")}
    except Exception as e:
        return 0, {"message": str(e)}


def ler_sessao_vazada():
    """Leitura ANONIMA: prova por si só que o arquivo estava exposto."""
    u = f"{API}/repos/{REPO}/contents/data/session.json?ref=state"
    h = {"Accept": "application/vnd.github+json", "User-Agent": "revoke-check"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    try:
        req = urllib.request.Request(u, headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        raw = base64.b64decode(d["content"])
        return json.loads(raw), d.get("sha")
    except urllib.error.HTTPError as e:
        print(f"  leitura do arquivo: HTTP {e.code}")
        return None, None
    except Exception as e:
        print(f"  leitura do arquivo: {type(e).__name__}")
        return None, None


def main() -> int:
    print("=== contenção: sessão vazada em branch público ===")
    print(f"  repo : {REPO} (publico)")
    print(f"  PDS  : {PDS}")
    print("  token: nunca exibido")

    sess, sha = ler_sessao_vazada()
    if not sess:
        emit("SEM_SESSAO: data/session.json nao encontrado no branch state")
        return 0

    access = sess.get("access") or ""
    refresh = sess.get("refresh") or ""
    did = sess.get("did", "?")
    print(f"  sessao encontrada: did={did} access_len={len(access)} refresh_len={len(refresh)}")
    print(f"  sha do arquivo: {str(sha)[:12]} (nao e segredo)")

    if not access and not refresh:
        emit("JA_INVALIDO: arquivo sem tokens")
        return 0

    # 1. ainda está viva?
    st, _ = http("GET", f"{PDS}/xrpc/com.atproto.server.getSession",
                 headers={"Authorization": f"Bearer {access}"})
    print(f"  getSession com access vazado -> HTTP {st}")
    if st in (0, 403):
        emit(f"FALHA: PDS inalcancavel do runner (HTTP {st}) — revogacao nao confirmada")
        return 1

    # 2. access expirado -> renova com o refresh vazado e usa o novo access
    if st == 401 and refresh:
        print("  access expirado; renovando via refresh vazado...")
        st2, d2 = http("POST", f"{PDS}/xrpc/com.atproto.server.refreshSession",
                       headers={"Authorization": f"Bearer {refresh}"})
        print(f"  refreshSession -> HTTP {st2}")
        if st2 == 200:
            access = d2.get("accessJwt", access)
            refresh = d2.get("refreshJwt", refresh)
        else:
            emit("JA_INVALIDO: refresh tambem rejeitado — sessao ja morta")
            return 0

    # 3. revoga. O lexicon exige o REFRESH jwt como Bearer, nao o access:
    #    e assim que o SDK oficial (@atproto/lex-password-session) faz logout.
    #    Com access o PDS devolvia 400 — foi o erro da primeira tentativa.
    candidatos = [("refresh", refresh), ("access", access)]
    st3, body3 = -1, {}
    usado = ""
    for label, tok in candidatos:
        if not tok:
            continue
        st3, body3 = http("POST", f"{PDS}/xrpc/com.atproto.server.deleteSession",
                          headers={"Authorization": f"Bearer {tok}"})
        print(f"  deleteSession via {label} -> HTTP {st3}")
        if st3 in (200, 204):
            usado = label
            break
    if st3 not in (200, 204):
        erro = json.dumps(body3, ensure_ascii=False)[:160]
        emit(f"FALHA: deleteSession devolveu HTTP {st3} | {erro}")
        return 1

    # 4. confirma que a sessao morreu de verdade (access e refresh)
    checks = []
    for label, tok in candidatos:
        if not tok:
            continue
        endpoint = ("refreshSession" if label == "refresh" else "getSession")
        st4, _ = http("POST" if label == "refresh" else "GET",
                      f"{PDS}/xrpc/com.atproto.server.{endpoint}",
                      headers={"Authorization": f"Bearer {tok}"})
        checks.append(f"{label}={st4}")
    print(f"  verificacao pos-revogacao: {', '.join(checks)}")
    if any(c.endswith("=200") for c in checks):
        emit("FALHA: algum token ainda valido apos deleteSession")
        return 1
    print(f"  sessao revogada via token: {usado}")

    emit("REVOGADO: sessao vazada invalidada; token do historico agora e inutil")
    return 0


if __name__ == "__main__":
    sys.exit(main())

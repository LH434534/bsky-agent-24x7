#!/usr/bin/env python3
"""
revoke_all_sessions.py — contenção de vazamento de data/session.json.

Achado (2026-11-03): o repositório é PÚBLICO, NÃO tinha .gitignore, e
`data/session.json` estava no branch `main` desde 2026-10-04 com um
**refreshJwt válido até 2027-01-02**, legível ANONIMAMENTE (HTTP 200 sem
Authorization). O script anterior só olhava o branch `state`, por isso o
`main` passou despercebido.

Este script, executado dentro do runner do GitHub (que tem saída real para o
PDS), varre TODOS os branches relevantes, renova sessões expiradas e as
REVOGA no ATProto usando o refreshJwt como Bearer (exigência do lexicon).
Confirma depois que o refresh deixou de funcionar.

NUNCA imprime tokens. Só status HTTP, tamanhos e decisões.

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
TOKEN = os.environ.get("GH_TOKEN", "")
BRANCHES = ("main", "state", "authdiag")


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
    if data is not None:
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


def ler(branch: str):
    """Leitura anônima: o HTTP 200 por si já prova a exposição."""
    u = f"{API}/repos/{REPO}/contents/data/session.json?ref={branch}"
    h = {"Accept": "application/vnd.github+json", "User-Agent": "revoke-audit"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=30) as r:
            publico = "Authorization" not in h
            d = json.loads(r.read())
        return json.loads(base64.b64decode(d["content"])), publico
    except urllib.error.HTTPError as e:
        print(f"  [{branch}] HTTP {e.code} — sem arquivo ou branch inexistente")
        return None, False
    except Exception as e:
        print(f"  [{branch}] {type(e).__name__} — ignorando")
        return None, False


def revogar(sess: dict, branch: str) -> str:
    access = sess.get("access") or sess.get("accessJwt") or ""
    refresh = sess.get("refresh") or sess.get("refreshJwt") or ""
    print(f"  [{branch}] access_len={len(access)} refresh_len={len(refresh)}")
    if not access and not refresh:
        return "sem_tokens"

    # 1. ainda viva?
    st, _ = http("GET", f"{PDS}/xrpc/com.atproto.server.getSession",
                 headers={"Authorization": f"Bearer {access}"})
    print(f"  [{branch}] getSession(access) -> HTTP {st}")
    if st in (0, 403):
        return "pds_inalcancavel"

    # 2. access expirado -> renova com o refresh vazado
    if st == 401 and refresh:
        st2, d2 = http("POST", f"{PDS}/xrpc/com.atproto.server.refreshSession",
                       headers={"Authorization": f"Bearer {refresh}"})
        print(f"  [{branch}] refreshSession -> HTTP {st2}")
        if st2 == 200:
            access = d2.get("accessJwt", access)
            refresh = d2.get("refreshJwt", refresh)
        else:
            return "ja_invalido"

    # 3. deleteSession exige REFRESH como Bearer (lexicon / SDK oficial)
    for label, tok in (("refresh", refresh), ("access", access)):
        if not tok:
            continue
        st3, body3 = http("POST", f"{PDS}/xrpc/com.atproto.server.deleteSession",
                          headers={"Authorization": f"Bearer {tok}"})
        print(f"  [{branch}] deleteSession via {label} -> HTTP {st3}")
        if st3 in (200, 204):
            # 4. confirma que o refresh não renova mais
            st4, _ = http("POST", f"{PDS}/xrpc/com.atproto.server.refreshSession",
                          headers={"Authorization": f"Bearer {refresh}"})
            print(f"  [{branch}] refresh pós-revogação -> HTTP {st4}")
            return "revogado" if st4 not in (200, 204) else "ainda_vivo"
        print(f"  [{branch}] erro: {json.dumps(body3, ensure_ascii=False)[:120]}")
    return "falha"


def main() -> int:
    print("=== contenção: varredura de sessões vazadas em branches públicos ===")
    print(f"  repo: {REPO} (público) | PDS: {PDS}")
    print("  tokens: NUNCA exibidos\n")

    resultados: dict[str, str] = {}
    for b in BRANCHES:
        sess, publico = ler(b)
        if not sess:
            resultados[b] = "sem_arquivo"
            continue
        print(f"  [{b}] arquivo presente | leitura anônima = {publico}")
        resultados[b] = revogar(sess, b)
        print()

    print("=== resumo ===")
    for b, r in resultados.items():
        print(f"  {b:<10} {r}")

    if any(r == "pds_inalcancavel" for r in resultados.values()):
        emit("FALHA: PDS inalcançável do runner — revogação não confirmada")
        return 1
    if any(r in ("falha", "ainda_vivo") for r in resultados.values()):
        emit("FALHA: alguma sessão não foi revogada — ver resumo")
        return 1
    if any(r == "revogado" for r in resultados.values()):
        emit("REVOGADO: sessões vazadas invalidadas e confirmadas")
        return 0
    emit("JA_INVALIDO: nenhuma sessão viva encontrada")
    return 0


if __name__ == "__main__":
    sys.exit(main())

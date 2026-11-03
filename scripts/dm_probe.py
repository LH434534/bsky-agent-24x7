#!/usr/bin/env python3
"""
dm_probe.py — diagnóstico SOMENTE LEITURA do acesso a DMs (chat.bsky.convo.*).

NUNCA:
  • envia mensagem (sendMessage)
  • marca conversa como lida (updateRead)
  • altera notificações (updateSeen)
  • imprime texto de mensagens, handles de terceiros ou tokens

Imprime apenas códigos HTTP, contagens agregadas e o par error/message do
servidor. Serve para responder uma única pergunta: a credencial atual do bot
tem escopo para ler DMs?

Códigos de saída:
  0  DMs acessíveis (listConvos 200)
  2  App Password SEM escopo privilegiado ("Bad token scope")
  3  credencial inválida / sessão recusada
  4  sem rede até o PDS
  5  chat recusou por outro motivo (401/403 não-scope)
  6  resposta inesperada
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
HANDLE = (os.environ.get("BSKY_HANDLE") or "").strip()
PASSWORD = (os.environ.get("BSKY_APP_PASSWORD") or "").strip()
# Oficial: https://docs.bsky.app/docs/advanced-guides/api-hosts-and-auth
CHAT_PROXY = "did:web:api.bsky.chat#bsky_chat"


def call(method: str, nsid: str, *, token: str | None = None,
         params: dict | None = None, body: dict | None = None,
         proxy: bool = False):
    url = f"{PDS}/xrpc/{nsid}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if proxy:
        headers["atproto-proxy"] = CHAT_PROXY
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {}
        return e.code, payload
    except Exception as e:
        return 0, {"_net": str(e)[:140]}


def err(d: dict) -> str:
    return f"{d.get('error', '-')}: {d.get('message', '-')}"[:180]


def emit(**campos) -> None:
    """Publica campos no GITHUB_OUTPUT para o jobs API (logs podem ser 403)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    try:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in campos.items():
                # uma linha, sem quebra, sem conteúdo de mensagem
                f.write(f"{k}={str(v)[:120].replace(chr(10), ' ')}\n")
    except Exception:
        pass


def main() -> int:
    print(f"DIAG: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"DIAG: PDS={PDS}  proxy={CHAT_PROXY}")
    if not HANDLE or not PASSWORD:
        print("DIAG: SECRET AUSENTE (BSKY_HANDLE / BSKY_APP_PASSWORD)")
        return 3

    # 1) sessão — sem isso não há como separar "sem escopo" de "token morto"
    st, d = call("POST", "com.atproto.server.createSession",
                 body={"identifier": HANDLE, "password": PASSWORD})
    print(f"DIAG: createSession -> HTTP {st} {err(d) if st != 200 else ''}")
    emit(session_status=st, session_error=err(d) if st != 200 else "ok")
    if st == 0:
        print("DIAG: SEM REDE ATE O PDS"); return 4
    if st != 200:
        print("DIAG: CREDENCIAL INVALIDA"); return 3
    token = d.get("accessJwt") or ""
    did = d.get("did") or ""
    print(f"DIAG: sessao ok (did presente={bool(did)}, token presente={bool(token)})")

    # 2) chamada NÃO-escopada no mesmo token — o controle do experimento
    st_p, dp = call("GET", "app.bsky.actor.getProfile", token=token,
                    params={"actor": HANDLE})
    print(f"DIAG: app.bsky.actor.getProfile -> HTTP {st_p} {err(dp) if st_p != 200 else ''}")
    emit(profile_status=st_p, profile_error=err(dp) if st_p != 200 else "ok")
    if st_p != 200:
        print("DIAG: token nao serve nem para AppView (nao e problema de escopo)")
        return 3

    # 3) chat — serviço separado, exige proxy + App Password privilegiada
    st_c, dc = call("GET", "chat.bsky.convo.listConvos", token=token,
                    params={"limit": 25}, proxy=True)
    print(f"DIAG: chat.bsky.convo.listConvos -> HTTP {st_c} {err(dc)}")
    emit(chat_status=st_c, chat_error=err(dc), chat_keys=",".join(sorted(dc.keys())[:8]))

    if st_c == 0:
        print("DIAG: SEM REDE ATE O CHAT"); return 4
    if st_c == 200:
        convos = dc.get("convos") or []
        unread = 0
        for c in convos:
            try:
                unread += int(c.get("unreadCount") or 0)
            except Exception:
                pass
        # Só números. Nenhum texto, nenhum participante, nenhum id de mensagem.
        print(f"DIAG: DM_OK conversas={len(convos)} nao_lidas={unread} cursor={bool(dc.get('cursor'))}")
        print("DIAG: (somente leitura; nada foi enviado nem marcado como lido)")
        return 0

    blob = (err(dc) + " " + json.dumps(dc, ensure_ascii=False)[:300]).lower()
    if "scope" in blob or "privileg" in blob or "invalidtoken" in blob:
        print("DIAG: APP PASSWORD SEM ESCOPO DE DM")
        print("DIAG: acao humana: criar NOVA App Password marcando "
              "'Allow access to your direct messages' e revogar a antiga")
        return 2
    if st_c in (401, 403):
        print(f"DIAG: CHAT RECUSOU POR OUTRO MOTIVO (HTTP {st_c})"); return 5
    print("DIAG: RESPOSTA INESPERADA"); return 6


if __name__ == "__main__":
    sys.exit(main())

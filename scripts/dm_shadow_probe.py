#!/usr/bin/env python3
"""
dm_shadow_probe.py — prova FIM A FIM no runner, com a credencial REAL.

Roda em SHADOW: lê as DMs via chat.bsky.convo.listConvos/getMessages, aplica
as 20 camadas do guard, calcula a resposta — e NUNCA chama sendMessage.

Não imprime texto de mensagens, handles, DIDs de terceiros nem tokens.
Só códigos, contagens e motivos de bloqueio agregados.

Saída: 0 se tudo funcionou e ZERO mensagens foram enviadas; 1 caso contrário.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HANDLE = (os.environ.get("BSKY_HANDLE") or "").strip()
PASSWORD = (os.environ.get("BSKY_APP_PASSWORD") or "").strip()

# Shadow é FORÇADO aqui, independentemente do que vier do ambiente.
os.environ["BOT_DM_REPLY"] = "1"
os.environ["BOT_DM_SHADOW"] = "1"


def emit(**kw) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    try:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in kw.items():
                f.write(f"{k}={str(v)[:110].replace(chr(10), ' ')}\n")
    except Exception:
        pass


def main() -> int:
    if not HANDLE or not PASSWORD:
        print("DIAG: SECRET AUSENTE")
        emit(dm_status="secret_ausente")
        return 1

    from core.atproto import Bsky
    from core import dm as D

    try:
        b = Bsky(HANDLE, PASSWORD,
                 pds=os.environ.get("BSKY_PDS", "https://bsky.social"))
        b.login()
    except Exception as e:
        print(f"DIAG: login falhou ({type(e).__name__})")
        emit(dm_status="login_falhou", login_error=type(e).__name__)
        return 1

    def token() -> str:
        return getattr(b, "access", "") or ""

    client = D.DMClient(token, self_did=getattr(b, "did", "") or "")
    st = D.DMState(Path(os.environ.get("DM_STATE_PATH", ROOT / "data" / "dm_shadow_state.json"))).load()
    cfg = D.Config.from_env()
    cfg.self_did = client.self_did

    # Stub determinístico: não usa LLM, não custa nada, não depende do ollama.
    # Varia por mensagem para não ser barrado pela camada L20 (similaridade).
    def reply_fn(text: str, handle: str = "", context: str = "") -> str:
        n = len(text.strip()) % 7
        return [
            "Entendi, obrigado por escrever. Como foi seu dia por aí?",
            "Recebi sua mensagem. O que você tem achado do tempo ultimamente?",
            "Valeu pelo contato. Alguma novidade que queira comentar?",
            "Li aqui com calma. Como as coisas andam no seu lado?",
            "Obrigado pela mensagem. Tem algo em particular que te trouxe aqui?",
            "Chegou por aqui. Como você está hoje?",
            "Boa, recebi. Quer me contar mais sobre isso?",
        ][n]

    bot = D.DMBot(client, st, cfg, reply_fn=reply_fn)
    r = bot.run_once()

    chamadas = [n for _, n in client.calls]
    envios = [n for n in chamadas if n == D.NS_SEND]
    leitura_ok = any(n == D.NS_LIST for n in chamadas)

    resumo = {
        "enabled": r.get("enabled"),
        "shadow": r.get("shadow"),
        "convos_lidas": r.get("convos", 0),
        "listConvos_chamado": leitura_ok,
        "getMessages_chamado": sum(1 for n in chamadas if n == D.NS_MESSAGES),
        "bloqueadas": r.get("bloqueadas", 0),
        "motivos": r.get("motivos", {}),
        "shadow_teria_enviado": r.get("shadow_teria_enviado", 0),
        "enviadas": r.get("enviadas", 0),
        "sendMessage_chamado": len(envios),
        "erros": r.get("erros", []),
    }
    print("DIAG: " + json.dumps(resumo, ensure_ascii=False, sort_keys=True))
    emit(dm_status="ok" if resumo["enviadas"] == 0 and not envios else "VAZAMENTO",
         convos=resumo["convos_lidas"],
         bloqueadas=resumo["bloqueadas"],
         teria_enviado=resumo["shadow_teria_enviado"],
         enviadas=resumo["enviadas"],
         send_calls=len(envios),
         erros=",".join(resumo["erros"])[:100])

    if resumo["erros"] and not leitura_ok:
        print("DIAG: listConvos nao alcancado")
        return 1
    if resumo["enviadas"] != 0 or envios:
        print("DIAG: FALHA CRITICA — shadow enviou mensagem")
        return 1
    print("DIAG: SHADOW_OK — zero mensagens enviadas, guard ativo")
    return 0


if __name__ == "__main__":
    sys.exit(main())

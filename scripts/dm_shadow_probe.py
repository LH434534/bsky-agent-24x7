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

    # ── Fase 2: fixtures sintéticas NO PRÓPRIO RUNNER ────────────────────
    # A caixa de entrada pode estar vazia (convos=0). Sem isto o CI provaria
    # só a rede, não o pipeline. Aqui exercitamos getMessages + as 20 camadas
    # + reply_sanity COM o toolkit real instalado, sem tocar em rede alguma.
    from datetime import datetime, timezone

    agora = __import__("time").time()
    fresco = datetime.fromtimestamp(agora - 60, timezone.utc).isoformat().replace("+00:00", "Z")

    class LocalClient(D.DMClient):
        def __init__(self):
            super().__init__(lambda: "tok", self_did="did:plc:BOT")
            self.sent = []
            self.calls = []

        def call(self, method, nsid, *, params=None, body=None):
            self.calls.append((method, nsid))
            if nsid == D.NS_LIST:
                return 200, {"convos": [
                    {"id": "c_ok", "status": "accepted", "kind": "direct",
                     "members": [{"did": "d1"}, {"did": "did:plc:BOT"}]},
                    {"id": "c_spam", "status": "accepted", "kind": "direct",
                     "members": [{"did": "d2"}, {"did": "did:plc:BOT"}]},
                    {"id": "c_optout", "status": "accepted", "kind": "direct",
                     "members": [{"did": "d3"}, {"did": "did:plc:BOT"}]},
                    {"id": "c_request", "status": "request", "kind": "direct",
                     "members": [{"did": "d4"}, {"did": "did:plc:BOT"}]},
                ]}
            if nsid == D.NS_MESSAGES:
                cid = (params or {}).get("convoId")
                textos = {
                    "c_ok": ("Oi, tudo bem com você hoje?", "did:plc:HUMANO"),
                    "c_spam": ("FREE AIRDROP 100x join telegram dm me", "did:plc:H2"),
                    "c_optout": ("não responda mais, por favor", "did:plc:H3"),
                    "c_request": ("Oi!", "did:plc:H4"),
                }
                txt, snd = textos.get(cid, ("", ""))
                return 200, {"messages": [
                    {"id": "m_" + str(cid), "text": txt,
                     "sender": {"did": snd}, "sentAt": fresco}]}
            if nsid == D.NS_SEND:
                self.sent.append(body)
                return 200, {}
            return 404, {}

    lc = LocalClient()
    cfg2 = D.Config.from_env()
    cfg2.self_did = "did:plc:BOT"
    cfg2.shadow = True
    cfg2.enabled = True
    st2 = D.DMState(Path(os.environ.get("DM_FIXTURE_STATE", ROOT / "data" / "dm_fixture_state.json")))
    bot2 = D.DMBot(lc, st2, cfg2, reply_fn=reply_fn)
    r2 = bot2.run_once()
    esp = {
        "teria_enviado": r2.get("shadow_teria_enviado", 0),
        "bloqueadas": r2.get("bloqueadas", 0),
        "enviadas": r2.get("enviadas", 0),
        "send_calls": sum(1 for _, n in lc.calls if n == D.NS_SEND),
        "motivos": sorted(r2.get("motivos", {}).keys()),
    }
    print("DIAG FIXTURE: " + json.dumps(esp, ensure_ascii=False, sort_keys=True))
    emit(fx_teria_enviado=esp["teria_enviado"], fx_bloqueadas=esp["bloqueadas"],
         fx_enviadas=esp["enviadas"], fx_send_calls=esp["send_calls"])

    if resumo["erros"] and not leitura_ok:
        print("DIAG: listConvos nao alcancado")
        return 1
    if resumo["enviadas"] != 0 or envios:
        print("DIAG: FALHA CRITICA — shadow enviou mensagem")
        return 1
    if esp["enviadas"] != 0 or esp["send_calls"] != 0:
        print("DIAG: FALHA CRITICA — fixture enviou mensagem")
        return 1
    if esp["teria_enviado"] != 1 or esp["bloqueadas"] != 3:
        print(f"DIAG: guard nao se comportou como esperado: {esp}")
        return 1
    print("DIAG: SHADOW_OK — zero mensagens enviadas, 1 elegivel, 3 bloqueadas")
    return 0


if __name__ == "__main__":
    sys.exit(main())

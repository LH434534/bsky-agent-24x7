#!/usr/bin/env python3
"""Teste de integração REAL do toolkit dentro de core/atproto.py.

Roda no sandbox e no runner do GitHub.
Sem rede e sem credenciais: Bsky é instanciado, _call/ensure_fresh são
substituídos por fakes e verificamos o record que seria enviado ao PDS.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.atproto import ATProtoError, AuthError, Bsky  # noqa: E402
import core.toolkit as tk  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def new_client(tmp):
    b = Bsky("@teste.bsky.social", "senha-fake",
             session_file=str(Path(tmp) / "session.json"))
    b.did = "did:plc:teste"
    b.access = "access-fake"
    b.refresh = "refresh-fake"
    b.ensure_fresh = Mock()
    b._save_session = Mock()
    b._load_session = Mock(return_value=False)
    captured = {}

    def fake_call(method, nsid, **kw):
        captured["body"] = kw.get("body")
        if nsid == "com.atproto.repo.createRecord":
            return {"uri": "at://did:plc:teste/app.bsky.feed.post/abc",
                    "cid": "bafyfake"}
        return {}
    b._call = fake_call
    return b, captured


print("\n=== 1. post(): idioma real em vez de pt-BR fixo ===")
with tempfile.TemporaryDirectory() as td:
    b, cap = new_client(td)
    b.post("Good morning, the weather is beautiful today and we should talk about it.")
    rec = cap["body"]["record"]
    check("langs = ['en']", rec["langs"] == ["en"], str(rec["langs"]))
    check("texto preservado", "Good morning" in rec["text"])

print("\n=== 2. post(): ftfy corrige mojibake antes de publicar ===")
with tempfile.TemporaryDirectory() as td:
    b, cap = new_client(td)
    b.post("The Mona Lisa doesnÃ¢â‚¬â„¢t have eyebrows.")
    rec = cap["body"]["record"]
    check("apóstrofo correto", "doesn't" in rec["text"], rec["text"])
    check("nenhum Ã restou", "Ã" not in rec["text"])

print("\n=== 3. post(): langs explícitos continuam tendo precedência ===")
with tempfile.TemporaryDirectory() as td:
    b, cap = new_client(td)
    b.post("Good morning everyone", langs=["pt-BR", "en"])
    check("langs explícitos", cap["body"]["record"]["langs"] == ["pt-BR", "en"])

print("\n=== 4. Outbox: segundo post idêntico é BLOQUEADO ===")
with tempfile.TemporaryDirectory() as td:
    b, cap = new_client(td)
    b.post("Mensagem única de teste")
    try:
        b.post("Mensagem única de teste")
        check("duplicata bloqueada", False, "postou duas vezes!")
    except ATProtoError as e:
        check("duplicata bloqueada", "duplicado" in str(e), str(e)[:80])
    b.post("Mensagem diferente de teste")
    check("texto diferente passa", True)

print("\n=== 5. Circuit breaker: 401 repetido não insiste ===")
with tempfile.TemporaryDirectory() as td:
    b, _ = new_client(td)
    r = Mock(status_code=401, text='{"error":"AuthenticationRequired"}')
    r.json.return_value = {"error": "AuthenticationRequired"}
    b.s.post = Mock(return_value=r)
    for i in range(2):
        try:
            b.login()
        except AuthError:
            pass
    check("guard abriu", b.guard.is_open)
    try:
        b.login()
        check("3ª login bloqueada", False, "deveria ter abortado")
    except RuntimeError as e:
        check("3ª login bloqueada", "circuito aberto" in str(e))
    check("s.post chamado só 2x", b.s.post.call_count == 2,
          f"chamadas={b.s.post.call_count}")

print("\n=== 6. TTLCache e RateLimitGuard presentes ===")
with tempfile.TemporaryDirectory() as td:
    b, _ = new_client(td)
    check("_handle_cache é TTLCache", type(b._handle_cache).__name__ == "TTLCache",
          type(b._handle_cache).__name__)
    check("rate guard instanciado", b.rate is not None)
    b.rate.observe({"ratelimit-remaining": "0"})
    check("rate guard detecta esgotamento", b.rate.should_pause())

print("\n=== 7. Kill switch BOT_TOOLKIT=0 mantém comportamento antigo ===")
with tempfile.TemporaryDirectory() as td:
    tk.ENABLED = False
    import core.atproto as ap
    ap.TOOLKIT_ON = False
    b, cap = new_client(td)
    b.post("Good morning, the weather is beautiful today and we should talk about it.")
    check("langs volta a pt-BR", cap["body"]["record"]["langs"] == ["pt-BR"],
          str(cap["body"]["record"]["langs"]))
    tk.ENABLED = True
    ap.TOOLKIT_ON = True

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

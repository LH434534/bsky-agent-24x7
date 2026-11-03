#!/usr/bin/env python3
"""
toolkit_selftest.py — valida o kit SEM rede, SEM credenciais e SEM GPU.

Roda no sandbox e no runner do GitHub:
    python scripts/toolkit_selftest.py
exit 0 = tudo OK | 1 = alguma asserção falhou
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.toolkit import (  # noqa: E402
    ENABLED, Outbox, PermanentFailureGuard, RateLimitGuard, TransientError,
    clean_text, describe, detect_language, handle_cache, health_snapshot,
    human_uptime, network_retry, prepare_post, sentiment, tone_hint,
    validate_json_file,
)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


print("\n=== 0. bibliotecas carregadas ===")
d = describe()
for k, v in d["libs"].items():
    check(f"lib {k}", v, "presente" if v else "AUSENTE (fallback ativo)")
check("kill switch ligado por padrão", ENABLED is True)

print("\n=== 1. ftfy: mojibake do feed ===")
broken = "The Mona Lisa doesnÃ¢â‚¬â„¢t have eyebrows."
fixed = clean_text(broken)
check("mojibake corrigido", "doesn't" in fixed and "Ã" not in fixed, repr(fixed[:70]))
check("texto limpo é idempotente", clean_text(fixed) == fixed)
check("NFC aplicado", clean_text("cafe\u0301") == "café")
check("não lança com lixo", clean_text(None) == "" and clean_text(123) == "")

print("\n=== 2. langdetect: langs corretos no post ===")
for txt, esperado in [
    ("Bom dia, hoje o céu está lindo e o café está pronto, vamos conversar sobre isso tudo.", "pt-BR"),
    ("Good morning, the weather is beautiful today and we should talk about this whole thing.", "en"),
    ("Buenos días, el cielo está hermoso hoy y deberíamos hablar de todo esto ahora.", "es"),
]:
    got = detect_language(txt)
    check(f"detecta {esperado}", got == esperado, f"obtido={got}")
check("texto vazio → pt-BR", detect_language("") == "pt-BR")

print("\n=== 3. vaderSentiment: tom social ===")
check("positivo > 0", sentiment("I love this, absolutely wonderful!") > 0.4)
check("negativo < 0", sentiment("This is terrible, I hate it so much.") < -0.4)
check("sombrio classificado", tone_hint("I am devastated, this is the worst tragedy ever.") == "sombrio",
      tone_hint("I am devastated, this is the worst tragedy ever."))
check("neutro classificado", tone_hint("The meeting is at 3pm.") == "neutro")

print("\n=== 4. prepare_post: ponto de integração ===")
texto, langs, meta = prepare_post(broken)
check("texto corrigido na saída", "doesn't" in texto)
check("langs NÃO é pt-BR fixo", langs == ["en"], str(langs))
check("meta completa", all(k in meta for k in ("changed", "lang", "tone", "graphemes")))
t2, l2, m2 = prepare_post("Olá mundo", langs=["pt-BR", "en"])
check("langs explícitos têm precedência", l2 == ["pt-BR", "en"])

import core.toolkit as tk  # noqa: E402
# Sem reload: recarregar trocaria a identidade de TransientError e quebraria
# o isinstance() capturado no decorator tenacity.
_saved = tk.ENABLED
tk.ENABLED = False
off_text, off_langs, off_meta = tk.prepare_post(broken)
check("kill switch desliga a correção",
      off_text == broken.strip() and off_meta["toolkit"] == "off")
tk.ENABLED = _saved
check("kill switch religa", tk.ENABLED is True)

print("\n=== 5. Outbox: idempotência (o bug mais comum em bots) ===")
with tempfile.TemporaryDirectory() as td:
    ob = Outbox(Path(td) / "outbox.json", window_s=60)
    check("chave estável", ob.key("Olá") == ob.key(" Olá "))
    check("não visto antes", not ob.seen("primeiro post"))
    ob.mark("primeiro post")
    check("visto após mark", ob.seen("primeiro post"))
    check("hash diferente não colide", not ob.seen("segundo post"))
    raw = json.loads((Path(td) / "outbox.json").read_text())
    check("JSON válido com entries", isinstance(raw.get("entries"), dict))
    ob.clear()
    check("clear funciona", not ob.seen("primeiro post"))
    ob2 = Outbox(Path(td) / "outbox.json", window_s=0)
    ob2.mark("expira")
    time.sleep(0.01)
    check("janela expira", not ob2.seen("expira"))

print("\n=== 6. tenacity: retry SÓ no transitório ===")
tentativas = {"n": 0}


@network_retry
def flaky():
    tentativas["n"] += 1
    if tentativas["n"] < 3:
        raise TransientError("502 UpstreamFailure")
    return "ok"


check("3ª tentativa sucesso", flaky() == "ok", f"tentativas={tentativas['n']}")
check("retry aconteceu", tentativas["n"] == 3)

permanentes = {"n": 0}


@network_retry
def auth_fail():
    permanentes["n"] += 1
    raise ValueError("401 AuthenticationRequired")  # NÃO é TransientError


try:
    auth_fail()
except ValueError:
    pass
check("401 NÃO é repetido", permanentes["n"] == 1, f"tentativas={permanentes['n']}")

print("\n=== 7. circuit breaker: fim das 532 retentativas ===")
g = PermanentFailureGuard(threshold=2, reset_s=0.2)
g.check()
g.record_failure()
g.check()
g.record_failure()
check("circuito abre na 2ª falha", g.is_open)
try:
    g.check()
    check("check() bloqueia", False, "não levantou")
except RuntimeError:
    check("check() bloqueia", True, "mensagem explica a ação humana")
time.sleep(0.25)
check("circuito fecha após reset", not g.is_open)
g.record_failure()
g.record_success()
check("sucesso zera contador", g.fails == 0)

print("\n=== 8. cachetools: TTL no cache de handles ===")
c = handle_cache(maxsize=8, ttl=60)
c["@alice"] = "did:plc:abc"
check("TTLCache ou dict funcional", c.get("@alice") == "did:plc:abc")

print("\n=== 9. RateLimitGuard: respeita ratelimit-reset ===")
rl = RateLimitGuard()
rl.observe({"ratelimit-remaining": "0", "ratelimit-reset": str(int(time.time()) + 45)})
check("should_pause quando esgotado", rl.should_pause())
check("wait até reset", 40 < rl.wait_seconds() <= 46, f"{rl.wait_seconds():.1f}s")
check("snapshot serializável", isinstance(json.dumps(rl.snapshot()), str))
rl2 = RateLimitGuard()
rl2.observe({"ratelimit-remaining": "2999"})
check("não pausa com budget", not rl2.should_pause())

print("\n=== 10. psutil + humanize + jsonschema ===")
h = health_snapshot()
check("telemetria tem mem_pct", "mem_pct" in h, str(list(h)[:6]))
up = human_uptime(3700)
check("uptime é duração, não '56 years ago'",
      isinstance(up, str) and "56 years" not in up and ("hour" in up or "h" in up), up)
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "state.json"
    p.write_text(json.dumps({"entries": {}}))
    check("schema válido aceito", validate_json_file(p, Outbox.SCHEMA))
    p.write_text(json.dumps({"quebrado": 1}))
    check("schema inválido rejeitado", not validate_json_file(p, Outbox.SCHEMA))

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

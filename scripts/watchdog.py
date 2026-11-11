#!/usr/bin/env python3
"""watchdog.py — supervisor com circuit breaker para o workflow principal.

Se não houver run ativo, o watchdog reativa o bot. Para evitar tempestades de
runs quando existe uma falha persistente (por exemplo, App Password inválida),
aplica backoff exponencial às falhas consecutivas e pausa após três falhas.
Uma execução cancelada manualmente também permanece parada até novo dispatch.

Variáveis:
  GH_TOKEN          token com actions:write (no Actions, use github.token)
  GH_REPO           owner/repo (no Actions, github.repository)
  WORKFLOW_FILE     Nome do arquivo do workflow (padrão abaixo)
  WATCHDOG_DRY_RUN  1 = só decide, não dispara (para testes)
  GITHUB_API        base da API (permite mock local)

Saída: imprime ATIVO / AGUARDANDO / PAUSADO / DISPARADO / DRY-RUN / ERRO.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
REPO = os.environ.get("GH_REPO", "")
TOKEN = os.environ.get("GH_TOKEN", "")
WF = os.environ.get("WORKFLOW_FILE", "bsky-bot-24x7.yml")
DRY = os.environ.get("WATCHDOG_DRY_RUN") == "1"
BASE_BACKOFF_SECONDS = 15 * 60
MAX_BACKOFF_SECONDS = 60 * 60
MAX_CONSECUTIVE_FAILURES = 3
FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}


def emit(decision: str) -> None:
    """Expõe a decisão para o GitHub Actions (steps nomeados = legível via API)."""
    print(decision, flush=True)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"decision={decision}\n")


def call(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "bsky-watchdog/1.0")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
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
            return e.code, {"message": raw[:200].decode("utf8", "replace")}
    except Exception as e:
        return 0, {"message": str(e)}


def _timestamp(value: str) -> float:
    """Converte timestamp ISO-8601 do GitHub para epoch; exige fuso explícito."""
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp sem fuso")
    return parsed.timestamp()


def recovery_guard(runs, now=None):
    """Retorna AGUARDANDO/PAUSADO se a recuperação automática não deve rodar.

    `runs` é a lista `workflow_runs` da API. Um sucesso quebra a sequência de
    falhas. Timestamp ausente/inválido bloqueia de forma conservadora.
    """
    if not runs:
        return None
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    ordered = sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)
    latest = ordered[0]
    conclusion = latest.get("conclusion")

    if conclusion == "cancelled":
        return "PAUSADO: última sessão cancelada manualmente; use workflow_dispatch para reativar"

    streak = 0
    for run in ordered:
        result = run.get("conclusion")
        if result in FAILURE_CONCLUSIONS:
            streak += 1
        elif result == "success":
            break
        else:
            break

    if streak == 0:
        return None
    if streak >= MAX_CONSECUTIVE_FAILURES:
        return (f"PAUSADO: {streak} falhas consecutivas; corrija a causa e use "
                "workflow_dispatch para reativar")

    try:
        ended_at = _timestamp(latest.get("updated_at", ""))
    except (TypeError, ValueError):
        return "AGUARDANDO: timestamp da última falha inválido; recuperação bloqueada por segurança"
    delay = min(BASE_BACKOFF_SECONDS * (2 ** (streak - 1)), MAX_BACKOFF_SECONDS)
    remaining = delay - max(0, now - ended_at)
    if remaining > 0:
        return (f"AGUARDANDO: backoff de {streak} falha(s); nova tentativa em "
                f"{int(remaining)}s")
    return None


def main() -> int:
    if not REPO or not TOKEN:
        emit("ERRO: GH_REPO / GH_TOKEN ausentes")
        return 2

    # Verifica os estados que significam "já tem sessão a caminho".
    for status in ("in_progress", "queued"):
        st, d = call("GET", f"/repos/{REPO}/actions/workflows/{WF}/runs"
                            f"?per_page=20&status={status}")
        n = d.get("total_count", 0)
        if st == 200 and n > 0:
            emit(f"ATIVO: {n} run(s) '{status}' — nada a fazer")
            return 0
        if st != 200:
            emit(f"ERRO: consulta {status} devolveu HTTP {st}")
            return 2

    st, d = call("GET", f"/repos/{REPO}/actions/workflows/{WF}/runs"
                        "?per_page=10&status=completed")
    if st != 200:
        emit(f"ERRO: consulta das últimas execuções devolveu HTTP {st}")
        return 2
    guard = recovery_guard(d.get("workflow_runs", []))
    if guard:
        emit(guard)
        return 0

    if DRY:
        emit("DRY-RUN: nenhuma sessão ativa ou em backoff — dispararia workflow_dispatch")
        return 0

    st, _ = call("POST", f"/repos/{REPO}/actions/workflows/{WF}/dispatches",
                 {"ref": "main"})
    if st in (200, 204):
        emit("DISPARADO: nova sessão iniciada (HTTP %s)" % st)
        return 0
    emit(f"ERRO: dispatch devolveu HTTP {st}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

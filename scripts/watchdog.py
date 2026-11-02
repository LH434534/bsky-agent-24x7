#!/usr/bin/env python3
"""
watchdog.py — supervisor de cobertura 24h para o workflow principal.

Problema que resolve:
  • o cron do GitHub Actions atrasa (e pode nem disparar) sob carga;
  • o job principal dura 330 min e o cron atual só tem 4 horários/dia
    → sobram janelas de ~30 min entre sessões;
  • se um job cai no meio, ninguém reinicia até o próximo cron.

Lógica: a cada execução, pergunta à API se existe algum run do workflow
principal em andamento (in_progress) ou na fila (queued). Se não houver,
dispara workflow_dispatch. Nunca cria duas sessões sobrepostas.

Variáveis:
  GH_TOKEN          token com actions:write (no Actions, use github.token)
  GH_REPO           owner/repo (no Actions, github.repository)
  WORKFLOW_FILE    Nome do arquivo do workflow (padrão abaixo)
  WATCHDOG_DRY_RUN 1 = só decide, não dispara (para testes)
  GITHUB_API        base da API (permite mock local)

Saída: imprime "ATIVO" / "DISPARADO" / "DRY-RUN" / "ERRO".
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
REPO = os.environ.get("GH_REPO", "")
TOKEN = os.environ.get("GH_TOKEN", "")
WF = os.environ.get("WORKFLOW_FILE", "bsky-bot-24x7.yml")
DRY = os.environ.get("WATCHDOG_DRY_RUN") == "1"


def emit(decision: str) -> None:
    """Expõe a decisão para o GitHub Actions (steps nomeados = legível via API)."""
    print(decision)
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


def main() -> int:
    if not REPO or not TOKEN:
        emit("ERRO: GH_REPO / GH_TOKEN ausentes")
        return 2

    # Verifica os dois estados que significam "já tem sessão a caminho".
    # per_page=20 cobre filas longas sem custo extra.
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

    if DRY:
        emit("DRY-RUN: nenhuma sessão ativa — dispararia workflow_dispatch")
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

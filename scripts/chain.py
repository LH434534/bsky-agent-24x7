#!/usr/bin/env python3
"""
chain.py — renova a sessão do bot sem depender do scheduler do GitHub.

Ideia: cada execução do workflow, AO TERMINAR (if: always()), dispara a
próxima via workflow_dispatch. Assim a cadeia se sustenta sozinha:
não depende de cron, não deixa os ~30 min de buraco entre jobs de 330 min,
e reinicia automaticamente mesmo quando o job cai no meio.

O watchdog (a cada 15 min) continua como SEGUNDA camada, cobrindo o caso de
este step final falhar. O cron original fica como TERCEIRA camada.

Guardrails (importante — um loop sem freio queimaria minutos/atrasaria tudo):
  • kill switch: vars.BOT_CHAIN = "0" para a cadeia imediatamente
  • exclui o PRÓPRIO run da checagem de "já tem sessão ativa"
    (senão nunca dispararia, pois ele ainda está in_progress)
  • não dispara se o run foi cancelado manualmente
  • falha instantânea (< 120 s) NÃO dispara: evita storm; o watchdog cobre
  • checa in_progress E queued antes de disparar (sem bots duplicados)
  • dry-run para testar a decisão sem criar runs

Modo probe (teste fim-a-fim, finito):
  --gen N --max-gen M  → repassa chain_gen=N+1 e para em M.
  Permite PROVAR o encadeamento real em ~1 minuto com 4 runs.

Saída (também em $GITHUB_OUTPUT para steps nomeados):
  DESLIGADO | CANCELADO | ATIVO | AGUARDANDO | DISPARADO | DRY-RUN | ERRO
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GH_REPO", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
JOB_STATUS = os.environ.get("CHAIN_JOB_STATUS", "")
ENABLED = os.environ.get("CHAIN_ENABLED", "1")
DRY = os.environ.get("CHAIN_DRY_RUN") == "1"
PROBE = os.environ.get("CHAIN_PROBE") == "1"
MIN_SECONDS = 120
WF_DEFAULT = "bsky-bot-24x7.yml"

DECISION = ""


def emit(msg: str) -> None:
    global DECISION
    DECISION = msg
    print(msg, flush=True)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("decision=" + msg.replace("\n", " ") + "\n")


def call(method: str, path: str, body=None):
    req = urllib.request.Request(API + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "bsky-chain/1.0")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        # 422/403/404 precisam virar "ERRO" legível, não traceback silencioso.
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"message": raw[:200].decode("utf8", "replace")}
    except Exception as e:
        return 0, {"message": str(e)}


def outros_ativos(wf: str) -> int:
    """Conta runs ativos/na fila EXCLUINDO este próprio run."""
    total = 0
    for status in ("in_progress", "queued"):
        st, d = call("GET", f"/repos/{REPO}/actions/workflows/{wf}/runs"
                            f"?per_page=20&status={status}")
        if st != 200:
            raise RuntimeError(f"consulta {status} -> HTTP {st}")
        for r in d.get("workflow_runs", []):
            if str(r["id"]) != str(RUN_ID):
                total += 1
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default=WF_DEFAULT)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--gen", type=int, default=None, help="geração atual (modo probe)")
    ap.add_argument("--max-gen", type=int, default=3, help="para nesta geração")
    a = ap.parse_args()

    if not TOKEN or not REPO:
        emit("ERRO: GH_TOKEN / GH_REPO ausentes")
        return 2

    if ENABLED in ("0", "false", "no", "off"):
        emit("DESLIGADO: kill switch BOT_CHAIN=0")
        return 0

    if JOB_STATUS == "cancelled":
        emit("CANCELADO: run cancelado manualmente — cadeia interrompida")
        return 0

    # Anti-storm: falha instantânea não renova; o watchdog de 15 min cobre.
    if not PROBE:
        try:
            start = float(os.environ.get("CHAIN_START_TS", "0"))
            dur = time.time() - start if start else 9999
        except ValueError:
            dur = 9999
        if JOB_STATUS not in ("success", "") and dur < MIN_SECONDS:
            emit(f"AGUARDANDO: falha em {dur:.0f}s — watchdog retoma em <=15 min")
            return 0

    # Modo probe: cadeia finita para prova real sem loop infinito.
    if a.gen is not None and a.gen >= a.max_gen:
        emit(f"FIM: geracao {a.gen} >= max {a.max_gen} — cadeia encerrada")
        return 0

    try:
        n = outros_ativos(a.workflow)
    except Exception as e:
        emit(f"ERRO: {str(e)[:120]}")
        return 2

    if n > 0 and not PROBE:
        emit(f"ATIVO: {n} outra(s) sessao(oes) em andamento/fila — nao duplico")
        return 0

    body = {"ref": a.ref}
    if a.gen is not None:
        body["inputs"] = {"chain_gen": str(a.gen + 1)}

    if DRY:
        emit(f"DRY-RUN: dispararia {a.workflow} (gen={a.gen}, outros_ativos={n})")
        return 0

    st, _ = call("POST", f"/repos/{REPO}/actions/workflows/{a.workflow}/dispatches", body)
    if st in (200, 204):
        nxt = f" gen={a.gen + 1}" if a.gen is not None else ""
        emit(f"DISPARADO: proxima sessao de {a.workflow}{nxt} (HTTP {st})")
        return 0
    emit(f"ERRO: dispatch -> HTTP {st}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

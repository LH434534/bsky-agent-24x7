#!/usr/bin/env python3
"""
resilience_selftest.py — prova objetiva das correções de disponibilidade.

Cobre o incidente real: bot parado ~8h38 entre #155 e #156 porque
nenhum schedule disparou e a cadeia de renovação dependia de steps que
não rodam quando o JOB estoura timeout.

    python scripts/resilience_selftest.py
exit 0 = OK | 1 = falha
"""
from __future__ import annotations

import ast
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import yaml
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if ok: PASS += 1
    else: FAIL += 1


def load(p):
    return (ROOT / p).read_text(encoding="utf-8")


print("\n=== 1. segredo: causa raiz do incidente fechada ===")
gi = load(".gitignore")
check(".gitignore existe e cobre session.json", "session.json" in gi)
check("cobre JWT/env", ".jwt" in gi and ".env" in gi)
def _versionado(rel):
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", rel],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return (ROOT / rel).exists()
check("nenhum session.json versionado", not _versionado("data/session.json"))
check("gate anti-vazamento existe", (ROOT / "scripts" / "secret_gate.py").exists())
r = subprocess.run([sys.executable, "scripts/secret_gate.py", "--repo", "."],
                   cwd=ROOT, capture_output=True, text=True, timeout=90)
check("secret_gate retorna OK no repo atual", r.returncode == 0,
      r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"rc={r.returncode}")

# O CI #5 falhou aqui: 4 falsos positivos (template .env.example, a própria
# docstring do gate e o relatório citando `xxxx-xxxx-xxxx-xxxx`). Trava os
# dois lados: placeholder é permitido, segredo REAL continua bloqueado.
sys.path.insert(0, str(ROOT / "scripts"))
import secret_gate as SG  # noqa: E402
check("placeholder xxxx-xxxx-xxxx-xxxx NAO é segredo",
      SG.segredo_real("xxxx-xxxx-xxxx-xxxx") is False)
check("placeholder your-xxxx-xxxx-xxxx NAO é segredo",
      SG.segredo_real("your-xxxx-xxxx-xxxx") is False)
check("App Password real É segredo", SG.segredo_real("k574-uofr-ped3-6jim") is True)  # gate:fixture
check(".env.example é template permitido", bool(SG.TEMPLATE_OK.search("deploy/.env.example")))
check(".env real continua proibido", not SG.TEMPLATE_OK.search("deploy/.env"))
check("session.json continua proibido", not SG.TEMPLATE_OK.search("data/session.json"))
with tempfile.TemporaryDirectory() as td:
    p = Path(td)
    (p / ".gitignore").write_text("data/session.json\n")
    (p / "deploy").mkdir(); (p / "deploy" / ".env.example").write_text("PWD=xxxx-xxxx-xxxx-xxxx\n")
    ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "secret_gate.py"), "--repo", str(p)],
                        capture_output=True, text=True, timeout=60).returncode
    check("repo só com template → OK", ok == 0, f"rc={ok}")
    (p / "deploy" / ".env").write_text("PWD=k574-uofr-ped3-6jim\n")  # gate:fixture
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "secret_gate.py"), "--repo", str(p)],
                         capture_output=True, text=True, timeout=60)
    check(".env com senha real → BLOQUEADO", bad.returncode == 1,
          bad.stdout.strip().splitlines()[-1] if bad.stdout.strip() else "")
    (p / "deploy" / ".env").unlink()
    (p / "leak.txt").write_text("token ghp_abcdefghijklmnopqrstuvwxyz1234567890\n")  # gate:fixture
    bad2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "secret_gate.py"), "--repo", str(p)],
                          capture_output=True, text=True, timeout=60)
    check("GitHub PAT real → BLOQUEADO", bad2.returncode == 1)

print("\n=== 2. workflow principal: timeout de STEP < timeout de JOB ===")
wf = ROOT / ".github" / "workflows" / "bsky-bot-24x7.yml"
d = yaml.safe_load(wf.read_text())
job = d["jobs"]["bot"]
steps = job["steps"]
job_to = job["timeout-minutes"]
run = next(s for s in steps if s.get("name") == "run agent")
chain = next(s for s in steps if s.get("name") == "renovar sessao (24h)")
check("job timeout = 350", job_to == 350, str(job_to))
check("step 'run agent' TEM timeout próprio", "timeout-minutes" in run, str(run.get("timeout-minutes")))
check("step timeout < job timeout", run["timeout-minutes"] < job_to,
      f"{run['timeout-minutes']} < {job_to}")
check("step 'renovar sessao' if: always()", str(chain.get("if")) == "always()")
check("step 'renovar sessao' tem teto de tempo", "timeout-minutes" in chain)
env_txt = run["env"]["RUN_MINUTES"]
m = re.search(r"\|\|\s*'(\d+)'", str(env_txt))
run_min = int(m.group(1)) if m else -1
check("RUN_MINUTES < step timeout", 0 < run_min < run["timeout-minutes"],
      f"{run_min} < {run['timeout-minutes']}")
check("folga >= 20 min para os steps finais", run["timeout-minutes"] - run_min >= 20,
      f"{run['timeout-minutes'] - run_min} min")
check("chain.py usa --retries", "--retries" in str(chain.get("run", "")))
check("persist state bloqueia segredo antes do push",
      "segredo seria commitado" in wf.read_text())

print("\n=== 3. watchdogs: minutos fora do pico (menos atraso de schedule) ===")
for f in ("watchdog.yml", "watchdog-prime.yml"):
    t = (ROOT / ".github" / "workflows" / f).read_text()
    crons = re.findall(r'cron:\s*"([^"]+)"', t)
    check(f"{f} tem cron", bool(crons), str(crons))
    for c in crons:
        minutos = c.split()[0]
        nums = [int(x) for x in re.findall(r"\d+", minutos)] if minutos != "*" else []
        check(f"{f}: evita minuto 0/30 de pico", 0 not in nums and 30 not in nums,
              minutos)
wd_txt = (ROOT / ".github" / "workflows" / "watchdog.yml").read_text()
check("watchdog tem workflow_dispatch (teste manual)", "workflow_dispatch:" in wd_txt)

print("\n=== 4. nenhum subprocesso sem timeout (travamento = job morto) ===")
sem = []
for p in sorted(ROOT.rglob("*.py")):
    if ".git" in p.parts: continue
    try: t = ast.parse(p.read_text())
    except Exception: continue
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            recv = n.func.value.id if isinstance(n.func.value, ast.Name) else ""
            if recv == "subprocess" and n.func.attr in ("run", "check_output", "call", "check_call"):
                if "timeout" not in {k.arg for k in n.keywords}:
                    sem.append(f"{p.relative_to(ROOT)}:{n.lineno}")
check("0 subprocess.run sem timeout", not sem, str(sem[:5]))
sem_to = []
for p in sorted(ROOT.rglob("*.py")):
    if ".git" in p.parts: continue
    try: t = ast.parse(p.read_text())
    except Exception: continue
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in ("post", "get", "request", "put", "delete"):
                recv = n.func.value
                is_session = (isinstance(recv, ast.Attribute) and recv.attr == "s") or \
                             (isinstance(recv, ast.Name) and recv.id in ("requests", "session"))
                if is_session and "timeout" not in {k.arg for k in n.keywords}:
                    sem_to.append(f"{p.relative_to(ROOT)}:{n.lineno}")
check("0 chamadas HTTP sem timeout", not sem_to, str(sem_to[:5]))

print("\n=== 5. estados corrompidos não derrubam o bot ===")
for mod, cls, fn in [
    ("core.antispam", "SpamGuard", "spam_state.json"),
    ("core.agency", "Agency", "agency_state.json"),
    ("core.adaptive", "Adaptive", "adaptive_state.json"),
    ("core.conductor", "Conductor", "conductor_state.json"),
    ("core.growth", "Growth", "growth_state.json"),
    ("core.immune", "Immune", "immune_state.json"),
    ("core.social", "Social", "social_state.json"),
    ("core.images", "Images", "images_state.json"),
]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / fn; p.write_text('{"quebrado": ')
        try:
            C = getattr(importlib.import_module(mod), cls)
            try: obj = C(p)
            except TypeError:
                obj = C(); obj.file = p
            obj.load()
            check(f"{cls} sobrevive a JSON truncado", True)
        except Exception as e:
            check(f"{cls} sobrevive a JSON truncado", False, f"{type(e).__name__}: {e}")

print("\n=== 6. todos os módulos importam (pega import removido por engano) ===")
mods = [f"core.{p.stem}" for p in sorted((ROOT / "core").glob("*.py")) if p.stem != "__init__"]
mods += ["gh_runner", "main", "immortal"]
bad = []
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append(f"{m}:{type(e).__name__}")
check(f"{len(mods)}/{len(mods)} módulos importam", not bad, str(bad[:4]))

print("\n=== 7. telemetria não some em silêncio ===")
gh = load("gh_runner.py")
check("log() não usa 'except: pass'", "except Exception as e:" in gh and
      "arquivo indisponivel" in gh)
check("scheduler sem código morto t0/spent",
      "t0 = time.time()" not in load("core/scheduler.py"))

print("\n=== 8. chain.py: matriz de decisão REAL contra API mock ===")
class Handler(BaseHTTPRequestHandler):
    estado = {"ativo": 0, "dispatch_status": 204, "falhas": 0}
    def log_message(self, *a): pass
    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if "runs" in self.path:
            runs = ([{"id": 999}] * self.estado["ativo"]) if "status=in_progress" in self.path else []
            self._send(200, {"workflow_runs": runs})
        else: self._send(200, {})
    def do_POST(self):
        if self.estado["falhas"] > 0:
            self.estado["falhas"] -= 1; self._send(503, {"message": "transient"}); return
        self._send(self.estado["dispatch_status"], {})

srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
sys.path.insert(0, str(ROOT / "scripts"))
import chain as CH  # noqa: E402


def run_chain(extra_env, args=None):
    global CH
    env = {"GITHUB_API": base, "GH_TOKEN": "x", "GH_REPO": "o/r",
           "GITHUB_RUN_ID": "1", "CHAIN_ENABLED": "1", "CHAIN_START_TS": "0",
           "CHAIN_JOB_STATUS": "", "CHAIN_PROBE": "0", "CHAIN_DRY_RUN": "0"}
    env.update(extra_env)
    old = dict(os.environ)
    os.environ.clear(); os.environ.update(env)
    sys.argv = ["chain.py"] + (args or [])
    # chain.py lê ENABLED/API/DRY no nível de módulo: precisa reler a cada
    # cenário, senão o 1º import congelaria todas as decisões.
    CH = importlib.reload(CH)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            try: rc = CH.main()
            except SystemExit as e: rc = e.code or 0
    finally:
        os.environ.clear(); os.environ.update(old)
    return rc, CH.DECISION, buf.getvalue()

Handler.estado.update({"ativo": 0, "dispatch_status": 204, "falhas": 0})
rc, dec, _ = run_chain({"CHAIN_ENABLED": "0"})
check("kill switch → DESLIGADO", dec.startswith("DESLIGADO"), dec[:50])
rc, dec, _ = run_chain({"CHAIN_JOB_STATUS": "cancelled"})
check("cancelamento → CANCELADO (não renova)", dec.startswith("CANCELADO"), dec[:50])
rc, dec, _ = run_chain({"CHAIN_JOB_STATUS": "failure", "CHAIN_START_TS": str(time.time() - 5)})
check("falha em <120s → AGUARDANDO (anti-storm)", dec.startswith("AGUARDANDO"), dec[:60])
Handler.estado["ativo"] = 1
rc, dec, _ = run_chain({})
check("outra sessão ativa → ATIVO (sem duplicata)", dec.startswith("ATIVO"), dec[:60])
Handler.estado["ativo"] = 0
rc, dec, _ = run_chain({}, ["--workflow", "b.yml", "--ref", "main"])
check("dispatch → DISPARADO", dec.startswith("DISPARADO") and rc == 0, dec[:60])
Handler.estado.update({"falhas": 1, "dispatch_status": 204})
rc, dec, out = run_chain({}, ["--retries", "2", "--retry-delay", "0"])
check("503 transitório → retry → DISPARADO", dec.startswith("DISPARADO") and "tentativa 1/2" in out,
      dec[:70])
Handler.estado.update({"falhas": 0, "dispatch_status": 500})
rc, dec, _ = run_chain({}, ["--retries", "1", "--retry-delay", "0"])
check("erro persistente → ERRO (não finge sucesso)", dec.startswith("ERRO") and rc != 0, dec[:70])
srv.shutdown()

print("\n=== 9. suítes existentes continuam verdes (regressão) ===")
for s in ("toolkit_selftest.py", "toolkit_integration.py", "human_selftest.py", "viral_selftest.py"):
    r = subprocess.run([sys.executable, f"scripts/{s}"], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    ultima = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"rc={r.returncode}"
    check(f"{s} rc=0", r.returncode == 0, ultima[:70])

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

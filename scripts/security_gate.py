#!/usr/bin/env python3
"""
security_gate.py — varredura estática do próprio repositório antes de cada sessão.

Based on Tencent Zhuque Lab AI-Infra-Guard
https://github.com/Tencent/AI-Infra-Guard
Licença: Apache-2.0 (atribuição exigida pelo NOTICE do projeto).

Este script é uma reimplementação ENXUTA, em stdlib puro, das categorias de risco
publicadas no static pre-scan do MCP Scan da A.I.G. Não substitui a ferramenta
oficial: roda em <1 s, sem API key, sem LLM e sem instalar nada — adequado para
um bot que executa scripts de instalação em toda sessão.

Uso:
    python scripts/security_gate.py --repo . --json /tmp/gate.json
exit 0 = OK | 1 = BLOQUEADO | 2 = erro de execução
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Categorias derivadas de AI-Infra-Guard / mcp-scan utils/pre_scan.py
RULES = [
    ("reverse_shell", re.compile(r"(socket\.connect|subprocess|/bin/(ba)?sh).*\d+\.\d+\.\d+\.\d+", re.I)),
    ("cloud_metadata", re.compile(r"169\.254\.169\.254|metadata\.google\.internal|metadata\.azure\.com", re.I)),
    ("credential_access", re.compile(r"(~/|HOME|USERPROFILE).*(\.ssh|\.aws|\.env|credentials|authorized_keys)", re.I)),
    # Sem DOTALL: a versão anterior casava um b64decode legítimo na linha 70
    # com a palavra "token" 100 linhas depois. Só é risco quando decodifica
    # E executa na mesma linha.
    ("encoded_payload", re.compile(
        r"(?:eval|exec)\s*\(\s*(?:base64\.)?(?:b64decode|urlsafe_b64decode)|"
        r"(?:base64\.)?(?:b64decode|urlsafe_b64decode)\s*\([^)]{0,120}\)\s*\)?"
        r"[^;\n]{0,60}(?:eval|exec)\s*\(", re.I)),
    ("ssh_key_write", re.compile(r"authorized_keys|id_rsa|\.ssh.*open\(.*w", re.I)),
    ("prompt_injection", re.compile(r"ignore\s+(previous|above|all)\s+(instructions?|rules?)|SYSTEM\s*OVERRIDE", re.I)),
    ("curl_pipe_exec", re.compile(r"curl\s+.*\|\s*(ba)?sh|wget\s+.*\|\s*(ba)?sh", re.I)),
]

CRITICOS = {"reverse_shell", "cloud_metadata", "credential_access",
            "encoded_payload", "ssh_key_write", "prompt_injection"}

# curl|sh conhecido e documentado: instalação do Ollama no setup_brain.sh.
# Fica como PENDENTE (não como "seguro"). Um QUARTO arquivo derruba o build.
CURL_BASELINE = {"install.sh", "install_immortal.sh", "scripts/setup_brain.sh"}

SKIP_DIRS = {".git", ".github", "__pycache__", "node_modules", "data", ".venv", "venv"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".db", ".pyc", ".woff", ".ttf"}
# Um scanner não se auto-avalia: as próprias regex de risco casariam consigo.
SKIP_FILES = {"security_gate.py"}
# Suítes de teste PRECISAM conter payloads de ataque para provar que o
# auditor bloqueia. O marcador só é honrado em arquivos cujo NOME é de teste
# e apenas na LINHA marcada — um `evil.py` com a mesma marca continua sendo
# denunciado. Ver scripts/gate_selftest.py (prova de nao-regressao).
FIXTURE_TAG = "# gate:fixture"
FIXTURE_FILES = re.compile(r"(?:^|/)(?:test_.*|.*_selftest|.*_test)\.py$", re.I)
# Só código executável pode "rodar" um curl|sh. Texto em .md é documentação.
EXEC_EXT = {".py", ".sh", ".bash", ".yml", ".yaml", ".ps1", ".bat", ".js", ".ts", ""}
MAX_BYTES = 400_000


def scan(repo: str) -> dict:
    root = Path(repo).resolve()
    achados = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            p = Path(dirpath) / fname
            if (fname in SKIP_FILES or p.suffix.lower() in SKIP_EXT
                    or p.stat().st_size > MAX_BYTES):
                continue
            rel = str(p.relative_to(root))
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Remove APENAS linhas explicitamente marcadas, e APENAS em
            # arquivos de teste. Todo o resto continua sendo varrido exatamente
            # como antes (inclusive padroes que cruzam varias linhas).
            if FIXTURE_FILES.search(rel):
                txt = "".join(ln for ln in txt.splitlines(keepends=True)
                              if FIXTURE_TAG not in ln)
            for categoria, rx in RULES:
                if not rx.search(txt):
                    continue
                if categoria == "curl_pipe_exec" and rel in CURL_BASELINE:
                    nivel, motivo = "PENDENTE", "curl|sh conhecido do bootstrap do Ollama — revisar checksum"
                elif categoria == "curl_pipe_exec" and p.suffix.lower() not in EXEC_EXT:
                    nivel, motivo = "ATENCAO", "citação em documentação (não executável)"
                elif categoria in CRITICOS:
                    nivel, motivo = "BLOQUEANTE", "padrão de risco crítico"
                elif categoria == "curl_pipe_exec":
                    nivel, motivo = "BLOQUEANTE", "novo curl|sh não autorizado"
                else:
                    nivel, motivo = "ATENCAO", "revisar"
                achados.append({"arquivo": rel, "categoria": categoria,
                                "nivel": nivel, "motivo": motivo})

    crit = [a for a in achados if a["nivel"] == "BLOQUEANTE"]
    pend = [a for a in achados if a["nivel"] == "PENDENTE"]
    return {
        "ferramenta": "security_gate (derivado de Tencent Zhuque Lab AI-Infra-Guard)",
        "url": "https://github.com/Tencent/AI-Infra-Guard",
        "repo": str(root),
        "total": len(achados),
        "por_nivel": dict(Counter(a["nivel"] for a in achados)),
        "por_categoria": dict(Counter(a["categoria"] for a in achados)),
        "bloqueantes": crit,
        "pendentes": [a["arquivo"] for a in pend],
        "achados": achados,
        "decisao": "BLOQUEADO" if crit else "OK",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", help="salva relatório completo")
    a = ap.parse_args()
    r = scan(a.repo)
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, ensure_ascii=False))
    print(f"  repo       : {r['repo']}")
    print(f"  achados    : {r['total']}  {r['por_nivel']}")
    print(f"  categorias : {r['por_categoria']}")
    for x in r["bloqueantes"][:12]:
        print(f"    BLOQUEANTE {x['arquivo']}: {x['categoria']}")
    if r["pendentes"]:
        print(f"  pendentes  : {', '.join(sorted(set(r['pendentes'])))}")
    print(f"  DECISAO    : {r['decisao']}")
    return 1 if r["decisao"] == "BLOQUEADO" else 0


if __name__ == "__main__":
    sys.exit(main())

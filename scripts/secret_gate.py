#!/usr/bin/env python3
"""
secret_gate.py — impede que credenciais voltem a um repositório PÚBLICO.

Motivo (incidente real de 2026-11-03): o repo é público, NÃO tinha
.gitignore, e `data/session.json` ficou no `main` desde 2026-10-04 com um
refreshJwt válido até 2027-01-02, legível anonimamente (HTTP 200). O
revoke_all_sessions.py invalidou os 3 branches; este gate impede a repetição.

Varre arquivos versionados e falha se encontrar:
  • JWT (access/refresh do ATProto)
  • GitHub PAT (ghp_ / github_pat_)
  • App Password do Bluesky (xxxx-xxxx-xxxx-xxxx)
  • chave privada PEM
  • arquivos chamados session.json / *.jwt / .env

NUNCA imprime o segredo: só caminho, linha e tipo.
Uso: python scripts/secret_gate.py --repo . [--json saida.json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("github_pat", re.compile(r"\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("bsky_app_password", re.compile(r"\b[a-z0-9]{4}(?:-[a-z0-9]{4}){3}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
]
BAD_NAMES = re.compile(r"(^|/)(session\.json|\.env(?:\..*)?|[^/]+\.jwt)$", re.I)
# Templates versionados são o caminho SEGURO de documentar variáveis.
# São explicitamente permitidos; o `.env` real continua proibido.
TEMPLATE_OK = re.compile(r"(^|/)\.env\.(example|sample|template|dist)$", re.I)
# Placeholders documentais: `xxxx-xxxx-xxxx-xxxx`, `your-...`, `<...>`.
# Um App Password REAL tem blocos alfanuméricos variados; um placeholder é
# repetição ou palavra óbvia. Sem este filtro o gate acusa a própria
# documentação — exatamente o falso positivo encontrado no CI #5.
PLACEHOLDER = re.compile(
    r"^(?:x{4}|y{4}|a{4}|0{4}|1{4}|\*{4}|<[^>]*>|your|seu_|sua_|example|"
    r"changeme|placeholder|dummy|teste|aaa|bbb|abcd)", re.I)


def segredo_real(valor: str) -> bool:
    """Descarta placeholders, mantém credenciais de verdade."""
    blocos = valor.split("-")
    if len(blocos) != 4:
        return False
    if any(PLACEHOLDER.search(b) for b in blocos):
        return False
    # 4 blocos iguais, ou blocos com um único caractere repetido
    if len(set(blocos)) == 1 or any(len(set(b)) <= 1 for b in blocos):
        return False
    return True


FIXTURE_TAG = "# gate:fixture"
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
SKIP_EXT = {".pyc", ".pyo", ".db", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4"}
MAX_BYTES = 2_000_000


def arquivos_versionados(repo: Path) -> list[Path]:
    """Arquivos versionados; fora de um repo git, varre o diretório.

    ATENÇÃO: `git ls-files` num diretório que não é repo devolve rc=128 com
    stdout VAZIO. Checar só `try/except` fazia o gate devolver "OK" sem
    varrer nada — falso negativo perigoso. Valida o returncode.
    """
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-files"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return [repo / x for x in r.stdout.splitlines() if x.strip()]
    except Exception:
        pass
    return [p for p in repo.rglob("*")
            if p.is_file() and not any(d in SKIP_DIRS for d in p.parts)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    achados: list[dict] = []

    for p in arquivos_versionados(repo):
        rel = str(p.relative_to(repo))
        if any(d in SKIP_DIRS for d in p.relative_to(repo).parts):
            continue
        if BAD_NAMES.search(rel) and not TEMPLATE_OK.search(rel):
            achados.append({"arquivo": rel, "linha": 0, "tipo": "nome_proibido"})
        if p.suffix.lower() in SKIP_EXT or p.stat().st_size > MAX_BYTES:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            # Suítes de teste PRECISAM conter segredos falsos para provar que
            # o gate bloqueia. Só linhas explicitamente marcadas são ignoradas;
            # qualquer segredo sem a marca continua sendo denunciado.
            if FIXTURE_TAG in ln:
                continue
            for tipo, rx in PATTERNS:
                m = rx.search(ln)
                if not m:
                    continue
                if tipo == "bsky_app_password" and not segredo_real(m.group(0)):
                    continue
                achados.append({"arquivo": rel, "linha": i, "tipo": tipo})

    gi = repo / ".gitignore"
    tem_gitignore = gi.exists() and "session.json" in gi.read_text(encoding="utf-8", errors="replace")
    if not tem_gitignore:
        achados.append({"arquivo": ".gitignore", "linha": 0, "tipo": "gitignore_ausente_ou_incompleto"})

    resumo = {"repo": str(repo), "achados": achados, "total": len(achados),
              "decisao": "BLOQUEADO" if achados else "OK"}
    print(f"  repo     : {repo}")
    print(f"  achados  : {len(achados)}")
    for x in achados[:20]:
        print(f"    {x['tipo']:<28} {x['arquivo']}:{x['linha']}")
    print(f"  DECISAO  : {resumo['decisao']}")
    if a.json:
        Path(a.json).write_text(json.dumps(resumo, indent=2, ensure_ascii=False))
    return 1 if achados else 0


if __name__ == "__main__":
    sys.exit(main())

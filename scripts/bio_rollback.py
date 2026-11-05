#!/usr/bin/env python3
"""Rollback MANUAL da bio — restaura o checkpoint gravado antes da troca.

Uso (no runner ou num host com as variáveis de credencial):
    python scripts/bio_rollback.py

Nunca é chamado automaticamente pelo bot. Requer BSKY_HANDLE e
BSKY_APP_PASSWORD no ambiente. Não imprime a descrição nem tokens.
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


def main() -> int:
    if not HANDLE or not PASSWORD:
        print("ERRO: BSKY_HANDLE/BSKY_APP_PASSWORD ausentes")
        return 2
    from core.atproto import Bsky
    from core.profile import BioState, rollback

    state = BioState().load()
    if not state.anterior:
        print("ERRO: nenhum checkpoint em data/bio_state.json — nada a restaurar")
        return 3
    try:
        b = Bsky(HANDLE, PASSWORD,
                 pds=os.environ.get("BSKY_PDS", "https://bsky.social"))
        b.login()
    except Exception as e:
        print(f"ERRO: login falhou ({type(e).__name__})")
        return 4
    r = rollback(b, state)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("restaurado") else 5


if __name__ == "__main__":
    sys.exit(main())

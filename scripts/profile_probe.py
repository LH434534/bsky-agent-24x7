#!/usr/bin/env python3
"""Probe SOMENTE-LEITURA do record app.bsky.actor.profile no runner real.

Confirma que `com.atproto.repo.getRecord` funciona com a App Password e
descobre qual base responde (entryway ou PDS real do didDoc).

NUNCA chama putRecord. NUNCA imprime a descrição, tokens, DIDs de terceiros
nem conteúdo de mensagens — só códigos, comprimentos e hashes.

Saída: 0 = leitura OK; 2 = falha (credencial/rede/rota).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HANDLE = (os.environ.get("BSKY_HANDLE") or "").strip()
PASSWORD = (os.environ.get("BSKY_APP_PASSWORD") or "").strip()


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
        emit(bio_ok="0", bio_motivo="secret_ausente", bio_put_calls="0")
        return 2

    from core.atproto import Bsky
    try:
        b = Bsky(HANDLE, PASSWORD,
                 pds=os.environ.get("BSKY_PDS", "https://bsky.social"))
        b.login()
    except Exception as e:
        print(f"DIAG: login falhou ({type(e).__name__})")
        emit(bio_ok="0", bio_motivo="login_falhou", bio_put_calls="0")
        return 2

    try:
        rec = b.get_profile_record()
    except Exception as e:
        print(f"DIAG: getRecord falhou ({type(e).__name__})")
        emit(bio_ok="0", bio_motivo="getrecord_falhou",
             login_ok="1", bio_put_calls="0")
        return 2

    value = rec.get("value") or {}
    desc = str(value.get("description") or "")
    cid = str(rec.get("cid") or "")
    base = str(rec.get("base") or "")
    campos = sorted(str(k) for k in value.keys())
    h = hashlib.sha256(desc.encode("utf-8")).hexdigest()[:16]

    print("DIAG: PROFILE OK (somente leitura, zero putRecord)")
    emit(bio_ok="1", login_ok="1", bio_cid=bool(cid),
         bio_tem_descricao=bool(desc), bio_len=len(desc),
         bio_hash=h, bio_base=("pds_real" if base and base != b.pds else "entryway"),
         bio_campos=",".join(campos)[:110], bio_put_calls="0")
    return 0


if __name__ == "__main__":
    sys.exit(main())

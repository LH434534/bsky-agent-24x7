#!/usr/bin/env python3
"""Confirma que o bot PUBLICOU: lê o feed público da conta e grava data/post_check.json."""
import json, os
from pathlib import Path
import requests

PUBLIC = "https://public.api.bsky.app"
PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
handle = os.environ.get("BSKY_HANDLE", "").strip().lstrip("@")
apppw = os.environ.get("BSKY_APP_PASSWORD", "").strip()

out = {"handle": handle}

r = requests.post(f"{PDS}/xrpc/com.atproto.server.createSession",
                  json={"identifier": handle, "password": apppw}, timeout=30)
out["login"] = r.status_code
if r.status_code != 200:
    out["erro"] = r.text[:200]
else:
    did = r.json()["did"]
    out["did"] = did
    f = requests.get(f"{PUBLIC}/xrpc/app.bsky.feed.getAuthorFeed",
                     params={"actor": did, "limit": 5}, timeout=25)
    out["feed_status"] = f.status_code
    if f.status_code == 200:
        posts = []
        for it in f.json().get("feed", [])[:5]:
            p = it.get("post", {})
            rec = p.get("record", {})
            posts.append({
                "quando": rec.get("createdAt"),
                "texto": (rec.get("text", "") or "")[:90],
                "uri": p.get("uri"),
            })
        out["posts"] = posts
        # feed vazio e legitimo (conta nova / sem posts): [{}] como default
        # NAO protege, porque .get("feed") devolve [] e [0] estoura.
        itens = f.json().get("feed") or []
        out["autor_handle"] = (itens[0].get("post", {}).get("author", {}).get("handle")
                               if itens else None)
        out["total_posts_no_feed"] = len(itens)

Path("data").mkdir(exist_ok=True)
Path("data/post_check.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(json.dumps(out, indent=2, ensure_ascii=False))

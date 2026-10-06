"""Teste do motor de imagem: gerar, validar, comprimir, anexar e publicar.

Usa um servidor ATProto falso que implementa uploadBlob de verdade — assim o
teste prova que o blob sobe e o post sai com embed, não só que a URL é válida.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, "/data/workspace/bsky_agent")

from core.atproto import Bsky, PostRef        # noqa: E402
from core.images import (Images, image_prompt, alt_text,      # noqa: E402
                         looks_like_image, compress, gen_offline)

DATA = Path("/data/workspace/bsky_agent/data")
ok = True


def check(label, cond, detail=""):
    global ok
    print(f"  {'✅' if cond else '❌'}" + f" {label}" + (f" — {detail}" if detail else ""))
    ok = ok and cond


# ══════════════════════════════════════════════════ servidor falso
UPLOADED = []
POSTS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        p = self.path
        if "uploadBlob" in p:
            mime = self.headers.get("Content-Type", "")
            UPLOADED.append({"bytes": raw, "mime": mime})
            if not raw:
                return self._j(400, {"error": "EmptyBody"})
            return self._j(200, {"blob": {
                "$type": "blob",
                "ref": {"$link": "bafkrei" + str(len(raw)) + "test"},
                "mimeType": mime, "size": len(raw)}})
        if "createSession" in p:
            return self._j(200, {"did": "did:plc:teste", "handle": "teste",
                                 "accessJwt": "A", "refreshJwt": "R"})
        if "createRecord" in p:
            body = json.loads(raw)
            POSTS.append(body)
            rec = body.get("record", {})
            return self._j(200, {"uri": "at://did:plc:teste/app.bsky.feed.post/1",
                                 "cid": "cid1", "rec": rec})
        return self._j(200, {})

    def do_GET(self):
        if "getSession" in self.path:
            return self._j(200, {"did": "did:plc:teste", "handle": "teste"})
        return self._j(200, {})


srv = HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

print("═══ 1. validação do que vem da API ═══")
check("aceita JPEG", looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 600) == "image/jpeg")
check("aceita PNG", looks_like_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 600) == "image/png")
check("rejeita HTML de erro", looks_like_image(b"<html>error</html>" * 40) is None)
check("rejeita vazio", looks_like_image(b"") is None)

print("\n═══ 2. prompt e alt text ═══")
texto = "Ainda tá essa neblina pesada pela cidade hoje de manhã."
p = image_prompt(texto)
print(f"  prompt: {p[:90]}")
check("prompt tem o conteúdo do post", "neblina" in p)
check("prompt tem estilo visual", "phone photo" in p)
check("prompt sem link/mention", "http" not in p and "@" not in p)
a = alt_text(texto, p)
check("alt text vem do post", len(a) > 10 and "neblina" in a)
check("alt text corta em 190", len(alt_text("palavra " * 60, p)) <= 200)

print("\n═══ 3. geração e compressão ═══")
raw = gen_offline(texto, 12345, 1024, 1024)
check("gerou bytes", bool(raw), f"{len(raw)} bytes")
check("é imagem válida", looks_like_image(raw) is not None)
d, m = compress(raw, "image/jpeg")
check("comprime abaixo de 950 KB", len(d) < 950_000, f"{len(d)//1024} KB")
check("mantém mime jpeg", m == "image/jpeg")
d2, _ = compress(gen_offline(texto, 999, 1024, 1024), "image/jpeg")
check("seed diferente muda a imagem", d != d2)

# compressão de imagem grande
try:
    from PIL import Image
    big = Image.new("RGB", (3000, 3000))
    for x in range(0, 3000, 37):
        for y in range(0, 3000, 37):
            big.putpixel((x, y), ((x * y) % 255, (x + y) % 255, (x ^ y) % 255))
    buf = io.BytesIO()
    big.save(buf, "PNG")
    d3, m3 = compress(buf.getvalue(), "image/png")
    check("reduz imagem gigante", len(d3) < 950_000,
          f"{len(buf.getvalue())//1024} KB → {len(d3)//1024} KB")
except Exception as e:
    print(f"  (pulou compressão grande: {str(e)[:60]})")

print("\n═══ 4. upload de blob no ATProto ═══")
b = Bsky("teste", "senha", pds=f"http://127.0.0.1:{port}",
         session_file=str(DATA / "sess_teste.json"))
b.login()
blob = b.upload_blob(d, "image/jpeg")
check("upload devolveu blob", bool(blob.get("ref")), str(blob.get("ref"))[:44])
check("servidor recebeu os bytes", len(UPLOADED) == 1 and
      len(UPLOADED[0]["bytes"]) == len(d))
check("mime foi enviado", UPLOADED[0]["mime"] == "image/jpeg")

try:
    b.upload_blob(b"", "image/jpeg")
    check("rejeita upload vazio", False)
except Exception:
    check("rejeita upload vazio", True)

print("\n═══ 5. post com a imagem anexada ═══")
ref = b.post("testando com imagem", images=[{"blob": blob, "alt": "uma imagem",
                                             "aspect": {"width": 1024, "height": 1024}}])
check("post publicou", bool(ref.uri))
rec = POSTS[-1]["record"]
embed = rec.get("embed")
check("post tem embed", embed is not None)
check("embed é do tipo imagens", embed.get("$type") == "app.bsky.embed.images")
check("embed tem 1 imagem", len(embed.get("images", [])) == 1)
check("imagem tem alt", embed["images"][0].get("alt") == "uma imagem")
check("imagem referencia o blob", embed["images"][0]["image"]["ref"]["$link"]
      == blob["ref"]["$link"])
check("imagem tem aspect ratio", embed["images"][0].get("aspectRatio") is not None)

print("\n═══ 6. post sem imagem continua normal ═══")
b.post("só texto")
check("sem embed quando não há imagem", "embed" not in POSTS[-1]["record"])

print("\n═══ 7. motor: provedores e fallback ═══")
I = Images(DATA / "images_state_teste.json")
I.enabled = True
I.min_gap = 0
print(f"  ordem inicial: {I.order()}")
check("pollinations é o primário", I.order()[0] == "pollinations")
I.note("pollinations", False)
I.note("pollinations", False)
I.note("pollinations", False)
check("cai depois de falhar", I.order()[0] != "pollinations", I.order()[0])

# força só offline pra não depender da rede no teste
I2 = Images(DATA / "images_state_teste.json")
I2.enabled = True
I2.min_gap = 0
made = None
for name in ["offline"]:
    from core.images import PROVIDERS
    fn = dict(PROVIDERS)[name]
    t0 = time.time()
    r = fn(image_prompt(texto), 7, 1024, 1024)
    if r and looks_like_image(r):
        dd, mm = compress(r, "image/jpeg")
        from core.images import Made
        made = Made(data=dd, mime=mm, provider=name, prompt=image_prompt(texto),
                    alt=alt_text(texto, image_prompt(texto)), seconds=time.time() - t0)
check("gerou algo postável", made is not None and len(made.data) < 950_000)
if made:
    bl = b.upload_blob(made.data, made.mime)
    ref2 = b.post("post com imagem gerada", images=[{"blob": bl, "alt": made.alt}])
    check("post com imagem gerada publicou", bool(ref2.uri))
    check("embed presente", POSTS[-1]["record"].get("embed", {}).get("$type")
          == "app.bsky.embed.images")

srv.shutdown()
print(f"\n{'IMAGENS OK' if ok else 'FALHOU'}")
sys.exit(0 if ok else 1)

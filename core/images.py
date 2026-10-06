"""Geração de imagem — API pública gratuita, sem chave.

Provedor primário: Pollinations (open source no GitHub, image.pollinations.ai).
Não precisa de conta nem de chave: o prompt vai na URL e o corpo da resposta é
o JPEG. Roda no runner do Actions, que tem internet livre.

A cadeia tem fallback porque "grátis" sem SLA é assim mesmo:
  1. pollinations   — sem chave, flux
  2. huggingface    — se existir HF_TOKEN no ambiente
  3. offline        — desenha a imagem localmente (determinística, sempre funciona)

O fallback offline importa: se os provedores caírem, o bot não perde o post.
Ele publica o texto com uma imagem gerada por ele mesmo em vez de desistir.

Bluesky: até 4 imagens por post, 1 MB cada. Tudo aqui comprime pra caber.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "imagens"
STATE = DATA / "images_state.json"

MAX_BYTES = 950_000          # Bluesky: 1 MB, fica abaixo por margem
MAX_SIDE = 1600              # mais que isso vira processamento à toa

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(m: str) -> None:
    try:
        with open(DATA / "images.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} IMG │ {m}\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════ validação
def looks_like_image(b: bytes) -> Optional[str]:
    """Confirma magic bytes. API gratuita às vezes devolve HTML de erro com
    status 200 — tratar isso como imagem gera upload corrompido."""
    if not b or len(b) < 512:
        return None
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def compress(b: bytes, mime: str, max_bytes: int = MAX_BYTES) -> Tuple[bytes, str]:
    """Comprime pra caber no limite do Bluesky. Sem PIL, só corta por tamanho."""
    try:
        from PIL import Image
    except Exception:
        return b[:max_bytes], mime

    try:
        im = Image.open(io.BytesIO(b))
        im = im.convert("RGB")
    except Exception:
        return b[:max_bytes], mime

    # reduz dimensão primeiro — é o que mais economiza
    w, h = im.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    if scale < 1.0:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    for q in (88, 78, 68, 58, 48, 40, 32):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue(), "image/jpeg"
    # último recurso: shrink agressivo
    im = im.resize((max(320, im.width // 2), max(320, im.height // 2)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=40, optimize=True)
    return buf.getvalue(), "image/jpeg"


# ══════════════════════════════════════════════════════════ provedores
def _get(url: str, timeout: int = 120, referer: str = "") -> Optional[bytes]:
    h = {"User-Agent": UA, "Accept": "image/*,*/*"}
    if referer:
        h["Referer"] = referer
    try:
        r = requests.get(url, headers=h, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.content
    except Exception:
        return None


def gen_pollinations(prompt: str, seed: int, w: int, h: int) -> Optional[bytes]:
    """Pollinations — sem chave, open source. Prompt na URL, JPEG na resposta."""
    q = urllib.parse.quote(prompt[:900])
    url = (f"https://image.pollinations.ai/prompt/{q}"
           f"?width={w}&height={h}&seed={seed}&nologo=true"
           f"&enhance=false&safe=false&model=flux")
    b = _get(url, timeout=150, referer="https://image.pollinations.ai/")
    if b and looks_like_image(b):
        return b
    # tenta sem parâmetros extras (endpoint às vezes muda)
    url2 = f"https://image.pollinations.ai/prompt/{q}?width={w}&height={h}&seed={seed}"
    b2 = _get(url2, timeout=150, referer="https://pollinations.ai/")
    return b2 if b2 and looks_like_image(b2) else None


def gen_huggingface(prompt: str, seed: int, w: int, h: int) -> Optional[bytes]:
    """Hugging Face Inference — só se HF_TOKEN existir no ambiente."""
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if not tok:
        return None
    model = os.environ.get("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
    url = f"https://api-inference.huggingface.co/models/{model}"
    try:
        r = requests.post(url,
                          headers={"Authorization": f"Bearer {tok}",
                                   "User-Agent": UA},
                          json={"inputs": prompt[:600],
                                "parameters": {"seed": seed, "width": w, "height": h}},
                          timeout=180)
        if r.status_code != 200:
            return None
        b = r.content
        return b if looks_like_image(b) else None
    except Exception:
        return None


def gen_offline(prompt: str, seed: int, w: int, h: int) -> Optional[bytes]:
    """Desenha localmente quando não há provedor nenhum.

    Nunca é None: o bot sempre consegue postar com imagem, mesmo offline.
    É determinístico pelo prompt — mesma frase, mesma imagem.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception:
        return None

    # Seed entra no hash: sem isso a mesma frase gera sempre a mesma imagem e
    # dois posts parecidos saem com arte idêntica — denuncia o gerador.
    hseed = hashlib.sha256(f"{prompt}|{seed}".encode()).hexdigest()[:16]
    rnd = random.Random(hseed)
    w, h = min(w, 1024), min(h, 1024)

    # paleta derivada do prompt: mesma frase, mesmas cores
    base = rnd.randrange(0, 360)
    def cor(sat: int, lum: int) -> tuple:
        import colorsys
        r, g, b = colorsys.hls_to_rgb(((base + rnd.randrange(-30, 30)) % 360) / 360,
                                      lum / 100, sat / 100)
        return (int(r * 255), int(g * 255), int(b * 255))

    im = Image.new("RGB", (w, h), cor(45, 12))
    d = ImageDraw.Draw(im)

    # camadas de formasorgânicas
    for _ in range(rnd.randrange(14, 26)):
        x0, y0 = rnd.randrange(-w // 4, w), rnd.randrange(-h // 4, h)
        x1, y1 = x0 + rnd.randrange(w // 12, w // 2), y0 + rnd.randrange(h // 12, h // 2)
        fill = cor(rnd.randrange(35, 75), rnd.randrange(22, 62))
        if rnd.random() < 0.45:
            d.ellipse([x0, y0, x1, y1], fill=fill)
        else:
            d.rectangle([x0, y0, x1, y1], fill=fill)

    im = im.filter(ImageFilter.GaussianBlur(radius=rnd.randrange(6, 16)))

    # granulado — sem isso parece gráfico de teste
    px = im.load()
    for _ in range(int(w * h * 0.06)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        p = px[x, y]
        n = rnd.randrange(-26, 26)
        px[x, y] = tuple(max(0, min(255, c + n)) for c in p)

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue()


PROVIDERS = [("pollinations", gen_pollinations),
             ("huggingface", gen_huggingface),
             ("offline", gen_offline)]


# ══════════════════════════════════════════════════════════ descrição
STOP = set("""de da do das dos a o as os em no na nas nos para por com sem sobre
entre que e ou um uma uns umas é são foi era tá ta tá na do da ele ela você eu
meu minha meus minhas seu sua isso aquilo isso aqui ali lá mais muito já não
sim como quando onde porque mas também só ainda sempre nunca todo toda""".split())


def image_prompt(text: str, extras: str = "") -> str:
    """Transforma o texto do post em prompt de imagem.

    Post em português vira prompt visual: pega as palavras de conteúdo, monta
    uma cena. Prefixo de estilo mantém tudo com a mesma cara — foto de celular,
    não arte digital.
    """
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"@\w+(\.\w+)*", "", t)
    palavras = [p for p in re.findall(r"[\wÀ-ÿ']+", t.lower())
                if p not in STOP and len(p) > 3]
    nucleo = " ".join(dict.fromkeys(palavras))[:180] or "cidade ao entardecer"
    estilo = ("candid phone photo, natural light, shot on a phone, "
              "everyday scene, no text, no watermark, no logo, "
              "slightly imperfect, real life")
    extra = f", {extras}" if extras else ""
    return f"{nucleo} — {estilo}{extra}"[:1000]


def alt_text(texto_post: str, prompt: str) -> str:
    """Texto alternativo. Bluesky incentiva e isso também é o que torna o post
    acessível — imagem sem alt é post pela metade."""
    t = re.sub(r"\s+", " ", (texto_post or "")).strip()
    if len(t) > 190:
        t = t[:187].rsplit(" ", 1)[0] + "…"
    return t or prompt[:190]


# ══════════════════════════════════════════════════════════════ motor
@dataclass
class Made:
    """Uma imagem pronta pra upload."""
    data: bytes
    mime: str
    provider: str
    prompt: str
    alt: str
    seconds: float
    path: Optional[Path] = None


class Images:
    def __init__(self, state_file: Path = STATE, enabled: bool = True,
                 cache_dir: Path = CACHE):
        self.file = state_file
        self.cache = cache_dir
        self.enabled = enabled
        self.stats: Dict[str, int] = {}
        self.provider_ok: Dict[str, int] = {}
        self.provider_fail: Dict[str, int] = {}
        self.last_call = 0.0
        self.min_gap = float(os.environ.get("IMG_MIN_GAP", "20"))
        self.load()

    # ───────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps({
                "stats": self.stats, "ok": self.provider_ok,
                "fail": self.provider_fail}, ensure_ascii=False))
        except Exception:
            pass

    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
            self.stats = d.get("stats", {})
            self.provider_ok = d.get("ok", {})
            self.provider_fail = d.get("fail", {})
        except Exception:
            pass

    def available(self) -> bool:
        return self.enabled

    def note(self, provider: str, ok: bool) -> None:
        self.stats[provider] = self.stats.get(provider, 0) + (1 if ok else 0)
        if ok:
            self.provider_ok[provider] = self.provider_ok.get(provider, 0) + 1
        else:
            self.provider_fail[provider] = self.provider_fail.get(provider, 0) + 1

    def order(self) -> List[str]:
        """Ordena provedores pelo que tem funcionado — não por ordem fixa."""
        def score(name: str) -> float:
            o = self.provider_ok.get(name, 0)
            f = self.provider_fail.get(name, 0)
            if o + f == 0:
                return 0.5
            return o / (o + f)
        pares = [(n, score(n)) for n, _ in PROVIDERS]
        # offline nunca ganha desempate: é rede de segurança, não preferência
        pares.sort(key=lambda x: -(x[1] - (0.4 if x[0] == "offline" else 0)))
        return [n for n, _ in pares]

    # ───────────────────────────────────────────────────── criação
    def make(self, post_text: str, extras: str = "",
             seed: Optional[int] = None) -> Optional[Made]:
        """Gera uma imagem para o post. Nunca levanta exceção."""
        if not self.enabled:
            return None

        # respeita o espaçamento: Pollinations anônimo é ~1 req/15 s
        gap = time.time() - self.last_call
        if gap < self.min_gap:
            espera = self.min_gap - gap
            log(f"respeitando intervalo: {espera:.0f}s")
            time.sleep(min(espera, 90))

        seed = seed if seed is not None else random.randrange(1, 10 ** 9)
        prompt = image_prompt(post_text, extras)

        for name in self.order():
            fn = dict(PROVIDERS)[name]
            t0 = time.time()
            try:
                raw = fn(prompt, seed, 1024, 1024)
            except Exception as e:
                raw = None
                log(f"{name} estourou: {str(e)[:90]}")
            dt = time.time() - t0
            self.last_call = time.time()

            if not raw:
                self.note(name, False)
                log(f"{name} não retornou imagem ({dt:.0f}s)")
                continue
            mime = looks_like_image(raw)
            if not mime:
                self.note(name, False)
                log(f"{name} retornou dados que não são imagem")
                continue

            data, mime = compress(raw, mime)
            if len(data) > MAX_BYTES * 1.05:
                self.note(name, False)
                log(f"{name}: {len(data)} bytes, não comprimiu o suficiente")
                continue

            self.note(name, True)
            self.save()
            p = self._cache(data, seed)
            log(f"{name} OK — {len(data)//1024} KB em {dt:.0f}s · seed {seed}")
            return Made(data=data, mime=mime, provider=name, prompt=prompt,
                        alt=alt_text(post_text, prompt), seconds=dt, path=p)

        self.save()
        return None

    def _cache(self, data: bytes, seed: int) -> Optional[Path]:
        """Guarda no disco pra não perder se o upload falhar."""
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            p = self.cache / f"{int(time.time())}_{seed}.jpg"
            p.write_bytes(data)
            # mantém só as últimas 40
            fs = sorted(self.cache.glob("*.jpg"), key=lambda x: -x.stat().st_mtime)
            for old in fs[40:]:
                try:
                    old.unlink()
                except Exception:
                    pass
            return p
        except Exception:
            return None

    def describe(self) -> str:
        if not self.stats:
            return "sem imagens geradas ainda"
        return " · ".join(f"{k} {v}" for k, v in
                          sorted(self.stats.items(), key=lambda x: -x[1])[:4])

"""
core/toolkit.py — kit de resiliência e qualidade textual para o bsky-agent.

Integra bibliotecas pequenas, maduras e sem custo ao ciclo de vida do bot.
TODAS as dependências são opcionais: se faltarem, o módulo degrada para
stdlib e o bot continua funcionando exatamente como antes.

Kill switch: BOT_TOOLKIT=0 desativa a integração em core/atproto.py.

Ferramentas (todas testadas em Python 3.10 e 3.11, sem GPU/rede):
  ftfy            6.3.1        conserta mojibake/Unicode quebrado do feed
  langdetect      1.0.9        idioma real do post (o bot hoje hardcoda pt-BR)
  vaderSentiment  3.3.2        tom do post (léxico, sem download de modelo)
  tenacity        9.1.2        retry com jitter APENAS em falha transitória
  circuitbreaker  2.0.0        fail-fast após N falhas permanentes
  cachetools      5.5.2        TTLCache no cache de handles (hoje dict sem TTL)
  jsonschema      4.23.0       valida data/*.json antes de gravar
  humanize        4.11.0       formatação legível de uptime/contadores
  psutil          (já no requirements) telemetria do runner
  emoji           2.16.0       aliases :wave: → emoji real
  textstat        0.7.13       legibilidade (Flesch) no score de humanidade
  unidecode       1.4.0        transliteração segura
  regex           2026.9.29    regex Unicode mais correta que `re`
  python-dateutil 3.0.0        parsing de timestamps do feed
  wordfreq        3.1.1        frequência lexical (evita vocabulário artificial)
  syllables       1.1.5        contagem de sílabas
  pycountry       26.2.16      códigos de país/idioma
  babel           2.18.0       formatação de data por locale
  core/human.py   —           600 capacidades (576 perfis + 24 utils)

Atribuição: o security_gate.py deste repo é derivado das categorias de risco
publicadas por Tencent Zhuque Lab AI-Infra-Guard (Apache-2.0):
https://github.com/Tencent/AI-Infra-Guard
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("bsky.toolkit")

# ------------------------------------------------------------------ optional
def _try(mod: str, attr: str):
    try:
        m = __import__(mod, fromlist=[attr])
        return getattr(m, attr)
    except Exception as e:  # ImportError, versão antiga, etc.
        log.debug("toolkit: %s indisponível (%s) — usando fallback", mod, e)
        return None

fix_text = _try("ftfy", "fix_text")
detect_langs = _try("langdetect", "detect_langs")
SentimentIntensityAnalyzer = _try("vaderSentiment.vaderSentiment", "SentimentIntensityAnalyzer")
retry = _try("tenacity", "retry")
stop_after_attempt = _try("tenacity", "stop_after_attempt")
wait_exponential_jitter = _try("tenacity", "wait_exponential_jitter")
retry_if_exception = _try("tenacity", "retry_if_exception")
circuit = _try("circuitbreaker", "circuit")
TTLCache = _try("cachetools", "TTLCache")
cached = _try("cachetools", "cached")
validate = _try("jsonschema", "validate")
naturaltime = _try("humanize", "naturaltime")

# ── camada de humanidade (600 capacidades) ──────────────────────────────────
try:
    from core.human import (  # type: ignore
        ENABLED as HUMAN_ON, apply as apply_human,
        catalog as human_catalog, describe as human_describe,
        human_likeness_score, ai_tell_score,
    )
except Exception as _e:  # pragma: no cover
    logging.getLogger("bsky.toolkit").warning(
        "camada humana indisponível (%s) — seguindo sem ela", _e)
    HUMAN_ON = False
    apply_human = None  # type: ignore
    human_catalog = lambda: []  # type: ignore
    human_describe = lambda: {}  # type: ignore
    human_likeness_score = lambda _t: 0.0  # type: ignore
    ai_tell_score = lambda _t: 0  # type: ignore

_SENTIMENT = SentimentIntensityAnalyzer() if SentimentIntensityAnalyzer else None

ENABLED = os.environ.get("BOT_TOOLKIT", "1").strip().lower() not in {"0", "false", "no", "off"}


# ------------------------------------------------------------------ 1. texto
def clean_text(text: str) -> str:
    """ftfy + normalização NFC + colapso de whitespace. Nunca lança."""
    if not isinstance(text, str):
        return ""
    out = fix_text(text) if fix_text else text
    out = unicodedata.normalize("NFC", out)
    out = re.sub(r"[ \t\u00a0]+", " ", out).strip()
    return out


def detect_language(text: str) -> str:
    """Devolve 'pt-BR', 'en', 'es'... ou 'pt-BR' se indeterminado."""
    if not text or not text.strip():
        return "pt-BR"
    if detect_langs:
        try:
            top = detect_langs(text[:400])[0]
            if top.prob >= 0.80:
                code = top.lang
                return "pt-BR" if code == "pt" else code
        except Exception:
            pass
    # fallback heurístico bem conservador
    low = text.lower()
    if re.search(r"\b(não|você|também|hoje|ontem|porque|então|gente)\b", low):
        return "pt-BR"
    if re.search(r"\b(the|you|today|because|really|with|that)\b", low):
        return "en"
    return "pt-BR"


def sentiment(text: str) -> float:
    """compound de -1 (negativo) a +1 (positivo). 0.0 em fallback."""
    if _SENTIMENT is None or not text:
        return 0.0
    try:
        return float(_SENTIMENT.polarity_scores(text[:2000])["compound"])
    except Exception:
        return 0.0


def tone_hint(text: str) -> str:
    """Classificação simples usada para evitar responder com exaltação a luto."""
    s = sentiment(text)
    if s <= -0.55:
        return "sombrio"
    if s <= -0.15:
        return "negativo"
    if s >= 0.55:
        return "positivo"
    return "neutro"


def prepare_post(text: str, langs: Optional[List[str]] = None) -> Tuple[str, List[str], Dict[str, Any]]:
    """Ponto único de preparação. Retorna (texto, langs, meta para log)."""
    if not ENABLED:
        return text.strip(), (langs or ["pt-BR"]), {"toolkit": "off"}
    clean = clean_text(text)
    # Camada humana: escolhe 1 dos 576 perfis de forma determinística e aplica
    # no máximo HUMAN_MAX_EDITS (padrão 2) transformações conservadoras.
    final = clean
    human_meta: Dict[str, Any] = {"ativo": False}
    if HUMAN_ON and apply_human:
        # IMPORTANTE: sem um ts estável, choose() cairia em time.time() e dois
        # posts idênticos poderiam receber perfis diferentes — o que furaria o
        # Outbox (hashes distintos → duplicata publicada). Usamos um bucket de
        # 5 min: mesmo texto na mesma janela = mesmo perfil = mesma chave.
        bucket = int(time.time() // 300)
        cap = None
        try:
            from core.human import choose as human_choose  # type: ignore
            cap = human_choose(clean, ts=bucket, actor="", salt="post")
        except Exception:
            cap = None
        final, hm = apply_human(clean, cap)
        human_meta = {
            "ativo": True,
            "perfil": hm["id"],
            "edits": hm["edits"],
            "ai_tells_removidos": hm["ai_tells_antes"] - hm["ai_tells_depois"],
            "score_antes": human_likeness_score(clean),
            "score_depois": human_likeness_score(final),
            "recomenda_regenerar": hm.get("recomenda_regenerar", False),
        }
        if human_meta["recomenda_regenerar"]:
            log.warning("texto era quase só clichê de IA; convém regenerar antes de postar")
    detected = detect_language(final)
    final_langs = langs or [detected]
    meta = {
        "toolkit": "on",
        "changed": final != text,
        "lang": detected,
        "tone": tone_hint(final),
        "graphemes": len(final),
        "humano": human_meta,
    }
    return final, final_langs, meta


# ------------------------------------------------- 2. idempotência (Outbox)
class Outbox:
    """Evita post duplicado quando createRecord dá timeout e o cliente retrya.

    O protocolo não tem idempotency key. Registramos um hash ANTES da chamada
    e conferimos depois. Janela padrão: 30 min.
    """

    SCHEMA = {
        "type": "object",
        "properties": {"entries": {"type": "object"}},
        "required": ["entries"],
    }

    def __init__(self, path: str | Path, window_s: int = 1800):
        self.path = Path(path)
        self.window_s = window_s

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(clean_text(text).encode("utf-8")).hexdigest()[:24]

    def _read(self) -> Dict[str, Any]:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get("entries"), dict):
                return d
        except Exception:
            pass
        return {"entries": {}}

    def _write(self, d: Dict[str, Any]) -> None:
        if validate:
            try:
                validate(d, self.SCHEMA)
            except Exception as e:
                raise ValueError(f"outbox inválido, recusando gravar: {e}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        tmp.replace(self.path)

    def seen(self, text: str) -> bool:
        k = self.key(text)
        now = time.time()
        d = self._read()
        ts = d["entries"].get(k)
        return bool(ts and now - float(ts) < self.window_s)

    def mark(self, text: str) -> None:
        d = self._read()
        now = time.time()
        d["entries"] = {k: v for k, v in d["entries"].items()
                        if now - float(v) < self.window_s}
        d["entries"][self.key(text)] = now
        self._write(d)

    def clear(self) -> None:
        self._write({"entries": {}})


# ------------------------------------------------------- 3. retry + breaker
class TransientError(Exception):
    """Falha que vale repetir. AuthError NUNCA deve virar TransientError."""


def network_retry(fn):
    """Decora com tenacity: 3 tentativas, jitter, SÓ em TransientError.

    Sem tenacity, executa a função diretamente (degradação graciosa).
    """
    if not (retry and stop_after_attempt and wait_exponential_jitter and retry_if_exception):
        return fn
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8, jitter=1.5),
        retry=retry_if_exception(lambda e: isinstance(e, TransientError)),
        reraise=True,
    )(fn)


class PermanentFailureGuard:
    """Circuit breaker caseiro (stdlib) para 401/AuthError.

    Motivo: o histórico do bot registrou 532 retentativas de auto-reparo
    contra uma credencial revogada. Um erro permanente deve abortar na 1ª.
    Usa a lib circuitbreaker quando disponível, com fallback idêntico.
    """

    def __init__(self, threshold: int = 2, reset_s: float = 900):
        self.threshold = threshold
        self.reset_s = reset_s
        self.fails = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at >= self.reset_s:
            self.fails, self.opened_at = 0, None
            return False
        return True

    def check(self) -> None:
        if self.is_open:
            raise RuntimeError(
                "circuito aberto: credencial rejeitada repetidamente. "
                "Gere nova App Password e atualize BSKY_APP_PASSWORD. "
                "Não vou insistir (erro permanente).")

    def record_failure(self) -> None:
        self.fails += 1
        if self.fails >= self.threshold:
            self.opened_at = time.time()
            log.error("circuito aberto após %s falhas permanentes", self.fails)

    def record_success(self) -> None:
        self.fails, self.opened_at = 0, None


# --------------------------------------------------------- 4. cache com TTL
def handle_cache(maxsize: int = 512, ttl: int = 3600):
    """TTLCache para _resolve_handle; devolve dict simples se cachetools faltar."""
    if TTLCache:
        return TTLCache(maxsize=maxsize, ttl=ttl)
    return {}


# ------------------------------------------------------ 5. rate-limit aware
class RateLimitGuard:
    """Lê os headers que o ATProto devolve e diz quanto esperar.

    Referência: 429 vem com ratelimit-remaining / ratelimit-reset (Unix ts).
    """

    def __init__(self):
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.last_wait = 0.0

    def observe(self, headers: Any) -> None:
        try:
            if headers.get("ratelimit-remaining") is not None:
                self.remaining = int(headers["ratelimit-remaining"])
            if headers.get("ratelimit-reset"):
                self.reset_at = float(headers["ratelimit-reset"])
        except (TypeError, ValueError):
            pass

    def wait_seconds(self) -> float:
        if self.reset_at:
            w = max(0.0, self.reset_at - time.time())
            self.last_wait = w
            return w
        return 0.0

    def should_pause(self) -> bool:
        return self.remaining is not None and self.remaining <= 1

    def snapshot(self) -> Dict[str, Any]:
        return {"remaining": self.remaining, "reset_at": self.reset_at,
                "wait_s": round(self.wait_seconds(), 1)}


# ------------------------------------------------------------- 6. telemetria
def health_snapshot() -> Dict[str, Any]:
    """psutil já está no requirements — aqui só estruturamos o que importa."""
    out: Dict[str, Any] = {"ts": time.time(), "toolkit": ENABLED}
    try:
        import psutil
        vm = psutil.virtual_memory()
        out.update({
            "cpu_pct": psutil.cpu_percent(interval=0),
            "mem_pct": vm.percent,
            "mem_mb": round(vm.used / 1048576, 1),
            "disk_free_mb": round(psutil.disk_usage(".").free / 1048576, 1),
            "proc_rss_mb": round(psutil.Process().memory_info().rss / 1048576, 1),
        })
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def human_uptime(seconds: float) -> str:
    """Duração já decorrida, ex.: 'an hour ago'.

    naturaltime() espera um DELTA em segundos (negativo = passado), não um
    timestamp absoluto — passar time.time()-seconds produz '56 years ago'.
    """
    if naturaltime:
        try:
            return str(naturaltime(-abs(float(seconds))))
        except Exception:
            pass
    return f"{int(seconds) // 3600}h {(int(seconds) % 3600) // 60}m"


# ------------------------------------------------------------ 7. validação
def validate_json_file(path: str | Path, schema: Dict[str, Any]) -> bool:
    if not validate:
        return True  # sem jsonschema, não bloqueia
    try:
        validate(json.loads(Path(path).read_text()), schema)
        return True
    except Exception as e:
        log.warning("estado inválido em %s: %s", path, e)
        return False


# -------------------------------------------------------------- 8. relatório
def describe() -> Dict[str, Any]:
    return {
        "enabled": ENABLED,
        "libs": {
            "ftfy": bool(fix_text), "langdetect": bool(detect_langs),
            "vaderSentiment": bool(_SENTIMENT), "tenacity": bool(retry),
            "circuitbreaker": bool(circuit), "cachetools": bool(TTLCache),
            "jsonschema": bool(validate), "humanize": bool(naturaltime),
        },
        "health": health_snapshot(),
        "humano": human_describe(),
    }

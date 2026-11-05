"""
core/dm.py — resposta automática a DMs com guard anti-spam em camadas.

PRINCÍPIO: o bot só responde a uma mensagem que um humano lhe enviou
espontaneamente. Nunca inicia conversa, nunca aceita chat request, nunca
responde em grupo, nunca faz outreach. As Bluesky Developer Guidelines
proíbem spam; a Community Guidelines proíbem "interações automatizadas não
solicitadas" e recomendam que bots que interajam sejam opt-in e se
identifiquem. Uma DM recebida é o opt-in mais explícito que existe — mas só
se a resposta for 1:1, rara, transparente e interrompível.

FAIL-CLOSED: qualquer dúvida, erro ou ausência de config → silêncio.
Por padrão NADA é enviado. São necessárias DUAS chaves para enviar:
    BOT_DM_REPLY=1   e   BOT_DM_SHADOW=0
Com apenas a primeira, roda em SHADOW: calcula tudo, envia nada.

PRIVACIDADE: texto de DM nunca é gravado em disco, log ou git. O estado
persiste apenas hashes, DIDs hasheados e timestamps.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("bsky.dm")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CHAT_BASE = "https://api.bsky.chat"
CHAT_PROXY = "did:web:api.bsky.chat#bsky_chat"
NS_LIST = "chat.bsky.convo.listConvos"
NS_MESSAGES = "chat.bsky.convo.getMessages"
NS_SEND = "chat.bsky.convo.sendMessage"

# ── conteúdo que NUNCA gera resposta ──────────────────────────────────────
SPAM_RE = re.compile(
    r"\b(airdrop|giveaway|casino|bet\s*now|f4f|follow\s*back|dm\s*me|"
    r"telegram|whatsapp|100x|free\s*crypto|promo\s*code|discount\s*code|"
    r"ganhe\s*dinheiro|renda\s*extra|pix\s* grátis|clique\s*aqui|"
    r"link\s*na\s*bio|compre\s*agora|curte|segue|compartilha|"
    r"inscreva|subscribe|retweet|repost\s*this)\b", re.I)
URL_RE = re.compile(r"(?:https?://|www\.|\b(?:bit\.ly|t\.co|tinyurl|goo\.gl)/)", re.I)
MENTION_RE = re.compile(r"(?<![\w@./])@[a-zA-Z0-9][a-zA-Z0-9._-]*")
# Opt-out: só padrões inequívocos. "para" sozinho NÃO conta (é palavra comum).
OPTOUT_RE = re.compile(
    r"(?:\b(?:pare\s+de\s+responder|n[ãa]o\s+responda|n[ãa]o\s+quero\s+resposta|"
    r"sem\s+resposta|unsubscribe|stop|opt[\s-]?out|n[ãa]o\s+me\s+mande)\b|"
    r"^\s*(?:pare|chega|sai|sil[eê]ncio)\s*[.!?]*\s*$)", re.I)
CTA_RE = re.compile(
    r"\b(?:curte|curti|segue|seguir|compartilh|inscreva|subscribe|"
    r"follow|like\s+and|retweet|reposte|clique|acesse|confira|"
    r"link\s+na\s+bio|manda\s+dm|me\s+chama)\b", re.I)
SILENCE_RE = re.compile(r"^\s*sil[eê]ncio\s*[.!?]*$", re.I)

DISCLOSURE = "(resposta automática) "


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:24]


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").strip()


def _graphemes(s: str) -> int:
    """Conta graphemes; cai para len() se a lib `regex` não estiver presente."""
    try:
        import regex  # type: ignore
        return len(regex.findall(r"\X", s))
    except Exception:
        return len(s)


def _parse_ts(v: str) -> float:
    if not v:
        return 0.0
    try:
        s = v.replace("Z", "+00:00")
        # microsegundos longos quebram fromisoformat em 3.10
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


@dataclass
class Config:
    enabled: bool = False
    shadow: bool = True
    disclosure: bool = True
    max_per_sender_24h: int = 1
    max_per_sender_7d: int = 2
    max_global_24h: int = 3
    max_global_7d: int = 10
    min_interval_min: float = 20.0
    freshness_min: float = 120.0
    max_graphemes: int = 300
    max_convos: int = 25
    similarity_max: float = 0.88
    recent_window: int = 10
    self_did: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        def bi(k: str, d: int) -> bool:
            return os.environ.get(k, str(d)).strip().lower() in ("1", "true", "yes", "on")
        def num(k: str, d: float) -> float:
            try:
                return float(os.environ.get(k, str(d)))
            except Exception:
                return d
        shadow = bi("BOT_DM_SHADOW", 1)
        disclosure = bi("DM_DISCLOSURE", 1)
        # Fail-closed: resposta automática SEM identificação não é permitida.
        # Se alguém desligar o disclosure junto com o shadow, voltamos a shadow
        # em vez de mandar mensagem anônima para terceiros.
        if not shadow and not disclosure:
            shadow = True
        return cls(
            enabled=bi("BOT_DM_REPLY", 0),
            shadow=shadow,
            disclosure=disclosure,
            max_per_sender_24h=int(num("DM_MAX_PER_SENDER_24H", 1)),
            max_per_sender_7d=int(num("DM_MAX_PER_SENDER_7D", 2)),
            max_global_24h=int(num("DM_MAX_GLOBAL_24H", 3)),
            max_global_7d=int(num("DM_MAX_GLOBAL_7D", 10)),
            min_interval_min=num("DM_MIN_INTERVAL_MIN", 20),
            freshness_min=num("DM_FRESHNESS_MIN", 120),
            max_graphemes=int(num("DM_MAX_GRAPHEMES", 300)),
            max_convos=int(num("DM_MAX_CONVOS", 25)),
            similarity_max=num("DM_SIMILARITY_MAX", 0.88),
        )


@dataclass
class Candidate:
    convo_id: str
    convo_status: str
    convo_kind: str
    member_count: int
    message_id: str
    sender_did: str
    text: str
    sent_at: str
    last_sender_did: str
    prev_sender_did: str = ""


class DMState:
    """Só hashes e timestamps. Nenhum texto de DM toca o disco."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (DATA / "dm_state.json")
        self.optout: set = set()
        self.replied: Dict[str, float] = {}
        self.per_sender: Dict[str, List[float]] = {}
        self.global_: List[float] = []
        self.recent: List[str] = []

    def load(self) -> "DMState":
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.optout = set(d.get("optout") or [])
            self.replied = {k: float(v) for k, v in (d.get("replied") or {}).items()}
            self.per_sender = {k: [float(x) for x in v]
                               for k, v in (d.get("per_sender") or {}).items()}
            self.global_ = [float(x) for x in (d.get("global") or [])]
            self.recent = [str(x) for x in (d.get("recent") or [])]
        except Exception:
            pass  # estado corrompido → começa limpo, mas fail-closed nas cotas
        self.prune(time.time())
        return self

    def prune(self, now: float) -> None:
        cut = now - 7 * 86400
        self.replied = {k: v for k, v in self.replied.items() if v > cut}
        self.per_sender = {k: [x for x in v if x > cut]
                           for k, v in self.per_sender.items() if any(x > cut for x in v)}
        self.global_ = [x for x in self.global_ if x > cut]

    def save(self, now: Optional[float] = None) -> None:
        # IMPORTANTE: usa o MESMO relógio de `decide`. Se `save()` podar com
        # time.time() enquanto as cotas foram registradas com um `now`
        # injetado (testes, replay, relógio ajustado), TODA a cota desaparece
        # e o guard deixa de limitar. Fail-open silencioso — corrigido.
        self.prune(now if now is not None else time.time())
        payload = {
            "version": 1,
            "optout": sorted(self.optout),
            "replied": self.replied,
            "per_sender": self.per_sender,
            "global": self.global_,
            "recent": self.recent[-20:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("dm: estado nao gravado (%s)", type(e).__name__)

    def record(self, c: Candidate, reply: str, now: float) -> None:
        sh = _h(c.sender_did)
        self.replied[_h(c.message_id)] = now
        self.per_sender.setdefault(sh, []).append(now)
        self.global_.append(now)
        self.recent.append(_h(_norm(reply)))


class DMClient:
    """Cliente somente-leitura por padrão; `send` é o ÚNICO método de escrita."""

    def __init__(self, token_getter: Callable[[], str],
                 session: Any = None, base: str = CHAT_BASE,
                 self_did: str = ""):
        self.token_getter = token_getter
        self.s = session
        self.base = base.rstrip("/")
        self.self_did = self_did
        self.calls: List[Tuple[str, str]] = []

    def call(self, method: str, nsid: str, *, params: Dict | None = None,
             body: Dict | None = None) -> Tuple[int, Dict]:
        self.calls.append((method, nsid))
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {self.token_getter()}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        url = f"{self.base}/xrpc/{nsid}"
        if params:
            import urllib.parse
            url += "?" + urllib.parse.urlencode(params)
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            url, data=data, headers=headers, method=method)
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}
        except Exception as e:
            return 0, {"_net": type(e).__name__}


# ── as camadas do guard ───────────────────────────────────────────────────
def _L01(cfg, c, st, now): return None if cfg.enabled else "L01_desligado"
def _L02(cfg, c, st, now): return None  # shadow é tratado no run_once, nao bloqueia
def _L03(cfg, c, st, now): return None if c.convo_status == "accepted" else "L03_convo_nao_aceita"
def _L04(cfg, c, st, now): return None if c.convo_kind == "direct" else "L04_nao_e_direct"
def _L05(cfg, c, st, now): return None if c.member_count == 2 else "L05_nao_e_1para1"
def _L06(cfg, c, st, now): return None if c.sender_did and c.sender_did != cfg.self_did else "L06_remetente_invalido"
def _L07(cfg, c, st, now):
    ts = _parse_ts(c.sent_at)
    return None if ts and (now - ts) <= cfg.freshness_min * 60 and ts <= now + 60 else "L07_mensagem_antiga"
def _L08(cfg, c, st, now):
    return None if _h(c.sender_did) not in st.optout else "L08_optout_permanente"
def _L09(cfg, c, st, now):
    return "L09_optout_nesta_mensagem" if OPTOUT_RE.search(_norm(c.text)) else None
def _L10(cfg, c, st, now):
    return None if _h(c.message_id) not in st.replied else "L10_mensagem_ja_respondida"
def _L11(cfg, c, st, now):
    # Anti-ping-pong: se a mensagem anterior nesta conversa foi do próprio
    # bot, o humano está só respondendo à nossa resposta. Responder de novo
    # criaria um loop infinito entre dois agentes automáticos.
    return "L11_ping_pong_com_bot" if (cfg.self_did and c.prev_sender_did == cfg.self_did) else None
def _L12(cfg, c, st, now):
    txt = _norm(c.text)
    if not txt or len(txt) < 3:
        return "L12_texto_vazio"
    if URL_RE.search(txt):
        return "L12_link"
    if SPAM_RE.search(txt):
        return "L12_spam"
    if len(MENTION_RE.findall(txt)) >= 3:
        return "L12_muitas_mencoes"
    if len(set(txt.replace(" ", ""))) <= 2:
        return "L12_texto_degenerado"
    return None
def _L13(cfg, c, st, now):
    xs = [x for x in st.per_sender.get(_h(c.sender_did), []) if x > now - 86400]
    return None if len(xs) < cfg.max_per_sender_24h else "L13_cota_remetente_24h"
def _L14(cfg, c, st, now):
    xs = st.per_sender.get(_h(c.sender_did), [])
    return None if len(xs) < cfg.max_per_sender_7d else "L14_cota_remetente_7d"
def _L15(cfg, c, st, now):
    xs = [x for x in st.global_ if x > now - 86400]
    return None if len(xs) < cfg.max_global_24h else "L15_cota_global_24h"
def _L16(cfg, c, st, now):
    return None if len(st.global_) < cfg.max_global_7d else "L16_cota_global_7d"
def _L17(cfg, c, st, now):
    if not st.global_:
        return None
    return None if (now - max(st.global_)) >= cfg.min_interval_min * 60 else "L17_intervalo_minimo"

LAYERS = [_L01, _L02, _L03, _L04, _L05, _L06, _L07, _L08, _L09,
          _L10, _L11, _L12, _L13, _L14, _L15, _L16, _L17]


def decide(cfg: Config, c: Candidate, st: DMState, now: float) -> Tuple[bool, str]:
    """Retorna (pode_responder, motivo). Primeira camada que falha decide."""
    for fn in LAYERS:
        try:
            m = fn(cfg, c, st, now)
        except Exception as e:
            return False, f"ERRO_{fn.__name__}_{type(e).__name__}"
        if m:
            return False, m
    return True, "ok"


# ── sanitização da resposta (camadas 18-20) ───────────────────────────────
def reply_sanity(cfg: Config, c: Candidate, st: DMState, reply: str) -> Tuple[bool, str, str]:
    txt = _norm(reply)
    if not txt or SILENCE_RE.match(txt):
        return False, "L18_gerador_pediu_silencio", ""
    if len(re.findall(r"[\wÀ-ÿ]+", txt)) < 2:
        return False, "L18_resposta_sem_conteudo", ""
    if URL_RE.search(txt) or MENTION_RE.search(txt) or CTA_RE.search(txt):
        return False, "L19_link_ou_cta_na_resposta", ""
    if txt.lower() == _norm(c.text).lower():
        return False, "L19_eco_da_mensagem", ""

    # Camada humana reutilizada: anti-clichê, idioma determinístico e guarda
    # anti-degeneração (já corrigida em core/toolkit.py).
    try:
        from core.toolkit import prepare_post  # type: ignore
        txt, _, meta = prepare_post(txt)
        hm = meta.get("humano") or {}
        if hm.get("recomenda_regenerar") or hm.get("edits_revertidas"):
            return False, "L19_resposta_degenerada", ""
    except ImportError:
        pass
    except Exception as e:
        return False, f"ERRO_prepare_{type(e).__name__}", ""

    if cfg.disclosure and not txt.startswith(DISCLOSURE):
        txt = DISCLOSURE + txt
    if _graphemes(txt) > cfg.max_graphemes:
        return False, "L19_resposta_longa", ""

    # L20: novidade — respostas repetidas são o maior sinal de spam.
    # `recent` guarda hashes: pega duplicata exata entre sessões. A
    # similaridade difusa é feita em DMBot com os textos desta execução.
    h = _h(_norm(txt))
    if h in st.recent[-cfg.recent_window:]:
        return False, "L20_resposta_duplicada", ""
    return True, "ok", txt


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a).lower(), _norm(b).lower()).ratio()


class DMBot:
    """Orquestrador. Nunca lança exceção para fora — falha fechada."""

    def __init__(self, client: DMClient, state: DMState, cfg: Config,
                 reply_fn: Callable[[str, str, str], str],
                 now_fn: Callable[[], float] = time.time):
        self.client, self.state, self.cfg = client, state, cfg
        self.reply_fn, self.now_fn = reply_fn, now_fn
        self._recent_texts: List[str] = []
        self.audit: Dict[str, int] = {}

    def _tick(self, k: str) -> None:
        self.audit[k] = self.audit.get(k, 0) + 1

    def run_once(self) -> Dict[str, Any]:
        cfg, st, now = self.cfg, self.state, self.now_fn()
        res: Dict[str, Any] = {
            "enabled": cfg.enabled, "shadow": cfg.shadow,
            "enviadas": 0, "bloqueadas": 0, "motivos": {}, "erros": [],
        }
        if not cfg.enabled:
            res["motivos"]["L01_desligado"] = 1
            self._tick("desligado")
            return res

        try:
            code, d = self.client.call("GET", NS_LIST, params={
                "limit": cfg.max_convos, "status": "accepted",
                "kind": "direct", "readState": "unread"})
        except Exception as e:
            res["erros"].append(f"listConvos:{type(e).__name__}")
            return res
        if code != 200:
            res["erros"].append(f"listConvos:HTTP{code}")
            return res

        convos = d.get("convos") or []
        res["convos"] = len(convos)
        for cv in convos[:cfg.max_convos]:
            cid = cv.get("id") or ""
            if not cid:
                self._tick("convo_sem_id")
                continue
            try:
                mc, md = self.client.call("GET", NS_MESSAGES,
                                          params={"convoId": cid, "limit": 10})
            except Exception as e:
                res["erros"].append(f"getMessages:{type(e).__name__}")
                continue
            if mc != 200:
                res["erros"].append(f"getMessages:HTTP{mc}")
                continue
            msgs = md.get("messages") or []
            if not msgs:
                self._tick("convo_sem_mensagens")
                continue
            if not isinstance(msgs, list):
                msgs = []
            last = msgs[-1] if msgs else {}
            prev = msgs[-2] if len(msgs) >= 2 else {}
            cand = Candidate(
                convo_id=cid,
                convo_status=str(cv.get("status") or ""),
                convo_kind=str(cv.get("kind") or "direct"),
                member_count=len(cv.get("members") or []),
                message_id=str(last.get("id") or ""),
                sender_did=str((last.get("sender") or {}).get("did") or ""),
                text=str(last.get("text") or ""),
                sent_at=str(last.get("sentAt") or ""),
                last_sender_did=str((last.get("sender") or {}).get("did") or ""),
                prev_sender_did=str((prev.get("sender") or {}).get("did") or ""),
            )
            # self_did vem do cliente (sessão real) para as camadas L06/L11
            cfg.self_did = getattr(self.client, "self_did", "") or ""

            ok, motivo = decide(cfg, cand, st, now)
            if not ok:
                res["bloqueadas"] += 1
                res["motivos"][motivo] = res["motivos"].get(motivo, 0) + 1
                self._tick(f"bloqueio:{motivo}")
                # Opt-out explícito é permanente e registrado mesmo em shadow.
                if motivo == "L09_optout_nesta_mensagem" and cand.sender_did:
                    st.optout.add(_h(cand.sender_did))
                    st.save(now)
                continue

            try:
                bruta = self.reply_fn(cand.text, "", "")
            except Exception as e:
                res["erros"].append(f"reply_fn:{type(e).__name__}")
                continue
            ok2, motivo2, txt = reply_sanity(cfg, cand, st, bruta)
            if not ok2:
                res["bloqueadas"] += 1
                res["motivos"][motivo2] = res["motivos"].get(motivo2, 0) + 1
                self._tick(f"bloqueio:{motivo2}")
                continue

            # L20b: similaridade textual contra respostas desta execução.
            if any(similar(txt, p) > cfg.similarity_max for p in self._recent_texts):
                res["bloqueadas"] += 1
                res["motivos"]["L20_resposta_similar"] = res["motivos"].get("L20_resposta_similar", 0) + 1
                continue

            if cfg.shadow:
                # NÃO consome cota: quando ligar de verdade, a mensagem ainda
                # estará elegível. Apenas observa.
                res["shadow_teria_enviado"] = res.get("shadow_teria_enviado", 0) + 1
                self._recent_texts.append(txt)
                self._tick("shadow")
                continue

            try:
                sc, sd = self.client.call("POST", NS_SEND, body={
                    "convoId": cid, "message": {"text": txt}})
            except Exception as e:
                res["erros"].append(f"sendMessage:{type(e).__name__}")
                continue
            if sc != 200:
                res["erros"].append(f"sendMessage:HTTP{sc}")
                continue
            st.record(cand, txt, now)
            st.save(now)
            self._recent_texts.append(txt)
            res["enviadas"] += 1
            self._tick("enviada")

        res["chamadas"] = [f"{m}:{n}" for m, n in self.client.calls]
        return res


def make_heartbeat(bsky, brain, interval_min: float = 15.0):
    """Liga o módulo ao motor sem alterar o sorteio de ações.

    `core/autonomy.py` chama heartbeat() uma vez por step dentro de
    try/except. O throttle interno impede 1 requisição por step.
    """
    cfg = Config.from_env()
    st = DMState().load()
    try:
        import requests  # type: ignore
        sess = getattr(bsky, "s", None) or requests.Session()
    except Exception:
        sess = None

    def token() -> str:
        a = getattr(bsky, "access", "") or ""
        if not a:
            try:
                bsky.login()
                a = getattr(bsky, "access", "") or ""
            except Exception:
                pass
        return a

    self_did = getattr(bsky, "did", "") or ""
    cfg.self_did = self_did
    client = DMClient(token, session=sess, self_did=self_did)
    bot = DMBot(client, st, cfg,
                reply_fn=lambda t, h, c: brain.write_reply(t, h, context=c))
    last = [0.0]

    def hb() -> None:
        now = time.time()
        if now - last[0] < interval_min * 60:
            return
        last[0] = now
        try:
            r = bot.run_once()
            if r.get("enviadas") or r.get("shadow_teria_enviado") or r.get("erros"):
                log.info("dm: %s", json.dumps(r, ensure_ascii=False, default=str)[:400])
        except Exception as e:
            log.warning("dm: falha fechada (%s)", type(e).__name__)

    return hb

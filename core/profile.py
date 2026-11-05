"""Bio autoavaliada — o bot DECIDE se muda ou não, e prova por quê.

Ideia do pedido: "ela também escolhe se muda ou não a bio se não tiver no
nível dela". Traduzido para critérios objetivos e auditáveis:

  * a bio atual é avaliada por regras explícitas (vazia, genérica, CTA,
    clichê de IA, autoridade inventada, termo de bot/IA, manipulação);
  * se não houver problema, a decisão é **NÃO MEXER** — estabilidade é um
    resultado legítimo, e mudar bio toda hora pareceria spam;
  * se houver problema, uma candidata pré-escrita na voz do personagem é
    submetida aos mesmos gates e só aplicada se for objetivamente melhor;
  * cooldown de 30 dias, no máximo 1 troca/30d, mínimo de 10 posts medidos;
  * escrita é um MERGE: preserva avatar, banner, displayName, labels,
    pinnedPost, pronouns, website, createdAt. Usa `swapRecord` com o CID
    lido, então uma alteração concorrente faz a escrita FALHAR em vez de
    apagar o que outra sessão fez;
  * a bio anterior fica gravada para rollback.

SEGURANÇA / ESCOPO
------------------
A bio é do PRÓPRIO perfil: não notifica ninguém, não gera interação, é
reversível. Ainda assim é uma escrita pública, então `BOT_BIO_APPLY=0`
mantém tudo em DRY-RUN (decide e loga, não grava). Rollback em 1 comando.

Nunca usamos LLM para gerar a bio: 4 candidatas determinísticas, zero custo,
zero latência e nenhuma chance do modelo inventar número ou promessa.

Kill switch: BOT_BIO=0 desliga a camada inteira.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import influence as INF

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

ENABLED = os.environ.get("BOT_BIO", "1").strip().lower() not in {"0", "false", "no", "off"}
APPLY = os.environ.get("BOT_BIO_APPLY", "0").strip().lower() in {"1", "true", "yes", "on"}
MIN_DAYS = float(os.environ.get("BIO_MIN_DAYS", "30"))
MIN_POSTS = int(os.environ.get("BIO_MIN_POSTS", "10"))
MAX_PER_30D = int(os.environ.get("BIO_MAX_PER_30D", "1"))
MAX_GRAPHEMES = 256          # limite do lexicon app.bsky.actor.profile
MARGEM = 5.0                 # score mínimo de melhora para justificar a troca

# ── o que caracteriza uma bio "fora do nível" ──────────────────────────────
GENERICA_RX = re.compile(
    r"\b(?:bem[- ]vindo|acompanhe|novidades|conte[uú]do|posts?\s+di[aá]rios?|"
    r"intelig[eê]ncia\s+artificial|\bia\b|\bbot\b|assistente|modelo\s+de\s+linguagem|"
    r"engenharia\s+social|influencer|especialista\s+em|crescer|seguidores|"
    r"engage|engajamento|growth|4x|10x|guru|mentor)\b", re.I)

SEGREDO_RX = re.compile(
    r"(ghp_|github_pat_|sk-[A-Za-z0-9]{16,}|\b\d{4}-\d{4}-\d{4}-\d{4}\b|"
    r"accessJwt|refreshJwt|Bearer\s+[A-Za-z0-9._-]{20,})", re.I)
URL_RX = re.compile(r"https?://|www\.|\b(?:bit\.ly|t\.co|tinyurl)/\S+", re.I)

CANDIDATAS: List[str] = [
    "18 anos, Brasil. Falo de jogo, café, código e do que eu reparo no dia. No meu ritmo.",
    "Curto detalhe pequeno: um café que saiu bom, uma música que grudou, um código que compilou. Escrevo daqui.",
    "Jogo, música, futebol e o caos normal do dia. Uma ideia por post, sem pressa.",
    "Observando o dia a dia e escrevendo o que sobra. Café, tela e assunto besta, no bom sentido.",
]


def graphemes(s: str) -> int:
    """Aproximação conservadora: conta pontos de código, não bytes."""
    return len(s)


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class BioState:
    path: Path = DATA / "bio_state.json"
    ultima_troca: float = 0.0
    trocas: List[float] = field(default_factory=list)
    anterior: Dict[str, Any] = field(default_factory=dict)
    decisoes: List[Dict[str, Any]] = field(default_factory=list)

    def load(self) -> "BioState":
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.ultima_troca = float(d.get("ultima_troca") or 0)
            self.trocas = [float(x) for x in (d.get("trocas") or [])]
            self.anterior = dict(d.get("anterior") or {})
            self.decisoes = list(d.get("decisoes") or [])[-25:]
        except Exception:
            pass                      # corrompido → começa limpo, fail-closed
        self.trocas = [x for x in self.trocas if x > time.time() - 30 * 86400]
        return self

    def save(self, now: Optional[float] = None) -> None:
        agora = now if now is not None else time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "ultima_troca": self.ultima_troca,
                "trocas": self.trocas[-10:],
                "anterior": self.anterior,
                # Só metadados. Nenhum segredo, nenhum texto de terceiro.
                "decisoes": self.decisoes[-25:],
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            pass

    def registrar_decisao(self, d: Dict[str, Any]) -> None:
        self.decisoes.append({
            "ts": time.time(),
            "mudar": bool(d.get("mudar")),
            "motivo": str(d.get("motivo") or "")[:120],
            "score_atual": d.get("score_atual"),
            "score_novo": d.get("score_novo"),
            "dry_run": not APPLY,
        })


def candidata(arquétipo: str = "", agora: Optional[float] = None) -> str:
    """Determinística: mesma data + mesmo arquétipo → mesma candidata."""
    dia = time.strftime("%Y-%m-%d", time.gmtime(agora if agora is not None else time.time()))
    h = int(hashlib.sha256(f"{dia}|{arquétipo}".encode()).hexdigest()[:12], 16)
    return CANDIDATAS[h % len(CANDIDATAS)]


def problemas(bio: str) -> List[str]:
    """Lista explícita do que desqualifica a bio atual. Vazia = está no nível."""
    if not isinstance(bio, str):
        return ["nao_texto"]
    t = bio.strip()
    p: List[str] = []
    if len(t) < 20:
        p.append("vazia_ou_curta_demais")
    if graphemes(t) > MAX_GRAPHEMES:
        p.append("acima_de_256_graphemes")
    if GENERICA_RX.search(t):
        p.append("generica_ou_afirma_ser_ia")
    if SEGREDO_RX.search(t):
        p.append("contem_padrao_de_segredo")
    if URL_RX.search(t):
        p.append("contem_link")
    for m in INF.blocklist(t):
        m = str(m)
        if m not in p:
            p.append(m)
    return p


def bio_segura(texto: str) -> Tuple[bool, List[str]]:
    if not isinstance(texto, str):
        return False, ["nao_texto"]
    t = texto.strip()
    m = problemas(t)
    ok, extra = INF.seguro(t)
    for e in extra:
        if e not in m:
            m.append(e)
    return (ok and not m), m


@dataclass
class Decisao:
    mudar: bool
    motivo: str
    atual: str = ""
    candidata: str = ""
    score_atual: float = 0.0
    score_novo: float = 0.0
    razoes: List[str] = field(default_factory=list)
    dry_run: bool = True

    def resumo(self) -> Dict[str, Any]:
        return {
            "mudar": self.mudar, "motivo": self.motivo,
            "score_atual": self.score_atual, "score_novo": self.score_novo,
            "razoes": self.razoes[:8], "dry_run": self.dry_run,
            "candidata_hash": _h(self.candidata) if self.candidata else "",
        }


def avaliar(bio_atual: str, posts_recentes: List[str], state: BioState,
            *, arquétipo: str = "", outcomes: Optional[List[Any]] = None,
            now: Optional[float] = None, apply: Optional[bool] = None) -> Decisao:
    """O bot ESCOLHE. Nunca lança. Sem dados suficientes → NÃO MEXER."""
    if not ENABLED:
        return Decisao(False, "BOT_BIO=0", dry_run=True)
    agora = now if now is not None else time.time()
    permitido = APPLY if apply is None else bool(apply)
    atual = (bio_atual or "").strip()
    proposta = candidata(arquétipo, agora)
    dry = not permitido

    probs = problemas(atual)
    score_atual = INF.autoridade(atual, historico=posts_recentes, outcomes=outcomes)["score"]
    ok_nova, probs_nova = bio_segura(proposta)
    score_novo = INF.autoridade(proposta, historico=posts_recentes, outcomes=outcomes)["score"]

    def nao(motivo: str, razoes: Optional[List[str]] = None) -> Decisao:
        return Decisao(False, motivo, atual, proposta, score_atual, score_novo,
                       (razoes or [])[:8], dry)

    # 1) Sem amostra, não há como julgar "nível".
    if len(posts_recentes) < MIN_POSTS:
        return nao("amostra_insuficiente_nao_mexer",
                   [f"{len(posts_recentes)} posts < {MIN_POSTS} mínimo"])
    # 2) Já está coerente → estabilidade é a decisão correta.
    if not probs:
        return nao("bio_ja_coerente_nao_mexer",
                   [f"score {score_atual:.1f}; nenhum problema objetivo"])
    # 3) A candidata também precisa estar impecável.
    if not ok_nova:
        return nao("candidata_reprovada_nao_mexer", probs_nova)
    # 4) Tem que ser objetivamente melhor.
    if score_novo < score_atual + MARGEM:
        return nao("sem_melhora_objetiva_nao_mexer",
                   [f"{score_atual:.1f} -> {score_novo:.1f}, margem {MARGEM}"])
    # 5) Cooldown e teto de trocas.
    if agora - state.ultima_troca < MIN_DAYS * 86400:
        return nao("cooldown_nao_mexer",
                   [f"última troca há {(agora - state.ultima_troca) / 86400:.1f}d"])
    if len(state.trocas) >= MAX_PER_30D:
        return nao("teto_de_trocas_nao_mexer",
                   [f"{len(state.trocas)} trocas em 30d"])
    # 6) Só então decide mudar.
    return Decisao(True, "bio_fora_do_nivel_e_candidata_melhor",
                   atual, proposta, score_atual, score_novo,
                   ["problemas: " + ", ".join(probs[:3]),
                    f"score {score_atual:.1f} -> {score_novo:.1f}"], dry)


def aplicar(bsky: Any, decisao: Decisao, state: BioState,
            *, now: Optional[float] = None,
            apply: Optional[bool] = None) -> Dict[str, Any]:
    """Executa a decisão. Com BOT_BIO_APPLY=0 é DRY-RUN (zero escrita).

    `apply` permite injetar a autorização em testes e no heartbeat sem
    depender de variável de ambiente global. Retorna sempre um dict —
    nunca levanta exceção para o heartbeat.
    """
    agora = now if now is not None else time.time()
    permitido = APPLY if apply is None else bool(apply)
    dry = not permitido
    r: Dict[str, Any] = {"mudar": decisao.mudar, "aplicada": False,
                         "dry_run": dry, "motivo": decisao.motivo}
    resumo = {**decisao.resumo(), "dry_run": dry}
    if not decisao.mudar or dry:
        state.registrar_decisao(resumo)
        state.save(agora)
        return r
    try:
        rec = bsky.get_profile_record()
        value = dict(rec.get("value") or {})
        cid = str(rec.get("cid") or "")
        base = str(rec.get("base") or "")
        atual_real = str(value.get("description") or "")
        # Outra sessão mudou a bio desde a avaliação? Não sobrescrever.
        if atual_real.strip() and atual_real.strip() != decisao.atual.strip():
            r.update({"aplicada": False, "motivo": "bio_mudou_desde_avaliacao"})
            state.registrar_decisao({"mudar": False, "motivo": r["motivo"],
                                     "dry_run": dry})
            state.save(agora)
            return r
        ok, mot = bio_segura(decisao.candidata)
        if not ok:
            r["motivo"] = "candidata_reprovada_no_momento_da_escrita: " + ",".join(mot[:3])
            state.registrar_decisao({"mudar": False, "motivo": r["motivo"],
                                     "dry_run": dry})
            state.save(agora)
            return r
        # Rollback ANTES de gravar.
        state.anterior = {"description": atual_real, "cid": cid,
                          "display_name": str(value.get("displayName") or ""),
                          "ts": agora}
        out = bsky.put_profile_description(value, cid, decisao.candidata, base=base)
        state.ultima_troca = agora
        state.trocas.append(agora)
        state.registrar_decisao(resumo)
        state.save(agora)
        r.update({"aplicada": True, "uri": str(out.get("uri") or ""),
                  "cid_novo": str(out.get("cid") or ""),
                  "anterior_hash": _h(atual_real),
                  "nova_hash": _h(decisao.candidata)})
    except Exception as e:
        r.update({"aplicada": False, "erro": f"{type(e).__name__}: {str(e)[:120]}"})
    return r


def rollback(bsky: Any, state: BioState) -> Dict[str, Any]:
    """Restaura a bio anterior. Ação humana, disparada por script, não automática."""
    ant = state.anterior or {}
    if not ant:
        return {"restaurado": False, "motivo": "sem_checkpoint"}
    try:
        rec = bsky.get_profile_record()
        value = dict(rec.get("value") or {})
        cid = str(rec.get("cid") or "")
        base = str(rec.get("base") or "")
        out = bsky.put_profile_description(value, cid,
                                           str(ant.get("description") or ""), base=base)
        state.decisoes.append({"ts": time.time(), "mudar": False,
                              "motivo": "rollback_manual"})
        state.save()
        return {"restaurado": True, "uri": str(out.get("uri") or ""),
                "descricao_hash": _h(str(ant.get("description") or ""))}
    except Exception as e:
        return {"restaurado": False, "erro": f"{type(e).__name__}: {str(e)[:120]}"}


def make_heartbeat(bsky: Any, mem: Any, growth: Any = None,
                   *, interval_min: float = 180.0,
                   log: Optional[Callable[[str], None]] = None):
    """Avalia a bio a cada 3 h, fora do sorteio de ações, isolado por try/except."""
    state = BioState().load()
    last = [0.0]
    emit = log or (lambda _s: None)

    def hb() -> None:
        if not ENABLED:
            return
        agora = time.time()
        if agora - last[0] < interval_min * 60:
            return
        last[0] = agora
        try:
            posts = list(mem.recent_texts(40) or [])
            outcomes = list(getattr(growth, "outcomes", []) or []) if growth else []
            arq = ""
            try:
                from .personas import escolher
                arq = escolher()
            except Exception:
                pass
            rec = bsky.get_profile_record()
            atual = str((rec.get("value") or {}).get("description") or "")
            d = avaliar(atual, posts, state, arquétipo=arq, outcomes=outcomes, now=agora)
            r = aplicar(bsky, d, state, now=agora)
            emit("bio: " + json.dumps({**d.resumo(), **r}, ensure_ascii=False,
                                     default=str)[:400])
        except Exception as e:
            emit(f"bio: avaliação isolada ({type(e).__name__})")

    return hb


def descrever() -> Dict[str, Any]:
    return {
        "ativo": ENABLED, "aplicar": APPLY,
        "min_dias": MIN_DAYS, "min_posts": MIN_POSTS,
        "max_trocas_30d": MAX_PER_30D,
        "candidatas": len(CANDIDATAS),
        "merge_preserva": ["avatar", "banner", "displayName", "labels",
                           "pinnedPost", "pronouns", "website", "createdAt"],
        "protecao_concorrencia": "swapRecord com o CID lido",
        "rollback": "data/bio_state.json + scripts/bio_rollback.py",
        "kill_switch": "BOT_BIO=0 / BOT_BIO_APPLY=0 (dry-run)",
    }

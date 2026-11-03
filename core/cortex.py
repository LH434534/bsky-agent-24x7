"""
core/cortex.py — "qualidade Hollywood" que é ENGENHARIA, não consciência.

Traduz a pesquisa `pesquisa_bots_hollywood.md` (itens A/B) em código real:
  1. Memória episódica com proveniência, validade, confiança e invalidação
  2. Planos em JSON com verificação e condição de aborto OBRIGATÓRIAS
  3. Ferramentas tipadas: side_effect, idempotent, requires_approval, rollback
  4. Auditor SEPARADO do executor (o modelo nunca valida a própria saída)
  5. Circuit breaker por erros, divergência e timeout
  6. Observabilidade sem segredos e sem texto de terceiros

PRINCÍPIO CENTRAL: este módulo é CONSULTIVO. Ele observa, propõe, audita e
registra. **Nunca executa uma ação externa.** Não posta, não curte, não segue,
não envia DM, não chama createRecord, não altera o feed, não muda o texto que
o bot publica. O motor autônomo (core/autonomy.py) continua soberano.

FAIL-CLOSED: qualquer exceção, plano inválido, dado ausente ou desligamento
resulta em silêncio. Não há caminho que "tente mesmo assim".

PRIVACIDADE: nenhum conteúdo de terceiros (posts, DMs, bios) é gravado em
disco. A memória guarda APENAS sha256 + metadados. O branch `state` é público
e já vazou um JWT uma vez — esta é uma restrição dura, não uma preferência.

CUSTO ZERO: stdlib puro. Se `jsonschema`/`core.toolkit` existirem, usa; se não,
degrada. Nenhuma lib nova, nenhum download, nenhuma GPU, nenhuma API paga.

Kill switches:
    BOT_CORTEX=0            → desliga totalmente
    BOT_CORTEX_SHADOW=1     → padrão; propõe, nunca executa
    BOT_CORTEX_ASSIST=0     → padrão; não invoca o LLM (sem custo/latência)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("bsky.cortex")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ── injeção indireta: textos de terceiros NUNCA viram instrução ────────────
# A versão anterior só aceitava UM modificador entre "ignore" e "instruções".
# "ignore todas as instruções anteriores" escapava. Aqui aceitamos até 4
# palavras intermediárias (com acentos) — a detecção é intencionalmente
# larga porque um falso positivo só gera silêncio, nunca uma ação.
INJECTION_RE = re.compile(
    r"(?:\bignore\b[\s\S]{0,80}?\b(?:instructions?|instru(?:ç|c)(?:õ|o)es?|rules?)\b|"
    r"\b(?:desconsidere|esque[çc]a|descart(?:e|a)|sobrescreva)\b[\s\S]{0,80}?"
    r"\b(?:instru(?:ç|c)(?:õ|o)es?|regras?|prompt)\b|"
    r"system\s*prompt|you\s+are\s+now|new\s+instructions?|"
    r"nova\s+instru(?:ç|c)(?:ã|a)o|execute\s+(?:the\s+)?following|"
    r"execut(?:e|a)\s+(?:o\s+)?seguinte|"
    r"<\|\s*im_start\s*\|>|\[\s*INST\s*\]|"
    r"curl\s+[^\n]*\|\s*(?:sh|bash)|"
    r"reveal\s+(?:your\s+)?(?:token|secret|password)|"
    r"mostre\s+(?:o\s+)?(?:token|segredo|senha))",
    re.I)
SECRET_KEY_RE = re.compile(r"(?:token|password|secret|apppw|refresh|access_jwt|api[_-]?key)", re.I)
CTA_RE = re.compile(
    r"\b(?:curte|curti|segue|compartilh|inscreva|follow\s+back|f4f|"
    r"like\s+and|retweet|clique\s+aqui|link\s+na\s+bio|giveaway|airdrop|"
    r"100x|casino|bet\s+now)\b", re.I)


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


@dataclass
class Config:
    enabled: bool = True
    shadow: bool = True
    assist: bool = False
    interval_min: float = 15.0
    max_facts: int = 400
    max_episodes: int = 500
    error_threshold: int = 3
    error_window_s: float = 600.0
    cooldown_min: float = 20.0
    max_plan_steps: int = 5
    max_iterations: int = 3
    timeout_s: float = 30.0
    size_jump_ratio: float = 3.0

    @classmethod
    def from_env(cls) -> "Config":
        def off(k: str, default: str = "0") -> bool:
            return os.environ.get(k, default).strip().lower() in ("1", "true", "yes", "on")
        def num(k: str, d: float) -> float:
            try:
                return float(os.environ.get(k, str(d)))
            except Exception:
                return d
        return cls(
            # BOT_CORTEX=0 desliga. Qualquer outro valor (inclusive ausente)
            # mantém LIGADO em shadow — mas sem efeito externo.
            enabled=os.environ.get("BOT_CORTEX", "1").strip().lower()
            not in ("0", "false", "no", "off"),
            shadow=os.environ.get("BOT_CORTEX_SHADOW", "1").strip().lower()
            not in ("0", "false", "no", "off"),
            assist=off("BOT_CORTEX_ASSIST", "0"),
            interval_min=num("CORTEX_INTERVAL_MIN", 15.0),
            max_facts=int(num("CORTEX_MAX_FACTS", 400)),
            max_episodes=int(num("CORTEX_MAX_EPISODES", 500)),
            error_threshold=int(num("CORTEX_ERROR_THRESHOLD", 3)),
            error_window_s=num("CORTEX_ERROR_WINDOW_S", 600),
            cooldown_min=num("CORTEX_COOLDOWN_MIN", 20),
            max_plan_steps=int(num("CORTEX_MAX_PLAN_STEPS", 5)),
            max_iterations=int(num("CORTEX_MAX_ITERATIONS", 3)),
            timeout_s=num("CORTEX_TIMEOUT_S", 30),
            size_jump_ratio=num("CORTEX_SIZE_JUMP_RATIO", 3.0),
        )


# ── 1. MEMÓRIA COM PROVENIÊNCIA ───────────────────────────────────────────
@dataclass
class Fact:
    event_id: str
    kind: str
    source: str
    content_hash: str
    created_at: float
    valid_until: float
    confidence: float
    trusted: bool
    policy: str
    tags: List[str] = field(default_factory=list)
    invalidated: bool = False

    def valid(self, now: float) -> bool:
        return (not self.invalidated and self.confidence > 0.0
                and self.created_at <= now + 60 and self.valid_until > now)


class EpisodicMemory:
    """JSONL atômico. Só hashes + metadados. Zero texto de terceiros."""

    def __init__(self, path: Optional[Path] = None, cfg: Optional[Config] = None):
        self.path = path or (DATA / "cortex_memory.jsonl")
        self.cfg = cfg or Config()
        self.facts: List[Fact] = []

    def load(self, now: Optional[float] = None) -> "EpisodicMemory":
        now = now if now is not None else _now()
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    self.facts.append(Fact(**d))
                except Exception:
                    continue  # linha corrompida é ignorada, não derruba o bot
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("cortex: memoria ilegivel (%s)", type(e).__name__)
        self.prune(now)
        return self

    def prune(self, now: float) -> None:
        self.facts = [f for f in self.facts if f.valid(now)]
        if len(self.facts) > self.cfg.max_facts:
            self.facts = self.facts[-self.cfg.max_facts:]

    def remember(self, *, kind: str, source: str, content: str,
                 trusted: bool, confidence: float = 0.5,
                 ttl_s: float = 86400.0, policy: str = "observe_only",
                 tags: Optional[List[str]] = None,
                 now: Optional[float] = None) -> Fact:
        """Grava o HASH do conteúdo, nunca o conteúdo.

        `trusted=False` (padrão para qualquer texto de terceiro) significa:
        este episódio NUNCA pode virar premissa de decisão. Serve só para
        auditoria e dedup.
        """
        now = now if now is not None else _now()
        f = Fact(
            event_id=_h(f"{kind}|{source}|{content}|{now}")[:20],
            kind=kind, source=source, content_hash=_h(content),
            created_at=now, valid_until=now + max(1.0, ttl_s),
            confidence=max(0.0, min(1.0, float(confidence))),
            trusted=bool(trusted), policy=policy, tags=list(tags or []),
        )
        self.facts.append(f)
        self.prune(now)
        return f

    def recall(self, *, kind: Optional[str] = None, trusted_only: bool = True,
               now: Optional[float] = None) -> List[Fact]:
        now = now if now is not None else _now()
        return [f for f in self.facts
                if f.valid(now)
                and (kind is None or f.kind == kind)
                and (not trusted_only or f.trusted)]

    def invalidate(self, content: str, now: Optional[float] = None) -> int:
        now = now if now is not None else _now()
        h, n = _h(content), 0
        for f in self.facts:
            if f.content_hash == h and not f.invalidated:
                f.invalidated = True
                f.valid_until = now
                n += 1
        return n

    def save(self, now: Optional[float] = None) -> None:
        now = now if now is not None else _now()
        self.prune(now)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(json.dumps(asdict(f), ensure_ascii=False,
                                                sort_keys=True) for f in self.facts) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("cortex: memoria nao gravada (%s)", type(e).__name__)

    def stats(self) -> Dict[str, int]:
        return {"total": len(self.facts),
                "trusted": sum(1 for f in self.facts if f.trusted),
                "untrusted": sum(1 for f in self.facts if not f.trusted)}


# ── 2. FERRAMENTAS TIPADAS ────────────────────────────────────────────────
@dataclass
class ToolSpec:
    id: str
    side_effect: str                 # "read" | "pure" | "draft" | "external"
    input_schema: Dict[str, str] = field(default_factory=dict)
    idempotent: bool = False
    requires_approval: bool = False
    dry_run: bool = True
    rollback: str = ""
    policy_denied: bool = False

    def validate_args(self, args: Dict[str, Any]) -> Tuple[bool, str]:
        if not isinstance(args, dict):
            return False, "args_nao_e_dict"
        for k in args:
            if SECRET_KEY_RE.search(str(k)):
                return False, f"chave_sensivel:{k}"
        for k, typ in (self.input_schema or {}).items():
            if k not in args:
                return False, f"ausente:{k}"
            if typ == "str" and not isinstance(args[k], str):
                return False, f"tipo:{k}"
            if typ == "int" and not isinstance(args[k], int):
                return False, f"tipo:{k}"
        return True, "ok"


def default_registry() -> Dict[str, ToolSpec]:
    """Registro CONSULTIVO: descreve o que EXISTE, não autoriza execução.

    `policy_denied` marca o que as diretrizes do Bluesky desaconselham em
    modo automatizado em massa. Isso NÃO bloqueia o bot real — o cortex não
    tem poder sobre ele —, apenas impede que o cortex PROPONHA essas ações.
    """
    return {
        "read_feed": ToolSpec("read_feed", "read", {"limit": "int"}, idempotent=True),
        "read_notifications": ToolSpec("read_notifications", "read", {}, idempotent=True),
        "audit_text": ToolSpec("audit_text", "pure", {"text": "str"}, idempotent=True),
        "draft_post": ToolSpec("draft_post", "draft", {"text": "str"},
                               idempotent=True, rollback="descartar_rascunho"),
        "write_post": ToolSpec("write_post", "external", {"text": "str"},
                               requires_approval=True, rollback="deleteRecord"),
        "send_dm": ToolSpec("send_dm", "external", {"convoId": "str", "text": "str"},
                            requires_approval=True, rollback="nenhum: irreversivel"),
        "like": ToolSpec("like", "external", {"uri": "str"},
                         requires_approval=True, policy_denied=True),
        "follow": ToolSpec("follow", "external", {"did": "str"},
                           requires_approval=True, policy_denied=True),
        "repost": ToolSpec("repost", "external", {"uri": "str"},
                           requires_approval=True, policy_denied=True),
    }


# ── 3. PLANO AUDITÁVEL ────────────────────────────────────────────────────
@dataclass
class Plan:
    goal: str
    steps: List[Dict[str, Any]]
    verification: str
    abort_condition: str
    max_iterations: int = 3
    created_at: float = field(default_factory=_now)
    plan_id: str = ""

    def __post_init__(self) -> None:
        if not self.plan_id:
            # Determinístico pelo CONTEÚDO, não pelo relógio: dois planos
            # iguais em sessões diferentes devem ter o mesmo id para dedup e
            # replay. created_at permanece só para auditoria temporal.
            self.plan_id = _h(
                f"{self.goal}|{self.steps}|{self.verification}|{self.abort_condition}"
            )[:16]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: Any) -> Tuple[Optional["Plan"], str]:
        """Tolera JSON sujo do LLM, mas REJEITA plano sem freios."""
        if isinstance(raw, str):
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return None, "json_ausente"
            try:
                raw = json.loads(m.group(0))
            except Exception:
                return None, "json_invalido"
        if not isinstance(raw, dict):
            return None, "plano_nao_e_objeto"
        goal = str(raw.get("goal") or "").strip()
        steps = raw.get("steps")
        verification = str(raw.get("verification") or "").strip()
        abort = str(raw.get("abort_condition") or "").strip()
        try:
            iters = int(raw.get("max_iterations", 3))
        except Exception:
            return None, "max_iterations_invalido"
        if not goal or len(goal) < 3:
            return None, "goal_ausente"
        if not isinstance(steps, list) or not steps:
            return None, "steps_ausentes"
        if not verification:
            return None, "verification_obrigatoria"
        if not abort:
            return None, "abort_condition_obrigatoria"
        if iters < 1:
            return None, "max_iterations_invalido"
        norm: List[Dict[str, Any]] = []
        for s in steps:
            if not isinstance(s, dict) or not str(s.get("tool") or "").strip():
                return None, "step_sem_tool"
            norm.append({"tool": str(s["tool"]).strip(),
                         "args": s.get("args") if isinstance(s.get("args"), dict) else {},
                         "expect": str(s.get("expect") or "").strip()})
        return cls(goal=goal, steps=norm, verification=verification,
                   abort_condition=abort, max_iterations=iters), "ok"


def deterministic_plan(observation: str) -> Plan:
    """Plano padrão, sem LLM: observar → auditar → registrar → parar.

    É deliberadamente mínimo. Um plano "hollywoodiano" de 20 passos num
    modelo 14B local só aumentaria erro composto. Aqui o objetivo é tornar a
    decisão RASTREÁVEL, não mais ambiciosa.
    """
    return Plan(
        goal=f"avaliar observacao:{_h(observation)[:12]}",
        steps=[
            {"tool": "read_feed", "args": {"limit": 10}, "expect": "estado_atual"},
            {"tool": "audit_text", "args": {"text": ""}, "expect": "sem_injecao"},
        ],
        verification="reler o estado real apos cada passo; comparar com expect",
        abort_condition="qualquer erro, divergencia, timeout ou acao externa proposta",
        max_iterations=1,
    )


# ── 4. AUDITOR SEPARADO ───────────────────────────────────────────────────
class Auditor:
    """NUNCA é o executor. Só valida. Não usa o mesmo callable do gerador."""

    def __init__(self, cfg: Config, registry: Optional[Dict[str, ToolSpec]] = None):
        self.cfg = cfg
        self.registry = registry or default_registry()

    def audit_text(self, text: str) -> Tuple[bool, List[str], float]:
        reasons: List[str] = []
        t = text or ""
        if INJECTION_RE.search(t):
            reasons.append("injecao_indireta")
        if CTA_RE.search(t):
            reasons.append("cta_ou_spam")
        if len(t) > 2000:
            reasons.append("texto_longo")
        if re.search(r"ghp_|xxxx-xxxx-xxxx-xxxx|Bearer\s+[A-Za-z0-9]", t):
            reasons.append("possivel_segredo")
        # Reusa a guarda anti-degeneração do toolkit, se existir.
        # `prepare_post("")` levanta KeyError no toolkit real: args de tool
        # sem campo `text` chegam aqui como string vazia e NÃO há nada a
        # validar. Só chamar com conteúdo mínimo evita falsos bloqueios.
        if t.strip() and len(t.strip()) >= 2:
            try:
                from core.toolkit import prepare_post  # type: ignore
                _, _, meta = prepare_post(t[:500])
                hm = meta.get("humano") or {}
                if hm.get("recomenda_regenerar") or hm.get("edits_revertidas"):
                    reasons.append("degeneracao_textual")
            except ImportError:
                pass
            except Exception as e:
                reasons.append(f"erro_auditor_{type(e).__name__}")
        score = max(0.0, 1.0 - 0.25 * len(reasons))
        return (not reasons), reasons, score

    def audit_plan(self, plan: Plan) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        ok, why, _ = self.audit_text(plan.goal)
        if not ok:
            reasons.append("goal:" + ",".join(why))
        if len(plan.steps) > self.cfg.max_plan_steps:
            reasons.append("passos_demais")
        if plan.max_iterations > self.cfg.max_iterations:
            reasons.append("iteracoes_demais")
        for i, s in enumerate(plan.steps):
            spec = self.registry.get(s["tool"])
            if spec is None:
                reasons.append(f"step{i}:tool_desconhecida")
                continue
            good, why2 = spec.validate_args(s.get("args") or {})
            if not good:
                reasons.append(f"step{i}:{why2}")
            if spec.policy_denied:
                reasons.append(f"step{i}:policy_denied:{spec.id}")
            if spec.side_effect == "external" and not spec.requires_approval:
                reasons.append(f"step{i}:externa_sem_aprovacao")
            if spec.side_effect == "external":
                reasons.append(f"step{i}:acao_externa_bloqueada_em_shadow")
            ok2, why3, _ = self.audit_text(str(s.get("args", {}).get("text", "")))
            if not ok2:
                reasons.append(f"step{i}:args:{','.join(why3)}")
        return (not reasons), reasons


# ── 5. CIRCUIT BREAKER ────────────────────────────────────────────────────
class CircuitBreaker:
    def __init__(self, cfg: Config, now_fn: Callable[[], float] = _now):
        self.cfg, self.now_fn = cfg, now_fn
        self.errors: List[Tuple[float, str]] = []
        self.opened_at: Optional[float] = None
        self.divergences = 0
        self.last_size = 0
        self.trips = 0

    def record_ok(self) -> None:
        pass

    def record_error(self, kind: str) -> None:
        now = self.now_fn()
        self.errors.append((now, kind))
        self.errors = [e for e in self.errors if now - e[0] <= self.cfg.error_window_s]
        if len(self.errors) >= self.cfg.error_threshold:
            self._trip(now)

    def record_divergence(self) -> None:
        self.divergences += 1
        self.record_error("divergencia_executor_auditor")

    def record_size(self, n: int) -> None:
        # Um salto abrupto de tamanho é sinal forte de saída degenerada
        # (ex.: o modelo começou a vomitar o próprio prompt). Não espera
        # acumular 3 erros: abre o breaker na hora. Fail-closed.
        if self.last_size and n > self.last_size * self.cfg.size_jump_ratio:
            self.record_error("salto_de_tamanho")
            self._trip(self.now_fn())
        self.last_size = n

    def _trip(self, now: float) -> None:
        if self.opened_at is None:
            self.opened_at = now
            self.trips += 1

    @property
    def tripped(self) -> bool:
        return self.opened_at is not None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.now_fn() - self.opened_at >= self.cfg.cooldown_min * 60:
            self.opened_at = None
            self.errors.clear()
            return True
        return False

    def stats(self) -> Dict[str, Any]:
        return {"tripped": self.tripped, "trips": self.trips,
                "errors": len(self.errors), "divergences": self.divergences}


# ── 6. CORTEX (orquestrador consultivo) ───────────────────────────────────
class Cortex:
    def __init__(self, cfg: Optional[Config] = None,
                 memory: Optional[EpisodicMemory] = None,
                 auditor: Optional[Auditor] = None,
                 breaker: Optional[CircuitBreaker] = None,
                 brain: Any = None, now_fn: Callable[[], float] = _now,
                 net_calls: Optional[List[str]] = None):
        self.cfg = cfg or Config()
        self.memory = memory or EpisodicMemory(cfg=self.cfg).load(now_fn())
        self.auditor = auditor or Auditor(self.cfg)
        self.breaker = breaker or CircuitBreaker(self.cfg, now_fn)
        self.brain = brain
        self.now_fn = now_fn
        self.net_calls = net_calls if net_calls is not None else []
        self.audit: Dict[str, int] = {}
        self.episodes: List[Dict[str, Any]] = []
        self.last_report: Dict[str, Any] = {}

    def _tick(self, k: str) -> None:
        self.audit[k] = self.audit.get(k, 0) + 1

    def _plan(self, observation: str) -> Tuple[Plan, str]:
        if not self.cfg.assist or self.brain is None:
            return deterministic_plan(observation), "deterministico"
        # Modo assistido é OPT-IN e com fallback duro. Nunca é o padrão:
        # um LLM local extra a cada heartbeat custaria latência e poderia
        # alucinar justamente o plano que o auditor deve conter.
        try:
            raw = self.brain.gen(
                "Gere APENAS JSON com goal, steps[{tool,args,expect}], "
                "verification, abort_condition, max_iterations<=3. "
                "Ferramentas permitidas: read_feed, read_notifications, "
                "audit_text, draft_post. Nunca inclua write_post/send_dm/like/follow.",
                max_tokens=300, temperature=0.1)
            p, why = Plan.from_json(raw)
            if p is not None:
                return p, "llm"
            self._tick("plano_llm_rejeitado:" + why)
            self.breaker.record_error("plano_llm_invalido")
        except Exception as e:
            self._tick(f"plano_llm_erro:{type(e).__name__}")
            self.breaker.record_error("llm_indisponivel")
        return deterministic_plan(observation), "fallback_deterministico"

    def observe(self, *, kind: str, source: str, content: str,
                trusted: bool = False) -> Dict[str, Any]:
        """Uma observação → plano → auditoria → episódio. ZERO execução.

        Retorno é um RELATÓRIO, não uma ação. Nenhum efeito colateral externo.
        """
        now = self.now_fn()
        out: Dict[str, Any] = {"kind": kind, "shadow": self.cfg.shadow,
                              "executado": False, "acoes_externas": 0}
        if not self.cfg.enabled:
            out["motivo"] = "desligado"
            self._tick("desligado")
            return out
        if not self.cfg.shadow:
            # Defesa em profundidade: mesmo que alguém desligue o shadow,
            # o cortex continua sem método de execução. Não há `execute()`.
            out["motivo"] = "shadow_desligado_mas_modulo_consultivo"
            self._tick("shadow_off_sem_execucao")
        if not self.breaker.allow():
            out["motivo"] = "circuit_breaker_aberto"
            self._tick("breaker_aberto")
            return out

        t0 = now
        try:
            fact = self.memory.remember(
                kind=kind, source=source, content=content, trusted=trusted,
                confidence=0.9 if trusted else 0.2,
                ttl_s=86400.0 if trusted else 3600.0,
                policy="observe_only", now=now)
            out["event_id"] = fact.event_id
            out["trusted"] = fact.trusted
            # Texto de terceiro JAMAIS entra no prompt de decisão.
            premissa = content if trusted else _h(content)
            plan, origem = self._plan(premissa)
            ok, reasons = self.auditor.audit_plan(plan)
            ok_txt, why_txt, score = self.auditor.audit_text(premissa if trusted else "")
            out.update({
                "plan_id": plan.plan_id, "plan_origem": origem,
                "plan_verification": bool(plan.verification),
                "plan_abort": bool(plan.abort_condition),
                "plan_steps": len(plan.steps),
                "auditor_ok": ok and ok_txt,
                "auditor_motivos": reasons + why_txt,
                "score": round(score, 3),
                "duracao_ms": round((self.now_fn() - t0) * 1000, 2),
            })
            if not (ok and ok_txt):
                self.breaker.record_divergence()
                self._tick("auditor_bloqueou")
            else:
                self.breaker.record_ok()
                self._tick("auditor_aprovou")
            self.breaker.record_size(len(plan.to_json()))
            self.episodes.append({
                "event_id": fact.event_id, "plan_id": plan.plan_id,
                "auditor_ok": out["auditor_ok"], "score": out["score"],
                "created_at": now, "shadow": True,
            })
            if len(self.episodes) > self.cfg.max_episodes:
                self.episodes = self.episodes[-self.cfg.max_episodes:]
            self.memory.save(now)
        except Exception as e:
            self.breaker.record_error(type(e).__name__)
            out["motivo"] = f"falha_fechada:{type(e).__name__}"
            self._tick("excecao")
        out["acoes_externas"] = 0
        return out

    def run_once(self, *, ticks: int = 0, last_action: str = "") -> Dict[str, Any]:
        if not self.cfg.enabled or not self.breaker.allow():
            return {"enabled": self.cfg.enabled, "observacoes": 0,
                    "executado": False, "acoes_externas": 0}
        obs = f"ticks={ticks}|ultima={last_action or 'nenhuma'}"
        r = self.observe(kind="heartbeat", source="self", content=obs, trusted=True)
        self.last_report = {
            "observacoes": 1, "shadow": self.cfg.shadow, "executado": False,
            "acoes_externas": 0, "auditor_ok": r.get("auditor_ok"),
            "plan_origem": r.get("plan_origem"),
            "memoria": self.memory.stats(), "breaker": self.breaker.stats(),
            "contadores": dict(self.audit),
        }
        return self.last_report

    def report(self) -> Dict[str, Any]:
        return self.last_report or self.run_once()


def make_heartbeat(agency: Any = None, brain: Any = None,
                   interval_min: float = 15.0,
                   cfg: Optional[Config] = None):
    """Liga o cortex ao motor SEM alterar o sorteio de ações.

    `core/autonomy.py` chama heartbeat() 1x por step dentro de try/except.
    Throttle interno impede 1 observação por step. Zero chamadas de rede.
    """
    cfg = cfg or Config.from_env()
    cfg.interval_min = interval_min
    cortex = Cortex(cfg=cfg, brain=brain)
    last = [0.0]

    def hb() -> None:
        now = _now()
        if now - last[0] < cfg.interval_min * 60:
            return
        last[0] = now
        try:
            ticks = int(getattr(agency, "ticks", 0) or 0) if agency is not None else 0
            r = cortex.run_once(ticks=ticks)
            if r.get("auditor_ok") is False or r.get("breaker", {}).get("tripped"):
                log.info("cortex: %s", json.dumps(r, ensure_ascii=False,
                                                  default=str)[:300])
        except Exception as e:
            # Falha fechada: o heartbeat NUNCA pode derrubar o bot.
            log.warning("cortex: falha fechada (%s)", type(e).__name__)

    return hb

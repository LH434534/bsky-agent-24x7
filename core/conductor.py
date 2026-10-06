"""Maestro — o nível meta que governa o sistema inteiro.

Até aqui, cada parte ficou autônoma por conta própria: a agência decide o que
postar, o grafo social decide com quem falar. Mas QUEM decide se o sistema deve
estar acordado? Quem percebe que ele está postando demais e engajando de menos?
Quem nota que faz uma hora que não acontece nada útil?

Isso é o maestro. Ele não executa ações no Bluesky — ele governa o processo:
  - observa a própria saúde (ações, erros, latência, diversidade)
  - detecta padrões de degradação antes de virar falha
  - ajusta a postura do sistema: acelerar, desacelerar, mudar foco, descansar
  - chama o sistema imune quando algo quebra de verdade
  - sintoniza os parâmetros adaptativos com a evidência acumulada

Ele também pode decidir PARAR. Um sistema que nunca para é automático, não
autônomo — autonomia inclui saber a hora de ficar quieto.
"""
from __future__ import annotations

import json
import random
import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "conductor_state.json"


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} MAESTRO │ {m}"
    print(line, flush=True)
    try:
        with open(DATA / "agent.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


@dataclass
class Vitals:
    """Sinais vitais do sistema, medidos continuamente."""
    acoes: Deque[tuple] = field(default_factory=lambda: deque(maxlen=60))
    erros: Deque[float] = field(default_factory=lambda: deque(maxlen=40))
    latencias: Deque[float] = field(default_factory=lambda: deque(maxlen=40))
    ultima_acao: float = 0.0
    acertos: int = 0
    tentativas: int = 0
    nascimento: float = field(default_factory=time.time)

    def note(self, kind: str, ok: bool, seconds: float = 0.0) -> None:
        self.acoes.append((time.time(), kind, ok))
        self.tentativas += 1
        if ok:
            self.acertos += 1
            self.ultima_acao = time.time()
            if seconds > 0:
                self.latencias.append(seconds)
        else:
            self.erros.append(time.time())

    def taxa(self, janela_s: float = 3600) -> float:
        c = time.time() - janela_s
        r = [a for a in self.acoes if a[0] > c]
        if not r:
            return 0.0
        return sum(1 for a in r if a[2]) / len(r)

    def erros_1h(self) -> int:
        c = time.time() - 3600
        return sum(1 for t in self.erros if t > c)

    def latencia_media(self) -> float:
        return statistics.mean(self.latencias) if self.latencias else 0.0

    def diversidade(self) -> int:
        """Quantos tipos diferentes de ação ele fez — sistema preso em loop
        faz sempre a mesma coisa."""
        return len(set(k for _, k, _ in self.acoes))

    def minutos_parado(self) -> float:
        return (time.time() - self.ultima_acao) / 60 if self.ultima_acao else 999.0


# Posturas que o maestro pode adotar
POSTURES = {
    "normal":      dict(freq=1.0,  explica="segue o ritmo"),
    "cauteloso":   dict(freq=0.55, explica="apanhando — diminui o ritmo"),
    "recuperando": dict(freq=0.25, explica="muitos erros — quase parado"),
    "ativo":       dict(freq=1.45, explica="tudo indo bem — acelera um pouco"),
    "silencioso":  dict(freq=0.0,  explica="decidiu ficar quieto"),
}


class Conductor:
    def __init__(self, agency=None, adaptive=None, immune=None, social=None,
                 state_file: Path = STATE):
        self.A = agency
        self.AD = adaptive
        self.IM = immune
        self.S = social
        self.file = state_file
        self.v = Vitals()
        self.posture = "normal"
        self.posture_since = time.time()
        self.silent_until = 0.0
        self.cycles = 0
        self.decisions: List[dict] = []
        self.focus_shift: Dict[str, float] = {}
        self.last_tune = time.time()
        self.last_assessment = 0.0
        self.load()

    # ───────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps({
                "posture": self.posture,
                "posture_since": self.posture_since,
                "silent_until": self.silent_until,
                "cycles": self.cycles,
                "decisions": self.decisions[-40:],
                "healthy_minutes": getattr(self, "healthy_minutes", 0.0),
            }, ensure_ascii=False))
        except Exception:
            pass

    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
        except Exception:
            return
        self.posture = d.get("posture", "normal")
        self.posture_since = d.get("posture_since", time.time())
        self.silent_until = d.get("silent_until", 0.0)
        self.cycles = d.get("cycles", 0)
        self.decisions = d.get("decisions", [])

    # ─────────────────────────────────────────────────── observação
    def note(self, kind: str, ok: bool, seconds: float = 0.0) -> None:
        self.v.note(kind, ok, seconds)
        if self.AD is not None:
            self.AD.note("day_" + kind if f"day_{kind}" in self.AD.p else "day_posts", ok)
            if not ok:
                self.AD.note("retry_backoff", False)
            else:
                self.AD.note("retry_backoff", True)

    def note_error(self, text: str = "") -> None:
        self.v.erros.append(time.time())
        if self.AD and ("429" in str(text) or "rate" in str(text).lower()):
            log(self.AD.on_rate_limit())

    # ──────────────────────────────────────────────── autoavaliação
    def assess(self) -> Tuple[str, str]:
        """Olha os próprios sinais vitais e decide a postura.

        Isso é o que falta num sistema automático: ele não se olha no espelho.
        """
        taxa = self.v.taxa(3600)
        erros = self.v.erros_1h()
        parado = self.v.minutos_parado()
        div = self.v.diversidade()
        n = len(self.v.acoes)

        if erros >= 8:
            return "recuperando", f"{erros} erros na última hora"
        if erros >= 4 or taxa < 0.35 and n > 10:
            return "cauteloso", f"taxa {taxa:.2f}, {erros} erros"
        if n >= 25 and taxa > 0.90 and erros == 0:
            return "ativo", f"taxa {taxa:.2f}, {n} ações, sem erros"
        # ficou muito tempo sem fazer nada de útil?
        if parado > 180 and self.posture != "silencioso":
            return "silencioso", f"{parado:.0f} min sem ação útil — melhor descansar"
        if div <= 2 and n > 20:
            return "cauteloso", f"preso em {div} tipo de ação — sem variedade"
        return "normal", f"taxa {taxa:.2f}, {erros} erros, {div} tipos"

    def set_posture(self, p: str, why: str) -> None:
        if p == self.posture:
            return
        old = self.posture
        self.posture = p
        self.posture_since = time.time()
        self.decisions.append({"t": time.time(), "from": old, "to": p, "why": why})
        log(f"  postura: {old} → {p}  ({why})")
        if p == "silencioso":
            # silêncio de 40 min a 3 h — decisão, não falha
            self.silent_until = time.time() + random.uniform(2400, 10800)
        self.save()

    @property
    def freq(self) -> float:
        if time.time() < self.silent_until:
            return 0.0
        return POSTURES.get(self.posture, POSTURES["normal"])["freq"]

    def should_act(self) -> bool:
        """O maestro pode vetar uma ação — é ele que segura a rédea."""
        if time.time() < self.silent_until:
            return False
        return random.random() < self.freq

    # ──────────────────────────────────────────────────── ciclo
    def cycle(self, health_check: Optional[Callable[[], bool]] = None) -> List[str]:
        """Uma rodada de governo. Chamada periodicamente pelo motor."""
        self.cycles += 1
        fez = []

        # 1) autoavaliação
        if time.time() - self.last_assessment > 600:
            self.last_assessment = time.time()
            p, why = self.assess()
            self.set_posture(p, why)

        # 2) sintonia dos parâmetros
        if self.AD is not None and time.time() - self.last_tune > 1800:
            self.last_tune = time.time()
            mudou = self.AD.tune()
            for m in mudou:
                log(f"  parâmetro: {m}")
                fez.append(m)

        # 3) saúde: está doente? chama o sistema imune
        if self.IM is not None and self.v.erros_1h() >= 3:
            recent = self.recent_log(200)
            ep = self.IM.respond(recent, health_check)
            if ep is not None:
                fez.append(f"imune: {ep.remedy} → {'curou' if ep.ok else 'falhou'}")

        # 4) equilíbrio de comportamento — ele está só falando e nunca ouvindo?
        if self.v.tentativas > 30:
            self._rebalance()

        if self.cycles % 10 == 0:
            self.save()
        return fez

    def _rebalance(self) -> None:
        """Detecta desequilíbrio e muda o foco.

        13 posts e 3 interações é monólogo, não conversa. Um sistema automático
        nunca nota isso porque não tem opinião sobre o próprio comportamento.
        """
        kinds = Counter(k for _, k, _ in self.v.acoes)
        total = sum(kinds.values())
        if total < 20:
            return
        fala = kinds.get("post", 0)
        ouve = kinds.get("reply", 0) + kinds.get("like", 0) + kinds.get("follow", 0)
        if fala > 3 * ouve and ouve < 6:
            log(f"  desequilíbrio: {fala} posts vs {ouve} interações — mudando o foco")
            self.focus_shift["reply"] = time.time()
            if self.A is not None:
                # empurra a vontade de falar com alguém e segura a de postar
                self.A.inner.vontade_falar = min(1.0, self.A.inner.vontade_falar + 0.35)
                self.A.inner.vontade_postar = max(0.0, self.A.inner.vontade_postar - 0.30)
                self.A.inner.curiosidade = min(1.0, self.A.inner.curiosidade + 0.20)
            self.v.acoes.clear()          # reseta a amostra
            self.decisions.append({"t": time.time(), "from": "post", "to": "reply",
                                   "why": f"{fala} posts vs {ouve} interações"})

    @staticmethod
    def recent_log(n: int = 200) -> str:
        try:
            p = DATA / "agent.log"
            if not p.exists():
                return ""
            lines = p.read_text(errors="ignore").splitlines()
            return "\n".join(lines[-n:])
        except Exception:
            return ""

    # ──────────────────────────────────────────────────── relatório
    def report(self) -> str:
        v = self.v
        return (f"postura {self.posture} ({POSTURES.get(self.posture,{}).get('explica','')}) · "
                f"taxa {v.taxa(3600):.2f} · erros {v.erros_1h()} · "
                f"{len(v.acoes)} ações · {v.diversidade()} tipos · "
                f"latência {v.latencia_media():.1f}s")

    def describe(self) -> str:
        return f"{self.posture} · {self.v.taxa(3600):.2f} taxa · {self.v.erros_1h()} erros"

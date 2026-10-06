"""Agência — a diferença entre automático e autônomo.

Automático: pesos fixos, relógio, sempre faz algo a cada tick.
Autônomo:  tem estado interno que deriva, delibera antes de agir, pode decidir
           não fazer nada, forma interesses, lembra do que já disse, e age porque
           algo aconteceu — não porque o cron disparou.

Modelo mental: alguém que abre o app quando dá vontade, olha o feed, reage ao
que chamou atenção, às vezes posta, às vezes só olha e fecha.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "agency_state.json"


# ══════════════════════════════════════════════════════════════ ciclo de vida
@dataclass
class Inner:
    """Estado interno. Deriva a cada deliberação; nada aqui é sorteado do zero."""

    energia: float = 0.72          # cai com ação e com o dia, recupera dormindo
    humor: float = 0.10            # -1 ruim ... +1 bom; afetado pelo que lê
    vontade_postar: float = 0.35   # acumula devagar; posting satisfaz
    vontade_falar: float = 0.30    # vontade de responder alguém
    curiosidade: float = 0.65      # propensão a explorar / buscar
    paciencia: float = 1.00        # cai com erros, 429, fadiga
    fadiga_feed: float = 0.00      # sobe lendo muito; gera "vou sair do app"
    ocupado: float = 0.00          # vida real: aula, trabalho, saiu

    # para onde cada dimensão volta naturalmente — sem isso tudo satura em 1.0
    BASE = {
        "energia": 0.70, "humor": 0.08, "vontade_postar": 0.30,
        "vontade_falar": 0.25, "curiosidade": 0.60,
        "paciencia": 0.95, "fadiga_feed": 0.10, "ocupado": 0.0,
    }

    def drift(self, hours: float) -> None:
        """Deriva com o tempo E reverte à média.

        Só acumular faz todo valor saturar em 0 ou 1 em poucas horas — aí o
        estado interno deixa de ser estado e vira constante. A reversão à média
        mantém cada dimensão viva e dentro de uma faixa plausível.
        """
        h = max(0.0, min(hours, 12.0))          # protege contra saltos de clock
        pull = 1.0 - (1.0 - 0.12) ** h           # quanto mais tempo, mais volta à base

        self.energia = _clamp(self.energia - 0.020 * h + random.uniform(-0.02, 0.02))
        # vontades crescem de forma assintótica — linear satura em 1.0 em poucas
        # horas e aí a "vontade" vira constante, o que é exatamente o oposto de
        # estado interno
        self.vontade_postar = _clamp(self.vontade_postar + 0.075 * h * (1.0 - self.vontade_postar))
        self.vontade_falar = _clamp(self.vontade_falar + 0.065 * h * (1.0 - self.vontade_falar))
        self.fadiga_feed = _clamp(self.fadiga_feed - 0.10 * h)
        self.paciencia = _clamp(self.paciencia + 0.06 * h)
        self.ocupado = _clamp(self.ocupado - 0.30 * h)
        self.humor = _clamp(self.humor + random.uniform(-0.03, 0.03))
        self.curiosidade = _clamp(self.curiosidade + random.uniform(-0.04, 0.04))

        # Reversão à média em dois tempos:
        #   - por hora decorrida (devagar)
        #   - por tick (constante) — sem esse termo, ganhos repetidos de leitura
        #     empurram humor e energia para o teto e o estado vira constante.
        for k, base in self.BASE.items():
            cur = getattr(self, k)
            setattr(self, k, _clamp(cur + (base - cur) * (pull * 0.25 + 0.075)))

    def react(self, what: str, weight: float = 1.0) -> None:
        """O mundo afeta o estado interno — é isso que faz a diferença."""
        w = weight
        table = {
            # leu algo bom
            "leu_bom":        dict(humor=+0.025, curiosidade=+0.05, fadiga_feed=+0.03),
            "leu_ruim":       dict(humor=-0.08, paciencia=-0.04),
            "leu_spam":       dict(humor=-0.02, fadiga_feed=+0.06, paciencia=-0.03),
            "leu_engraçado":  dict(humor=+0.10, vontade_falar=+0.08),
            "leu_pergunta":   dict(vontade_falar=+0.22, curiosidade=+0.06),
            "leu_polémica":   dict(humor=-0.03, vontade_falar=+0.14, energia=-0.03),
            # próprias ações
            "postou":         dict(vontade_postar=-0.85, energia=-0.09, humor=+0.05,
                                   fadiga_feed=+0.05),
            "respondeu":      dict(vontade_falar=-0.75, energia=-0.04, humor=+0.06),
            "curtiu":         dict(energia=-0.008, fadiga_feed=+0.02),
            "seguiu":         dict(curiosidade=+0.04, energia=-0.01),
            "repostou":       dict(energia=-0.02, vontade_falar=+0.03),
            # atrito
            "rate_limit":     dict(paciencia=-0.30, humor=-0.06, energia=-0.05),
            "erro":           dict(paciencia=-0.18, humor=-0.05),
            "rejeitado":      dict(paciencia=-0.06, vontade_postar=+0.10),
            # vida real
            "scrollou":       dict(fadiga_feed=+0.09, energia=-0.02),
            "descansou":      dict(energia=+0.30, fadiga_feed=-0.55, humor=+0.04),
            "ocupado":        dict(ocupado=+0.80, energia=-0.05),
        }
        d = table.get(what)
        if not d:
            return
        for k, v in d.items():
            setattr(self, k, _clamp(getattr(self, k) + v * w))


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _clamp_hours(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ═══════════════════════════════════════════════════════════════════ ritmo
@dataclass
class Rhythm:
    """Circadiano de verdade — não é 'quiet hours', é disposição por hora.

    Um cara de 18: dorme tarde, some de manhã (escola/faculdade), volta de tarde,
    pico à noite. E tem dia que simplesmente tá ocupado.
    """
    tz_offset: int = -3

    # disposição por hora local (0 = fora do ar, 1 = muito ativo)
    CURVE = {
        0: 0.45, 1: 0.30, 2: 0.20, 3: 0.12, 4: 0.08, 5: 0.06,
        6: 0.10, 7: 0.25, 8: 0.15, 9: 0.10, 10: 0.12, 11: 0.20,
        12: 0.55, 13: 0.45, 14: 0.40, 15: 0.55, 16: 0.70, 17: 0.80,
        18: 0.85, 19: 0.95, 20: 1.00, 21: 1.00, 22: 0.90, 23: 0.70,
    }

    def local_hour(self) -> float:
        return ((time.time() + self.tz_offset * 3600) / 3600) % 24

    def disposition(self) -> float:
        h = self.local_hour()
        base = self.CURVE[int(h) % 24]
        nxt = self.CURVE[(int(h) + 1) % 24]
        frac = h - int(h)
        # interpola + ruído do dia (tem dia que tá ocupado, tem dia que não)
        return _clamp(base * (1 - frac) + nxt * frac + random.uniform(-0.08, 0.08))

    def is_school_hours(self) -> bool:
        h = self.local_hour()
        wd = time.gmtime((time.time() + self.tz_offset * 3600)).tm_wday
        return wd < 5 and 7.5 <= h <= 17.0

    def label(self) -> str:
        h = self.local_hour()
        if 5 <= h < 12:
            return "manhã"
        if 12 <= h < 18:
            return "tarde"
        if 18 <= h < 24:
            return "noite"
        return "madrugada"


# ═══════════════════════════════════════════════════════════════ interesses
@dataclass
class Interests:
    """Interesses que EVOLUEM a partir do que ele engaja — não lista fixa."""
    weights: Dict[str, float] = field(default_factory=dict)
    seen: Dict[str, int] = field(default_factory=dict)

    def boost(self, topic: str, amount: float = 0.15) -> None:
        t = _norm_topic(topic)
        if not t:
            return
        self.weights[t] = _clamp(self.weights.get(t, 0.30) + amount, 0.0, 0.85)
        self.seen[t] = self.seen.get(t, 0) + 1

    def decay(self, factor: float = 0.985) -> None:
        """Esquecimento — interesse não alimentado esfria."""
        for k in list(self.weights):
            self.weights[k] = min(self.weights[k] * factor, 0.90)
            if self.weights[k] < 0.02:
                del self.weights[k]

    def sample(self, k: int = 3) -> List[str]:
        if not self.weights:
            return []
        items = list(self.weights.items())
        total = sum(w for _, w in items) or 1.0
        out, acc = [], 0.0
        for _ in range(k):
            r = random.uniform(0, total)
            for t, w in items:
                acc += w
                if r <= acc:
                    out.append(t)
                    break
            acc = 0.0
        return list(dict.fromkeys(out))

    def top(self, k: int = 5) -> List[Tuple[str, float]]:
        return sorted(self.weights.items(), key=lambda x: -x[1])[:k]


def _norm_topic(t: str) -> str:
    t = (t or "").strip().lower()
    t = "".join(c for c in t if c.isalnum() or c in " -_")
    return " ".join(t.split()[:4])[:48]


# ═════════════════════════════════════════════════════════════════ memória
@dataclass
class Mind:
    """O que ele está pensando agora — o 'contexto' que a maioria dos bots não tem."""
    last_read: List[str] = field(default_factory=list)      # posts que viu
    opinions: Dict[str, str] = field(default_factory=dict)  # assunto -> opinião formada
    said: List[str] = field(default_factory=list)           # o que já postou (recente)
    pending: List[str] = field(default_factory=list)        # gatilhos não resolvidos
    last_action: Dict[str, float] = field(default_factory=dict)
    opened_at: float = 0.0
    session_len: float = 0.0
    in_session: bool = False

    def remember_read(self, text: str) -> None:
        self.last_read.append((text or "")[:220])
        self.last_read = self.last_read[-40:]

    def remember_said(self, text: str) -> None:
        self.said.append((text or "")[:220])
        self.said = self.said[-60:]

    def has_said_similar(self, text: str) -> bool:
        """Já disse algo parecido? Gente real não se repete."""
        t = set((text or "").lower().split())
        if len(t) < 4:
            return False
        for s in self.said[-30:]:
            o = set(s.lower().split())
            if not o:
                continue
            j = len(t & o) / len(t | o)
            if j > 0.45:
                return True
        return False

    def form_opinion(self, topic: str, opinion: str) -> None:
        self.opinions[_norm_topic(topic)] = opinion[:220]
        if len(self.opinions) > 60:
            oldest = list(self.opinions)[:10]
            for k in oldest:
                self.opinions.pop(k, None)

    def opinion_on(self, topic: str) -> Optional[str]:
        return self.opinions.get(_norm_topic(topic))


# ══════════════════════════════════════════════════════════════════ agência
@dataclass
class Agency:
    """O núcleo autônomo. Decide SE age, QUANDO age, e O QUE faz."""

    inner: Inner = field(default_factory=Inner)
    rhythm: Rhythm = field(default_factory=Rhythm)
    interests: Interests = field(default_factory=Interests)
    mind: Mind = field(default_factory=Mind)
    tz_offset: int = -3
    last_tick: float = field(default_factory=time.time)
    ticks: int = 0
    idle_until: float = 0.0
    _seed_topics: List[str] = field(default_factory=list)
    _wants_visit: str = ""          # nome de alguém que ele quer ir ver
    _wants_visit_at: float = 0.0
    _wants_measure: bool = False    # quer ver o alcance dos próprios posts
    _wants_seek: bool = False       # quer achar onde comentar rende mais

    # ───────────────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({
                "inner": asdict(self.inner),
                "interests": {"weights": self.interests.weights, "seen": self.interests.seen},
                "mind": {
                    "opinions": self.mind.opinions,
                    "said": self.mind.said[-40:],
                    "last_read": self.mind.last_read[-20:],
                    "last_action": self.mind.last_action,
                },
                "tz_offset": self.tz_offset,
                "ticks": self.ticks,
                "idle_until": self.idle_until,
            }, ensure_ascii=False, indent=1))
        except Exception:
            pass

    @classmethod
    def load(cls, tz_offset: int = -3) -> "Agency":
        a = cls(tz_offset=tz_offset)
        a.rhythm = Rhythm(tz_offset=tz_offset)
        try:
            d = json.loads(STATE.read_text())
        except Exception:
            return a
        try:
            a.inner = Inner(**{k: v for k, v in d.get("inner", {}).items()
                               if k in Inner.__dataclass_fields__})
        except Exception:
            pass
        i = d.get("interests", {})
        a.interests = Interests(weights=i.get("weights", {}), seen=i.get("seen", {}))
        m = d.get("mind", {})
        a.mind.opinions = m.get("opinions", {})
        a.mind.said = m.get("said", [])
        a.mind.last_read = m.get("last_read", [])
        a.mind.last_action = m.get("last_action", {})
        a.ticks = d.get("ticks", 0)
        # Um job novo não pode herdar um "fora do app até daqui 4h" — isso
        # congelaria o agente. Limita a espera restante a 1h no máximo.
        a.idle_until = min(d.get("idle_until", 0.0), time.time() + 3600)
        a.mind.in_session = False
        return a

    # ─────────────────────────────────────────────────────────────── tick
    def tick(self) -> None:
        now = time.time()
        hours = (now - self.last_tick) / 3600.0
        self.last_tick = now
        self.ticks += 1
        self.inner.drift(hours)
        self.interests.decay()

    # ──────────────────────────────────────────────────── SESSÃO (app aberto)
    def should_open_app(self) -> Tuple[bool, str]:
        """Abrir o app é uma decisão, não um agendamento."""
        now = time.time()
        if now < self.idle_until:
            return False, f"fora do app até {time.strftime('%H:%M', time.localtime(self.idle_until))}"
        if self.mind.in_session:
            return True, "já está no app"

        disp = self.rhythm.disposition()
        i = self.inner

        if self.rhythm.is_school_hours() and random.random() < 0.72:
            return False, "ocupado (aula/faculdade)"
        if i.ocupado > 0.55:
            return False, "ocupado com coisa da vida real"
        if i.energia < 0.18:
            return False, "sem energia"
        if i.fadiga_feed > 0.75:
            return False, "cansado de rolar o feed"

        # vontade composta: disposição × energia × paciência × curiosidade
        urge = (0.45 * disp + 0.22 * i.energia + 0.18 * i.curiosidade
                + 0.10 * i.vontade_postar + 0.10 * i.vontade_falar
                + 0.08 * i.paciencia - 0.30 * i.fadiga_feed)
        if random.random() < _clamp(urge, 0.02, 0.95):
            self.mind.in_session = True
            self.mind.opened_at = now
            # sessão de gente real: 3–25 min, enviesada pra curta
            self.mind.session_len = min(25.0, random.lognormvariate(math.log(6.5), 0.75)) * 60
            return True, f"abriu o app ({self.mind.session_len/60:.0f} min, {self.rhythm.label()})"
        return False, f"sem vontade (urge={urge:.2f})"

    def still_in_session(self) -> bool:
        if not self.mind.in_session:
            return False
        elapsed = time.time() - self.mind.opened_at
        if elapsed > self.mind.session_len:
            self.close_app("tempo de sessão")
            return False
        if self.inner.fadiga_feed > 0.85:
            self.close_app("cansou do feed")
            return False
        if self.inner.energia < 0.12:
            self.close_app("sem energia")
            return False
        return True

    def close_app(self, why: str) -> None:
        self.mind.in_session = False
        self.inner.react("descansou", 0.35)
        # fora por 20 min – 2.5 h, enviesado pra ~45 min
        gap = _clamp_hours(random.lognormvariate(math.log(2700), 0.55), 1200, 2.5 * 3600)
        self.idle_until = time.time() + gap
        log_line(f"fechou o app ({why}) — volta em {gap/60:.0f} min")

    # ───────────────────────────────────────────────────── DELIBERAÇÃO (o quê)
    def decide(self, have_notifs: bool = False,
               candidates: Optional[List[dict]] = None) -> Tuple[str, str]:
        """Escolhe a próxima ação com motivo. Pode devolver 'nada'.

        Isso é o coração: não é peso fixo, é 'o que eu quero fazer agora,
        dado o que acabei de ver e como eu tô'.
        """
        i = self.inner
        cands = candidates or []

        # 1) alguém falou comigo? isso tem prioridade — é social, não algoritmo
        if have_notifs and i.vontade_falar > 0.25 and random.random() < 0.75:
            return "notifications", "alguém interagiu, quer responder"

        # 2) vi algo que pede resposta?
        for c in cands[:6]:
            txt = (c.get("text") or "")
            if "?" in txt and i.vontade_falar > 0.40 and random.random() < 0.65:
                return "reply", "vi uma pergunta e quero responder"
            if i.vontade_falar > 0.62 and random.random() < 0.35:
                return "reply", "tô com vontade de falar com alguém"

        # 3) vontade de postar acumulada + tenho algo a dizer?
        if i.vontade_postar > 0.42 and i.energia > 0.28:
            p = _clamp((i.vontade_postar - 0.36) * 1.9 + 0.15 * i.humor)
            if random.random() < p:
                if random.random() < 0.15 and i.energia > 0.45:
                    return "thread", "tenho mais a dizer que cabe num post"
                return "post", "tô com vontade de postar"

        # 4) curiosidade → explorar
        if i.curiosidade > 0.55 and i.energia > 0.25 and random.random() < 0.45:
            return "harvest", "quero ver o que tá rolando"

        # 5) engajamento leve — o que gente mais faz
        if cands and i.energia > 0.20:
            r = random.random()
            if r < 0.50:
                return "like", "achei legal, vou curtir"
            if r < 0.62:
                return "repost", "vale repassar"

        # 6) medir o próprio alcance — crescer exige saber o que funcionou
        if self._wants_measure and i.curiosidade > 0.30 and random.random() < 0.5:
            return "measure", "quero ver o que meus posts renderam"

        # 7) procurar onde comentar rende visibilidade
        if self._wants_seek and i.vontade_falar > 0.35 and random.random() < 0.4:
            return "seek", "quero comentar onde mais gente vai ver"

        # 8) lembrar de alguém e ir ver o que ela postou — iniciativa, não reação
        if self._wants_visit and i.curiosidade > 0.40 and random.random() < 0.35:
            return "visit", f"faz tempo que não vejo o que {self._wants_visit} postou"

        # 7) seguir gente nova
        if i.curiosidade > 0.45 and random.random() < 0.28:
            return "follow", "quero seguir gente nova"

        # 7) decidiu não fazer nada — isso é autonomia, não é falha
        self.inner.react("scrollou")
        return "nada", "só olhando o feed"

    # ────────────────────────────────────────────────────────── consequências
    def react(self, what: str, weight: float = 1.0) -> None:
        self.inner.react(what, weight)

    def note_action(self, kind: str, text: str = "") -> None:
        self.mind.last_action[kind] = time.time()
        if text:
            self.mind.remember_said(text)
        if kind == "post":
            self.react("postou")
        elif kind in ("reply", "notifications"):
            self.react("respondeu")
        elif kind == "like":
            self.react("curtiu")
        elif kind == "repost":
            self.react("repostou")
        elif kind == "follow":
            self.react("seguiu")

    def rest(self, hours: float) -> None:
        """Recupera enquanto está fora do app. Sem isso a energia só cai e o
        agente definha em poucos dias."""
        i = self.inner
        h = max(0.0, min(hours, 12.0))
        i.energia = _clamp(i.energia + 0.16 * h)
        i.fadiga_feed = _clamp(i.fadiga_feed - 0.45 * h)
        i.paciencia = _clamp(i.paciencia + 0.12 * h)
        i.humor = _clamp(i.humor + 0.02 * h)

    READ_KINDS = ("bom", "ruim", "spam", "engraçado", "pergunta", "polémica")

    def note_read(self, text: str, kind: str = "neutro", topic: str = "") -> None:
        self.mind.remember_read(text)
        self.react(f"leu_{kind}" if kind in self.READ_KINDS else "scrollou", 0.5)
        if topic:
            self.interests.boost(topic, 0.06)

    def note_failure(self, kind: str = "erro") -> None:
        self.react("rate_limit" if kind == "rate_limit" else "erro")

    def note_rejected(self) -> None:
        self.react("rejeitado")

    # ────────────────────────────────────────────────────────── pós-ação
    def after_action(self, did: bool) -> float:
        """Quanto tempo até a próxima coisa. Dentro de sessão = curto e irregular."""
        i = self.inner
        if self.mind.in_session:
            # dentro do app: 40s a 6min, mais devagar se cansado
            base = random.uniform(40, 360)
            base *= (1.0 + i.fadiga_feed) * (1.2 - 0.4 * i.energia)
            if not did:
                base *= 0.55                      # scrollando: passa rápido
            return min(base, 900)
        return 0.0

    def wants_to_visit(self, people: List) -> None:
        """Lembrar de alguém: 'faz tempo que não vejo o que fulano postou'.

        A intenção fica guardada por até 6 h. Se ele não esbarrar com a pessoa
        no feed, acaba indo atrás dela — isso é iniciativa, não reação.
        """
        if not people:
            return
        p = random.choice(people)
        self._wants_visit = f"@{p.handle}" if p.handle else "alguém"
        self._wants_visit_at = time.time()

    def visit_expired(self) -> bool:
        return time.time() - self._wants_visit_at > 6 * 3600

    def growth_intentions(self, hours_since_measure: float) -> None:
        """Crescimento também é intenção: medir de tempos em tempos, e procurar
        oportunidade quando faz tempo que só posta no vazio."""
        self._wants_measure = hours_since_measure > 3.0
        self._wants_seek = (self.inner.vontade_falar > 0.35
                            and self.inner.curiosidade > 0.40)

    # ────────────────────────────────────────────────────────── assuntos
    def next_topic(self, seed: Optional[List[str]] = None) -> str:
        """Assunto vem dos interesses dele — que evoluíram — não de lista fixa."""
        if self.interests.weights and random.random() < 0.65:
            t = self.interests.sample(1)
            if t:
                return t[0]
        pool = list(self.interests.weights) or seed or self._seed_topics or ["o dia"]
        return random.choice(pool) if pool else "o dia"

    def seed_topics(self, topics: List[str]) -> None:
        self._seed_topics = topics
        for t in topics:
            self.interests.boost(t, 0.02)

    # ──────────────────────────────────────────────────────────── retrato
    def describe(self) -> str:
        i = self.inner
        top = ", ".join(t for t, _ in self.interests.top(3)) or "—"
        return (f"{self.rhythm.label()} · energia {i.energia:.2f} · humor {i.humor:+.2f} · "
                f"postar {i.vontade_postar:.2f} · falar {i.vontade_falar:.2f} · "
                f"fadiga {i.fadiga_feed:.2f} · interesses: {top}")

    def mood_hint(self) -> str:
        """Injetado no prompt — o modelo escreve diferente conforme o humor."""
        i = self.inner
        if i.energia < 0.30:
            return "Você está com preguiça agora. Frase curta. Sem empolgação."
        if i.humor > 0.35:
            return "Você está bem hoje. Deixa isso aparecer, sem forçar."
        if i.humor < -0.25:
            return "Você está meio irritado com algo pequeno. Pode reclamar de leve."
        if i.fadiga_feed > 0.6:
            return "Você tá meio cansado de rolar o feed. Tomo mais direto."
        if self.rhythm.label() == "madrugada":
            return "É madrugada e você não deveria estar acordado. Tomo meio zumbi."
        if self.rhythm.label() == "manhã":
            return "É de manhã. Você tá começando o dia."
        return ""


def log_line(m: str) -> None:
    try:
        with open(DATA / "agency.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} │ {m}\n")
    except Exception:
        pass

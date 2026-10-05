"""Grafo social — ele conhece pessoas, não só posts.

Sem isso, interagir é automático: pega o post mais ranqueado e reage. Com isso
é autônomo: ele lembra quem é quem, quem já falou com ele, quem responde de
volta, quem só posta link, quem combina com ele — e decide por causa disso.

Cada pessoa guardada:
  afinidade      o quanto ele gosta do que a pessoa posta (sobe com interação boa)
  reciprocidade  a pessoa responde quando ele fala? (0 = nunca, 1 = sempre)
  fadiga         cansou dessa pessoa? (sobe se interage demais seguido)
  confiança      já viu coisa suficiente pra julgar?
"""
from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "social_state.json"


@dataclass
class Person:
    did: str
    handle: str = ""
    afinidade: float = 0.30        # 0..1 — gostar do conteúdo
    reciprocidade: float = 0.15    # 0..1 — responde de volta?
    fadiga: float = 0.0            # 0..1 — cansou de interagir
    confianca: float = 0.0         # 0..1 — já viu o suficiente?
    visto: int = 0                 # posts vistos
    falei: int = 0                 # vezes que ele respondeu/interagiu
    me_respondeu: int = 0          # vezes que a pessoa respondeu ele
    mutuo: bool = False            # segue ele de volta?
    ultimo: float = 0.0            # última interação
    criado: float = field(default_factory=time.time)
    topics: Dict[str, float] = field(default_factory=dict)
    nota: str = ""                 # impressão formada, em texto

    # ───────────────────────────────────────────────────────── deriva
    def idle(self, hours: float) -> None:
        """O tempo passa: fadiga cai, confiança não muda, afinidade esfria
        muito devagar."""
        h = max(0.0, min(hours, 24.0))
        self.fadiga = _c(self.fadiga - 0.10 * h)
        self.afinidade = _c(self.afinidade + (0.32 - self.afinidade) * 0.02 * h)

    def see(self, text: str, topic: str = "") -> None:
        self.visto += 1
        self.confianca = _c(self.confianca + 0.13)
        if topic:
            self.topics[topic] = _c(self.topics.get(topic, 0.0) + 0.10)
        self.ultimo = max(self.ultimo, 0.0)

    def liked(self) -> None:
        self.falei += 1
        self.fadiga = _c(self.fadiga + 0.16)
        self.afinidade = _c(self.afinidade + 0.05)
        self.ultimo = time.time()

    def replied(self) -> None:
        self.falei += 1
        self.fadiga = _c(self.fadiga + 0.26)
        self.afinidade = _c(self.afinidade + 0.09)
        self.ultimo = time.time()

    def got_reply(self) -> None:
        """A pessoa respondeu ele — isso muda tudo."""
        self.me_respondeu += 1
        self.reciprocidade = _c(self.reciprocidade + 0.34)
        self.afinidade = _c(self.afinidade + 0.16)
        self.fadiga = _c(self.fadiga - 0.30)      # vale a pena insistir
        self.ultimo = time.time()

    def annoyed(self) -> None:
        self.afinidade = _c(self.afinidade - 0.22)
        self.fadiga = _c(self.fadiga + 0.35)

    def followed(self) -> None:
        self.falei += 1
        self.afinidade = _c(self.afinidade + 0.10)
        self.ultimo = time.time()

    # ──────────────────────────────────────────────────────── leitura
    @property
    def calor(self) -> float:
        """Quanto ele quer interagir com essa pessoa AGORA.

        Desconhecido tem que ser frio: sem isso todo mundo começa em 0.37 e o
        ranqueamento por relação não muda nada na prática.
        """
        horas = (time.time() - self.ultimo) / 3600 if self.ultimo else 99.0
        saudade = min(1.0, horas / 30.0)
        base = (0.40 * self.afinidade + 0.30 * self.reciprocidade
                + 0.18 * saudade + 0.12 * self.confianca)
        # quem ele mal conhece não tem peso de relação — multiplica por confiança
        return max(0.0, base * (0.25 + 0.75 * self.confianca) - 0.55 * self.fadiga)

    def hours_since(self) -> float:
        return (time.time() - self.ultimo) / 3600 if self.ultimo else 999.0

    def top_topic(self) -> str:
        if not self.topics:
            return ""
        return max(self.topics.items(), key=lambda x: x[1])[0]

    def describe(self) -> str:
        return (f"@{self.handle or self.did[:12]} afin {self.afinidade:.2f} "
                f"recip {self.reciprocidade:.2f} fad {self.fadiga:.2f} "
                f"visto {self.visto} falei {self.falei}")


def _c(x: float) -> float:
    return max(0.0, min(1.0, x))


def _norm(t: str) -> str:
    t = (t or "").strip().lower()
    t = "".join(c for c in t if c.isalnum() or c in " -_")
    return " ".join(t.split()[:4])[:48]


class Social:
    """O grafo. Persiste, deriva com o tempo, e é consultado antes de agir."""

    def __init__(self, state_file: Path = STATE):
        self.file = state_file
        self.people: Dict[str, Person] = {}
        self.last_decay = time.time()
        self.load()

    # ─────────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps({
                "people": {d: asdict(p) for d, p in self.people.items()},
                "last_decay": self.last_decay,
            }, ensure_ascii=False))
        except Exception:
            pass

    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
        except Exception:
            return
        for did, p in (d.get("people") or {}).items():
            try:
                self.people[did] = Person(**{
                    k: v for k, v in p.items() if k in Person.__dataclass_fields__})
            except Exception:
                continue
        self.last_decay = d.get("last_decay", time.time())

    # ───────────────────────────────────────────────────────── ciclo
    def tick(self) -> None:
        hours = (time.time() - self.last_decay) / 3600
        if hours < 0.25:
            return
        self.last_decay = time.time()
        for p in self.people.values():
            p.idle(hours)
        # esquece quem nunca deu em nada
        if len(self.people) > 400:
            mortos = sorted(self.people.values(),
                            key=lambda p: p.confianca + p.afinidade)[:80]
            for p in mortos:
                self.people.pop(p.did, None)

    def get(self, did: str, handle: str = "") -> Person:
        p = self.people.get(did)
        if p is None:
            p = Person(did=did, handle=handle)
            self.people[did] = p
        if handle and not p.handle:
            p.handle = handle
        return p

    # ──────────────────────────────────────────────── decisões sociais
    def should_engage(self, did: str, handle: str = "", kind: str = "like",
                      curiosity: float = 0.0) -> Tuple[bool, str]:
        """Vale interagir com essa pessoa? Isso é a diferença entre reagir e
        escolher.

        `curiosity` vem do estado interno: quando está alto, ele dá uma chance
        pra gente que ainda não conhece — sem isso o grafo nunca cresceria,
        porque desconhecido sempre perde.
        """
        p = self.get(did, handle)
        if p.fadiga > 0.80:
            return False, f"já interagi demais com @{p.handle} (fadiga {p.fadiga:.2f})"
        if p.afinidade < 0.12 and p.confianca > 0.45:
            return False, f"@{p.handle} não me ganhou ainda"
        c = p.calor
        limiar = {"like": 0.12, "reply": 0.26, "repost": 0.34, "follow": 0.30}.get(kind, 0.22)
        # curiosidade abre espaço pra desconhecido
        limiar -= curiosity * 0.22
        limiar = max(0.05, limiar)
        if c < limiar:
            return False, f"@{p.handle} sem clima agora (calor {c:.2f} < {limiar:.2f})"
        # não repete em cima da hora
        if p.hours_since() < 0.5 and p.falei > 2:
            return False, f"acabei de falar com @{p.handle}"
        return True, f"calor {c:.2f} com @{p.handle}"

    def rank(self, cands: List[dict], kind: str = "reply",
             curiosity: float = 0.0) -> List[dict]:
        """Ordena candidatos por RELAÇÃO, não por like count.

        Post com 300 likes de um estranho perde para post de alguém que já
        conversou com ele — é assim que gente escolhe o que responder.
        """
        out = []
        for c in cands:
            did = c.get("did") or ""
            p = self.get(did, c.get("handle", ""))
            ok, _ = self.should_engage(did, c.get("handle", ""), kind, curiosity)
            if not ok:
                continue
            # relação domina; popularidade só desempata
            score = p.calor * 140
            score += 18 * p.reciprocidade
            score += 10 * p.mutuo
            score += 6 * min(p.confianca, 1.0)
            score -= 30 * p.fadiga
            score += (c.get("score", 0) or 0) * 0.06
            score += random.uniform(0, 4)
            out.append((score, c))
        out.sort(key=lambda x: -x[0])
        return [c for _, c in out]

    def warmest(self, k: int = 5) -> List[Person]:
        return sorted(self.people.values(), key=lambda p: -p.calor)[:k]

    def who_to_visit(self, k: int = 3) -> List[Person]:
        """Tem alguém que ele gosta e faz tempo que não vê? Isso gera a ação de
        ir lá olhar o perfil — iniciativa, não reação."""
        ps = [p for p in self.people.values()
              if p.afinidade > 0.35 and p.hours_since() > 6 and p.fadiga < 0.6]
        ps.sort(key=lambda p: -(p.calor + min(1.0, p.hours_since() / 48)))
        return ps[:k]

    # ───────────────────────────────────────────────────── registro
    def note_like(self, did: str, handle: str = "", topic: str = "") -> None:
        p = self.get(did, handle)
        p.liked()
        if topic:
            p.topics[topic] = _c(p.topics.get(topic, 0.0) + 0.05)
        self._impressao(p)

    def note_reply(self, did: str, handle: str = "", topic: str = "") -> None:
        p = self.get(did, handle)
        p.replied()
        if topic:
            p.topics[topic] = _c(p.topics.get(topic, 0.0) + 0.08)
        self._impressao(p)

    def note_follow(self, did: str, handle: str = "") -> None:
        self.get(did, handle).followed()

    def note_got_reply(self, did: str, handle: str = "") -> None:
        self.get(did, handle).got_reply()

    def note_annoyed(self, did: str, handle: str = "") -> None:
        self.get(did, handle).annoyed()

    def note_seen(self, did: str, handle: str = "", text: str = "", topic: str = "") -> None:
        p = self.get(did, handle)
        p.see(text, topic)

    def note_mutual(self, did: str) -> None:
        p = self.people.get(did)
        if p:
            p.mutuo = True

    @staticmethod
    def _impressao(p: Person) -> None:
        """Forma uma impressão em texto quando já viu o suficiente."""
        if p.confianca < 0.35 or p.nota:
            return
        t = p.top_topic()
        if p.reciprocidade > 0.5:
            p.nota = f"responde quando eu falo, fala de {t}" if t else "responde quando eu falo"
        elif p.afinidade > 0.55:
            p.nota = f"posto bastante coisa boa sobre {t}" if t else "posto bastante coisa boa"
        elif p.afinidade < 0.20:
            p.nota = "não me ganhou ainda"

    # ───────────────────────────────────────────────────── retrato
    def describe(self) -> str:
        if not self.people:
            return "ainda não conhece ninguém"
        w = self.warmest(3)
        return f"{len(self.people)} pessoas · mais próximas: " + ", ".join(
            f"@{p.handle or p.did[:10]}" for p in w)

    def context_for(self, did: str) -> str:
        """Texto injetado no prompt: 'você já conhece essa pessoa'."""
        p = self.people.get(did)
        if not p:
            return ""
        if p.confianca < 0.25:
            return ""
        bits = []
        if p.me_respondeu:
            bits.append("essa pessoa já respondeu você antes")
        if p.mutuo:
            bits.append("vocês se seguem")
        if p.nota:
            bits.append(p.nota)
        if p.falei >= 3:
            bits.append("vocês já conversaram algumas vezes")
        return "; ".join(bits)

"""Parâmetros que se ajustam sozinhos.

Todo sistema "automático" tem constantes: postar no máximo 20/dia, esperar
40 min entre posts, tentar 4 vezes antes de desistir. Quem escolheu 20? Ninguém.
Está no código porque alguém chutou.

Aqui cada constante vira um parâmetro que observa o resultado das próprias
decisões e se ajusta. Se ele posta 20 e ninguém reage, o teto cai. Se toma 429,
o intervalo sobe. Se um provedor de LLM responde rápido e bem, sobe na fila.

Regra: nenhum parâmetro é ajustado por palpite — só por evidência acumulada.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "adaptive_state.json"


@dataclass
class Param:
    """Um número que aprende.

    `safer` diz para que lado fica a segurança:
      +1 → valor maior é mais conservador (intervalos, backoff)
      -1 → valor menor é mais conservador (tetos diários, tamanho de lote)

    Sem isso o ajuste vai para o lado errado: intervalo encurtaria justo quando
    ele está apanhando, que é o oposto do que deveria acontecer.
    """
    value: float
    lo: float
    hi: float
    step: float = 0.10        # fração do valor usada como passo
    safer: int = 1
    n: int = 0                # evidências acumuladas
    good: int = 0             # evidências favoráveis
    bad: int = 0              # evidências contrárias
    last_change: float = 0.0
    note: str = ""

    def observe(self, ok: bool) -> None:
        self.n += 1
        if ok:
            self.good += 1
        else:
            self.bad += 1

    def ready(self, min_n: int = 8) -> bool:
        return self.n >= min_n

    def ratio(self) -> float:
        return self.good / self.n if self.n else 0.5

    def adjust(self, target: float = 0.85) -> Optional[float]:
        """Ajusta para manter a taxa de sucesso perto do alvo.

        Sucesso alto demais  → está conservador, pode avançar.
        Sucesso baixo demais → está agressivo, recua.
        """
        if not self.ready():
            return None
        r = self.ratio()
        before = self.value
        if r >= 0.97 and self.n >= 12:
            # folga demais: ousa na direção oposta à segurança
            f = (1 - self.step) if self.safer > 0 else (1 + self.step)
            self.value = max(self.lo, min(self.hi, self.value * f))
        elif r < target:
            # apanhando: move na direção da segurança
            f = (1 + self.step) if self.safer > 0 else (1 - self.step)
            self.value = max(self.lo, min(self.hi, self.value * f))
        if abs(self.value - before) > 1e-9:
            self.last_change = time.time()
            self.n, self.good, self.bad = 0, 0, 0     # recomeça a contar
            self.note = f"taxa {r:.2f} → {before:.3g} virou {self.value:.3g}"
            return self.value
        return None

    def decay_evidence(self) -> None:
        """Evidência antiga vale menos — o mundo muda."""
        if self.n > 40:
            self.n = int(self.n * 0.7)
            self.good = int(self.good * 0.7)
            self.bad = int(self.bad * 0.7)


class Adaptive:
    """O conjunto de parâmetros vivos do sistema."""

    def __init__(self, state_file: Path = STATE):
        self.file = state_file
        self.p: Dict[str, Param] = {
            # tetos: menor = mais conservador
            "day_posts":     Param(20, 3, 40, 0.12, safer=-1),
            "day_replies":   Param(35, 5, 70, 0.12, safer=-1),
            "day_follows":   Param(60, 5, 120, 0.12, safer=-1),
            "day_likes":     Param(150, 20, 300, 0.12, safer=-1),
            # intervalos: maior = mais conservador
            "gap_posts":     Param(2400, 600, 7200, 0.10, safer=1),
            "gap_replies":   Param(480, 120, 1800, 0.10, safer=1),
            "gap_likes":     Param(45, 15, 300, 0.10, safer=1),
            "retry_backoff": Param(30, 5, 300, 0.15, safer=1),
            "commit_min":    Param(12, 4, 30, 0.10, safer=1),
            "feed_n":        Param(8, 4, 20, 0.10, safer=-1),
        }
        self.providers: Dict[str, Param] = {}   # nome -> desempenho
        self.history: List[dict] = []
        self.last_tune = time.time()
        self.load()

    # ───────────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps({
                "params": {k: asdict(v) for k, v in self.p.items()},
                "providers": {k: asdict(v) for k, v in self.providers.items()},
                "history": self.history[-40:],
                "last_tune": self.last_tune,
            }, ensure_ascii=False))
        except Exception:
            pass

    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
        except Exception:
            return
        for k, v in (d.get("params") or {}).items():
            if k in self.p:
                try:
                    self.p[k] = Param(**{f: v[f] for f in Param.__dataclass_fields__ if f in v})
                except Exception:
                    pass
        for k, v in (d.get("providers") or {}).items():
            try:
                self.providers[k] = Param(**{f: v[f] for f in Param.__dataclass_fields__ if f in v})
            except Exception:
                pass
        self.history = d.get("history", [])
        self.last_tune = d.get("last_tune", time.time())

    # ───────────────────────────────────────────────────────── API
    def get(self, name: str) -> float:
        return self.p[name].value

    def note(self, name: str, ok: bool) -> None:
        if name in self.p:
            self.p[name].observe(ok)

    def note_provider(self, name: str, ok: bool, seconds: float = 0.0) -> None:
        p = self.providers.setdefault(name, Param(0.6, 0.05, 1.0, 0.15))
        p.observe(ok)
        # rápido também conta como bom
        if ok and seconds > 0 and seconds < 12:
            p.observe(True)

    def provider_order(self, names: List[str]) -> List[str]:
        """Ordena provedores por desempenho observado — não por ordem fixa."""
        def score(n: str) -> float:
            p = self.providers.get(n)
            if p is None or p.n == 0:
                return 0.5
            return p.ratio()
        return sorted(names, key=lambda n: -score(n))

    # ───────────────────────────────────────────────────── sintonia
    def tune(self, force: bool = False) -> List[str]:
        """Roda a sintonia. Chamado periodicamente pelo maestro."""
        if not force and time.time() - self.last_tune < 1800:
            return []
        self.last_tune = time.time()
        mudou = []
        for name, p in self.p.items():
            p.decay_evidence()
            v = p.adjust()
            if v is not None:
                mudou.append(f"{name}={v:.4g} ({p.note})")
                self.history.append({"t": time.time(), "p": name, "v": v, "note": p.note})
        if mudou:
            self.save()
        return mudou

    # ───────────────────────────────────────────────────── regras
    def on_rate_limit(self) -> str:
        """Tomou 429: sobe os intervalos na hora, não espera estatística."""
        out = []
        for k in ("gap_posts", "gap_replies", "gap_likes"):
            p = self.p[k]
            old = p.value
            p.value = min(p.hi, p.value * 1.35)
            p.bad += 3
            out.append(f"{k} {old:.0f}→{p.value:.0f}")
        self.p["retry_backoff"].value = min(self.p["retry_backoff"].hi,
                                            self.p["retry_backoff"].value * 1.4)
        self.save()
        return "429: " + ", ".join(out)

    def on_success_streak(self, n: int) -> Optional[str]:
        """Várias ações boas seguidas: pode afrouxar um pouco."""
        if n < 10:
            return None
        for k in ("gap_posts", "gap_replies", "gap_likes"):
            p = self.p[k]
            old = p.value
            p.value = max(p.lo, p.value * 0.94)
            if abs(old - p.value) > 1:
                self.save()
                return f"{k} {old:.0f}→{p.value:.0f}"
        return None

    # ──────────────────────────────────────────────────── diagnóstico
    def describe(self) -> str:
        return " · ".join(f"{k} {v.value:.4g}" for k, v in list(self.p.items())[:6])

    def report(self) -> str:
        lines = []
        for k, v in self.p.items():
            r = f"{v.ratio():.2f}" if v.n else "—"
            lines.append(f"  {k:<14} {v.value:<9.4g} taxa {r}  (n={v.n})")
        if self.providers:
            lines.append("  provedores:")
            for k, v in sorted(self.providers.items(), key=lambda x: -x[1].ratio()):
                lines.append(f"    {k:<12} {v.ratio():.2f} (n={v.n})")
        return "\n".join(lines)

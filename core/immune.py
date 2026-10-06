"""Sistema imune — conserta a si mesmo por hipótese, não por tabela fixa.

Autofix automático: casa o erro com uma regex numa tabela e roda a função.
Sistema imune autônomo: lê o próprio log, forma uma hipótese do que quebrou,
escolhe uma intervenção, aplica, observa se melhorou, e lembra do que funcionou
— inclusive do que NÃO funcionou, pra não repetir.

Também decide quando NÃO intervir: se está quebrando de um jeto que intervenção
piora, ele recua e espera em vez de martelar.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "immune_state.json"
LOG = DATA / "agent.log"


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} IMMUNE │ {m}"
    print(line, flush=True)
    try:
        with open(DATA / "immune.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


@dataclass
class Remedy:
    """Uma intervenção possível, com histórico de eficácia."""
    name: str
    fn: Callable[[], bool]
    tried: int = 0
    worked: int = 0
    failed: int = 0
    last_tried: float = 0.0
    cooldown_s: float = 600.0

    def ready(self) -> bool:
        return time.time() - self.last_tried > self.cooldown_s

    def score(self) -> float:
        """Eficácia, com bônus de incerteza — remédio nunca testado merece chance."""
        n = self.worked + self.failed
        if n == 0:
            return 0.55                      # otimismo inicial
        return self.worked / n

    def note(self, ok: bool) -> None:
        self.tried += 1
        self.last_tried = time.time()
        if ok:
            self.worked += 1
        else:
            self.failed += 1
            # quanto mais falha, mais tempo espera antes de tentar de novo
            self.cooldown_s = min(7200, self.cooldown_s * 1.6)


@dataclass
class Episode:
    """Um episódio de doença: sintoma → hipótese → remédio → resultado."""
    t: float
    symptom: str
    hypothesis: str
    remedy: str
    ok: bool
    detail: str = ""


class Immune:
    def __init__(self, state_file: Path = STATE):
        self.file = state_file
        self.remedies: Dict[str, Remedy] = {}
        self.episodes: List[Episode] = []
        self.blacklist: Dict[str, float] = {}     # sintoma -> até quando evitar
        self.healthy_streak: int = 0
        self.interventions: int = 0
        self.load()
        self._register()

    # ───────────────────────────────────────────────── remédios padrão
    def _register(self) -> None:
        import core.autofix as AF
        def wrap(fn):
            def _f() -> bool:
                try:
                    return bool(fn())
                except Exception:
                    log(f"remédio falhou: {traceback.format_exc()[-200:]}")
                    return False
            return _f

        for name, fn in (
            ("reparar_estado", AF.fix_corrupt_state),
            ("nova_sessao", AF.fix_session),
            ("reparar_db", AF.fix_db),
            ("limpar_pyc", AF.fix_pyc),
            ("liberar_disco", AF.fix_disk),
            ("zerar_cooldown", AF.fix_cooldown),
            ("restaurar_snapshot", AF.restore_snapshot),
            ("atualizar_codigo", AF.git_self_update),
        ):
            if name not in self.remedies:
                self.remedies[name] = Remedy(name=name, fn=wrap(fn))
            else:
                self.remedies[name].fn = wrap(fn)      # função não persiste

    # ───────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps({
                "remedies": {k: {f: getattr(v, f) for f in
                                 ("tried", "worked", "failed", "last_tried", "cooldown_s")}
                             for k, v in self.remedies.items()},
                "episodes": [asdict(e) for e in self.episodes[-60:]],
                "blacklist": self.blacklist,
                "interventions": self.interventions,
            }, ensure_ascii=False))
        except Exception:
            pass

    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
        except Exception:
            return
        for k, v in (d.get("remedies") or {}).items():
            self.remedies[k] = Remedy(name=k, fn=lambda: False, **v)
        for e in (d.get("episodes") or []):
            try:
                self.episodes.append(Episode(**e))
            except Exception:
                pass
        self.blacklist = d.get("blacklist", {})
        self.interventions = d.get("interventions", 0)

    # ────────────────────────────────────────────────── diagnóstico
    SYMPTOMS: List[Tuple[str, str, str]] = [
        # (regex, nome do sintoma, hipótese do que está acontecendo)
        (r"ExpiredToken|InvalidToken|401", "token",
         "sessão expirou — precisa renovar, não reiniciar"),
        (r"429|rate.?limit", "rate_limit",
         "fui rápido demais — recuar, não insistir"),
        (r"database is locked|disk I/O", "db_travado",
         "banco corrompido ou travado"),
        (r"no such table|malformed", "db_corrompido",
         "schema quebrado — restaurar"),
        (r"No module named|ImportError", "dependencia",
         "falta pacote — instalar"),
        (r"No space left", "disco",
         "disco cheio — limpar"),
        (r"MemoryError|Cannot allocate", "memoria",
         "memória esgotada — reduzir carga"),
        (r"ConnectionError|Timeout|timed out", "rede",
         "rede instável — esperar, não martelar"),
        (r"Traceback", "excecao",
         "erro não classificado — reparo genérico"),
    ]

    def diagnose(self, text: str) -> Optional[Tuple[str, str]]:
        """Lê o log e diz o que acha que está acontecendo."""
        t = text or ""
        for pat, name, hyp in self.SYMPTOMS:
            if re.search(pat, t, re.I):
                return name, hyp
        return None

    def pick_remedy(self, symptom: str) -> Optional[Remedy]:
        """Escolhe intervenção por histórico do que funcionou pra esse sintoma."""
        # o que já curou esse sintoma antes?
        cures = [e.remedy for e in self.episodes
                 if e.symptom == symptom and e.ok]
        if cures:
            from collections import Counter
            best = Counter(cures).most_common(1)[0][0]
            r = self.remedies.get(best)
            if r and r.ready():
                return r
        # senão, o remédio mais eficaz que está fora de cooldown
        cands = [r for r in self.remedies.values() if r.ready()]
        if not cands:
            return None
        cands.sort(key=lambda r: -(r.score() + (0.05 if r.tried == 0 else 0)))
        return cands[0]

    # ──────────────────────────────────────────────────── intervenção
    def should_intervene(self, symptom: str) -> Tuple[bool, str]:
        """Nem toda doença merece remédio. Às vezes esperar é a decisão certa."""
        now = time.time()
        until = self.blacklist.get(symptom, 0)
        if now < until:
            return False, f"{symptom} em quarentena até {time.strftime('%H:%M', time.localtime(until))}"
        # já interveio muito sem resultado? para de martelar
        recent = [e for e in self.episodes if e.symptom == symptom and now - e.t < 3600]
        failures = sum(1 for e in recent if not e.ok)
        if failures >= 3:
            self.blacklist[symptom] = now + 3600 * 3
            self.save()
            return False, f"{symptom} falhou 3x em 1h — recuando 3h"
        return True, "ok"

    def respond(self, text: str, health_check: Optional[Callable[[], bool]] = None) -> Optional[Episode]:
        """Ciclo completo: diagnostica → decide → intervém → avalia."""
        d = self.diagnose(text)
        if d is None:
            self.healthy_streak += 1
            return None
        symptom, hyp = d

        ok_int, why = self.should_intervene(symptom)
        if not ok_int:
            log(f"  ○ não vou intervir em {symptom}: {why}")
            return None

        r = self.pick_remedy(symptom)
        if r is None:
            # Sem remédio disponível também é uma falha de tratamento — registrar,
            # senão o sintoma nunca acumula falhas suficientes para entrar em
            # quarentena e o sistema fica martelando para sempre.
            log(f"  ○ {symptom}: sem remédio disponível (todos em cooldown)")
            self.episodes.append(Episode(t=time.time(), symptom=symptom, hypothesis=hyp,
                                         remedy="nenhum", ok=False,
                                         detail="sem remédio em cooldown"))
            self.save()
            return None

        log(f"  ● {symptom} — hipótese: {hyp}")
        log(f"      tentando {r.name} (eficácia {r.score():.2f}, {r.tried} tentativas)")
        self.interventions += 1
        try:
            applied = bool(r.fn())
        except Exception as e:
            applied = False
            log(f"      remédio estourou: {str(e)[:100]}")

        # avalia: o remédio aplicou E o sistema melhorou.
        # Só "o sistema está bem" não conta como cura — senão um remédio que
        # nem rodou seria creditado só porque o paciente sarou sozinho.
        if health_check is not None and applied:
            time.sleep(2)
            try:
                cured = bool(health_check())
            except Exception:
                cured = applied
        else:
            cured = applied

        r.note(cured)
        ep = Episode(t=time.time(), symptom=symptom, hypothesis=hyp,
                     remedy=r.name, ok=cured,
                     detail=f"aplicado={applied}")
        self.episodes.append(ep)
        log(f"      {'✓ curou' if cured else '✗ não curou'} com {r.name}")
        self.save()
        return ep

    # ──────────────────────────────────────────────────── relatório
    def report(self) -> str:
        lines = [f"  intervenções: {self.interventions} · episódios: {len(self.episodes)}"]
        if self.episodes:
            from collections import Counter
            c = Counter(e.symptom for e in self.episodes)
            lines.append("  sintomas: " + ", ".join(f"{k}×{v}" for k, v in c.most_common(5)))
            curados = sum(1 for e in self.episodes if e.ok)
            lines.append(f"  cura: {curados}/{len(self.episodes)}")
        eficazes = sorted(self.remedies.values(), key=lambda r: -r.score())[:4]
        if eficazes:
            lines.append("  remédios por eficácia:")
            for r in eficazes:
                lines.append(f"    {r.name:<20} {r.score():.2f} ({r.tried} tentativas)")
        return "\n".join(lines)

    def describe(self) -> str:
        c = sum(1 for e in self.episodes if e.ok)
        return f"{self.interventions} intervenções · {c}/{len(self.episodes)} curas"

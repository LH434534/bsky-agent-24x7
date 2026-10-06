"""Motor autônomo — substitui o loop de pesos fixos.

A diferença prática:
  loop antigo : sorteia ação por peso → executa → dorme 60-300s → repete
  este        : estado interno deriva → decide SE abre o app → olha o feed →
                reage ao que VIU (não ao relógio) → pode escolher não fazer nada →
                fecha o app quando cansado → sai por horas

Gatilhos, não agendamento. Estado, não sorteio.
"""
from __future__ import annotations

import json
import random
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

from core.agency import Agency, log_line          # noqa: E402


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} AUTO │ {m}"
    print(line, flush=True)
    try:
        with open(DATA / "agent.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# aciona um "handler" por nome; handlers devolvem True se fizeram algo
Handlers = Dict[str, Callable[[], bool]]


class Autonomous:
    def __init__(self, agency: Agency, handlers: Handlers,
                 feed_provider: Optional[Callable[[int], List[dict]]] = None,
                 notif_check: Optional[Callable[[], bool]] = None,
                 on_flush: Optional[Callable[[], None]] = None,
                 heartbeat: Optional[Callable[[], None]] = None,
                 social: Optional[object] = None,
                 conductor: Optional[object] = None,
                 adaptive: Optional[object] = None,
                 immune: Optional[object] = None,
                 growth: Optional[object] = None):
        self.A = agency
        self.H = handlers
        self.feed = feed_provider or (lambda n: [])
        self.notifs = notif_check or (lambda: False)
        self.flush = on_flush or (lambda: None)
        self.hb = heartbeat or (lambda: None)
        self.S = social
        self.C = conductor
        self.AD = adaptive
        self.IM = immune
        self.G = growth
        self.running = True
        self.stats: Dict[str, int] = {}
        self._last_flush = time.time()

    def stop(self, *a) -> None:
        log("parando (sinal recebido)")
        self.running = False

    # ─────────────────────────────────────────────────────────────── olhar
    def look(self, n: int = 8) -> List[dict]:
        """Olha o feed. Isso alimenta memória, humor e interesses."""
        try:
            items = self.feed(n) or []
        except Exception:
            return []
        for it in items:
            txt = it.get("text") or ""
            kind = self.classify(txt)
            topic = it.get("topic") or ""
            self.A.note_read(txt, kind, topic)
            if kind in ("bom", "engraçado"):
                self.A.interests.boost(topic or " ".join(txt.split()[:3]), 0.035)
            elif kind == "spam":
                self.A.interests.boost(topic, -0.03) if topic else None
        return items

    @staticmethod
    def classify(text: str) -> str:
        t = (text or "").lower()
        if not t.strip():
            return "neutro"
        if any(s in t for s in ("airdrop", "giveaway", "dm me", "f4f", "telegram",
                                "casino", "promo code", "100x", "crypto")):
            return "spam"
        if "?" in t:
            return "pergunta"
        if any(w in t for w in ("kkk", "haha", "mds", "muito bom", "engraçado", "😂")):
            return "engraçado"
        if any(w in t for w in ("morreu", "tragédia", "luto", "grave", "crime", "guerra")):
            return "ruim"
        if len(t) > 60:
            return "bom"
        return "neutro"

    # ───────────────────────────────────────────────────────────── executar
    def act(self, kind: str, reason: str) -> bool:
        if kind == "nada":
            log(f"      · {reason}")
            return False
        if kind == "thread":
            kind = "post"          # thread é post com mais fôlego
        if kind == "measure":
            pass                   # só relatório, entra nas estatísticas igual
        fn = self.H.get(kind)
        if fn is None:
            return False
        log(f"      → {kind} ({reason})")
        t0 = time.time()
        try:
            did = bool(fn())
        except Exception as e:
            did = False
            self.A.note_failure()
            if self.C is not None:
                self.C.note_error(str(e))
            log(f"      ✗ {kind} crashed: {str(e)[:120]}")
            log(traceback.format_exc()[-400:])
            return False
        self.stats[kind] = self.stats.get(kind, 0) + (1 if did else 0)
        if self.C is not None:
            self.C.note(kind, did, time.time() - t0)
        if did:
            self.A.note_action(kind)
            log(f"      ✓ {kind} em {time.time()-t0:.1f}s")
        else:
            # não fez: ou anti-spam barrou, ou rate limit, ou não achou nada
            self.A.note_rejected()
        return did

    # ──────────────────────────────────────────────────────────────── ciclo
    def step(self) -> float:
        """Uma deliberação completa. Devolve quantos segundos esperar."""
        self.A.tick()

        open_, why = self.A.should_open_app()
        if not open_:
            log(f"  ○ fora do app: {why}")
            # fora do app: o tempo passa e ele recupera energia / perde fadiga
            wait = random.uniform(240, 900)
            self.A.rest(wait / 3600.0)
            return wait

        log(f"  ● {why}")
        self.A.mind.in_session = True

        # 1) olha o feed — é isso que dispara o resto
        items = self.look(random.randint(5, 12))

        # 2) vê se alguém falou com ele
        have_notifs = False
        try:
            have_notifs = bool(self.notifs())
        except Exception:
            pass

        # 3b) intenções de crescimento: medir alcance, procurar oportunidade
        if self.G is not None:
            try:
                self.A.growth_intentions(
                    (time.time() - self.G.last_measure) / 3600.0
                    if self.G.last_measure else 99.0)
            except Exception:
                pass

        # 3) grafo social deriva e pode gerar vontade de visitar alguém
        if self.S is not None:
            try:
                self.S.tick()
            except Exception:
                pass
            if not self.A._wants_visit or self.A.visit_expired():
                try:
                    self.A.wants_to_visit(self.S.who_to_visit(3))
                except Exception:
                    pass

        # 4) delibera
        kind, reason = self.A.decide(have_notifs=have_notifs, candidates=items)
        # o maestro segura a rédea: pode vetar por postura ou silêncio
        if self.C is not None and kind != "nada" and not self.C.should_act():
            log(f"      ⊘ maestro vetou {kind} ({self.C.posture})")
            self.A.react("scrollou", 0.4)
        else:
            self.act(kind, reason)

        # 4) às vezes faz uma segunda coisa na mesma sessão (gente faz isso)
        if random.random() < 0.45 and self.A.inner.energia > 0.25:
            kind2, reason2 = self.A.decide(have_notifs=False, candidates=items)
            if kind2 != "nada":
                if self.C is not None and not self.C.should_act():
                    log(f"      ⊘ maestro vetou {kind2}")
                else:
                    self.act(kind2, reason2)

        # 5) continua no app?
        if not self.A.still_in_session():
            self.flush()
            self.A.save()
            return random.uniform(60, 180)

        wait = self.A.after_action(did=True)
        return max(20.0, wait)

    def run(self, until_ts: Optional[float] = None, max_steps: int = 100000) -> None:
        log(f"motor autônomo iniciado — {self.A.describe()}")
        steps = 0
        while self.running and steps < max_steps:
            if until_ts and time.time() >= until_ts:
                log("deadline atingido — encerrando")
                break
            steps += 1
            try:
                wait = self.step()
            except Exception:
                log(f"step crashed:\n{traceback.format_exc()[-600:]}")
                wait = 120.0

            try:
                self.hb()
            except Exception:
                pass

            if self.C is not None:
                try:
                    for f in self.C.cycle():
                        log(f"  ⋯ {f}")
                except Exception:
                    pass

            # Salvar por TEMPO, não por contagem de passos: quando ele está fora
            # do app cada passo dorme até 30 min, então 8 passos podiam levar
            # 4 horas — o estado (e as estatísticas de imagem) ficavam sem
            # commitar esse tempo todo.
            if steps % 8 == 0 or time.time() - self._last_flush > 480:
                self._last_flush = time.time()
                try:
                    self.flush()
                except Exception:
                    pass
                try:
                    self.A.save()
                except Exception:
                    pass
                log(f"  [{self.A.describe()}]")
                if self.C is not None:
                    log(f"  [{self.C.report()}]")
                if self.G is not None and steps % 32 == 0:
                    try:
                        log(f"  [{self.G.describe()}]")
                    except Exception:
                        pass

            # dorme em pedaços para responder a sinais
            end = time.time() + min(wait, 1800)
            while self.running and time.time() < end:
                time.sleep(min(1.0, end - time.time()))

        self.flush()
        self.A.save()
        log(f"fim — {steps} deliberações · {json.dumps(self.stats, ensure_ascii=False)}")

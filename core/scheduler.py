"""24/7 autonomous loop — weighted action selection, jittered sleeps, crash-proof."""
from __future__ import annotations

import logging
import os
import random
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger("loop")


@dataclass
class Weights:
    post: float = 1.0
    reply: float = 3.0
    follow: float = 1.6
    like: float = 2.6
    repost: float = 0.9
    notifications: float = 2.0
    harvest: float = 1.2


@dataclass
class Loop:
    weights: Weights = field(default_factory=Weights)
    tick_min: float = 60.0          # base sleep between decisions (seconds)
    tick_max: float = 300.0
    burst_sleep: float = 0.0
    state_every: int = 10           # save guard state every N ticks
    max_tick_errors: int = 8

    _running: bool = True
    _ticks: int = 0
    _errors: int = 0

    def stop(self, *a) -> None:
        log.info("shutdown signal received")
        self._running = False

    def _pick(self) -> str:
        items = [(k, v) for k, v in self.weights.__dict__.items() if v > 0]
        total = sum(v for _, v in items)
        r = random.uniform(0, total)
        acc = 0.0
        for k, v in items:
            acc += v
            if r <= acc:
                return k
        return items[0][0]

    def run(self, handlers: Dict[str, Callable[[], bool]],
            on_tick: Optional[Callable[[int], None]] = None,
            on_stop: Optional[Callable[[], None]] = None) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log.info("loop started — actions: %s", ", ".join(sorted(handlers)))

        while self._running:
            self._ticks += 1
            action = self._pick()
            fn = handlers.get(action)
            if fn is None:
                time.sleep(self.tick_min)
                continue
            t0 = time.time()
            try:
                did = fn()
            except Exception:
                self._errors += 1
                did = False
                log.error("handler %s crashed:\n%s", action, traceback.format_exc())
                backoff = min(900, 30 * 2 ** min(self._errors, 5))
                self._sleep(backoff, backoff + 120)
            else:
                if self._errors:
                    self._errors = max(0, self._errors - 1)

            if on_tick:
                try:
                    on_tick(self._ticks)
                except Exception:
                    log.debug("on_tick failed", exc_info=True)

            if self._ticks % self.state_every == 0 and on_stop:
                try:
                    on_stop()          # periodic state flush
                except Exception:
                    pass

            spent = time.time() - t0
            # doing something real -> rest a bit longer; idling -> come back sooner
            base = self.tick_max if did else self.tick_min
            self._sleep(base * random.uniform(0.6, 1.3),
                        base * random.uniform(1.3, 2.2) if did else base * 1.6)

        if on_stop:
            on_stop()
        log.info("loop stopped after %d ticks", self._ticks)

    def _sleep(self, lo: float, hi: float) -> None:
        """Interruptible sleep so Ctrl-C / SIGTERM is instant."""
        end = time.time() + max(lo, min(hi, max(hi, lo)))
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def once(self, action: str, handlers: Dict[str, Callable[[], bool]]) -> bool:
        fn = handlers.get(action)
        if not fn:
            raise KeyError(f"no handler for {action}")
        return bool(fn())

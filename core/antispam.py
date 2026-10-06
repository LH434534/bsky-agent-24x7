"""Anti-spam / anti-ban layer.

Bluesky bans on behavioural signals, not keyword lists. Everything here exists to keep
the account's action curve indistinguishable from a real, slightly-online human:
per-action token buckets, hard daily caps, human jitter, near-duplicate detection,
quiet hours, 429 backoff, and a cooldown escalation ladder.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import logging
log = logging.getLogger("spam")

DATA = Path(__file__).resolve().parent.parent / "data"


# ────────────────────────────────────────────────────────────── config table
@dataclass
class Limits:
    # daily hard caps — never cross these in a 24h window
    day_posts: int = 20
    day_replies: int = 35
    day_follows: int = 60
    day_likes: int = 150
    day_reposts: int = 25
    day_images: int = 8          # imagem pesa: poucas por dia bastam
    # minimum seconds between actions (jitter added on top)
    gap_posts: int = 2400        # ~40 min
    gap_replies: int = 480       # ~8 min
    gap_follows: int = 300       # 5 min
    gap_likes: int = 45
    gap_reposts: int = 900
    gap_images: int = 1800       # ~30 min entre posts com imagem
    # bursts
    burst_posts: int = 3
    burst_likes: int = 12
    # behaviour shaping
    quiet_hours: Tuple[int, int] = (2, 7)      # local, no posting
    max_hashtags: int = 2
    max_links_per_day: int = 4
    max_mentions_per_day: int = 20
    same_target_hours: int = 48                # don't engage same DID twice inside this
    simhash_threshold: int = 3                 # hamming distance for near-dup
    night_post_penalty: float = 0.15           # probability multiplier during 23:00-05:00


@dataclass
class SpamGuard:
    limits: Limits = field(default_factory=Limits)
    tz_offset: int = 0                      # your UTC offset in hours (Brazil = -3)
    state_file: Path = DATA / "spam_state.json"
    adaptive: object | None = None          # parâmetros vivos (core.adaptive)

    # runtime
    _last: Dict[str, float] = field(default_factory=dict)
    _burst: Dict[str, deque] = field(default_factory=dict)
    _day: Dict[str, List[float]] = field(default_factory=dict)
    _sig: List[int] = field(default_factory=list)
    _touched: Dict[str, float] = field(default_factory=dict)
    _cooldown: float = 0.0
    _errors: int = 0

    # ─────────────────────────────────────────────────────── persistence
    def load(self) -> None:
        try:
            d = json.loads(self.state_file.read_text())
            self._last = {k: float(v) for k, v in d.get("last", {}).items()}
            self._day = {k: [float(x) for x in v] for k, v in d.get("day", {}).items()}
            self._sig = [int(x) for x in d.get("sig", [])]
            self._touched = {k: float(v) for k, v in d.get("touched", {}).items()}
            self._errors = int(d.get("errors", 0))
            self._cooldown = float(d.get("cooldown", 0))
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "last": self._last, "day": self._day, "sig": self._sig[-400:],
                "touched": self._touched, "errors": self._errors,
                "cooldown": self._cooldown, "saved": time.time()}))
        except Exception as e:
            log.warning("spam state save failed: %s", e)

    # ───────────────────────────────────────────────────────────── helpers
    def local_hour(self) -> int:
        return (datetime.now(timezone.utc) + timedelta(hours=self.tz_offset)).hour

    def _prune(self, key: str) -> None:
        cutoff = time.time() - 86400
        self._day[key] = [t for t in self._day.get(key, []) if t > cutoff]

    def used_today(self, action: str) -> int:
        self._prune(action)
        return len(self._day.get(action, []))

    def record(self, action: str) -> None:
        now = time.time()
        self._day.setdefault(action, []).append(now)
        self._last[action] = now
        self._burst.setdefault(action, deque(maxlen=64)).append(now)

    # ────────────────────────────────────────────────────────── rate checks
    def ok(self, action: str, *, now: Optional[float] = None) -> Tuple[bool, str]:
        """Return (allowed, reason)."""
        now = now or time.time()
        L = self.limits

        if self._cooldown > now:
            return False, f"global cooldown {int(self._cooldown - now)}s"

        # caps e intervalos podem vir dos parâmetros vivos, quando existem
        cap = getattr(L, f"day_{action}s", None)
        if self.adaptive is not None:
            try:
                cap = int(self.adaptive.get(f"day_{action}s"))
            except Exception:
                pass
        if cap is not None and self.used_today(action) >= cap:
            return False, f"daily cap {action}={cap} reached"

        gap = getattr(L, f"gap_{action}s", 0) if hasattr(L, f"gap_{action}s") \
            else getattr(L, f"gap_{action}", 0)
        if self.adaptive is not None:
            try:
                gap = float(self.adaptive.get(f"gap_{action}s"))
            except Exception:
                pass
        last = self._last.get(action, 0)
        jitter = random.uniform(0.75, 1.45)
        need = gap * jitter
        if now - last < need:
            return False, f"too soon ({int(need - (now - last))}s left)"

        # burst guard: max N in any 30-min window for posts
        if action == "post":
            win = [t for t in self._burst.get(action, []) if now - t < 1800]
            if len(win) >= L.burst_posts:
                return False, "burst guard: 3 posts / 30min"
        # quiet hours — no posting, other actions relaxed
        h = self.local_hour()
        if action == "post" and L.quiet_hours[0] <= h < L.quiet_hours[1]:
            return False, "quiet hours"
        if action == "post" and (h >= 23 or h < 6):
            if random.random() > L.night_post_penalty:
                return False, "night suppression"
        return True, "ok"

    def wait_time(self, action: str) -> float:
        """Seconds to sleep before `action` is allowed again."""
        ok, _ = self.ok(action)
        if ok:
            return 0.0
        L = self.limits
        gap = getattr(L, f"gap_{action}s", 0) if hasattr(L, f"gap_{action}s") \
            else getattr(L, f"gap_{action}", 0)
        last = self._last.get(action, time.time())
        base = max(0.0, gap * 1.1 - (time.time() - last))
        return base + random.uniform(10, 90)

    # ──────────────────────────────────────────────────── content screening
    BANNED = [
        r"\bfollow\s*(me|back)\b", r"\bf4f\b", r"\bf/f\b", r"\blike\s*(and|&)\s*(rt|repost|follow)\b",
        r"\bairdrop\b", r"\b(100x|1000x)\b", r"\b(usdt|btc|eth)\s*(giveaway|doubl)",
        r"\bdm me\b", r"\bclick (here|link)\b", r"\bfree (crypto|money|followers)\b",
        r"\b(buy|sell)\s*(followers|likes)\b", r"\bpromo\s*code\b", r"\bnsfw\s*leak",
        r"\bmake \$?\d+k?\s*(a|a\s)?(day|week|month)\b", r"\bpassive income\b",
        r"\btelegram\s*(group|channel)\b", r"\bcasino\b", r"\bbet\s*now\b",
    ]
    AI_TELLS = [
        r"\bas an ai\b", r"\bi'?m (just )?a (language model|bot|ai)\b",
        r"\bin today'?s (fast|ever|digital)\b", r"\bdelve into\b", r"\bit'?s important to note\b",
        r"\bhere'?s the thing\b", r"\b(crucial|pivotal|testament) to\b", r"\blanscape\b",
        r"\bunlock the (power|potential)\b", r"\bgame[- ]changer\b", r"\blets? dive in\b",
        r"\bfurthermore\b", r"\bmoreover\b", r"\bin conclusion\b",
    ]

    # abreviações que o personagem não usa — texto com isso passou pelo _scrub errado
    SLANG = [
        r"\bvc\b", r"\bvcs\b", r"\btbm\b", r"\bmsm\b", r"\bpq\b", r"\bvdd\b",
        r"\bblz\b", r"\bfds\b", r"\bobg\b", r"\bvlw\b", r"\bqdo\b", r"\btd\b",
        r"\bmt\b", r"\bmto\b", r"\bsmp\b", r"\bcmg\b", r"\bpdc\b", r"\bflw\b",
    ]

    @staticmethod
    def simhash(text: str) -> int:
        t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
        toks = [t[i:i + 3] for i in range(len(t) - 2)] or [t]
        v = [0] * 64
        for tk in toks:
            h = int(hashlib.md5(tk.encode()).hexdigest()[:16], 16)
            for i in range(64):
                v[i] += 1 if (h >> i) & 1 else -1
        out = 0
        for i in range(64):
            if v[i] > 0:
                out |= (1 << i)
        return out

    @staticmethod
    def hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    def near_dup(self, text: str) -> bool:
        h = self.simhash(text)
        return any(self.hamming(h, s) <= self.limits.simhash_threshold for s in self._sig)

    def remember_text(self, text: str) -> None:
        self._sig.append(self.simhash(text))
        self._sig = self._sig[-400:]

    def screen(self, text: str) -> Tuple[bool, str]:
        """Content-level spam/heuristic screen. Returns (pass, reason)."""
        t = text.strip()
        low = t.lower()
        if not (8 <= len(t) <= 300):
            return False, f"length {len(t)}"
        for p in self.BANNED:
            if re.search(p, low):
                return False, f"banned pattern: {p}"
        for p in self.AI_TELLS:
            if re.search(p, low):
                return False, f"ai tell: {p}"
        for p in self.SLANG:
            if re.search(p, low):
                return False, f"abreviação proibida: {p}"
        if low.count("#") > self.limits.max_hashtags:
            return False, "too many hashtags"
        if len(re.findall(r"https?://", low)) > 1:
            return False, "too many links"
        if len(re.findall(r"@[a-z0-9._-]+", low)) > 3:
            return False, "mention stuffing"
        letters = [c for c in t if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.5:
            return False, "shouting"
        if len(set(re.findall(r"\w+", low))) < 4:
            return False, "too repetitive"
        if self.near_dup(t):
            return False, "near-duplicate of recent content"
        if re.search(r"(.)\1{5,}", t):
            return False, "character spam"
        return True, "ok"

    # ─────────────────────────────────────────────────────── target control
    def target_ok(self, did: str) -> bool:
        last = self._touched.get(did, 0)
        return time.time() - last > self.limits.same_target_hours * 3600

    def touch(self, did: str) -> None:
        self._touched[did] = time.time()
        if len(self._touched) > 20000:      # bound memory
            cutoff = time.time() - 30 * 86400
            self._touched = {k: v for k, v in self._touched.items() if v > cutoff}

    # ───────────────────────────────────────────────────────── escalation
    def penalty(self, kind: str = "soft") -> None:
        """Escalate after rate-limit / error signals."""
        self._errors += 1
        ladder = {"soft": 300, "429": 1800, "hard": 3600, "ban-risk": 21600}
        wait = ladder.get(kind, 300) * (1 + 0.5 * min(self._errors, 6))
        self._cooldown = time.time() + wait
        log.warning("penalty %s -> cooldown %ds (errors=%d)", kind, int(wait), self._errors)

    def forgive(self) -> None:
        self._errors = max(0, self._errors - 1)

    # ───────────────────────────────────────────────────────────── status
    def stats(self) -> Dict[str, object]:
        h = self.local_hour()
        return {
            "local_hour": h,
            "today": {a: self.used_today(a) for a in ("post", "reply", "follow", "like", "repost")},
            "caps": {a: getattr(self.limits, f"day_{a}")
                     for a in ("posts", "replies", "follows", "likes", "reposts")},
            "cooldown_left": max(0, int(self._cooldown - time.time())),
            "errors": self._errors,
            "texts_seen": len(self._sig),
            "targets_seen": len(self._touched),
        }

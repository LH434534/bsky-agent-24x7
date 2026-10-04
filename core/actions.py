"""Behaviours — the actual things the agent does on Bluesky."""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Dict, List, Optional

from .atproto import Bsky, PostRef, did_from_uri
from .antispam import SpamGuard
from .brain import Brain
from .memory import Memory

log = logging.getLogger("actions")

DEFAULT_QUERIES = [
    "python", "automação", "dev", "open source", "linux", "self-host", "docker",
    "bot", "api", "startups", "build in public", "engenharia de software",
    "inteligência artificial", "produtividade", "postgres", "rust", "typescript",
    "carreira tech", "ciência de dados", "infra",
]

SELF_PROMO_HINTS = ["giveaway", "airdrop", "promo", "discount code", "casino", "bet now"]


class Actions:
    def __init__(self, bsky: Bsky, brain: Brain, guard: SpamGuard, mem: Memory,
                 queries: Optional[List[str]] = None, persona_topics: Optional[List[str]] = None):
        self.b = bsky
        self.brain = brain
        self.g = guard
        self.m = mem
        self.queries = queries or DEFAULT_QUERIES
        self.persona_topics = persona_topics or []

    # ─────────────────────────────────────────────────────────────── helpers
    def _topic(self) -> str:
        pool = self.persona_topics or self.brain.pick_topics(3)
        if not pool:
            return "automation"
        t = random.choice(pool)
        self.m.use_topic(t)
        return t

    def _act(self, kind: str) -> bool:
        ok, why = self.g.ok(kind)
        if not ok:
            log.debug("skip %s: %s", kind, why)
            return False
        return True

    def _safe(self, text: str) -> Optional[str]:
        for _ in range(3):
            ok, why = self.g.screen(text)
            if ok:
                return text
            log.info("screen rejected (%s): %.60s", why, text)
            text = self.brain.write_post(self._topic(), style=random.choice(
                ["observation", "hot take", "question", "tip"]))
        return None

    def _post_text(self, text: str) -> bool:
        """Push through screen + dedupe, then publish."""
        clean = self._safe(text)
        if not clean:
            self.m.log_action("post", ok=False, err="screen rejected all attempts")
            return False
        if self.g.near_dup(clean):
            self.m.log_action("post", ok=False, err="near-dup")
            return False
        try:
            ref = self.b.post(clean)
        except Exception as e:
            self.m.log_action("post", text=clean, ok=False, err=str(e))
            if "429" in str(e) or "rate" in str(e).lower():
                self.g.penalty("429")
            return False
        self.g.record("post")
        self.g.remember_text(clean)
        self.m.log_action("post", text=clean, uri=ref.uri)
        self.m.record_reach(ref.uri, clean)
        log.info("POSTED: %s", clean)
        return True

    # ─────────────────────────────────────────────────────────── behaviours
    def do_post(self) -> bool:
        if not self._act("post"):
            return False
        topic = self._topic()
        style = random.choice(["hot take", "observation", "tip", "question", "contrarian"])
        if random.random() < 0.18:
            # thread — 2 self-replies, spaced
            return self.do_thread(topic)
        text = self.brain.write_post(topic, style=style)
        return self._post_text(text)

    def do_thread(self, topic: str = "") -> bool:
        if not self._act("post"):
            return False
        topic = topic or self._topic()
        parts = [self.brain.write_post(topic, style="hook"),
                 self.brain.write_post(topic, style="detail"),
                 self.brain.write_post(topic, style="punchline")]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            return self._post_text(parts[0] if parts else "...")
        refs = []
        parent = None
        for p in parts[:3]:
            clean = self._safe(p)
            if not clean:
                continue
            try:
                r = self.b.post(clean, reply_to=parent)
            except Exception as e:
                log.warning("thread part failed: %s", e)
                break
            refs.append(r)
            self.g.remember_text(clean)
            self.m.log_action("post", text=clean, uri=r.uri, topic=topic)
            time.sleep(random.uniform(6, 18))     # human-ish typing gap
            parent = r
        if refs:
            self.g.record("post")
            self.m.use_topic(topic)
            log.info("THREAD (%d) on %s", len(refs), topic)
            return True
        return False

    def _candidates(self) -> List[Dict[str, Any]]:
        """Harvest reply candidates from search + timeline, dedupe and rank."""
        out: List[Dict[str, Any]] = []
        q = random.choice(self.queries)
        try:
            out += self.b.search_posts(q, limit=25, sort="latest")
        except Exception as e:
            log.debug("search failed: %s", e)
        if random.random() < 0.5:
            try:
                out += [f["post"] for f in self.b.timeline(limit=30)]
            except Exception as e:
                log.debug("timeline failed: %s", e)

        # feed the offline brain with live language
        texts = [(p.get("record", {}) or {}).get("text", "") for p in out]
        self.brain.offline.feed([t for t in texts if t])

        ranked = []
        for p in out:
            uri = p.get("uri")
            if not uri or self.m.seen(uri):
                continue
            rec = p.get("record", {}) or {}
            text = (rec.get("text") or "").strip()
            author = (p.get("author") or {})
            did = author.get("did", "")
            handle = author.get("handle", "")
            if not text or not did or did == self.b.did:
                continue
            if self.g.target_ok(did) is False:
                continue
            low = text.lower()
            if any(h in low for h in SELF_PROMO_HINTS):
                continue
            if len(text) < 30 or len(text) > 600:
                continue
            score = (p.get("likeCount", 0) or 0) + 2 * (p.get("replyCount", 0) or 0)
            score += 3 * (p.get("repostCount", 0) or 0)
            score -= 5 if "http" in low else 0
            score += random.uniform(0, 6)
            ranked.append((score, uri, text, did, handle, p))
        ranked.sort(key=lambda x: -x[0])
        return [{"score": s, "uri": u, "text": t, "did": d, "handle": h, "raw": p}
                for s, u, t, d, h, p in ranked[:12]]

    def do_reply(self) -> bool:
        if not self._act("reply"):
            return False
        for cand in self._candidates():
            if not self.brain.judge_reply(cand["text"]):
                self.m.mark_seen(cand["uri"])
                continue
            text = self.brain.write_reply(cand["text"], cand["handle"])
            clean = self._safe(text)
            if not clean:
                self.m.mark_seen(cand["uri"])
                continue
            if self.g.near_dup(clean):
                self.m.mark_seen(cand["uri"])
                continue
            try:
                ref = self.b.post(clean, reply_to=PostRef(cand["uri"], cand["raw"]["cid"]))
            except Exception as e:
                self.m.log_action("reply", text=clean, target_did=cand["did"], ok=False, err=str(e))
                if "429" in str(e):
                    self.g.penalty("429")
                self.m.mark_seen(cand["uri"])
                continue
            self.g.record("reply")
            self.g.remember_text(clean)
            self.g.touch(cand["did"])
            self.m.mark_seen(cand["uri"])
            self.m.log_action("reply", text=clean, target_did=cand["did"],
                              target_handle=cand["handle"], uri=ref.uri)
            log.info("REPLIED to @%s: %s", cand["handle"], clean)
            return True
        return False

    def do_follow(self) -> bool:
        if not self._act("follow"):
            return False
        cands = self._candidates()
        random.shuffle(cands)
        for c in cands[:6]:
            did, handle = c["did"], c["handle"]
            if self.m.is_following(did) or not self.g.target_ok(did):
                continue
            try:
                prof = self.b.profile(handle) or {}
            except Exception:
                prof = {}
            # heuristic: skip empty/dead/spammy accounts
            if (prof.get("followersCount", 0) or 0) < 3:
                continue
            desc = (prof.get("description") or "").lower()
            if any(h in desc for h in SELF_PROMO_HINTS) or "follow back" in desc:
                continue
            try:
                feed = self.b.author_feed(handle, limit=5)
                texts = [(f.get("post", {}).get("record", {}) or {}).get("text", "")
                         for f in feed]
            except Exception:
                texts = []
            if not texts:
                continue
            if not self.brain.judge_follow(handle, prof.get("description", ""), texts):
                self.g.touch(did)
                continue
            try:
                uri = self.b.follow(did)
            except Exception as e:
                self.m.log_action("follow", target_did=did, ok=False, err=str(e))
                if "429" in str(e):
                    self.g.penalty("429")
                continue
            self.g.record("follow")
            self.g.touch(did)
            self.m.mark_follow(did, handle)
            self.m.log_action("follow", target_did=did, target_handle=handle, uri=uri)
            log.info("FOLLOWED @%s", handle)
            return True
        return False

    def do_like(self) -> bool:
        if not self._act("like"):
            return False
        cands = [c for c in self._candidates() if c["score"] > 0]
        random.shuffle(cands)
        n = random.randint(1, 4)
        done = 0
        for c in cands[:n]:
            try:
                self.b.like(c["uri"], c["raw"]["cid"])
            except Exception as e:
                log.debug("like failed: %s", e)
                continue
            self.g.record("like")
            self.g.touch(c["did"])
            self.m.mark_seen(c["uri"])
            self.m.log_action("like", target_did=c["did"], target_handle=c["handle"], uri=c["uri"])
            done += 1
            time.sleep(random.uniform(3, 12))
            if done >= self.g.limits.burst_likes:
                break
        if done:
            log.info("LIKED %d", done)
        return done > 0

    def do_repost(self) -> bool:
        if not self._act("repost"):
            return False
        cands = [c for c in self._candidates() if c["score"] > 4]
        random.shuffle(cands)
        for c in cands[:3]:
            try:
                self.b.repost(c["uri"], c["raw"]["cid"])
            except Exception as e:
                log.debug("repost failed: %s", e)
                continue
            self.g.record("repost")
            self.g.touch(c["did"])
            self.m.mark_seen(c["uri"])
            self.m.log_action("repost", target_did=c["did"], target_handle=c["handle"], uri=c["uri"])
            log.info("REPOSTED @%s", c["handle"])
            return True
        return False

    def do_engage_notifications(self) -> bool:
        """Answer people who replied to us — the highest-value organic signal."""
        if not self._act("reply"):
            return False
        try:
            notes = self.b.notifications(limit=30)
        except Exception as e:
            log.debug("notifications failed: %s", e)
            return False
        pend = [n for n in notes
                if n.get("reason") in ("reply", "mention") and not n.get("isRead")]
        for n in pend:
            author = (n.get("author") or {})
            did, handle = author.get("did", ""), author.get("handle", "")
            uri = n.get("uri")
            if not did or did == self.b.did or not self.g.target_ok(did):
                continue
            text = (n.get("record", {}) or {}).get("text", "")
            if not text:
                continue
            reply = self._safe(self.brain.write_reply(text, handle))
            if not reply:
                continue
            try:
                r = self.b.post(reply, reply_to=PostRef(uri, n["cid"]))
            except Exception:
                continue
            self.g.record("reply")
            self.g.remember_text(reply)
            self.g.touch(did)
            self.m.log_action("reply", text=reply, target_did=did, target_handle=handle, uri=r.uri)
            log.info("ANSWERED @%s: %s", handle, reply)
            return True
        return False

    def do_harvest(self) -> None:
        """Silent pass: refresh the offline corpus from live language. No writes."""
        c = self._candidates()
        self.brain.offline.feed([x["text"] for x in c])
        log.info("harvested %d posts into corpus", len(c))

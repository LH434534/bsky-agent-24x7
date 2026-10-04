"""Brain — pluggable LLM layer + fully offline fallback generator.

Provider chain (first reachable wins):
  1. Ollama      — local, free, unlimited.  https://github.com/ollama/ollama
  2. Groq        — free tier, ~500 tok/s.   https://github.com/groq/groq-api-cookbook
  3. OpenRouter  — free model roster.       https://github.com/OpenRouterTeam/ai-sdk-provider
  4. HuggingFace — serverless inference.
  5. Offline     — corpus-trained Markov + intent templates. Zero deps, always works.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

log = logging.getLogger("brain")

DATA = Path(os.environ.get("BSKY_AGENT_DATA", Path(__file__).resolve().parent.parent / "data"))
CORPUS = DATA / "corpus.txt"


# ═══════════════════════════════════════════════════════════════════ providers
class Provider:
    name = "provider"

    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str, *, max_tokens: int = 220,
                 temperature: float = 0.9) -> Optional[str]:
        return None


class Ollama(Provider):
    """Local models. Fast, private, unlimited, zero cost."""

    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434",
                 model: str = "qwen2.5:7b-instruct"):
        self.host = host.rstrip("/")
        self.model = os.environ.get("OLLAMA_MODEL", model)

    def available(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            if r.status_code != 200:
                return False
            have = {m["name"] for m in r.json().get("models", [])}
            for h in have:
                if h.split(":")[0] == self.model.split(":")[0]:
                    self.model = h
                    return True
            return False
        except Exception:
            return False

    def complete(self, system, user, *, max_tokens=220, temperature=0.9) -> Optional[str]:
        r = requests.post(f"{self.host}/api/chat", timeout=180, json={
            "model": self.model, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens,
                        "top_p": 0.95, "repeat_penalty": 1.15},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["message"]["content"].strip()


class OpenAICompat(Provider):
    """Groq / OpenRouter / Together / any OpenAI-shaped endpoint."""

    def __init__(self, name: str, base: str, model: str, env_key: str,
                 extra_headers: Dict[str, str] | None = None):
        self.name = name
        self.base = base.rstrip("/")
        self.model = os.environ.get(f"{name.upper()}_MODEL", model)
        self.env_key = env_key
        self.extra = extra_headers or {}

    def available(self) -> bool:
        return bool(os.environ.get(self.env_key))

    def complete(self, system, user, *, max_tokens=220, temperature=0.9) -> Optional[str]:
        key = os.environ[self.env_key]
        r = requests.post(f"{self.base}/chat/completions", timeout=120,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json", **self.extra},
                          json={"model": self.model, "temperature": temperature,
                                "max_tokens": max_tokens, "top_p": 0.95,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# ══════════════════════════════════════════════════════════ offline generator
class Offline(Provider):
    """Corpus-conditioned Markov generator + intent templates.

    Trains on whatever text you feed it (live Bluesky harvest, your own
    archive, any .txt). Always available, no network, no weights, fast.
    """

    name = "offline"

    OPENERS = [
        "hot take:", "nobody talks about this enough:", "honestly?", "real talk —",
        "unpopular opinion:", "quick note:", "the thing is,", "reminder:",
        "been thinking about this:", "small observation:",
    ]
    FRAMES = [
        "most people get {t} wrong. it's not about the tool, it's about the feedback loop you build around it.",
        "{t} looks simple until you ship it. then the edge cases arrive in groups of three.",
        "if your {t} setup needs a doc to explain, it's already too clever.",
        "the fastest way to improve at {t}: do it daily, in public, badly, for 30 days.",
        "everyone optimizes {t}. nobody measures whether it mattered.",
        "{t} is 10% knowing and 90% having done it enough times to stop panicking.",
        "you don't need a better {t} stack. you need to finish the one you started.",
        "the boring version of {t} wins more often than people admit.",
        "spent long enough around {t} to know: the hard part is never the part you prepared for.",
        "people ask what to learn next. usually the answer is: go deeper on {t}.",
    ]
    QUESTIONS = [
        "what's the one {t} habit that actually stuck for you?",
        "genuinely curious — how do you approach {t} when you only have 20 minutes?",
        "what did you believe about {t} a year ago that you no longer believe?",
        "what's the most overrated advice in {t}?",
        "what finally made {t} click for you?",
    ]
    REPLIES = [
        "this is the part people skip. the unglamorous middle is where it's decided.",
        "strong agree — and the corollary is that you have to ship before you feel ready.",
        "underrated take. most of the leverage is in the setup, not the execution.",
        "the 'boring and consistent' path is undefeated and nobody wants to hear it.",
        "yeah. and once you see it you can't unsee it in every project after.",
        "saving this. the framing is cleaner than the 40-min video version.",
        "the interesting bit is what this implies for small teams — they can move before it's obvious.",
    ]

    def __init__(self):
        self.chain: Dict[tuple, Dict[str, int]] = {}
        self.starts: List[tuple] = []
        self.topics: List[str] = []
        self._dirty = True

    # ---- corpus
    def feed(self, texts: Iterable[str]) -> None:
        n = 0
        with open(CORPUS, "a", encoding="utf-8") as f:
            for t in texts:
                t = (t or "").strip().replace("\n", " ")
                if len(t) < 25:
                    continue
                f.write(t + "\n")
                n += 1
        if n:
            self._dirty = True
            log.info("corpus +%d lines", n)

    def _train(self) -> None:
        seed = DATA / "seed_corpus.txt"
        if seed.exists() and (not CORPUS.exists() or CORPUS.stat().st_size < 4000):
            with open(CORPUS, "a", encoding="utf-8") as f:
                f.write("\n" + seed.read_text(encoding="utf-8"))
        if not CORPUS.exists():
            return
        raw = CORPUS.read_text(encoding="utf-8", errors="ignore")
        words = re.findall(r"[A-Za-zÀ-ɏ0-9''\-]+|[.!?,;:]", raw.lower())
        self.chain.clear()
        self.starts.clear()
        for i in range(len(words) - 2):
            k = (words[i], words[i + 1])
            self.chain.setdefault(k, {})
            nxt = words[i + 2]
            self.chain[k][nxt] = self.chain[k].get(nxt, 0) + 1
            if words[i] in ".!?":
                self.starts.append((words[i + 1], words[i + 2]) if i + 2 < len(words) else k)
        nouns = [w for w in re.findall(r"\b[a-z]{4,12}\b", raw.lower())]
        freq: Dict[str, int] = {}
        for w in nouns:
            freq[w] = freq.get(w, 0) + 1
        self.topics = [w for w, c in sorted(freq.items(), key=lambda x: -x[1])[:60]]
        self._dirty = False
        log.info("offline brain trained: %d bigrams, %d topics", len(self.chain), len(self.topics))

    def _gen(self, seed: str | None = None, max_words: int = 28) -> str:
        if not self.chain:
            return ""
        if seed:
            sw = re.findall(r"[a-z0-9']+", seed.lower())
            start = None
            for i in range(len(sw) - 1):
                if (sw[i], sw[i + 1]) in self.chain:
                    start = (sw[i], sw[i + 1])
                    break
        else:
            start = random.choice(self.starts) if self.starts else random.choice(list(self.chain))
        if not start:
            return ""
        out = list(start)
        for _ in range(max_words):
            k = (out[-2], out[-1])
            nxt = self.chain.get(k)
            if not nxt:
                break
            pool = [w for w, c in nxt.items() for _ in range(min(c, 6))]
            out.append(random.choice(pool))
            if out[-1] in ".!?" and len(out) > 10:
                break
        s = " ".join(w for w in out if w not in {",", ";", ":"})
        s = re.sub(r"\s+([,.!?])", r"\1", s).strip()
        return s[:1].upper() + s[1:] if s else ""

    def topic(self) -> str:
        if not self.topics:
            return random.choice(["automation", "building in public", "systems",
                                  "dev life", "shipping", "focus", "engineering"])
        return random.choice(self.topics)

    # ---- provider api
    def available(self) -> bool:
        return True

    def complete(self, system, user, *, max_tokens=220, temperature=0.9) -> Optional[str]:
        if self._dirty:
            self._train()
        u = user.lower()
        if "yes or no" in u or "exactly yes" in u:
            return "YES" if random.random() < 0.5 else "NO"
        if "reply" in u or "comment" in u or "respond to" in u:
            return random.choice(self.REPLIES)
        if "question" in u:
            return random.choice(self.QUESTIONS).replace("{t}", self.topic())
        t = self.topic()
        line = self._gen(seed=t, max_words=random.randint(16, 30))
        if not self._good(line):
            line = random.choice(self.FRAMES).replace("{t}", t)
        if random.random() < 0.45:
            line = f"{random.choice(self.OPENERS)} {line[0].lower() + line[1:]}"
        return line[:300]

    # ---- decisions (used when no LLM provider is reachable)
    SPAMMY = ["airdrop", "giveaway", "dm me", "follow back", "f4f", "100x",
              "telegram", "casino", "promo code", "free followers", "passive income"]

    def judge_reply(self, post_text: str) -> bool:
        t = (post_text or "").lower()
        if len(t) < 30 or any(s in t for s in self.SPAMMY):
            return False
        score = 0.30
        if "?" in t:
            score += 0.30                                  # questions are worth answering
        if any(w in t for w in ("how", "why", "should i", "what do you", "anyone else")):
            score += 0.12
        if 40 <= len(t) <= 320:
            score += 0.12                                  # substantive, readable
        if t.count("http") > 1:
            score -= 0.25
        if t.count("#") > 2:
            score -= 0.25
        return random.random() < min(score, 0.9)

    def judge_follow(self, handle: str, bio: str, posts: List[str]) -> bool:
        blob = " ".join([bio or ""] + list(posts)).lower()
        if any(s in blob for s in self.SPAMMY) or "follow back" in blob:
            return False
        if not posts:
            return False
        score = 0.45
        if len(posts) >= 3:
            score += 0.10
        if len(bio or "") > 20:
            score += 0.10
        if "bot" in blob and "automation" not in blob:
            score -= 0.25
        return random.random() < min(score, 0.85)

    @staticmethod
    def _good(line: str) -> bool:
        """Reject one-word / repetitive Markov sludge before it reaches the filter."""
        if not line or len(line) < 28:
            return False
        w = re.findall(r"[A-Za-zÀ-ɏ0-9']+", line)
        return len(w) >= 6 and len(set(x.lower() for x in w)) >= 5


# ═════════════════════════════════════════════════════════════════════ engine
PERSONA = """You are the voice of a single Bluesky account. You write like a real person:
opinionated, concrete, a little dry. Portuguese (pt-BR) unless the conversation is in English.

Hard rules:
- Max 280 characters. One idea. No emoji spam (one is fine, never more).
- No hashtags unless asked. No "here's the thing," no "in today's world," no AI tell-tale phrases.
- No engagement bait ("like and follow", "RT if", "comment below").
- Never claim to be an AI or a bot.
- Sound human: lowercase is fine, contractions are fine, mild swearing is fine.
- Output ONLY the text of the post/reply. No quotes, no explanation, no prefix."""


class Brain:
    def __init__(self, providers: Optional[List[Provider]] = None, persona: str = PERSONA):
        self.offline = Offline()
        self.providers: List[Provider] = providers or [
            Ollama(),
            OpenAICompat("groq", "https://api.groq.com/openai/v1",
                         "llama-3.3-70b-versatile", "GROQ_API_KEY"),
            OpenAICompat("openrouter", "https://openrouter.ai/api/v1",
                         "meta-llama/llama-3.3-70b-instruct", "OPENROUTER_API_KEY",
                         {"HTTP-Referer": "https://bsky.app", "X-Title": "bsky-agent"}),
            OpenAICompat("hf", "https://router.huggingface.co/v1",
                         "Qwen/Qwen2.5-72B-Instruct", "HF_TOKEN"),
        ]
        self.persona = persona
        self.active: Optional[Provider] = None
        self.fail_until: Dict[str, float] = {}

    def pick(self) -> Provider:
        now = time.time()
        for p in self.providers:
            if self.fail_until.get(p.name, 0) > now:
                continue
            try:
                if p.available():
                    if self.active is not p:
                        log.info("brain provider -> %s", p.name)
                    self.active = p
                    return p
            except Exception as e:
                log.debug("provider %s probe failed: %s", p.name, e)
        self.active = self.offline
        return self.offline

    def gen(self, instruction: str, *, context: str = "", max_tokens: int = 220,
            temperature: float = 0.9) -> str:
        """Generate text; falls through providers, ends at the offline engine."""
        user = instruction
        if context:
            user = f"Context:\n{context}\n\nTask:\n{instruction}"
        order = [self.pick(), self.offline]
        for p in order:
            if p is self.offline and self.active is not self.offline:
                pass
            try:
                out = p.complete(self.persona, user, max_tokens=max_tokens, temperature=temperature)
                if out:
                    return self._scrub(out)
            except Exception as e:
                log.warning("provider %s failed: %s", p.name, str(e)[:120])
                self.fail_until[p.name] = time.time() + 120
                continue
        return self._scrub(self.offline.complete(self.persona, user) or "...")

    @staticmethod
    def _scrub(text: str) -> str:
        t = text.strip().strip('"').strip("“”").strip()
        t = re.sub(r"^(post|reply|tweet|output|resposta)\s*[:：]\s*", "", t, flags=re.I)
        t = re.sub(r"\s+\n\s+", " ", t)
        t = re.sub(r"[ \t]{2,}", " ", t).strip()
        t = re.sub(r"```.*?```", "", t, flags=re.S).strip()
        return t[:300]

    # ------------------------------------------------------------- task sugar
    def write_post(self, topic: str, style: str = "hot take") -> str:
        return self.gen(
            f"Write one original Bluesky post, style: {style}. Topic seed: {topic}. "
            f"Max 280 characters. Portuguese preferred. One idea only.",
            max_tokens=90, temperature=1.0)

    def write_reply(self, post_text: str, author: str = "") -> str:
        return self.gen(
            f"Write a short, genuine reply to this post by @{author}: \"{post_text}\" "
            f"Add a real thought or a specific detail. Max 220 characters. No flattery, no 'great post'.",
            max_tokens=70, temperature=1.0)

    def pick_topics(self, n: int = 3) -> List[str]:
        if self.pick() is self.offline:
            return list(dict.fromkeys(self.offline.topic() for _ in range(n)))
        raw = self.gen(f"List {n} specific, concrete subjects this account should write about today. "
                       f"One per line, 2-5 words each, no numbering.", max_tokens=60, temperature=1.1)
        lines = [re.sub(r"^[\-\d\.\)\s]+", "", l).strip() for l in raw.splitlines()]
        topics = [l for l in lines if 2 <= len(l) <= 60 and len(l.split()) <= 5][:n]
        if not topics:
            topics = [self.offline.topic() for _ in range(n)]
        return list(dict.fromkeys(topics))

    def judge_follow(self, handle: str, bio: str, posts: List[str]) -> bool:
        if self.pick() is self.offline:
            return self.offline.judge_follow(handle, bio, posts)
        raw = self.gen(
            f"Decide whether to follow @{handle}. Bio: {bio or '(none)'}\n"
            f"Recent posts: {posts[:3]}\nAnswer with exactly YES or NO.",
            max_tokens=5, temperature=0.2).upper()
        return "YES" in raw[:10]

    def judge_reply(self, post_text: str) -> bool:
        if self.pick() is self.offline:
            return self.offline.judge_reply(post_text)
        raw = self.gen(
            f"Is this post worth a genuine reply from a real person? "
            f"Post: \"{post_text}\"\nAnswer exactly YES or NO. "
            f"NO if it is spam, a giveaway, crypto shilling, or needs no response.",
            max_tokens=5, temperature=0.2).upper()
        return "YES" in raw[:10]

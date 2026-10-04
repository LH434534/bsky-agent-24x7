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

from .persona import Persona, DEFAULT as DEFAULT_PERSONA

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
        "mano", "gente", "não vou mentir", "sério", "eu reparando agora",
    ]

    def __init__(self, P: Optional[Persona] = None):
        self.chain: Dict[tuple, Dict[str, int]] = {}
        self.starts: List[tuple] = []
        self.topics: List[str] = []
        self._dirty = True
        self.P: Persona = P or DEFAULT_PERSONA

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
        return random.choice(self.P.coisas)

    # ---- provider api
    def available(self) -> bool:
        return True

    def complete(self, system, user, *, max_tokens=220, temperature=0.9) -> Optional[str]:
        if self._dirty:
            self._train()
        u = user.lower()
        P = self.P
        if "sim ou nao" in u or "yes or no" in u or "exactly yes" in u:
            return "SIM" if random.random() < 0.5 else "NAO"
        if "resposta" in u or "responder" in u or '"' in u and "post de" in u:
            return random.choice(P.replies)
        if "pergunta" in u or "question" in u:
            return random.choice(P.questions).replace(
                "{coisa}", random.choice(P.coisas))
        coisa = random.choice(P.coisas)
        r = random.random()
        if r < 0.62:
            line = random.choice(P.frames).replace(
                "{coisa}", coisa).replace("{adj}", random.choice(P.adjetivos))
        else:
            line = random.choice(P.questions).replace("{coisa}", coisa)
        if random.random() < 0.30:
            op = random.choice(self.OPENERS)
            line = f"{op}, {line[0].lower() + line[1:]}"
        return line[:300]

    # ---- decisions (used when no LLM provider is reachable)
    SPAMMY = ["airdrop", "giveaway", "dm me", "follow back", "f4f", "100x",
              "telegram", "casino", "promo code", "free followers", "passive income"]

    def judge_reply(self, post_text: str) -> bool:
        t = (post_text or "").lower()
        if len(t) < 30 or any(s in t for s in self.SPAMMY):
            return False
        score = 0.28
        if "?" in t:
            score += 0.30                                  # perguntas valem resposta
        if any(w in t for w in ("você", "vocês", "alguém", "como", "por que", "porque",
                                "qual", "quem", "o que", "será", "concorda", "acham",
                                "how", "why", "should i", "what do you", "anyone else")):
            score += 0.14
        if 40 <= len(t) <= 320:
            score += 0.12                                  # substantivo e legível
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
PERSONA = DEFAULT_PERSONA.system          # compat: quem importava a string


class Brain:
    def __init__(self, providers: Optional[List[Provider]] = None,
                 persona: Optional[Persona] = None):
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
        self.P = persona or DEFAULT_PERSONA
        self.persona = self.P.system
        self.offline.P = self.P
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
            temperature: float = 0.9, system: Optional[str] = None) -> str:
        """Generate text; falls through providers, ends at the offline engine."""
        user = instruction
        if context:
            user = f"Context:\n{context}\n\nTask:\n{instruction}"
        sys_prompt = system or self.persona
        order = [self.pick(), self.offline]
        for p in order:
            try:
                out = p.complete(sys_prompt, user, max_tokens=max_tokens, temperature=temperature)
                if out:
                    return self._scrub(out)
            except Exception as e:
                log.warning("provider %s failed: %s", p.name, str(e)[:120])
                self.fail_until[p.name] = time.time() + 120
                continue
        return self._scrub(self.offline.complete(sys_prompt, user) or "...")

    # ── normalização de voz: expande abreviações e corta tiques ─────────────
    ABBREV = [
        (r"\bvc\b", "você"), (r"\bvcs\b", "vocês"), (r"\btbm\b", "também"),
        (r"\bmsm\b", "mesmo"), (r"\bpq\b", "porque"), (r"\bvdd\b", "verdade"),
        (r"\bbjs\b", "beijos"), (r"\bblz\b", "beleza"), (r"\bdboa\b", "de boa"),
        (r"\bfds\b", "fim de semana"), (r"\bhrs\b", "horas"), (r"\bmin\b", "minutos"),
        (r"\bobg\b", "obrigado"), (r"\bvlw\b", "valeu"), (r"\bqdo\b", "quando"),
        (r"\btd\b", "tudo"), (r"\bmt\b", "muito"), (r"\bmto\b", "muito"),
        (r"\bsmp\b", "sempre"), (r"\bcmg\b", "comigo"), (r"\bctg\b", "com você"),
        (r"\bplvr\b", "palavra"), (r"\bpdc\b", "pode crer"), (r"\bflw\b", "falou"),
        (r"\bsdd\b", "saudade"), (r"\bnem a pau\b", "de jeito nenhum"),
    ]
    AI_TICKS = [
        r"(?i)\bno mundo de hoje\b", r"(?i)\bna era digital\b",
        r"(?i)\bé importante ressaltar\b", r"(?i)\bvale destacar\b",
        r"(?i)\bcomo (?:uma )?(?:ia|inteligência artificial)\b",
        r"(?i)\bcomo (?:um )?(?:modelo de linguagem|assistente|bot)\b",
        r"(?i)^post\s*[:：]", r"(?i)^resposta\s*[:：]", r"(?i)^resposta\s*[:：]",
        r"(?i)^aqui está\b", r"(?i)^claro[,!]", r"(?i)^com certeza[,!]",
    ]

    @classmethod
    def _scrub(cls, text: str) -> str:
        t = text.strip().strip('"').strip("“”").strip("‘’").strip()
        # tira prefixos que modelos insistem em colocar
        t = re.sub(r"^(post|reply|tweet|output|resposta|saída|resposta final)\s*[:：]\s*",
                   "", t, flags=re.I)
        # tica de obediência no começo ("Claro!", "Com certeza!")
        t = re.sub(r"^(claro|com certeza|certamente|sem dúvida)\s*[,!]\s*", "", t, flags=re.I)
        t = re.sub(r"\s+\n\s+", " ", t)
        t = re.sub(r"[ \t]{2,}", " ", t).strip()
        t = re.sub(r"```.*?```", "", t, flags=re.S).strip()
        # expande abreviações (palavra inteira, preservando o resto da frase)
        for pat, full in cls.ABBREV:
            t = re.sub(pat, full, t)
        # remove qualquer tique de IA remanescente
        for pat in cls.AI_TICKS:
            t = re.sub(pat, "", t)
        t = re.sub(r"\s{2,}", " ", t).strip(" .,;:-")
        return t[:300]

    # ------------------------------------------------------------- task sugar
    def write_post(self, topic: str, style: str = "qualquer") -> str:
        """Escreve um post na voz do personagem."""
        return self.gen(
            f"Escreva um post agora. Semente de assunto (use ou ignore, é só um empurrão): {topic}\n"
            f"Estilo: {style}. Lembrete: sem abreviações, sem hashtag, uma ideia só.",
            max_tokens=90, temperature=1.05)

    def write_reply(self, post_text: str, author: str = "") -> str:
        return self.gen(
            f"Post de @{author or 'alguém'}:\n\"{post_text}\"\n\n"
            f"Escreva sua resposta. Lembrete: sem abreviações, sem elogio ao post, "
            f"entre 20 e 160 caracteres.",
            system=self.P.reply, max_tokens=70, temperature=1.05)

    def pick_topics(self, n: int = 3) -> List[str]:
        if self.pick() is self.offline:
            return list(dict.fromkeys(self.offline.topic() for _ in range(n)))
        raw = self.gen(self.P.topics, system=self.P.topics,
                       max_tokens=70, temperature=1.15)
        lines = [re.sub(r"^[\-\d\.\)\*\s]+", "", l).strip() for l in raw.splitlines()]
        topics = [l for l in lines if 2 <= len(l) <= 60 and len(l.split()) <= 6][:n]
        if not topics:
            topics = [self.offline.topic() for _ in range(n)]
        return list(dict.fromkeys(topics))

    def judge_follow(self, handle: str, bio: str, posts: List[str]) -> bool:
        if self.pick() is self.offline:
            return self.offline.judge_follow(handle, bio, posts)
        raw = self.gen(
            f"@{handle}\nBio: {bio or '(sem bio)'}\n"
            f"Posts recentes:\n" + "\n".join(f"- {p[:160]}" for p in posts[:3]),
            system=self.P.follow, max_tokens=6, temperature=0.3).upper()
        return "SIM" in raw[:12] or "YES" in raw[:12]

    def judge_reply(self, post_text: str) -> bool:
        if self.pick() is self.offline:
            return self.offline.judge_reply(post_text)
        raw = self.gen(f"Post:\n\"{post_text}\"",
                       system=self.P.reply_worth, max_tokens=6, temperature=0.3).upper()
        return ("SIM" in raw[:12] or "YES" in raw[:12]) and "NAO" not in raw[:12] \
            and "NOT" not in raw[:12]

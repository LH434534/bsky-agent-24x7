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
    "futebol", "jogos", "série", "música", "filme", "anime", "futebol brasileiro",
    "clima", "chuva", "calor", "final de semana", "sexta", "domingo",
    "faculdade", "escola", "prova", "trabalho", "estágio", "emprego",
    "celular", "android", "iphone", "internet", "wifi", "app",
    "comida", "lanche", "café", "pizza", "hambúrguer", "churrasco",
    "academia", "corrida", "skate", "praia", "viagem",
    "memes", "twitter", "instagram", "tiktok", "youtube", "streaming",
    "tecnologia", "programação", "python", "linux", "jogos indie",
    "sono", "preguiça", "segunda-feira", "saudade", "amizade",
]

SELF_PROMO_HINTS = ["giveaway", "airdrop", "promo", "discount code", "casino", "bet now"]


class Actions:
    def __init__(self, bsky: Bsky, brain: Brain, guard: SpamGuard, mem: Memory,
                 queries: Optional[List[str]] = None, persona_topics: Optional[List[str]] = None,
                 agency: Optional[Any] = None, social: Optional[Any] = None,
                 growth: Optional[Any] = None, images: Optional[Any] = None):
        self.b = bsky
        self.brain = brain
        self.g = guard
        self.m = mem
        self.queries = queries or DEFAULT_QUERIES
        self.persona_topics = persona_topics or []
        self.A = agency
        self.S = social
        self.G = growth
        self.I = images

    # ─────────────────────────────────────────────────────────────── helpers
    def _topic(self) -> str:
        """Assunto vem dos interesses dele (que evoluíram), não de lista fixa."""
        if self.A is not None:
            t = self.A.next_topic(self.persona_topics)
            self.m.use_topic(t)
            return t
        pool = self.persona_topics or self.brain.pick_topics(3)
        if not pool:
            return "automation"
        t = random.choice(pool)
        self.m.use_topic(t)
        return t

    def _mood(self) -> str:
        """Humor atual injetado no prompt — muda o tom do texto."""
        return self.A.mood_hint() if self.A is not None else ""

    def _curiosity(self) -> float:
        """Curiosidade atual — abre espaço pra gente nova no grafo social."""
        return getattr(self.A.inner, "curiosidade", 0.6) if self.A is not None else 0.6

    def _growth_hint(self) -> str:
        """O que funcionou antes, dito ao modelo em linguagem natural.

        Não é 'use pergunta' seco — é o modelo receber o dado e decidir como
        escrever. Forçar estrutura deixa o texto duro e repetitivo.
        """
        if self.G is None or not self.G.outcomes:
            return ""
        hooks = self.G.best_hooks()
        if not hooks:
            return ""
        melhor = hooks[0]
        formas = self.G.best_shapes()
        bits = [f"Estruturas que mais renderam engajamento nos seus posts: {melhor[0]}"]
        if len(hooks) > 1:
            bits.append(f"depois {hooks[1][0]}")
        if formas:
            bits.append(f"tamanho {formas[0][0]}")
        return ". ".join(bits) + ". Não force, só incline para esse lado."

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
                ["algo do seu dia", "uma opinião", "uma pergunta", "algo que você curte"]))
        return None

    def _post_text(self, text: str, with_image: bool = False) -> bool:
        """Push through screen + dedupe, then publish."""
        clean = self._safe(text)
        if not clean:
            self.m.log_action("post", ok=False, err="screen rejected all attempts")
            return False
        if self.g.near_dup(clean):
            self.m.log_action("post", ok=False, err="near-dup")
            return False

        imagens = None
        if with_image and self.I is not None and self.I.available():
            try:
                made = self.I.make(clean, extras=self._mood())
            except Exception as e:
                made = None
                log.info("geração de imagem falhou: %s", str(e)[:100])
            if made is not None:
                try:
                    blob = self.b.upload_blob(made.data, made.mime)
                    imagens = [{"blob": blob, "alt": made.alt,
                                "aspect": {"width": 1024, "height": 1024}}]
                    log.info("IMAGEM anexada (%s, %d KB)", made.provider,
                             len(made.data) // 1024)
                except Exception as e:
                    # upload falhou mas o texto continua valendo — posta sem imagem
                    log.info("upload da imagem falhou: %s", str(e)[:100])
                    imagens = None

        try:
            ref = self.b.post(clean, images=imagens)
        except Exception as e:
            # se o post com imagem falhar, tenta só o texto antes de desistir
            if imagens:
                try:
                    ref = self.b.post(clean)
                    log.info("post só-texto (imagem recusada)")
                except Exception as e2:
                    self.m.log_action("post", text=clean, ok=False, err=str(e2))
                    return False
            else:
                self.m.log_action("post", text=clean, ok=False, err=str(e))
                if "429" in str(e) or "rate" in str(e).lower():
                    self.g.penalty("429")
                return False
        self.g.record("post")
        self.g.remember_text(clean)
        self.m.log_action("post", text=clean, uri=ref.uri)
        self.m.record_reach(ref.uri, clean)
        if imagens:
            # conta no teto de imagens separado: sem isso o limite diário de
            # imagens nunca é aplicado e ele sai postando foto em tudo
            try:
                self.g.record("image")
            except Exception:
                pass
            self.m.log_action("image", target_handle=made.provider if made else "",
                              uri=ref.uri)
        log.info("POSTED%s: %s", " (com imagem)" if imagens else "", clean)
        return True

    # ─────────────────────────────────────────────────────────── behaviours
    def do_post(self) -> bool:
        if not self._act("post"):
            return False
        topic = self._topic()
        style = random.choice([
            "algo que você reparou hoje",
            "uma opinião meio impopular",
            "algo que te irritou de leve",
            "uma pergunta genuína",
            "algo que você está fazendo agora",
            "algo que você curte e ninguém comenta",
        ])
        if random.random() < 0.12:
            return self.do_thread(topic)
        gtext = self._growth_hint()
        text = self.brain.write_post(topic, style=style, mood=self._mood(),
                                     extra=gtext)
        # já disse algo parecido? gente real não se repete
        if self.A is not None and self.A.mind.has_said_similar(text):
            text = self.brain.write_post(self._topic(), style=style, mood=self._mood())
        com_imagem = self._wants_image()
        return self._post_text(text, with_image=com_imagem)

    def _wants_image(self) -> bool:
        """Decide se o post leva imagem. Vem da agência quando existe; senão,
        uma fração dos posts — todo post com imagem vira poluição visual."""
        if self.I is None or not self.I.available():
            return False
        if self.A is not None and hasattr(self.A, "_wants_image"):
            if self.A._wants_image():
                self.A._wants_image_at = 0.0     # consome a intenção
                return self.g.ok("image")[0] if hasattr(self.g, "ok") else True
        return random.random() < float(os.environ.get("IMG_RATE", "0.22"))

    def do_thread(self, topic: str = "") -> bool:
        if not self._act("post"):
            return False
        topic = topic or self._topic()
        parts = [self.brain.write_post(topic, style="o que chamou atenção"),
                 self.brain.write_post(topic, style="um detalhe específico"),
                 self.brain.write_post(topic, style="a conclusão simples")]
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

    def browse(self, n: int = 8) -> List[Dict[str, Any]]:
        """Só olhar o feed — sem compromisso de agir. É o que gente mais faz.

        Devolve posts crus (não ranqueados) para o motor autônomo formar
        estado interno antes de decidir qualquer coisa.
        """
        out: List[Dict[str, Any]] = []
        try:
            out += [f["post"] for f in self.b.timeline(limit=max(n, 6))]
        except Exception as e:
            log.debug("timeline failed: %s", e)
        if len(out) < n:
            try:
                out += self.b.search_posts(random.choice(self.queries), limit=n, sort="latest")
            except Exception as e:
                log.debug("search failed: %s", e)
        items = []
        for p in out[: max(n * 2, 12)]:
            rec = p.get("record", {}) or {}
            text = (rec.get("text") or "").strip()
            author = p.get("author") or {}
            if not text or author.get("did") == self.b.did:
                continue
            items.append({
                "uri": p.get("uri", ""),
                "cid": p.get("cid", ""),
                "text": text,
                "did": author.get("did", ""),
                "handle": author.get("handle", ""),
                "topic": " ".join(text.split()[:3]),
                "likes": p.get("likeCount", 0) or 0,
            })
        # alimenta o cérebro offline com linguagem viva
        self.brain.offline.feed([i["text"] for i in items])
        # e o grafo social: ver alguém postar já conta como convivência
        if self.S is not None:
            for i in items:
                if i["did"]:
                    self.S.note_seen(i["did"], i["handle"], i["text"], i.get("topic", ""))
        return items

    def has_pending_notifications(self) -> bool:
        """Tem alguém esperando resposta? Isso tem prioridade sobre tudo."""
        try:
            notes = self.b.notifications(limit=15)
        except Exception:
            return False
        for n in notes:
            if n.get("isRead"):
                continue
            if n.get("reason") in ("reply", "mention"):
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
        cands = self._candidates()
        # ordena por RELAÇÃO: quem ele conhece vem antes do post mais popular
        if self.S is not None:
            cands = self.S.rank(cands, "reply", self._curiosity())
        for cand in cands:
            if not self.brain.judge_reply(cand["text"]):
                self.m.mark_seen(cand["uri"])
                continue
            ctx = self.S.context_for(cand["did"]) if self.S is not None else ""
            text = self.brain.write_reply(cand["text"], cand["handle"], context=ctx)
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
            if self.S is not None:
                self.S.note_reply(cand["did"], cand["handle"],
                                  " ".join(cand["text"].split()[:3]))
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
        if self.S is not None:
            cands = self.S.rank(cands, "follow", self._curiosity())
        for c in cands[:6]:
            did, handle = c["did"], c["handle"]
            if self.m.is_following(did) or not self.g.target_ok(did):
                continue
            if self.S is not None:
                p = self.S.get(did, handle)
                # seguir é compromisso: precisa de confiança E afinidade
                if p.confianca < 0.30 and p.visto < 3:
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
            if self.S is not None:
                self.S.note_follow(did, handle)
            self.m.mark_follow(did, handle)
            self.m.log_action("follow", target_did=did, target_handle=handle, uri=uri)
            log.info("FOLLOWED @%s", handle)
            return True
        return False

    def do_like(self) -> bool:
        if not self._act("like"):
            return False
        cands = [c for c in self._candidates() if c["score"] > 0]
        if self.S is not None:
            cands = self.S.rank(cands, "like", self._curiosity())
        else:
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
            if self.S is not None:
                self.S.note_like(c["did"], c["handle"], " ".join(c["text"].split()[:3]))
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
        if self.S is not None:
            cands = self.S.rank(cands, "repost", self._curiosity())
        else:
            random.shuffle(cands)
        for c in cands[:3]:
            try:
                self.b.repost(c["uri"], c["raw"]["cid"])
            except Exception as e:
                log.debug("repost failed: %s", e)
                continue
            self.g.record("repost")
            self.g.touch(c["did"])
            if self.S is not None:
                self.S.note_like(c["did"], c["handle"], " ".join(c["text"].split()[:3]))
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
            # quem te respondeu ganha reciprocidade — é o sinal mais forte que existe
            if self.S is not None:
                self.S.note_got_reply(did, handle)
                self.S.note_mutual(did)
            ctx = self.S.context_for(did) if self.S is not None else ""
            reply = self._safe(self.brain.write_reply(text, handle, context=ctx))
            if not reply:
                continue
            try:
                r = self.b.post(reply, reply_to=PostRef(uri, n["cid"]))
            except Exception:
                continue
            self.g.record("reply")
            self.g.remember_text(reply)
            self.g.touch(did)
            if self.S is not None:
                self.S.note_reply(did, handle, " ".join(text.split()[:3]))
            self.m.log_action("reply", text=reply, target_did=did, target_handle=handle, uri=r.uri)
            log.info("ANSWERED @%s: %s", handle, reply)
            return True
        return False

    def do_visit(self) -> bool:
        """Ir olhar o perfil de alguém que ele gosta e faz tempo que não vê.

        Isso é iniciativa — nenhum bot automático faz, porque não vem de um
        gatilho do feed. Vem de lembrar de alguém.
        """
        if self.S is None:
            return False
        alvos = self.S.who_to_visit(3)
        if not alvos:
            return False
        for p in alvos:
            if not p.handle:
                continue
            try:
                feed = self.b.author_feed(p.handle, limit=8)
            except Exception as e:
                log.debug("visit %s failed: %s", p.handle, e)
                continue
            posts = [(f.get("post", {}) or {}).get("record", {}) or {} for f in feed]
            textos = [t.get("text", "") for t in posts if t.get("text")]
            if not textos:
                continue
            self.brain.offline.feed(textos)
            for t in textos:
                self.S.note_seen(p.did, p.handle, t, " ".join(t.split()[:3]))
            # achou algo que vale engajar?
            for f in feed[:5]:
                post = f.get("post") or {}
                rec = post.get("record") or {}
                txt = (rec.get("text") or "").strip()
                if not txt or self.m.seen(post.get("uri", "")):
                    continue
                if not self.brain.judge_reply(txt):
                    continue
                ok, _ = self.S.should_engage(p.did, p.handle, "like")
                if not ok:
                    continue
                try:
                    self.b.like(post["uri"], post["cid"])
                except Exception:
                    continue
                self.g.record("like")
                self.g.touch(p.did)
                self.S.note_like(p.did, p.handle, " ".join(txt.split()[:3]))
                self.m.mark_seen(post["uri"])
                self.m.log_action("like", target_did=p.did, target_handle=p.handle,
                                  uri=post["uri"])
                log.info("VISITOU @%s e curtiu: %s", p.handle, txt[:60])
                return True
        return False

    def do_image(self) -> bool:
        """Posta uma imagem. Quase sempre é uma foto do que ele está vendo ou
        fazendo — não uma arte abstracta solta."""
        if not self._act("post"):
            return False
        if self.I is None or not self.I.available():
            return False
        if not self.g.ok("image")[0]:
            return False
        estilos = [
            "o que você tá vendo agora",
            "algo que chamou atenção na rua",
            "o lugar onde você tá agora",
            "algo simples que ficou bonito",
            "um detalhe que só você reparou",
        ]
        text = self.brain.write_post(self._topic(), style=random.choice(estilos),
                                     mood=self._mood())
        clean = self._safe(text)
        if not clean:
            return False
        return self._post_text(clean, with_image=True)

    def do_seek(self) -> bool:
        """Procura ONDE comentar rende mais visibilidade — não comenta em qualquer
        post do feed. Conta com audiência + post fresco + poucas respostas.

        Isso é a diferença entre "responder aleatório" (automático) e "escolher
        onde sua resposta vai ser vista" (engenharia).
        """
        if self.G is None or not self._act("reply"):
            return False
        brutos: List[dict] = []
        for q in random.sample(self.queries, min(4, len(self.queries))):
            try:
                brutos += self.b.search_posts(q, limit=25, sort="latest")
            except Exception as e:
                log.debug("seek search failed: %s", e)
        try:
            brutos += [f["post"] for f in self.b.timeline(limit=30)]
        except Exception:
            pass

        oportunidades = self.G.rank_opportunities(brutos, 6)
        if not oportunidades:
            return False
        for p in oportunidades:
            rec = p.get("record") or {}
            author = p.get("author") or {}
            txt = (rec.get("text") or "").strip()
            did, handle = author.get("did", ""), author.get("handle", "")
            if not txt or not did or did == self.b.did:
                continue
            if self.m.seen(p.get("uri", "")) or not self.g.target_ok(did):
                continue
            if not self.brain.judge_reply(txt):
                self.m.mark_seen(p["uri"])
                continue
            ctx = self.S.context_for(did) if self.S is not None else ""
            reply = self._safe(self.brain.write_reply(txt, handle, context=ctx))
            if not reply:
                self.m.mark_seen(p["uri"])
                continue
            try:
                ref = self.b.post(reply, reply_to=PostRef(p["uri"], p["cid"]))
            except Exception as e:
                self.m.log_action("reply", text=reply, target_did=did, ok=False, err=str(e))
                self.G.note_tried(did)
                continue
            self.g.record("reply")
            self.g.remember_text(reply)
            self.g.touch(did)
            if self.S is not None:
                self.S.note_reply(did, handle, " ".join(txt.split()[:3]))
            self.G.note_tried(did)
            self.m.mark_seen(p["uri"])
            self.m.log_action("reply", text=reply, target_did=did,
                              target_handle=handle, uri=ref.uri)
            seg = author.get("followersCount", 0) or 0
            log.info("OPORTUNIDADE @%s (%d seguidores): %s", handle, seg, reply[:60])
            return True
        return False

    def do_measure(self) -> bool:
        """Mede o alcance real dos próprios posts. Sem isso não aprende nada."""
        if self.G is None:
            return False
        n = self.G.measure(self.b, limit=25)
        if n:
            log.info("MEDIU %d posts — %s", n, self.G.describe())
        return n > 0

    def do_harvest(self) -> None:
        """Silent pass: refresh the offline corpus from live language. No writes."""
        c = self._candidates()
        self.brain.offline.feed([x["text"] for x in c])
        log.info("harvested %d posts into corpus", len(c))

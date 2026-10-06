"""Crescimento — engenharia de engajamento medida, não chutada.

"Viralizar" não é postar mais. É colocar a coisa certa na frente das pessoas
certas na hora certa, e descobrir o que é "certo" medindo o próprio resultado.

Quatro máquinas aqui:

1. MEDIÇÃO     — busca o alcance real dos próprios posts (likes/replies/reposts
                 de verdade, não suposição) e guarda por post.
2. ATRIBUIÇÃO  — classifica cada post por estrutura (pergunta, opinião impopular,
                 lista, história, provocação) e cruza com o alcance. Descobre o
                 que faz o PERFIL dele crescer.
3. RELÓGIO     — aprende a hora em que a audiência dele está acordada, olhando
                 quando o engajamento chega. Não é palpite: é dado.
4. OPORTUNIDADE — onde um comentário rende mais visibilidade. Conta grande +
                 post fresco + poucas respostas = sua resposta aparece.

Tudo respeita o guard e o maestro — crescimento nunca passa por cima dos limites.
"""
from __future__ import annotations

import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "growth_state.json"


# ══════════════════════════════════════════════════════════════ ganchos
# Estrutura do texto — é isso que se testa, não o assunto.
HOOKS = {
    # ordem importa: padrões mais específicos primeiro, senão "reparei que
    # ninguém fala" cai em contrarian quando na verdade é observação
    "provocacao":     r"\b(impopular|polêmica|vão me crucificar|sei que vão discordar)\b",
    "lista":          r"\b(\d+\s+(coisas|motivos|dicas|sinais)|top \d+)",
    "util":           r"\b(como fazer|dica|truque|salva|guarda|anota)\w*",
    "observacao":     r"\b(reparei|percebi|notei|ninguém nota|ninguém repara)\w*",
    "contrarian":     r"\b(todo mundo erra|na verdade|é o contrário|superestimad|subestimad)\w*",
    "opiniao":        r"\b(acho|acredito|pra mim|na minha|eu prefiro|não curto)\b",
    "historia":       r"\b(hoje|ontem|semana passada|uma vez|aconteceu)\b",
    "pergunta":       r"\?",
}


def classify_hook(text: str) -> str:
    """Qual é a estrutura do post."""
    t = (text or "").lower()
    for name, pat in HOOKS.items():
        if re.search(pat, t):
            return name
    return "solto"


def shape(text: str) -> str:
    """Forma: curto / médio / longo. Èângulo menos óbvio que assunto."""
    n = len(text or "")
    if n < 60:
        return "curto"
    if n < 140:
        return "medio"
    return "longo"


@dataclass
class Outcome:
    """Um post publicado e o que ele rendeu."""
    uri: str
    ts: float
    text: str
    hook: str
    shape: str
    hour: int                 # hora local em que foi publicado
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    measured: float = 0.0     # quando foi medido

    @property
    def reach(self) -> float:
        """Alcance ponderado. Resposta vale mais que like: reply é conversa,
        repost é distribuição, like é só aprovação."""
        return self.likes + 3.0 * self.replies + 4.0 * self.reposts


class Growth:
    def __init__(self, state_file: Path = STATE):
        self.file = state_file
        self.outcomes: List[Outcome] = []
        self.hour_engagement: Dict[int, float] = defaultdict(float)
        self.hour_posts: Dict[int, int] = defaultdict(int)
        self.opportunities: List[dict] = []
        self.targets_tried: Dict[str, float] = {}
        self.last_measure = 0.0
        self.load()

    # ───────────────────────────────────────────────── persistência
    def save(self) -> None:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps({
                "outcomes": [asdict(o) for o in self.outcomes[-200:]],
                "hour_engagement": dict(self.hour_engagement),
                "hour_posts": dict(self.hour_posts),
                "targets_tried": self.targets_tried,
                "last_measure": self.last_measure,
            }, ensure_ascii=False))
        except Exception:
            pass

    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
        except Exception:
            return
        for o in (d.get("outcomes") or []):
            try:
                self.outcomes.append(Outcome(**{k: v for k, v in o.items()
                                                if k in Outcome.__dataclass_fields__}))
            except Exception:
                pass
        self.hour_engagement = defaultdict(float, {int(k): v for k, v in
                                                   (d.get("hour_engagement") or {}).items()})
        self.hour_posts = defaultdict(int, {int(k): v for k, v in
                                            (d.get("hour_posts") or {}).items()})
        self.targets_tried = d.get("targets_tried", {})
        self.last_measure = d.get("last_measure", 0.0)

    # ═══════════════════════════════════════════════ 1. MEDIÇÃO
    def measure(self, bsky, limit: int = 25) -> int:
        """Busca o alcance real dos próprios posts e atualiza o que funciona.

        Sem isso o sistema é cego: posta e nunca sabe se alguém viu.
        """
        if not getattr(bsky, "did", None):
            return 0
        try:
            feed = bsky.author_feed(bsky.did, limit=limit)
        except Exception:
            return 0
        novos = 0
        known = {o.uri for o in self.outcomes}
        for f in feed:
            post = f.get("post") or {}
            uri = post.get("uri")
            if not uri or uri in known:
                continue
            rec = post.get("record") or {}
            text = (rec.get("text") or "").strip()
            if not text:
                continue
            ts = self._post_ts(rec)
            o = Outcome(
                uri=uri, ts=ts, text=text,
                hook=classify_hook(text), shape=shape(text),
                hour=self._hour(ts, bsky),
                likes=post.get("likeCount", 0) or 0,
                replies=post.get("replyCount", 0) or 0,
                reposts=post.get("repostCount", 0) or 0,
                measured=time.time(),
            )
            self.outcomes.append(o)
            # relógio da audiência: engajamento por hora de publicação
            self.hour_engagement[o.hour] += o.reach
            self.hour_posts[o.hour] += 1
            novos += 1
        self.last_measure = time.time()
        if novos:
            self.save()
        return novos

    @staticmethod
    def _post_ts(rec: dict) -> float:
        t = rec.get("createdAt") or ""
        try:
            import datetime
            return datetime.datetime.fromisoformat(
                t.replace("Z", "+00:00")).timestamp()
        except Exception:
            return time.time()

    @staticmethod
    def _hour(ts: float, bsky) -> int:
        off = int(getattr(bsky, "tz_offset", -3) or -3)
        return int(((ts + off * 3600) / 3600) % 24)

    # ══════════════════════════════════════════ 2. ATRIBUIÇÃO
    def best_hooks(self, min_n: int = 3) -> List[Tuple[str, float, int]]:
        """Quais estruturas rendem mais alcance médio. Só com amostra mínima."""
        by: Dict[str, List[float]] = defaultdict(list)
        for o in self.outcomes:
            by[o.hook].append(o.reach)
        out = []
        for h, rs in by.items():
            if len(rs) >= min_n:
                out.append((h, statistics.mean(rs), len(rs)))
            elif len(rs) > 0:
                out.append((h, statistics.mean(rs) * 0.6, len(rs)))   # desconto por pouca amostra
        out.sort(key=lambda x: -x[1])
        return out

    def best_shapes(self) -> List[Tuple[str, float, int]]:
        by: Dict[str, List[float]] = defaultdict(list)
        for o in self.outcomes:
            by[o.shape].append(o.reach)
        out = [(s, statistics.mean(rs), len(rs)) for s, rs in by.items() if rs]
        out.sort(key=lambda x: -x[1])
        return out

    def best_topics(self) -> List[Tuple[str, float, int]]:
        """Assunto que rendeu. Usa as 3 primeiras palavras como rótulo."""
        by: Dict[str, List[float]] = defaultdict(list)
        for o in self.outcomes:
            k = " ".join(o.text.split()[:3]).lower()
            by[k].append(o.reach)
        out = [(k, statistics.mean(v), len(v)) for k, v in by.items() if len(v) >= 2]
        out.sort(key=lambda x: -x[1])
        return out

    EPSILON = 0.18          # fração reservada à exploração

    def pick_hook(self) -> str:
        """Escolhe a estrutura do próximo post pelo que rendeu antes.

        Epsilon-greedy: na maior parte do tempo usa o que funciona, mas guarda
        uma fração para testar coisa nova. Sem isso ele acha um acerto e repete
        para sempre — para de aprender e o perfil estagna.
        """
        todos = list(HOOKS) + ["solto"]
        hooks = [h for h in self.best_hooks() if h[1] > 0]
        if not hooks:
            return random.choice(todos)
        # explora: pega um gancho nunca testado, ou qualquer um
        if random.random() < self.EPSILON:
            testados = {h[0] for h in self.best_hooks()}
            virgens = [h for h in todos if h not in testados]
            return random.choice(virgens) if virgens else random.choice(todos)
        top = hooks[:3]
        return random.choices([h[0] for h in top],
                              weights=[max(0.05, h[1]) for h in top])[0]

    def pick_shape(self) -> str:
        s = self.best_shapes()
        todos = ["curto", "medio", "longo"]
        if not s:
            return random.choice(todos)
        if random.random() < self.EPSILON:
            testados = {x[0] for x in s}
            virgens = [x for x in todos if x not in testados]
            return random.choice(virgens) if virgens else random.choice(todos)
        return random.choices([x[0] for x in s[:3]],
                              weights=[max(0.05, x[1]) for x in s[:3]])[0]

    def pick_topic(self, fallback: List[str]) -> str:
        t = [x for x in self.best_topics() if x[1] > 0]
        if t and random.random() < 0.6:
            return random.choices([x[0] for x in t[:5]],
                                  weights=[max(0.05, x[1]) for x in t[:5]])[0]
        return random.choice(fallback) if fallback else "o dia"

    # ═══════════════════════════════════════════════ 3. RELÓGIO
    def best_hours(self, k: int = 4) -> List[int]:
        """Horas em que postar rendeu mais, por post (não por volume)."""
        out = []
        for h, total in self.hour_engagement.items():
            n = self.hour_posts.get(h, 0)
            if n == 0:
                continue
            out.append((h, total / n, n))
        out.sort(key=lambda x: -x[1])
        return [h for h, _, _ in out[:k]]

    def hour_score(self, hour: int) -> float:
        """0..1 — quão boa é essa hora para publicar, segundo os dados."""
        if not self.hour_posts:
            return 0.5
        n = self.hour_posts.get(hour, 0)
        if n == 0:
            return 0.45          # hora nunca testada: neutra, nem boa nem ruim
        media = self.hour_engagement[hour] / n
        todas = [self.hour_engagement[h] / self.hour_posts[h]
                 for h in self.hour_posts if self.hour_posts[h] > 0]
        mx = max(todas) or 1
        return max(0.05, min(1.0, media / mx))

    def should_post_now(self, tz_offset: int = -3) -> Tuple[bool, float]:
        """Vale postar agora? Usa o relógio aprendido, não um horário fixo."""
        hour = int(((time.time() + tz_offset * 3600) / 3600) % 24)
        s = self.hour_score(hour)
        if not self.hour_posts:
            return True, s            # sem dados ainda: posta e aprende
        return random.random() < (0.30 + 0.70 * s), s

    # ══════════════════════════════════════════ 4. OPORTUNIDADE
    def score_opportunity(self, post: dict, author: dict) -> float:
        """Onde um comentário rende mais visibilidade.

        A matemática do "reply guy" que funciona:
          - autor com audiência grande → sua resposta é vista
          - post FRESCO → ainda está no feed dos outros
          - POUCAS respostas → a sua não fica enterrada
          - engajamento já alto → o post está circulando
        Conta morta / spam / muito respondida = desperdício.
        """
        seg = author.get("followersCount", 0) or 0
        if seg < 40:                       # ninguém vai ver
            return 0.0
        # audiência: cresce mas satura — conta gigante já tem respostas demais
        aud = math.log10(max(seg, 10)) / 6.0
        aud = min(1.0, aud)

        rec = post.get("record") or {}
        ts = self._post_ts(rec)
        idade_min = (time.time() - ts) / 60
        # fresco: melhor nos primeiros 20 min, ainda vale até ~3h
        if idade_min > 180:
            return 0.0
        fresco = max(0.0, 1.0 - idade_min / 180.0)

        replies = post.get("replyCount", 0) or 0
        # Saturação é porteiro, não peso: se já tem 200 respostas, a sua afunda
        # e não importa o resto. Como peso, conta gigante com 900 likes ainda
        # pontuava alto mesmo estando totalmente enterrada.
        gate = 1.0 / (1.0 + (replies / 12.0) ** 1.4)

        eng = ((post.get("likeCount", 0) or 0)
               + 2 * (post.get("repostCount", 0) or 0))
        circulando = min(1.0, math.log10(max(eng, 1) + 1) / 3.0)

        texto = (rec.get("text") or "").lower()
        if any(s in texto for s in ("airdrop", "giveaway", "casino", "dm me",
                                    "telegram", "f4f")):
            return 0.0

        return (aud * 0.40 + fresco * 0.35 + circulando * 0.25) * gate

    def rank_opportunities(self, posts: List[dict], k: int = 5) -> List[dict]:
        """Ordena onde vale comentar."""
        scored = []
        for p in posts:
            author = p.get("author") or {}
            did = author.get("did", "")
            if not did:
                continue
            # não repete alvo na mesma janela
            if time.time() - self.targets_tried.get(did, 0) < 3600 * 6:
                continue
            s = self.score_opportunity(p, author)
            if s <= 0:
                continue
            scored.append((s, p))
        scored.sort(key=lambda x: -x[0])
        self.opportunities = [p for _, p in scored[:k]]
        return self.opportunities

    def note_tried(self, did: str) -> None:
        self.targets_tried[did] = time.time()
        # limpa alvos antigos
        if len(self.targets_tried) > 400:
            corte = time.time() - 86400 * 7
            self.targets_tried = {k: v for k, v in self.targets_tried.items()
                                  if v > corte}

    # ═════════════════════════════════════════════════════ diagnóstico
    def describe(self) -> str:
        n = len(self.outcomes)
        if n == 0:
            return "sem dados ainda (primeira medição pendente)"
        hooks = self.best_hooks()
        horas = self.best_hours(3)
        return (f"{n} posts medidos · melhor gancho {hooks[0][0] if hooks else '—'} · "
                f"melhores horas {horas}")

    def report(self) -> str:
        lines = []
        if not self.outcomes:
            return "  (nenhum post medido ainda)"
        lines.append(f"  posts medidos: {len(self.outcomes)}")
        total = sum(o.reach for o in self.outcomes)
        lines.append(f"  alcance total ponderado: {total:.0f}")
        lines.append("  ganchos por alcance médio:")
        for h, m, n in self.best_hooks()[:5]:
            lines.append(f"    {h:<12} {m:>6.2f}  (n={n})")
        lines.append("  formas:")
        for s, m, n in self.best_shapes()[:3]:
            lines.append(f"    {s:<12} {m:>6.2f}  (n={n})")
        if self.hour_posts:
            lines.append("  relógio da audiência (hora → alcance/post):")
            for h in self.best_hours(5):
                lines.append(f"    {h:>2}h  {self.hour_engagement[h]/self.hour_posts[h]:>6.2f}"
                             f"  (n={self.hour_posts[h]})")
        return "\n".join(lines)

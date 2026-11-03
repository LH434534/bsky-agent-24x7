"""
core/viral.py — qualidade de compartilhamento, calibrada com dados REAIS do bot.

═══════════════════════════════════════════════════════════════════════════
LIMITES ÉTICOS (não são decorativos — estão implementados como bloqueios)
═══════════════════════════════════════════════════════════════════════════
Este módulo NÃO faz, e nunca vai fazer:
  • like / reply / follow / repost automatizado em massa
  • astroturfing, contas falsas, compra de seguidores ou engajamento
  • inflar volume de postagens ou burlar rate limits / SpamGuard
  • fabricar outrage, negatividade ou ameaça para aumentar alcance
  • clickbait / curiosity gap que promete e não entrega
  • "engajamento forçado" ("curte", "comenta", "compartilha")
  • sequestro artificial de trends ou dados de contas alheias

Motivo: as Bluesky Developer Guidelines definem spam como "interações
automatizadas ou em massa, inclusive as que geram notificação (follow, like,
reply)" e "qualquer método de automatizar seguidores ou interações"
(https://docs.bsky.app/docs/advanced-guides/rate-limits). As Community
Guidelines vedam manipular sinais sociais para "unearned reach". O Transparency
Report 2025 mostra que "Be Authentic" respondeu por 81% das remoções, com
detecção automatizada de redes de spam coordenadas.

O que ESTE módulo faz, e que a literatura realmente sustenta:
  • linguagem CONCRETA → mais compartilhamentos (Hu et al., 2025, QJEP:
    15M tweets + 50k posts Reddit + experimento causal pré-registrado)
  • facilidade de processamento / legibilidade (Berger & Milkman 2012;
    Psychology Today 2026)
  • autenticidade e self-disclosure, segunda pessoa, ausência de clichê de IA
  • STEPPS de Jonah Berger (Contagious): valor prático, gatilhos, história
  • novidade: quase-duplicata detectada por similaridade, não só hash exato
  • excitação emocional SEGURA: surpresa/admiração positivas são permitidas;
    raiva/ameaça são PENALIZADAS, embora aumentem alcance bruto (ScienceDirect
    2024 mostra o viés de negatividade E que clickbait reduz compartilhamento
    por percepção de manipulação)

Tudo é EXPLICÁVEL: cada ponto vem com a razão em linguagem natural.
Tudo é CONSULTIVO por padrão: ranqueia variantes que o bot JÁ gerou e inclina
o prompt — nunca aumenta o número de posts nem de chamadas ao modelo.

Kill switches: BOT_VIRAL=0 · VIRAL_RANK=0 · VIRAL_MIN_N=3
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("bsky.viral")


def _try(mod: str, attr: str):
    try:
        return getattr(__import__(mod, fromlist=[attr]), attr)
    except Exception:
        return None


fuzz = _try("rapidfuzz", "fuzz")
croniter = _try("croniter", "croniter")
slugify = _try("slugify", "slugify")
windowed = _try("more_itertools", "windowed")
np_percentile = None
try:
    import numpy as np
    np_percentile = np.percentile
except Exception:
    pass
flesch = _try("textstat", "flesch_reading_ease")
zipf = _try("wordfreq", "zipf_frequency")
_SIA = _try("vaderSentiment.vaderSentiment", "SentimentIntensityAnalyzer")
_SENT = _SIA() if _SIA else None
try:
    from core.human import AI_TELLS, ai_tell_score, human_likeness_score
except Exception:
    AI_TELLS = []
    ai_tell_score = lambda _t: 0
    human_likeness_score = lambda _t: 50.0

ENABLED = os.environ.get("BOT_VIRAL", "1").strip().lower() not in {"0", "false", "no", "off"}
RANK_ENABLED = os.environ.get("VIRAL_RANK", "1").strip().lower() not in {"0", "false", "no", "off"}
MIN_N = int(os.environ.get("VIRAL_MIN_N", "3") or 3)
FUZZY_THRESHOLD = float(os.environ.get("VIRAL_FUZZY", "88") or 88)

# ──────────────────────────────────────────────────────────────── bloqueios
CLICKBAIT = re.compile(
    r"(você não vai acreditar|não vai acreditar no que|o que aconteceu depois|"
    r"vai te chocar|você nunca vai adivinhar|esse truque|segredo que (ninguém|médicos)|"
    r"you won't believe|what happened next|shocking|mind.?blowing|"
    r"não creas lo que|te va a sorprender)", re.I)
ENGAGEMENT_BAIT = re.compile(
    r"(curte e compartilha|deixa um like|comenta (aqui|abaixo)|compartilha com|"
    r"marca (alguém|um amigo)|salva esse post|rt se|like and share|"
    r"comment below|tag a friend|dale like y comparte)", re.I)
TOXICO = re.compile(
    r"(odeio|idiota|estúpido|burro|imbecil|vou te (pegar|derrubar)|ameaça|"
    r"hate|idiot|stupid|kill|te voy a (matar|joder))", re.I)
OUTRAGE = re.compile(r"(escândalo|absurdo!+|revoltante!+|indignante!+|vergonha!+|"
                     r"todo mundo é (burro|corrupto)|o fim do mundo)", re.I)
HASHTAG_SPAM = re.compile(r"(#\w+\s*){3,}")
URL_DUMP = re.compile(r"(https?://\S+\s*){3,}")

# ────────────────────────────────────────────────────────────── concretude
# Estudo de Hu et al. (2025): palavras que evocam imagem/sensação aumentam
# compartilhamento. Léxico enxuto + detectores estruturais, sem baixar corpora.
CONCRETO = set("""
janela luz céu rua prédio café xícara mesa cadeira porta chuva sol nuvem vento
praia areia mar água frio calor cheiro barulho silêncio som cor azul vermelho
verde amarelo branco preto mão dedo olho rosto cabelo pé cabeça voz papel
caneta teclado tela fio cabo servidor disco memória log erro porta rede
casa banco ônibus trem bicicleta livro página foto imagem vídeo relógio
segundo minuto hora dia noite manhã tarde ontem hoje amanhã
window light sky street building coffee cup table chair door rain sun cloud
wind beach sand sea water cold heat smell noise silence sound color blue red
green yellow white black hand finger eye face hair foot head voice paper
pen keyboard screen cable server disk memory log error network house bus train
bike book page photo image video clock second minute hour day night morning
yesterday today tomorrow
ventana luz cielo calle edificio café taza mesa silla puerta lluvia sol nube
viento playa arena mar agua frio calor olor ruido silencio sonido mano dedo
ojo cara pelo pie cabeza voz papel pantalla cable servidor disco error red
casa autobús tren bici libro página foto imagen vídeo reloj hora día noche
""".split())
ABSTRATO = set("""
paradigma essência conceito abstrato fundamentalmente ontologia epistemologia
metodologia abrangente holístico sinergia disrupção ecossistema mindset
framework concepção paradigm essence concept abstract fundamentally ontology
epistemology methodology comprehensive holistic synergy ecosystem paradigm
""".split())
SENSORIAL = re.compile(
    r"\b(brilh|escur|gelad|quent|úmid|sec|macio|ásper|barulh|silenc|cheir|"
    r"doce|amarg|azul|vermelh|verde|amarel|bright|dark|cold|warm|wet|dry|"
    r"soft|rough|loud|quiet|sweet|bitter)\w*", re.I)
NUMERO = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|h|min|s|km|m|kg|gb|mb|vezes|x|pessoas|dias|anos|posts)?\b", re.I)
SEGUNDA_PESSOA = re.compile(r"\b(você|teu|tua|seu|sua|contigo|you|your|yourself|tú|tu|tus)\b", re.I)
EU = re.compile(r"\b(eu|meu|minha|mim|I|my|me|yo|mi|mío)\b", re.I)
PERGUNTA = re.compile(r"\?")


def concreteness(text: str) -> Tuple[float, List[str]]:
    """0..1. Conta termos imageáveis, números e linguagem sensorial."""
    t = (text or "").lower()
    palavras = re.findall(r"[\wÀ-ÿ]+", t)
    if not palavras:
        return 0.0, []
    concretas = sum(1 for w in palavras if w in CONCRETO)
    abstratas = sum(1 for w in palavras if w in ABSTRATO)
    sensoriais = len(SENSORIAL.findall(t))
    numeros = len(NUMERO.findall(t))
    bruto = (concretas + 1.5 * sensoriais + 1.2 * numeros) / max(6, len(palavras) * 0.30)
    score = max(0.0, min(1.0, bruto - 0.30 * abstratas))
    razoes = []
    if concretas:
        razoes.append(f"{concretas} palavra(s) concreta(s)")
    if sensoriais:
        razoes.append(f"{sensoriais} termo(s) sensorial(is)")
    if numeros:
        razoes.append(f"{numeros} dado(s) específico(s)")
    if abstratas:
        razoes.append(f"penalizado por {abstratas} termo(s) abstrato(s)")
    return round(score, 3), razoes


def ease_of_processing(text: str) -> Tuple[float, List[str]]:
    """0..1. Legibilidade + palavras frequentes + frases curtas + 2ª pessoa."""
    t = text or ""
    palavras = re.findall(r"[\wÀ-ÿ]+", t)
    if not palavras:
        return 0.0, []
    s = 0.0
    razoes = []
    tam_medio = sum(len(w) for w in palavras) / len(palavras)
    if tam_medio <= 6:
        s += 0.25; razoes.append(f"palavras curtas ({tam_medio:.1f} letras)")
    elif tam_medio <= 8:
        s += 0.12
    if flesch:
        try:
            f = flesch(t)
            if 30 <= f <= 100:
                s += 0.30; razoes.append(f"Flesch {f:.0f} (fácil de ler)")
            elif f > 0:
                s += 0.10
        except Exception:
            pass
    frases = [x for x in re.split(r"[.!?\n]+", t) if x.strip()]
    if frases:
        media = sum(len(x.split()) for x in frases) / len(frases)
        if media <= 14:
            s += 0.20; razoes.append(f"frases curtas ({media:.0f} palavras)")
        elif media <= 22:
            s += 0.10
    if SEGUNDA_PESSOA.search(t):
        s += 0.15; razoes.append("segunda pessoa (mais pessoal)")
    if zipf:
        try:
            zs = [zipf(w.lower(), "pt") for w in palavras if len(w) > 3][:30]
            zs = [z for z in zs if z > 0]
            if zs and statistics.mean(zs) >= 4.0:
                s += 0.10; razoes.append("vocabulário frequente (processamento fácil)")
        except Exception:
            pass
    return round(min(1.0, s), 3), razoes


def authenticity(text: str) -> Tuple[float, List[str]]:
    """0..1. Self-disclosure, sem clichê de IA, sem tom corporativo."""
    t = text or ""
    tells = ai_tell_score(t)
    s = max(0.0, 1.0 - 0.25 * tells)
    razoes = [f"human_likeness {human_likeness_score(t):.0f}/100"]
    if EU.search(t):
        s = min(1.0, s + 0.20); razoes.append("self-disclosure (eu/meu)")
    if tells:
        razoes.append(f"{tells} clichê(s) de IA penalizado(s)")
    if re.search(r"\b(hoje|ontem|agora|agora mesmo|essa manhã|hoje cedo)\b", t, re.I):
        s = min(1.0, s + 0.10); razoes.append("marcador temporal concreto")
    return round(max(0.0, s), 3), razoes


def hook_strength(text: str) -> Tuple[float, str, List[str]]:
    """Força do gancho, sem curiosity gap enganoso."""
    t = (text or "").strip()
    if not t:
        return 0.0, "vazio", []
    # IMPORTANTE: split DEPOIS da pontuação, não nela. Usar `[.!?]` como
    # delimitador removia o "?" e uma pergunta virava "dado" por causa do "3h".
    primeira = re.split(r"(?<=[.!?])\s+|\n", t)[0].strip().lower()
    # Pergunta vem ANTES de número: "O que você faz às 3h?" é pergunta,
    # não headline de dado. A ordem aqui é semântica, não cosmética.
    if PERGUNTA.search(primeira):
        return 0.9, "pergunta", ["abre com pergunta genuína"]
    # Âncoras no INÍCIO: "3 coisas que reparei..." é dado, enquanto
    # "Reparei que ... às 6h" é observação — o número no meio não manda.
    if re.match(r"^\s*(reparei|percebi|notei|hoje|ontem|uma vez|aconteceu)\w*", primeira):
        return 0.8, "observacao", ["abre com observação situada"]
    if re.match(r"^\s*(na verdade|é o contrário|todo mundo|ninguém|acho|pra mim)\w*", primeira):
        return 0.75, "contraste", ["abre com contraste ou opinião"]
    if re.match(r"^\s*(como|passo|dica|anota|salva|guarda)\w*", primeira):
        return 0.8, "util", ["valor prático explícito (STEPPS)"]
    if re.match(r"^\s*\d+", primeira) or NUMERO.search(primeira):
        return 0.85, "dado", ["abre com número/dado específico"]
    if EU.search(primeira):
        return 0.65, "confissao", ["abre em primeira pessoa"]
    return 0.35, "solto", ["gancho fraco: começa sem âncora"]


def safe_arousal(text: str) -> Tuple[float, List[str]]:
    """Excitação POSITIVA permitida; negatividade/raiva penalizada.

    A literatura mostra que alta excitação aumenta compartilhamento, mas o
    viés de negatividade é justamente a alavanca que NÃO usamos: além de
    antiética, clickbait/ameaça reduzem compartilhamento por percepção de
    manipulação e violam as diretrizes de autenticidade.
    """
    t = text or ""
    razoes = []
    s = 0.5
    if _SENT:
        try:
            c = float(_SENT.polarity_scores(t[:1500])["compound"])
            if c >= 0.35:
                s += 0.30; razoes.append(f"valência positiva ({c:+.2f})")
            elif c <= -0.35:
                s -= 0.35; razoes.append(f"valência negativa penalizada ({c:+.2f})")
            s += min(0.15, abs(c) * 0.20)
        except Exception:
            pass
    if re.search(r"!|\?", t):
        s += 0.10
    if TOXICO.search(t):
        s -= 0.60; razoes.append("linguagem tóxica: bloqueio")
    if OUTRAGE.search(t):
        s -= 0.40; razoes.append("outrage manufacturado: bloqueio")
    return round(max(0.0, min(1.0, s)), 3), razoes


def novelty(text: str, historico: Sequence[str] | None = None) -> Tuple[float, List[str]]:
    """1.0 = inédito. rapidfuzz pega quase-duplicata que hash exato não pega."""
    if not historico:
        return 1.0, []
    try:
        candidatos = [h for h in list(historico)[-60:] if isinstance(h, str) and h]
    except (TypeError, AttributeError):
        return 1.0, ["histórico não iterável — novidade assumida"]
    pior, melhor_txt = 0.0, ""
    for h in candidatos:
        if not h:
            continue
        if fuzz:
            r = fuzz.token_sort_ratio(text, h)
        else:
            a, b = set(text.lower().split()), set(h.lower().split())
            r = 100 * len(a & b) / max(1, len(a | b))
        if r > pior:
            pior, melhor_txt = r, h
    if pior >= FUZZY_THRESHOLD:
        return 0.0, [f"quase-duplicata ({pior:.0f}% similar): {melhor_txt[:40]!r}"]
    if pior >= 70:
        return round(max(0.2, (FUZZY_THRESHOLD - pior) / FUZZY_THRESHOLD), 3), [
            f"similaridade moderada ({pior:.0f}%)"]
    return 1.0, [f"novo (máx. similaridade {pior:.0f}%)"]


def blocklist(text: str) -> List[str]:
    """Motivos de BLOQUEIO. Nunca silenciosos, sempre explicados."""
    b = []
    if CLICKBAIT.search(text): b.append("clickbait / curiosity gap enganoso")
    if ENGAGEMENT_BAIT.search(text): b.append("pedido de engajamento (contra diretrizes)")
    if TOXICO.search(text): b.append("linguagem tóxica ou ameaça")
    if OUTRAGE.search(text): b.append("outrage manufacturado")
    if HASHTAG_SPAM.search(text): b.append("spam de hashtags")
    if URL_DUMP.search(text): b.append("despejo de links")
    if ai_tell_score(text) >= 3: b.append("texto majoritariamente clichê de IA")
    if len(text.strip()) > 300: b.append("acima de 300 graphemes (limite do Bluesky)")
    return b


# ─────────────────────────────────────────────────── calibração com dados
@dataclass
class Calibracao:
    calibrado: bool = False
    n: int = 0
    por_hook: Dict[str, float] = field(default_factory=dict)
    por_shape: Dict[str, float] = field(default_factory=dict)
    mediana_reach: float = 0.0

    def resumo(self) -> Dict[str, Any]:
        return {"calibrado": self.calibrado, "n": self.n,
                "mediana_reach": round(self.mediana_reach, 2),
                "por_hook": dict(sorted(self.por_hook.items(), key=lambda x: -x[1])[:5]),
                "por_shape": dict(sorted(self.por_shape.items(), key=lambda x: -x[1])[:3])}


def _reach(o: Any) -> float:
    if hasattr(o, "reach"):
        try:
            return float(o.reach())
        except Exception:
            pass
    for k in ("reach", "engagement", "score", "total"):
        if isinstance(o, dict) and k in o:
            try: return float(o[k])
            except Exception: pass
    return 0.0


def _campo(o: Any, nome: str) -> str:
    if isinstance(o, dict):
        return str(o.get(nome, "solto"))
    return str(getattr(o, nome, "solto"))


def calibrar(outcomes: Iterable[Any] | None) -> Calibracao:
    """Aprende pesos com o PRÓPRIO histórico (core/growth.py).

    Sem >= MIN_N amostras devolve `calibrado=False` e pesos NEUTROS — não
    afirma "melhor horário" nem "hook comprovado" sem evidência. Isso é
    deliberado: o ATProto não expõe impressões/alcance, só contagens de
    engajamento que o próprio bot já coleta em Growth.measure().
    """
    c = Calibracao()
    itens = list(outcomes or [])
    c.n = len(itens)
    if len(itens) < MIN_N:
        return c
    reaches = [_reach(o) for o in itens]
    if sum(reaches) <= 0:
        return c
    c.calibrado = True
    c.mediana_reach = float(np_percentile(reaches, 50)) if np_percentile else statistics.median(reaches)
    for campo, alvo in (("hook", c.por_hook), ("shape", c.por_shape)):
        grupos: Dict[str, List[float]] = defaultdict(list)
        for o in itens:
            grupos[_campo(o, campo)].append(_reach(o))
        for k, vs in grupos.items():
            # suavização: divide pelo tamanho a amostra única não dominar
            alvo[k] = round(statistics.mean(vs) / math.sqrt(max(1, len(vs))), 3)
    return c


def viral_hint(cal: Calibracao | None = None, *, growth=None) -> str:
    """Dica em linguagem natural para o prompt, no estilo de _growth_hint.

    Nunca diz "use pergunta": dá o dado e deixa o modelo decidir, preservando
    a filosofia já escrita em core/actions.py.
    """
    if not ENABLED:
        return ""
    if cal is None and growth is not None:
        cal = calibrar(getattr(growth, "outcomes", []))
    if not cal or not cal.calibrado:
        return ("Prefira cenas concretas e observáveis a abstrações; uma ideia só; "
                "linguagem simples; sem chamar atenção para si. Não force, só incline.")
    ganchos = sorted(cal.por_hook.items(), key=lambda x: -x[1])[:2]
    formas = sorted(cal.por_shape.items(), key=lambda x: -x[1])[:1]
    partes = ["Nos seus posts, detalhes concretos e observações situadas renderam mais "
              "do que formulações abstratas."]
    if ganchos:
        partes.append(f"Estruturas que mais renderam: {ganchos[0][0]}"
                      + (f", depois {ganchos[1][0]}" if len(ganchos) > 1 else ""))
    if formas:
        partes.append(f"tamanho {formas[0][0]}")
    partes.append("Nada de promessa que o texto não cumpra, nem pedido de curtida. "
                  "Não force, só incline para esse lado.")
    juntas = ". ".join(p.strip().rstrip(".") for p in partes if p.strip())
    return juntas + "."


# ─────────────────────────────────────────────────────────────── avaliação
PESOS = {"concretude": 22, "processamento": 18, "autenticidade": 18,
         "gancho": 16, "novidade": 14, "arousal_seguro": 12}


def avaliar(texto: str, *, historico: Sequence[str] | None = None,
            cal: Calibracao | None = None) -> Dict[str, Any]:
    """Devolve relatório 0..100 EXPLICÁVEL. Nunca lança, nunca reescreve."""
    if not ENABLED:
        return {"score": 0, "desligado": True, "componentes": {}, "razoes": [],
                "bloqueios": [], "calibrado": False}
    t = texto or ""
    conc, r_conc = concreteness(t)
    ease, r_ease = ease_of_processing(t)
    auth, r_auth = authenticity(t)
    hook, hook_nome, r_hook = hook_strength(t)
    arou, r_arou = safe_arousal(t)
    nov, r_nov = novelty(t, historico)
    bloqueios = blocklist(t)

    comp = {"concretude": conc, "processamento": ease, "autenticidade": auth,
            "gancho": hook, "novidade": nov, "arousal_seguro": arou}
    score = sum(PESOS[k] * v for k, v in comp.items())
    # calibração real só ajusta o gancho; nunca inventa bônus sem dado
    bonus = 0.0
    razoes_cal = []
    if cal and cal.calibrado and cal.por_hook:
        melhor = max(cal.por_hook, key=cal.por_hook.get)
        pior = min(cal.por_hook, key=cal.por_hook.get)
        if cal.por_hook[pior] > 0 and hook_nome == melhor:
            bonus = min(6.0, 6.0 * (cal.por_hook[melhor] / cal.por_hook[pior] - 1))
            razoes_cal.append(f"gancho '{melhor}' calibrado em {cal.n} posts reais (+{bonus:.1f})")
        razoes_cal.append(f"calibração: {cal.n} outcomes, mediana reach {cal.mediana_reach:.1f}")
    score = round(min(100.0, score + bonus), 1)
    if bloqueios:
        score = round(score * 0.35, 1)

    razoes = [f"{k}: {v:.2f}" for k, v in comp.items()]
    razoes += r_conc[:3] + r_ease[:3] + r_auth[:2] + r_hook + r_arou[:2] + r_nov[:2] + razoes_cal
    return {
        "score": score,
        "componentes": comp,
        "hook": hook_nome,
        "razoes": razoes,
        "bloqueios": bloqueios,
        "calibrado": bool(cal and cal.calibrado),
        "amostras": cal.n if cal else 0,
        "recomenda_reescrita": bool(bloqueios) or score < 45,
    }


def rank_variants(variants: Sequence[str], *, historico: Sequence[str] | None = None,
                   cal: Calibracao | None = None) -> List[Dict[str, Any]]:
    """Ordena variantes QUE O BOT JÁ GEROU. Não cria texto, não posta nada.

    Desempate determinístico por SHA-256 (sem `random`), para auditoria.
    """
    if not ENABLED or not RANK_ENABLED:
        return [{"texto": v, "score": 0, "posicao": i + 1, "desligado": True}
                for i, v in enumerate(variants)]
    out = []
    for i, v in enumerate(variants):
        r = avaliar(v, historico=historico, cal=cal)
        h = hashlib.sha256((v or "").encode()).hexdigest()[:8]
        out.append({"texto": v, "score": r["score"], "hook": r["hook"],
                    "bloqueios": r["bloqueios"], "razoes": r["razoes"][:6],
                    "hash": h, "posicao_original": i + 1})
    out.sort(key=lambda x: (-x["score"], x["hash"]))
    for i, x in enumerate(out, 1):
        x["posicao"] = i
    return out


def escolher(variants: Sequence[str], *, historico=None, cal=None) -> Tuple[Optional[str], Dict[str, Any]]:
    """Atalho usado por core/actions.py. Devolve (melhor_texto, decisão)."""
    if not variants:
        return None, {"motivo": "sem variantes"}
    r = rank_variants(variants, historico=historico, cal=cal)
    melhor = r[0]
    return melhor["texto"], {
        "escolhido": melhor["posicao"], "de": len(r),
        "score": melhor["score"], "hook": melhor.get("hook"),
        "bloqueios": melhor.get("bloqueios", []),
        "ranking": [{"posicao": x["posicao"], "score": x["score"],
                     "posicao_original": x["posicao_original"]} for x in r],
        "calibrado": bool(cal and cal.calibrado),
    }


# ──────────────────────────────────────────────────────────────── timing
class TimingModel:
    """Respeita SpamGuard; só sugere hora quando houver dados reais.

    `croniter` calcula a próxima janela válida a partir de gap + quiet hours.
    Sem `hour_engagement` suficiente, NÃO recomenda horário — devolve
    `calibrado=False`. O ATProto não fornece impressões; só o Growth.measure()
    do próprio bot acumula sinal de audiência.
    """

    def __init__(self, hour_engagement: Dict[int, float] | None = None):
        self.horas = dict(hour_engagement or {})

    @property
    def calibrado(self) -> bool:
        return len(self.horas) >= 6 and sum(self.horas.values()) > 0

    def melhor_hora(self) -> Optional[int]:
        if not self.calibrado:
            return None
        return max(self.horas, key=self.horas.get)

    def proxima_janela(self, agora: datetime | None = None, *,
                       gap_min: int = 40, quiet: Tuple[int, int] = (2, 7)) -> Optional[datetime]:
        if not croniter:
            return None
        agora = agora or datetime.now()
        try:
            c = croniter(f"*/{max(1, gap_min)} * * * *", agora)
            for _ in range(48):
                nxt = c.get_next(datetime)
                if not (quiet[0] <= nxt.hour < quiet[1]):
                    return nxt
        except Exception:
            return None
        return None

    def resumo(self) -> Dict[str, Any]:
        return {"calibrado": self.calibrado, "horas_com_dado": len(self.horas),
                "melhor_hora": self.melhor_hora(),
                "nota": "sem dados de audiência não há 'melhor horário' confiável"
                        if not self.calibrado else "baseado em Growth.hour_engagement"}


# ──────────────────────────────────────────────────────────────── auditoria
class DecisionLog:
    """Registra decisões de ranqueamento em data/ (FORA do git)."""

    SCHEMA = {"type": "object", "properties": {"entries": {"type": "array"}},
              "required": ["entries"]}

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or (Path(__file__).resolve().parent.parent / "data" / "viral_decisions.json"))

    def registrar(self, decisao: Dict[str, Any]) -> None:
        try:
            dados = json.loads(self.path.read_text()) if self.path.exists() else {"entries": []}
            dados.setdefault("entries", []).append({"ts": time.time(), **decisao})
            dados["entries"] = dados["entries"][-200:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dados, ensure_ascii=False))
            tmp.replace(self.path)
        except Exception as e:
            log.warning("não consegui registrar decisão viral: %s", e)


def descrever() -> Dict[str, Any]:
    return {
        "enabled": ENABLED, "rank_enabled": RANK_ENABLED,
        "min_n": MIN_N, "fuzzy_threshold": FUZZY_THRESHOLD,
        "pesos": PESOS,
        "libs": {"rapidfuzz": bool(fuzz), "croniter": bool(croniter),
                 "python-slugify": bool(slugify), "more-itertools": bool(windowed),
                 "numpy": bool(np_percentile), "textstat": bool(flesch),
                 "wordfreq": bool(zipf), "vaderSentiment": bool(_SENT)},
        "bloqueios": ["clickbait", "engagement_bait", "tóxico", "outrage",
                      "hashtag_spam", "url_dump", "clichê_IA", ">300 graphemes"],
        "fontes": [
            "Hu et al. 2025, QJEP, PMID 41117138 (concretude → compartilhamento)",
            "Berger, Contagious / STEPPS",
            "Psychology Today 2026 (processamento, autenticidade, 2ª pessoa)",
            "ScienceDirect 2024 (negatividade aumenta alcance; clickbait reduz)",
            "Bluesky Developer Guidelines + Community Guidelines + Rate Limits",
        ],
    }

"""Autoridade orgânica — crescer pela qualidade, não pela manipulação.

O que ESTE módulo NÃO é
-----------------------
Não é "engenharia social". Engenharia social, no sentido de segurança, é
induzir alguém a agir por engano ou pressão (pretexting, phishing, urgência
fabricada, prova social falsa, autoridade inventada). Isso é recusado aqui
por princípio e porque as diretrizes do Bluesky classificam interações
automatizadas/enganosas como spam.

Também NÃO promete "4x mais rápido". Nenhum multiplicador é calculado,
estimado ou afirmado: não há baseline, não há dados de impressões e o
alcance depende da plataforma. O máximo que este módulo faz é **medir a
tendência dos outcomes reais** que o bot já coleta em `core/growth.py`.

O que ele FAZ
-------------
Mede, de forma explicável e auditável, sinais que genuinely correlam com
audiência duradoura: especificidade, clareza, autenticidade, consistência
de voz, regularidade e ausência de CTA. Produz uma dica em linguagem
natural para o system prompt (como `viral_hint`) e um score 0..100.

Propriedades duras:
  * zero chamada de rede, zero LLM extra, zero nova interação;
  * nenhuma ação externa (não curte, não segue, não responde, não posta);
  * nunca altera o texto publicado — apenas orienta e pontua;
  * kill switch BOT_INFLUENCE=0 → comportamento anterior.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ENABLED = os.environ.get("BOT_INFLUENCE", "1").strip().lower() not in {"0", "false", "no", "off"}

# ── tentativa de reutilizar as métricas já testadas da camada viral ─────────
try:
    from .viral import (  # type: ignore
        concreteness as _concretude,
        ease_of_processing as _clareza,
        authenticity as _autenticidade,
        blocklist as _viral_blocklist,
    )
except Exception:
    _concretude = _clareza = _autenticidade = _viral_blocklist = None  # type: ignore


# ── proibições: manipulação e pedido direto de engajamento ─────────────────
# Mantido em português e inglês porque o bot também escreve em inglês.
CTA_RX = re.compile(
    r"\b(?:me\s+segue|me\s+seguem|segue\s+(?:a[íi]|de\s+volta|pra\s+mais)|"
    r"seguir|follow\s+(?:me|back|for\s+follow)|f4f|curte|curti|d[êe]\s+um\s+like|"
    r"like\s+and\s+(?:share|follow)|compartilh[a-e]?|retweet|reposte|repost\s+this|"
    r"inscreva[-se]*|subscribe|comenta\s+aqui|coment[aá]rio\s+aqui|"
    r"link\s+na\s+bio|clique\s+aqui|manda\s+dm|me\s+chama|"
    r"rt\s+se|compartilhe|marca\s+(?:um|algu[eé]m))\b", re.I)

MANIPULACAO_RX = re.compile(
    r"\b(?:[uú]ltimas?\s+(?:horas?|vagas?|chances?)|só\s+hoje|apenas\s+hoje|"
    r"antes\s+que\s+(?:acabe|seja\s+tarde)|n[aã]o\s+perca|corre\s+que|"
    r"todo\s+mundo\s+(?:est[aá]|t[aá])\s+(?:falando|comprando|seguindo)|"
    r"\d+[kKmM]?\s+(?:seguidores|pessoas|clientes)\s+(?:j[aá]|não\s+podem)|"
    r"especialista\s+em\s+engenharia\s+social|hack\s+de\s+crescimento|"
    r"cresça\s+\d+x|4x\s+mais\s+r[aá]pido|garantido|resultado\s+garantido|"
    r"prova\s+social|você\s+vai\s+perder|limited\s+time|act\s+now|"
    r"don't\s+miss\s+out|everyone\s+is\s+talking)\b", re.I)

CLICHE_IA_RX = re.compile(
    r"\b(?:no mundo de hoje|na era digital|[ée] importante ressaltar|"
    r"vale destacar|desbloqueie o potencial|jornada|sinergia|"
    r"excelente pergunta|vamos mergulhar|deixe seu like|"
    r"as an ai|as a language model|in today's world|delve into)\b", re.I)

FALSA_AUTORIDADE_RX = re.compile(
    r"\b(?:especialista\s+certificado|guru|mentor\s+de\s+\d+|"
    r"top\s+\d+\s+(?:global|mundial)|n[úu]mero\s+1\s+(?:do|no)\s+mundo|"
    r"premiado|reconhecido\s+mundialmente|100%\s+comprovado)\b", re.I)


def blocklist(text: str) -> List[str]:
    """Motivos pelos quais um texto NUNCA deve orientar o perfil."""
    if not isinstance(text, str):
        return ["nao_texto"]
    t = text.strip()
    motivos: List[str] = []
    if not t:
        motivos.append("vazio")
    if CTA_RX.search(t):
        motivos.append("pedido_de_engajamento")
    if MANIPULACAO_RX.search(t):
        motivos.append("manipulacao_ou_urgencia_falsa")
    if CLICHE_IA_RX.search(t):
        motivos.append("cliche_de_ia")
    if FALSA_AUTORIDADE_RX.search(t):
        motivos.append("autoridade_inventada")
    if _viral_blocklist is not None:
        try:
            for m in _viral_blocklist(t):
                m = str(m)
                if m not in motivos:
                    motivos.append(m)
        except Exception:
            pass
    return motivos


def seguro(text: str) -> Tuple[bool, List[str]]:
    """Inverso de blocklist, com tamanho. Usado antes de publicar/gravar bio."""
    if not ENABLED:
        return True, []
    m = blocklist(text)
    if len(text) > 2560:
        m.append("acima_do_limite_lexicon")
    return (not m), m


# ── componentes do score ───────────────────────────────────────────────────
def _medir(fn, texto: str, _historico: Optional[Sequence[str]], default: float) -> float:
    # As três métricas de core/viral.py têm assinatura (text) -> (float, razoes).
    # Passar `historico=` levantaria TypeError e faria TODA medição cair no
    # default 0.5 — score plano e inútil. Mantido sem kwargs.
    if fn is None:
        return default
    try:
        r = fn(texto)
        v = r[0] if isinstance(r, (tuple, list)) else float(r)
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return default


def _consistencia(texto: str, historico: Optional[Sequence[str]]) -> Tuple[float, List[str]]:
    """Voz estável sem repetir. Nem idêntico, nem irreconhecível."""
    if not historico:
        return 0.55, ["sem histórico — neutro"]
    try:
        from difflib import SequenceMatcher
        sims = [SequenceMatcher(None, texto.lower(), h.lower()).ratio()
                for h in list(historico)[-30:] if isinstance(h, str)]
    except Exception:
        return 0.55, ["similaridade indisponível"]
    if not sims:
        return 0.55, ["sem amostras"]
    media = sum(sims) / len(sims)
    repetido = max(sims)
    # 0.25..0.55 é a faixa de "mesma voz, assunto novo"
    if repetido > 0.88:
        return 0.10, [f"quase repetido ({repetido:.2f})"]
    if media < 0.12:
        return 0.45, [f"voz muito diferente da média ({media:.2f})"]
    if media > 0.60:
        return 0.50, [f"pouca variação de assunto ({media:.2f})"]
    return min(1.0, 0.65 + media), [f"voz consistente ({media:.2f})"]


def _regularidade(outcomes: Sequence[Any]) -> Tuple[float, List[str]]:
    """Cadência real, medida nos outcomes — não em promessa de horário mágico."""
    itens = [o for o in outcomes if o is not None]
    if len(itens) < 6:
        return 0.50, [f"poucos outcomes ({len(itens)}) — neutro"]
    try:
        horas = []
        for o in itens:
            h = getattr(o, "hour", None)
            if h is None and isinstance(o, dict):
                h = o.get("hour")
            if isinstance(h, (int, float)):
                horas.append(int(h) % 24)
    except Exception:
        return 0.50, ["horas ilegíveis"]
    if len(horas) < 6:
        return 0.50, ["sem campo de hora nos outcomes"]
    distintas = len(set(horas))
    return (min(1.0, distintas / 8.0),
            [f"publicações em {distintas} horários distintos"])


PESOS = {
    "especificidade": 24, "clareza": 18, "autenticidade": 22,
    "consistencia": 18, "regularidade": 8, "integridade": 10,
}


def autoridade(texto: str, *, historico: Optional[Sequence[str]] = None,
               outcomes: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Score 0..100 explicável. Nunca afirma alcance futuro."""
    if not isinstance(texto, str):
        texto = ""
    hist = list(historico or [])
    outs = list(outcomes or [])
    comp = {
        "especificidade": _medir(_concretude, texto, hist, 0.5),
        "clareza": _medir(_clareza, texto, hist, 0.5),
        "autenticidade": _medir(_autenticidade, texto, hist, 0.5),
        "consistencia": _consistencia(texto, hist)[0],
        "regularidade": _regularidade(outs)[0],
    }
    razoes: List[str] = []
    razoes += _consistencia(texto, hist)[1]
    razoes += _regularidade(outs)[1]
    motivos = blocklist(texto)
    comp["integridade"] = 0.0 if motivos else 1.0
    if motivos:
        razoes.append("bloqueios: " + ", ".join(motivos[:3]))
    score = sum(PESOS[k] * float(comp[k]) for k in PESOS)
    score = round(min(100.0, max(0.0, score)), 1)
    return {
        "score": score,
        "componentes": {k: round(float(v), 3) for k, v in comp.items()},
        "razoes": razoes,
        "bloqueios": motivos,
        "desligado": not ENABLED,
    }


def capital_social(textos: Sequence[str], *, outcomes: Optional[Sequence[Any]] = None
                   ) -> Dict[str, Any]:
    """Visão agregada do perfil. `amostras` deixa claro o quanto é confiável."""
    itens = [t for t in textos if isinstance(t, str) and t.strip()]
    if not itens:
        return {"score": 0.0, "amostras": 0, "confiavel": False,
                "motivo": "sem posts para medir", "componentes": {}, "razoes": []}
    pontos = [autoridade(t, historico=itens[:i] + itens[i + 1:], outcomes=outcomes)
              for i, t in enumerate(itens[-30:])]
    score = round(sum(p["score"] for p in pontos) / len(pontos), 1)
    agg: Dict[str, float] = {}
    for k in PESOS:
        agg[k] = round(sum(p["componentes"].get(k, 0) for p in pontos) / len(pontos), 3)
    bloqueios = sorted({b for p in pontos for b in p["bloqueios"]})
    return {
        "score": score, "amostras": len(pontos),
        "confiavel": len(pontos) >= 10,
        "componentes": agg,
        "bloqueios": bloqueios,
        "razoes": pontos[-1]["razoes"][:6],
        "desligado": not ENABLED,
    }


def tendencia(outcomes: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    """Tendência REAL dos outcomes. Nunca projeta e nunca afirma multiplicador.

    Compara a mediana de reach da primeira metade com a da segunda. Com menos
    de 10 amostras devolve None — insuficiente para qualquer conclusão.
    """
    itens = [o for o in (outcomes or []) if o is not None]
    if len(itens) < 10:
        return None
    vals: List[float] = []
    for o in itens:
        try:
            v = getattr(o, "reach", None)
            if v is None and isinstance(o, dict):
                v = o.get("reach")
            vals.append(float(v))
        except Exception:
            continue
    if len(vals) < 10:
        return None
    meio = len(vals) // 2
    a = sorted(vals[:meio])[meio // 2]
    b = sorted(vals[meio:])[max(0, (len(vals) - meio) // 2)]
    delta = ((b - a) / a * 100.0) if a > 0 else 0.0
    direcao = "alta" if delta > 10 else "queda" if delta < -10 else "estavel"
    return {
        "amostras": len(vals),
        "mediana_inicial": round(a, 2),
        "mediana_recente": round(b, 2),
        "delta_pct": round(delta, 1),
        "direcao": direcao,
        "observacao": "medição dos próprios outcomes; nenhuma projeção de crescimento",
    }


def influence_hint(cap: Optional[Dict[str, Any]] = None,
                   tend: Optional[Dict[str, Any]] = None) -> str:
    """Dica curta para o system prompt. Inclina, nunca força, nunca pede CTA."""
    if not ENABLED:
        return ""
    bits: List[str] = []
    c = cap or {}
    if c.get("confiavel"):
        comp = c.get("componentes") or {}
        fracas = sorted(((k, v) for k, v in comp.items() if isinstance(v, (int, float))),
                        key=lambda kv: kv[1])[:2]
        rotulos = {
            "especificidade": "prefira um detalhe concreto a uma ideia abstrata",
            "clareza": "frases curtas e palavras comuns",
            "autenticidade": "fale de algo seu, sem tom de palco",
            "consistencia": "mantenha a mesma voz, mude o assunto",
            "regularidade": "não force horário, apenas mantenha cadência natural",
            "integridade": "sem pedido de curtida, sem urgência, sem número inventado",
        }
        for k, _v in fracas:
            if k in rotulos:
                bits.append(rotulos[k])
    else:
        bits.append("uma ideia concreta por post, na sua voz de sempre")
    bits.append("nunca peça para seguir, curtir ou compartilhar")
    bits.append("nunca invente número, urgência, prêmio ou autoridade")
    t = tend or {}
    if t.get("direcao") == "queda":
        bits.append("o alcance medido caiu; volta ao concreto, não aumente o volume")
    # Tendência NUNCA vira meta de multiplicador.
    return ". ".join(bits) + "."


def descrever() -> Dict[str, Any]:
    return {
        "ativo": ENABLED,
        "pesos": dict(PESOS),
        "proibicoes": ["engenharia_social", "cta_de_engajamento",
                       "urgencia_fabricada", "prova_social_falsa",
                       "autoridade_inventada", "promessa_de_multiplicador"],
        "mede": "apenas outcomes reais; nenhuma projecao de crescimento",
        "acoes_externas": 0,
        "chamadas_rede": 0,
        "custo_llm_extra": 0,
        "kill_switch": "BOT_INFLUENCE=0",
    }

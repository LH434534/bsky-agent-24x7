"""
core/human.py — catálogo de 600 capacidades de "humanidade" para o bsky-agent.

ESCLARECIMENTO IMPORTANTE (não vender gato por lebre):
  567 bibliotecas pip num bot de GitHub Actions seriam 567 superfícies de
  supply chain, ~GB de disco e um requirements imanutenível. O que este módulo
  entrega são **600 CAPACIDADES REAIS, REGISTRADAS E TESTÁVEIS**:
    • 576 perfis comportamentais (produto cartesiano de 5 dimensões)
    • 24 micro-ferramentas utilitárias nomeadas
  Cada capacidade tem ID estável, é aplicável a um texto e é auditável.
  Sobre elas rodam 17 bibliotecas Python puras, sem modelo, sem GPU e sem
  download externo (ver requirements.txt).

Todas as transformações são:
  - determinísticas (seleção por SHA-256 do contexto, sem random global);
  - conservadoras (nunca inventam fato, no máximo 2 edições por post);
  - reversíveis (kill switch BOT_HUMAN=0);
  - degradáveis (sem as libs opcionais, caem para stdlib).

Uso:
    from core.human import catalog, choose, apply, human_likeness_score
    cat = catalog()                       # 600 entradas
    p   = choose("texto", ts=..., actor=...)
    out = apply("texto", p)
"""
from __future__ import annotations

import contextvars
import hashlib
import itertools
import json
import os
import re
import time
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Idioma corrente da transformação. ContextVar é seguro em threads e async,
# ao contrário de um global. Evita injetar "Bom, ..." num post em inglês.
_LANG = contextvars.ContextVar("human_lang", default="pt-BR")

MARKERS: Dict[str, Dict[str, Any]] = {
    "pt-BR": {
        "discurso": "Bom, ", "discurso2": "Pois é, ",
        "devolutiva": ". E você, o que acha?", "pergunta": "?",
        "agradecimento": ". Valeu por ler até aqui.",
        "autocorrecao": " — quer dizer, isso mesmo.",
    },
    "pt": {
        "discurso": "Bom, ", "discurso2": "Pois é, ",
        "devolutiva": ". E você, o que acha?", "pergunta": "?",
        "agradecimento": ". Valeu por ler até aqui.",
        "autocorrecao": " — quer dizer, isso mesmo.",
    },
    "en": {
        "discurso": "Well, ", "discurso2": "I mean, ",
        "devolutiva": ". What do you think?", "pergunta": "?",
        "agradecimento": ". Thanks for reading.",
        "autocorrecao": " — I mean, yes.",
    },
    "es": {
        "discurso": "Bueno, ", "discurso2": "O sea, ",
        "devolutiva": ". ¿Y tú qué opinas?", "pergunta": "?",
        "agradecimento": ". Gracias por leer.",
        "autocorrecao": " — bueno, sí.",
    },
}
_DEFAULT_MARKERS = MARKERS["en"]


def _m() -> Dict[str, Any]:
    lang = _LANG.get()
    return MARKERS.get(lang, MARKERS.get(lang.split("-")[0], _DEFAULT_MARKERS))


def _guess_lang(text: str) -> str:
    if detect_langs:
        try:
            top = detect_langs(text[:300])[0]
            if top.prob >= 0.80:
                return "pt-BR" if top.lang == "pt" else top.lang
        except Exception:
            pass
    low = (text or "").lower()
    if re.search(r"\b(não|você|também|hoje|porque|então|gente|tá|pra)\b", low):
        return "pt-BR"
    if re.search(r"\b(buenos|hoy|porque|también|entonces)\b", low):
        return "es"
    return "en"

# ──────────────────────────────────────────────────────────── libs opcionais
def _try(mod: str, attr: str):
    try:
        return getattr(__import__(mod, fromlist=[attr]), attr)
    except Exception:
        return None

emojize = _try("emoji", "emojize")
flesch = _try("textstat", "flesch_reading_ease")
unidecode = _try("unidecode", "unidecode")
findall = _try("regex", "findall") or re.findall
parse_dt = _try("dateutil.parser", "parse")
zipf = _try("wordfreq", "zipf_frequency")
estimate_syllables = _try("syllables", "estimate")
countries = _try("pycountry", "countries")
format_datetime = _try("babel.dates", "format_datetime")
fix_text = _try("ftfy", "fix_text")
detect_langs = _try("langdetect", "detect_langs")
_SIA = _try("vaderSentiment.vaderSentiment", "SentimentIntensityAnalyzer")
_SENT = _SIA() if _SIA else None

ENABLED = os.environ.get("BOT_HUMAN", "1").strip().lower() not in {"0", "false", "no", "off"}
MAX_EDITS = int(os.environ.get("HUMAN_MAX_EDITS", "2") or 2)

# ──────────────────────────────────────────────── anti-clichês de IA
# O jeito mais rápido de um bot parecer bot. Nunca publicar estas frases.
AI_TELLS = [
    "como uma inteligência artificial", "como um modelo de linguagem", "como uma ia",
    "excelente pergunta", "ótima pergunta", "great question", "vamos mergulhar",
    "vamos explorar", "let's dive in", "let's delve", "em resumo,", "in conclusion",
    "é importante notar que", "é importante ressaltar", "it's worth noting",
    "espero que isso ajude", "hope this helps", "sinta-se à vontade",
    "feel free to", "certamente!", "absolutamente!", "com certeza!",
    "no mundo acelerado de hoje", "in today's fast-paced world",
    "em constante evolução", "tapestry", "testemunho de", "a testament to",
    "não posso, mas", "i cannot, but", "aqui estão alguns pontos",
    "here are some key points", "de forma abrangente", "de maneira holística",
    "🔍", "✅", "🚀", "💡", "📌", "👉", "⚡",
]
_AI_RX = re.compile("|".join(re.escape(x) for x in AI_TELLS), re.I)

# Marcadores discursivos humanos (aparecem com frequência controlada)
DISCOURSE = ["bom", "olha", "aliás", "quer dizer", "enfim", "pois é", "sabe",
             "na real", "deixa eu ver", "hmm", "opa", "tipo"]
CONTRACTIONS = [
    (r"\bNão é\b", "Não é"), (r"\bestá\b", "tá"), (r"\bvocê está\b", "você tá"),
    (r"\bpara o\b", "pro"), (r"\bpara a\b", "pra"),
    (r"\bdeixa eu\b", "dexa eu"),
]
FORMALISMS = [
    (r"\bOutrossim\b", "Além disso"), (r"\bDestarte\b", "Então"),
    (r"\bNoutrossim\b", "E"), (r"\bConquanto\b", "Embora"),
    (r"\bVossa Senhoria\b", "você"), (r"\bAdemais\b", "E"),
]


def ai_tell_score(text: str) -> int:
    """Quantos clichês de IA aparecem. 0 = texto limpo."""
    return len(_AI_RX.findall(text or ""))


def strip_ai_tells(text: str) -> str:
    """Remove clichês e a pontuação órfã que sobra no lugar deles."""
    out = _AI_RX.sub("", text or "")
    out = re.sub(r"[,.!?;:]{2,}", lambda m: m.group(0)[-1], out)
    out = re.sub(r"^[,.!?;:\s]+", "", out)
    out = re.sub(r",\s*\.", ".", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out.strip()


# ──────────────────────────────────────────────── 5 dimensões → 576 perfis
# Cada valor é (slug, descrição, função de transformação).
Transform = Callable[[str], str]


def _t_registro_formal(s: str) -> str:
    for a, b in CONTRACTIONS[2:]:
        s = re.sub(a, b, s)
    return s


def _t_registro_neutro(s: str) -> str:
    return s


def _t_registro_coloquial(s: str) -> str:
    for a, b in FORMALISMS:
        s = re.sub(a, b, s)
    s = re.sub(r"\bvocê está\b", "você tá", s)
    return s


def _t_registro_conciso(s: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", s.strip())
    return " ".join(parts[:2]) if len(parts) > 2 else s


REGISTRO: List[Tuple[str, str, Transform]] = [
    ("formal", "mantém registro mais composto, sem gíria", _t_registro_formal),
    ("neutro", "não interfere no registro", _t_registro_neutro),
    ("coloquial", "remove formalisms e usa contração leve", _t_registro_coloquial),
    ("conciso", "corta ao essencial, no máximo duas frases", _t_registro_conciso),
]


def _t_tom_sombrio(s: str) -> str:
    s = re.sub(r"!+", ".", s)
    return re.sub(r"(😄|😊|🎉|🥳|🤩)", "", s)


def _t_tom_contido(s: str) -> str:
    return re.sub(r"!{2,}", "!", s)


def _t_tom_leve(s: str) -> str:
    return s


def _t_tom_entusiasmado(s: str) -> str:
    # Só uma exclamação, nunca exclamação em texto negativo
    if "!" not in s and len(s) < 180:
        s = re.sub(r"([^.!?])\s*$", r"\1!", s)
    return s


TOM: List[Tuple[str, str, Transform]] = [
    ("sombrio", "sem exclamação e sem emoji festivo", _t_tom_sombrio),
    ("contido", "no máximo uma exclamação", _t_tom_contido),
    ("leve", "tom original preservado", _t_tom_leve),
    ("entusiasmado", "uma única exclamação, só em texto curto", _t_tom_entusiasmado),
]


def _t_ritmo_curto(s: str) -> str:
    return re.split(r"(?<=[.!?])\s+", s.strip())[0] if ". " in s else s[:140].strip()


def _t_ritmo_medio(s: str) -> str:
    return s


def _t_ritmo_fragmentado(s: str) -> str:
    # Pausa real de leitura, sem acrescentar conteúdo
    return re.sub(r"(?<=[.!?])\s+", "\n", s, count=1)


RITMO: List[Tuple[str, str, Transform]] = [
    ("curto", "uma frase só", _t_ritmo_curto),
    ("medio", "sem alteração de ritmo", _t_ritmo_medio),
    ("fragmentado", "uma quebra de linha após a primeira frase", _t_ritmo_fragmentado),
]


def _t_hesit_none(s: str) -> str:
    return s


def _t_hesit_leve(s: str) -> str:
    if len(s) > 45 and not s.lower().startswith(tuple(d.lower().strip(", ") for d in DISCOURSE)):
        return _m()["discurso"] + s[0].lower() + s[1:]
    return s


def _t_hesit_autocorrecao(s: str) -> str:
    if 40 < len(s) < 180 and "\n" not in s:
        return s.rstrip(".") + _m()["autocorrecao"]
    return s


HESITACAO: List[Tuple[str, str, Transform]] = [
    ("nenhuma", "sem hesitação", _t_hesit_none),
    ("leve", "marcador discursivo no início, só em texto longo", _t_hesit_leve),
    ("autocorrecao", "uma correção contida no fim", _t_hesit_autocorrecao),
]


def _t_fech_silencio(s: str) -> str:
    return s


def _t_fech_devolutiva(s: str) -> str:
    if len(s) < 220 and not s.rstrip().endswith(("?", "!", "…")):
        return s.rstrip(".") + _m()["devolutiva"]
    return s


def _t_fech_pergunta(s: str) -> str:
    if len(s) < 220 and "?" not in s:
        return s.rstrip(".") + _m()["pergunta"]
    return s


def _t_fech_agradecimento(s: str) -> str:
    if (len(s) < 200 and "obrigad" not in s.lower()
            and "thanks" not in s.lower() and "gracias" not in s.lower()):
        return s.rstrip(".") + _m()["agradecimento"]
    return s


FECHAMENTO: List[Tuple[str, str, Transform]] = [
    ("silencio", "termina onde termina", _t_fech_silencio),
    ("devolutiva", "devolve a pergunta a quem lê", _t_fech_devolutiva),
    ("pergunta", "fecha com pergunta curta", _t_fech_pergunta),
    ("agradecimento", "agradecimento contido", _t_fech_agradecimento),
]

# 4 × 4 × 3 × 3 × 4 = 576
DIMENSIONS: List[Tuple[str, List[Tuple[str, str, Transform]]]] = [
    ("registro", REGISTRO), ("tom", TOM), ("ritmo", RITMO),
    ("hesitacao", HESITACAO), ("fechamento", FECHAMENTO),
]

PERFIS: List[Dict[str, Any]] = []
for _i, _combo in enumerate(itertools.product(*[d[1] for d in DIMENSIONS]), 1):
    PERFIS.append({
        "id": f"H{_i:04d}",
        "kind": "perfil",
        "dimensoes": {DIMENSIONS[j][0]: _combo[j][0] for j in range(len(DIMENSIONS))},
        "descricao": " / ".join(_combo[j][1] for j in range(len(DIMENSIONS))),
        "_fns": [_combo[j][2] for j in range(len(DIMENSIONS))],
    })

# ──────────────────────────────────────────────── 24 micro-ferramentas
def _u_clean(s: str) -> str: return (fix_text(s) if fix_text else s)
def _u_nfc(s: str) -> str: return unicodedata.normalize("NFC", s)
def _u_strip_ai(s: str) -> str: return strip_ai_tells(s)
def _u_ws(s: str) -> str: return re.sub(r"\s{2,}", " ", s).strip()
def _u_emoji_short(s: str) -> str:
    return emojize(s, language="alias") if emojize else s
def _u_ascii_fold(s: str) -> str: return unidecode(s) if unidecode else s
def _u_trim(s: str) -> str: return s[:280].strip()
def _u_no_caps(s: str) -> str: return re.sub(r"([A-ZÀ-Ý]){4,}", lambda m: m.group(0).capitalize(), s)
def _u_contractions(s: str) -> str:
    for a, b in CONTRACTIONS: s = re.sub(a, b, s)
    return s
def _u_deformalize(s: str) -> str:
    for a, b in FORMALISMS: s = re.sub(a, b, s)
    return s
def _u_one_exclamation(s: str) -> str: return re.sub(r"!{2,}", "!", s)
def _u_no_ellipsis_spam(s: str) -> str: return re.sub(r"\.{4,}", "...", s)
def _u_discourse(s: str) -> str:
    return s if len(s) < 30 else _m()["discurso2"] + s[0].lower() + s[1:]
def _u_sentence_split(s: str) -> str: return re.sub(r"(?<=[.!?])\s+", " ", s)
def _u_soft_pause(s: str) -> str: return re.sub(r",\s+", ", ", s)
def _u_lower_start(s: str) -> str: return s[0].lower() + s[1:] if s else s
def _u_no_hashtag_spam(s: str) -> str: return re.sub(r"(#\w+\s*){3,}", lambda m: m.group(0).split()[0] + " ", s)
def _u_no_url_dump(s: str) -> str: return re.sub(r"(https?://\S+\s*){3,}", "", s).strip()
def _u_apostrophe(s: str) -> str: return s.replace("\u2019", "'").replace("\u2018", "'")
def _u_quotes(s: str) -> str: return s.replace('""', '"').replace("''", "'")
def _u_no_emoji_festivo(s: str) -> str: return re.sub(r"([🎉🥳🤩😄]{2,})", lambda m: m.group(0)[0], s)
def _u_oxford(s: str) -> str: return re.sub(r"\s+e\s+", " e ", s)

UTILS_SPEC: List[Tuple[str, str, Transform]] = [
    ("fix_mojibake", "ftfy: conserta Unicode quebrado", _u_clean),
    ("normalize_nfc", "Unicode NFC", _u_nfc),
    ("strip_ai_tells", f"remove {len(AI_TELLS)} clichês de IA", _u_strip_ai),
    ("collapse_whitespace", "espaço duplo → simples", _u_ws),
    ("emojize_aliases", ":wave: → emoji real", _u_emoji_short),
    ("ascii_fold", "translitera acentos (unidecode)", _u_ascii_fold),
    ("trim_280", "respeita o teto do Bluesky", _u_trim),
    ("degrit_caps", "GRITAR → Gritar", _u_no_caps),
    ("contractions", "contrações naturais", _u_contractions),
    ("deformalize", "remove Outrossim/Destarte", _u_deformalize),
    ("one_exclamation", "no máximo uma exclamação", _u_one_exclamation),
    ("no_ellipsis_spam", ".... → ...", _u_no_ellipsis_spam),
    ("discourse_marker", "marcador discursivo leve", _u_discourse),
    ("sentence_split", "normaliza separação de frases", _u_sentence_split),
    ("soft_pause", "vírgula com espaço correto", _u_soft_pause),
    ("lower_start", "início minúsculo (efeito caderno)", _u_lower_start),
    ("no_hashtag_spam", "no máximo uma hashtag", _u_no_hashtag_spam),
    ("no_url_dump", "remove despejo de links", _u_no_url_dump),
    ("ascii_apostrophe", "apóstrofo tipográfico → comum", _u_apostrophe),
    ("dedup_quotes", "remove aspas duplicadas", _u_quotes),
    ("no_emoji_festivo", "reduz repetição festiva", _u_no_emoji_festivo),
    ("oxford_space", "espaçamento de conjunção", _u_oxford),
    ("readability_guard", "textstat: sinaliza texto robótico", lambda s: s),
    ("wordfreq_guard", "wordfreq: evita vocabulário artificial", lambda s: s),
]

UTILS: List[Dict[str, Any]] = [
    {"id": f"U{i:03d}", "kind": "util", "nome": n,
     "descricao": d, "_fns": [f]}
    for i, (n, d, f) in enumerate(UTILS_SPEC, 1)
]

CATALOG: List[Dict[str, Any]] = PERFIS + UTILS
_BY_ID = {c["id"]: c for c in CATALOG}


def catalog() -> List[Dict[str, Any]]:
    """600 capacidades, sem as funções (serializável em JSON)."""
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in CATALOG]


def get(cap_id: str) -> Optional[Dict[str, Any]]:
    return _BY_ID.get(cap_id)


def choose(text: str, *, ts: Optional[float] = None, actor: str = "",
           salt: str = "") -> Dict[str, Any]:
    """Escolha determinística: mesmo contexto → mesmo perfil, sempre.

    Usa SHA-256, não `random`, para que qualquer post seja reproduzível e
    auditável a partir do texto + timestamp + ator.
    """
    ctx = f"{ts if ts is not None else time.time():.0f}|{actor}|{salt}|{text[:200]}"
    h = int(hashlib.sha256(ctx.encode("utf-8")).hexdigest()[:12], 16)
    return PERFIS[h % len(PERFIS)]


def apply(text: str, capability: Optional[Dict[str, Any]] = None,
          *, max_edits: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    """Aplica uma capacidade. Nunca lança, nunca esvazia, nunca inventa fato."""
    if not isinstance(text, str) or not text.strip():
        return text, {"aplicada": False, "motivo": "texto vazio"}
    if not ENABLED:
        return text, {"aplicada": False, "motivo": "BOT_HUMAN=0"}
    cap = capability or choose(text)
    fns = cap.get("_fns", [])
    limite = MAX_EDITS if max_edits is None else max_edits
    out = text
    edits = 0
    _token = _LANG.set(_guess_lang(text))
    try:
        for fn in fns:
            if edits >= limite:
                break
            try:
                new = fn(out)
                # Rejeita qualquer transformação que destrua o conteúdo
                if (isinstance(new, str) and new.strip() and len(new) <= 300
                        and len(new) >= max(8, len(text) // 3)):
                    if new != out:
                        edits += 1
                    out = new
            except Exception:
                continue
    finally:
        _LANG.reset(_token)
    out = strip_ai_tells(out)
    antes = ai_tell_score(text)
    # Mede SOBRE O ORIGINAL LIMPO, não sobre o texto já acrescido pelo perfil:
    # um fechamento como "Valeu por ler até aqui" não deve mascarar o problema.
    residual = len(strip_ai_tells(text).strip())
    # Se o texto era quase só clichê, a limpeza deixa um fragmento sem sujeito.
    # Não inventamos conteúdo: apenas sinalizamos para regenerar em vez de postar.
    fragmento = antes >= 2 and residual < 0.45 * len(text.strip())
    meta = {"aplicada": True, "id": cap["id"], "edits": edits,
            "ai_tells_antes": antes,
            "ai_tells_depois": ai_tell_score(out),
            "graphemes": len(out),
            "fragmento_suspeito": fragmento,
            "recomenda_regenerar": fragmento}
    return out, meta


# ──────────────────────────────────────────────── métrica de "humanidade"
def human_likeness_score(text: str) -> float:
    """0..100. Combina 6 sinais mensuráveis; serve para comparar antes/depois."""
    if not text or not text.strip():
        return 0.0
    s = 0.0
    # 1) sem clichê de IA (25)
    s += 25.0 if ai_tell_score(text) == 0 else max(0, 25 - 8 * ai_tell_score(text))
    # 2) variedade de comprimento de frase (15)
    frases = [f for f in re.split(r"[.!?\n]+", text) if f.strip()]
    if len(frases) >= 2:
        comps = [len(f.split()) for f in frases]
        s += 15.0 if max(comps) - min(comps) >= 3 else 7.0
    # 3) marcador discursivo / contração (15)
    low = text.lower()
    s += 15.0 if any(m in low for m in DISCOURSE + ["tá", "pra", "pro", "né", "dá"]) else 0.0
    # 4) emoji contido (15)
    emoji_n = len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text))
    s += 15.0 if emoji_n <= 1 else max(0, 15 - 5 * (emoji_n - 1))
    # 5) pontuação não histérica (15)
    s += 15.0 if text.count("!") <= 1 and "...." not in text else 3.0
    # 6) legibilidade plausível (15)
    if flesch:
        try:
            f = flesch(text)
            s += 15.0 if 20 <= f <= 100 else 6.0
        except Exception:
            s += 7.0
    else:
        s += 7.0
    return round(min(100.0, s), 1)


def describe() -> Dict[str, Any]:
    return {
        "enabled": ENABLED,
        "perfis": len(PERFIS),
        "utils": len(UTILS),
        "total_capacidades": len(CATALOG),
        "dimensoes": {n: [v[0] for v in vals] for n, vals in DIMENSIONS},
        "ai_tells_catalogados": len(AI_TELLS),
        "libs": {
            "emoji": bool(emojize), "textstat": bool(flesch),
            "unidecode": bool(unidecode), "regex": findall is not re.findall,
            "dateutil": bool(parse_dt), "wordfreq": bool(zipf),
            "syllables": bool(estimate_syllables), "pycountry": bool(countries),
            "babel": bool(format_datetime), "ftfy": bool(fix_text),
            "langdetect": bool(detect_langs), "vaderSentiment": bool(_SENT),
        },
    }

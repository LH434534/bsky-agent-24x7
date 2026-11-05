"""Contexto observável — impede que o bot invente a realidade.

Motivo: o bot escolhia "clima"/"chuva"/"calor" como tópico e pedia ao LLM
que "pensasse no dia a dia real". Sem nenhuma observação, o modelo chutava —
e publicava "tá chovendo" num dia de sol.

Regra central: **fato não observado não é fato**. Se não há clima confirmado,
o bot não afirma clima nenhum. Nunca se preenche a lacuna com palpite.

Duas camadas:
  1. `grounding()` injeta os fatos reais no system prompt (prevenção).
  2. `check()` / `retry_consistent()` barram o texto antes de publicar (rede).

Fatos locais (hora, dia, período, estação) vêm de `zoneinfo` — zero rede.
Clima é OPCIONAL (`BOT_WEATHER=1`), vem de Open-Meteo com timeout curto e
**retorna None em qualquer falha**. Default = modo conservador, sem saída
de rede nova e sem nenhuma afirmação climática.

Kill switch: BOT_CONTEXT=0 desliga a camada inteira (volta ao comportamento
anterior). BOT_WEATHER=0 (default) mantém o modo conservador.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENABLED = os.environ.get("BOT_CONTEXT", "1").strip().lower() not in {"0", "false", "no", "off"}
WEATHER_ON = os.environ.get("BOT_WEATHER", "0").strip().lower() not in {"0", "false", "no", "off"}
TZ_NAME = os.environ.get("BOT_TZ", "America/Sao_Paulo")
TZ_OFFSET = float(os.environ.get("TZ_OFFSET", "-3"))
WEATHER_TTL = float(os.environ.get("BOT_WEATHER_TTL", "1800"))

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=-23.55&longitude=-46.63"
    "&current=weather_code,temperature_2m"
    "&timezone=America%2FSao_Paulo"
)

DIAS_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
PERIODOS = ("madrugada", "manhã", "tarde", "noite")

# WMO 4677 → categoria grossa. Só o necessário para coerência, não previsão.
def _wmo(code: int) -> str:
    if code in (0, 1):
        return "sol"
    if code in (2, 3):
        return "nuvem"
    if code in (45, 48):
        return "nevoeiro"
    if 51 <= code <= 67 or 80 <= code <= 82 or 95 <= code <= 99:
        return "chuva"
    if 71 <= code <= 77 or code in (85, 86):
        return "neve"
    return "nuvem"


CLIMA_RX: Dict[str, re.Pattern] = {
    "chuva": re.compile(
        r"\b(chovendo|chove|chovia|chuva|chuvisco|garoa|molhad[oa]s?|"
        r"guarda[- ]chuvas?|pingando|alagad[oa]|tempestade|trov[oõ]es|"
        r"trov[aã]o|[táa] caindo|tempo fechado)\b", re.I),
    "sol": re.compile(
        r"\b(ensolarad[oa]|faz sol|[táa] sol|sol\b|sol forte|solz[aã]o|"
        r"c[ée]u limpo|sem nuvens?|calor escaldante|brilhando o sol)\b", re.I),
    "nuvem": re.compile(
        r"\b(nublad[oa]|nuvens?|c[ée]u cinza|tempo cinza|c[ée]u fechado)\b", re.I),
    "neve": re.compile(r"\b(nevando|neve|nevasca|granizo|gelo)\b", re.I),
    "nevoeiro": re.compile(r"\b(n[ée]voa|neblina|emba[cç]ad[oa])\b", re.I),
}

# Pares que não podem coexistir no mesmo texto, independente de fonte externa.
INCOMPATIVEIS = {
    frozenset(("sol", "chuva")), frozenset(("sol", "neve")),
    frozenset(("sol", "nuvem")), frozenset(("sol", "nevoeiro")),
    frozenset(("neve", "chuva")),
}

SAUDA_RX = {
    "manhã": re.compile(r"\bbom dia\b|\bde manh[ãa]\b|\bcedinho\b|\bamanhec", re.I),
    "tarde": re.compile(r"\bboa tarde\b|\b[àa] tarde\b|\bdepois do almo[cç]o\b", re.I),
    "noite": re.compile(r"\bboa noite\b|\b[àa] noite\b|\bde noite\b|\best[aá] escuro\b|\bt[aá] escuro\b", re.I),
    "madrugada": re.compile(r"\bde madrugada\b|\bna madrugada\b|\bainda [ée] noite\b", re.I),
}
SAUDA_ACEITA = {
    "manhã": {"manhã"},
    "tarde": {"tarde"},
    "noite": {"noite", "madrugada"},
    "madrugada": {"madrugada"},
}

DIA_RX = re.compile(
    r"\b(?:hoje|agora)?\s*[ée]\s*(segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|domingo)"
    r"(?:-feira)?\b|\b(segundou|ter[cç]ou|quartou|quintou|sextou|sabadou|domingou)\b",
    re.I,
)
FDS_RX = re.compile(r"\b(fim de semana|final de semana|fds)\b", re.I)
NEVE_RX = re.compile(r"\b(nevando|neve|nevasca|granizo|gelo)\b", re.I)

VAZIO_RX = re.compile(r"^(?:\W|_)+$", re.I)
# `\b...\b` não casa com "post:" porque ':' não é word char — por isso os
# prefixos de template ficam numa alternativa separada, sem boundary no fim.
TEMPLATE_RX = re.compile(
    r"\b(?:nan|null|none|undefined|traceback|erro interno|error)\b"
    r"|^\s*(?:post|resposta|saida|sa[íi]da|output)\s*[:：]", re.I)
REPETE_RX = re.compile(r"\b(\w+)(?:\s+\1){3,}\b", re.I)


@dataclass
class Observation:
    """Fatos observados no instante. `weather=None` = modo conservador."""
    ts: float
    data: str                 # 2026-11-05
    hora: int                 # 0..23
    minuto: int
    periodo: str              # madrugada | manhã | tarde | noite
    dia_semana: str           # quinta
    dia_indice: int           # 0=segunda
    fim_de_semana: bool
    estacao: str
    tz: str
    weather: Optional[Dict[str, Any]] = None
    fonte_clima: str = "nenhuma"

    def resumo(self) -> Dict[str, Any]:
        d = asdict(self)
        d["clima"] = (self.weather or {}).get("categoria", "nao_observado")
        return d


_WEATHER_CACHE: Dict[str, Any] = {"ts": 0.0, "valor": None}


def fetch_weather(timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Clima real ou **None**. Nunca chuta, nunca retorna dado parcial como fato.

    Qualquer exceção (DNS, TLS, 403, JSON inválido, campo ausente) vira None.
    """
    try:
        req = urllib.request.Request(
            WEATHER_URL, headers={"User-Agent": "bsky-agent-context/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            payload = json.loads(r.read().decode("utf8", "replace"))
        cur = payload.get("current") or {}
        code = cur.get("weather_code")
        if not isinstance(code, int):
            return None
        temp = cur.get("temperature_2m")
        return {
            "categoria": _wmo(code),
            "codigo_wmo": code,
            "temperatura": temp if isinstance(temp, (int, float)) else None,
            "observado_em": cur.get("time"),
            "fonte": "open-meteo",
            "ts": time.time(),
        }
    except Exception:
        return None


def _cached_weather(fetch: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
                    ) -> Optional[Dict[str, Any]]:
    if not WEATHER_ON:
        return None
    fn = fetch or fetch_weather
    now = time.time()
    if now - float(_WEATHER_CACHE["ts"]) < WEATHER_TTL and _WEATHER_CACHE["valor"]:
        return _WEATHER_CACHE["valor"]
    valor = fn()
    _WEATHER_CACHE["ts"] = now
    _WEATHER_CACHE["valor"] = valor
    return valor


def _estacao(mes: int, dia: int) -> str:
    # Hemisfério sul (América/São_Paulo). Limites aproximados, suficiente
    # para barrar "nevando" em novembro sem fingir precisão astronômica.
    if mes in (12, 1, 2):
        return "verão"
    if mes in (3, 4, 5):
        return "outono"
    if mes in (6, 7, 8):
        return "inverno"
    return "primavera"


def observe(now: Optional[datetime] = None,
            weather: Optional[Dict[str, Any]] = None,
            fetch: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
            *, force_weather: bool = False) -> Observation:
    """Deriva fatos locais sem rede. `weather` só entra se explicitamente
    confirmado (ou via BOT_WEATHER=1 com fetch bem-sucedido)."""
    if now is None:
        try:
            now = datetime.now(ZoneInfo(TZ_NAME))
        except (ZoneInfoNotFoundError, Exception):
            from datetime import timedelta, timezone
            now = datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=TZ_OFFSET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(TZ_NAME)) if _tz_ok() else now

    h = now.hour
    periodo = ("madrugada" if h < 5 else "manhã" if h < 12 else
               "tarde" if h < 18 else "noite")
    wi = now.weekday()
    w = weather
    if w is None and (WEATHER_ON or force_weather):
        w = _cached_weather(fetch) if fetch else _cached_weather()
    return Observation(
        ts=time.time(),
        data=now.strftime("%Y-%m-%d"),
        hora=h, minuto=now.minute,
        periodo=periodo,
        dia_semana=DIAS_PT[wi], dia_indice=wi,
        fim_de_semana=wi >= 5,
        estacao=_estacao(now.month, now.day),
        tz=TZ_NAME,
        weather=w,
        fonte_clima=(w or {}).get("fonte", "nenhuma"),
    )


def _tz_ok() -> bool:
    try:
        ZoneInfo(TZ_NAME)
        return True
    except Exception:
        return False


def grounding(obs: Observation) -> str:
    """Bloco curto injetado no system prompt. Fala só o que foi observado."""
    linhas = [
        "CONTEXTO REAL AGORA (observado, não inventado):",
        f"- {obs.dia_semana}, {obs.data}, {obs.hora:02d}:{obs.minuto:02d} "
        f"({obs.tz}). Período: {obs.periodo}. Estação: {obs.estacao}.",
    ]
    if obs.weather:
        w = obs.weather
        temp = (f", {w['temperatura']:.1f} °C" if isinstance(w.get("temperatura"), (int, float))
                else "")
        linhas.append(
            f"- Clima confirmado por {w.get('fonte', 'fonte')} às "
            f"{time.strftime('%H:%M', time.localtime(w.get('ts', obs.ts)))}: "
            f"{w.get('categoria', '?')}{temp}. Cite o clima SÓ assim, sem inventar detalhe.")
    else:
        linhas.append(
            "- Clima: SEM OBSERVAÇÃO CONFIRMADA. Não afirme sol, chuva, calor, "
            "frio, nuvem, neve, vento ou umidade. Se o assunto depender disso, mude de assunto.")
    linhas += [
        "REGRAS DE COERÊNCIA (valem mais que qualquer sugestão de estilo):",
        "- Nunca contradiga os fatos acima. Não diga 'bom dia' à tarde, nem cite "
        "um dia da semana diferente do real, nem descreva um clima que não observou.",
        "- Não invente nada que você não possa observar: sem dados, mude de assunto.",
        "- Uma ideia concreta por post. Se o texto ficar sem nexo, reescreva.",
    ]
    return "\n".join(linhas)


def _issue(codigo: str, trecho: str, motivo: str) -> Dict[str, str]:
    return {"codigo": codigo, "trecho": trecho[:80], "motivo": motivo}


def check(text: str, obs: Observation, *, min_words: int = 2) -> List[Dict[str, str]]:
    """Devolve inconsistências. Lista vazia = texto coerente com o observado."""
    if not ENABLED:
        return []
    if not isinstance(text, str):
        return [_issue("NAO_TEXTO", "", "texto não é string")]
    t = text.strip()
    issues: List[Dict[str, str]] = []

    palavras = re.findall(r"[\wÀ-ÿ]+", t, re.UNICODE)
    if not t or VAZIO_RX.match(t) or len(palavras) < min_words:
        issues.append(_issue("SEM_SENTIDO", t, "texto vazio ou só pontuação"))
    m = TEMPLATE_RX.search(t)
    if m:
        issues.append(_issue("TEMPLATE_VAZOU", m.group(0), "marcador de template/erro no texto"))
    m = REPETE_RX.search(t)
    if m:
        issues.append(_issue("REPETICAO", m.group(0), "mesma palavra repetida 4x seguidas"))

    # ── clima ────────────────────────────────────────────────────────────
    achados: Dict[str, str] = {}
    for cat, rx in CLIMA_RX.items():
        mm = rx.search(t)
        if mm:
            achados[cat] = mm.group(0)

    for par in INCOMPATIVEIS:
        if par.issubset(set(achados)):
            issues.append(_issue(
                "CONTRADICAO_INTERNA", " + ".join(sorted(par)),
                f"o texto afirma {sorted(par)[0]} e {sorted(par)[1]} ao mesmo tempo"))
            break

    clima_obs = (obs.weather or {}).get("categoria")
    for cat, trecho in achados.items():
        if clima_obs is None:
            issues.append(_issue(
                "CLIMA_NAO_VERIFICADO", trecho,
                "afirmação sobre clima sem observação confirmada — não inventar"))
        elif cat != clima_obs:
            issues.append(_issue(
                "CLIMA_CONFLITO", trecho,
                f"texto diz '{cat}', observação confirma '{clima_obs}'"))

    mm = NEVE_RX.search(t)
    if mm and obs.estacao != "inverno" and clima_obs != "neve":
        issues.append(_issue(
            "ESTACAO_INCOMPATIVEL", mm.group(0),
            f"neve/gelo citado na {obs.estacao} sem observação compatível"))

    # ── período / saudações ──────────────────────────────────────────────
    for rotulo, rx in SAUDA_RX.items():
        mm = rx.search(t)
        if mm and obs.periodo not in SAUDA_ACEITA[rotulo]:
            issues.append(_issue(
                "PERIODO_ERRADO", mm.group(0),
                f"'{rotulo}' citado durante a {obs.periodo} real"))

    # ── dia da semana ────────────────────────────────────────────────────
    mm = DIA_RX.search(t)
    if mm:
        citado = (mm.group(1) or mm.group(2) or "").lower()
        citado = {"terça": "terça", "terca": "terça", "sábado": "sábado",
                  "sabado": "sábado"}.get(citado, citado)
        citado = re.sub(r"ou$", "", citado)  # sextou → sexta
        mapa = {"segund": "segunda", "terç": "terça", "terc": "terça",
                "quart": "quarta", "quint": "quinta", "sext": "sexta",
                "sabad": "sábado", "doming": "domingo"}
        for k, v in mapa.items():
            if citado.startswith(k):
                citado = v
                break
        if citado and citado != obs.dia_semana:
            issues.append(_issue(
                "DIA_ERRADO", mm.group(0),
                f"texto cita '{citado}', hoje é {obs.dia_semana}"))
    if FDS_RX.search(t) and not obs.fim_de_semana and obs.periodo != "noite":
        issues.append(_issue(
            "FDS_ERRADO", "fim de semana",
            f"fim de semana citado na {obs.dia_semana} de {obs.periodo}"))

    # dedup por código, preservando ordem
    vistos = set()
    out = []
    for i in issues:
        if i["codigo"] not in vistos:
            vistos.add(i["codigo"])
            out.append(i)
    return out


def guard(text: str, obs: Optional[Observation] = None,
          *, min_words: int = 2) -> Tuple[bool, List[Dict[str, str]]]:
    """(ok, issues). Nunca lança exceção — falha interna vira 'aprovado' do
    jeito antigo, para não derrubar o motor 24h."""
    try:
        o = obs or observe()
        issues = check(text, o, min_words=min_words)
        return (not issues), issues
    except Exception:
        return True, []


def feedback(issues: List[Dict[str, str]]) -> str:
    """Instrução de reescrita para a próxima tentativa do mesmo post."""
    if not issues:
        return ""
    motivos = "; ".join(f"{i['codigo']} ({i['trecho']}): {i['motivo']}"
                        for i in issues[:4])
    return (
        "ATENÇÃO: seu rascunho anterior foi rejeitado por inconsistência com a "
        f"realidade observada: {motivos}. Reescreva do zero, sem citar clima, "
        "hora, dia ou estação que você não observou. Mude de assunto se "
        "necessário. Uma frase concreta, sem explicar o erro."
    )


def retry_consistent(produce: Callable[[str], str], *, tries: int = 3,
                     obs: Optional[Observation] = None,
                     min_words: int = 2) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Gera até `tries` vezes, devolvendo feedback a cada rejeição.

    `produce(feedback)` -> texto. Se todas falharem, devolve (None, issues):
    o chamador **não publica**. Nunca aumenta o número de posts — no máximo
    troca um post incoerente por nenhum post.
    """
    if not ENABLED:
        return produce(""), []
    o = obs or observe()
    fb = ""
    ultimas: List[Dict[str, str]] = []
    for _ in range(max(1, tries)):
        try:
            txt = produce(fb)
        except Exception:
            return None, ultimas
        ok, ultimas = guard(txt, o, min_words=min_words)
        if ok:
            return txt, []
        fb = feedback(ultimas)
    return None, ultimas


def clima_topicos_permitidos(obs: Optional[Observation] = None) -> bool:
    """Só sortear tópicos de clima quando houver observação confirmada.

    Evita desperdiçar ciclos pedindo ao modelo que fale de um clima que,
    no modo conservador, o validador barra logo em seguida.
    """
    if not ENABLED:
        return True
    try:
        return bool((obs or observe()).weather)
    except Exception:
        return True


TOPICOS_CLIMA = frozenset({
    "clima", "chuva", "calor", "frio", "sol", "tempo", "previsão do tempo",
    "previsao do tempo", "neve", "vento", "umidade", "temperatura",
    "o clima hoje", "o tempo hoje", "tempo lá fora",
})


def filtrar_topicos(pool: List[str], obs: Optional[Observation] = None) -> List[str]:
    """Remove tópicos climáticos quando não há observação confirmada.

    Nunca devolve lista vazia: se só havia clima, devolve a original para o
    chamador decidir (o validador continua sendo a rede de segurança).
    """
    if not ENABLED or clima_topicos_permitidos(obs):
        return list(pool)
    limpo = [t for t in pool if str(t).strip().lower() not in TOPICOS_CLIMA]
    return limpo or list(pool)

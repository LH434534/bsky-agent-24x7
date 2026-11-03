#!/usr/bin/env python3
"""
human_selftest.py — valida o catálogo de 600 capacidades SEM rede/GPU/credencial.

    python scripts/human_selftest.py
exit 0 = OK | 1 = falha
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.human as H  # noqa: E402
import core.toolkit as T  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


print("\n=== 1. catálogo: mais de 567 capacidades ===")
cat = H.catalog()
check("total = 600", len(cat) == 600, str(len(cat)))
check("576 perfis", len(H.PERFIS) == 576)
check("24 micro-ferramentas", len(H.UTILS) == 24)
check("IDs únicos", len({c["id"] for c in cat}) == 600)
check("serializável em JSON", isinstance(json.dumps(cat), str))
d = H.describe()
check("4×4×3×3×4 = 576",
      [len(v) for v in d["dimensoes"].values()] == [4, 4, 3, 3, 4],
      str([len(v) for v in d["dimensoes"].values()]))
check("12 libs reconhecidas", sum(d["libs"].values()) >= 10,
      f"{sum(d['libs'].values())}/12")
check("clichês de IA catalogados",
      d["ai_tells_catalogados"] == len(H.AI_TELLS) and len(H.AI_TELLS) >= 40,
      str(d["ai_tells_catalogados"]))

print("\n=== 2. seleção determinística (sem random global) ===")
p1 = H.choose("Bom dia, o céu está bonito hoje", ts=1730000000, actor="alice")
p2 = H.choose("Bom dia, o céu está bonito hoje", ts=1730000000, actor="alice")
p3 = H.choose("Bom dia, o céu está bonito hoje", ts=1730000001, actor="alice")
check("mesmo contexto → mesmo perfil", p1["id"] == p2["id"], p1["id"])
check("contexto diferente → perfil diferente (esperado)", p1["id"] != p3["id"],
      f"{p1['id']} vs {p3['id']}")
ids = {H.choose(f"texto {i}", ts=1730000000 + i, actor="x")["id"] for i in range(20000)}
check("cobertura > 500 dos 576 perfis", len(ids) > 500, f"{len(ids)}/576 distintos")
check("todos os IDs existem no catálogo", all(H.get(i) for i in list(ids)[:50]))

print("\n=== 3. anti-clichê de IA ===")
ia = ("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀 "
      "Vamos mergulhar em alguns pontos. É importante notar que isso é ✅.")
check("detecta clichês", H.ai_tell_score(ia) >= 5, str(H.ai_tell_score(ia)))
limpo = H.strip_ai_tells(ia)
check("remove clichês", H.ai_tell_score(limpo) == 0, repr(limpo[:60]))
check("não esvazia o texto", len(limpo.strip()) > 0)
score_ia = H.human_likeness_score(ia)
humano = "Bom, o céu tá bonito hoje. Achei que valia dizer isso. E você, o que acha?"
score_h = H.human_likeness_score(humano)
check("texto humano pontua mais que texto de IA", score_h > score_ia,
      f"IA={score_ia} humano={score_h}")

print("\n=== 4. apply() nos 576 perfis: nenhum quebra o post ===")
base = ("O céu está bonito hoje pela manhã. A luz bate diferente nas janelas do prédio "
        "e ninguém parece ter pressa na rua. Acho que isso merece uma pausa.")
falhas = []
for p in H.PERFIS:
    try:
        out, m = H.apply(base, p)
        if (not out.strip() or len(out) > 300 or len(out) < len(base) // 3
                or H.ai_tell_score(out) > 0):
            falhas.append((p["id"], len(out)))
    except Exception as e:
        falhas.append((p["id"], str(e)[:50]))
check("576/576 perfis aplicam com segurança", not falhas, str(falhas[:5]))
check("no máximo 2 edições por post", all(H.apply(base, p)[1]["edits"] <= H.MAX_EDITS
                                          for p in H.PERFIS[:80]))
check("apply vazio não lança", H.apply("")[0] == "")

print("\n=== 5. as 24 micro-ferramentas ===")
bad = []
for u in H.UTILS:
    try:
        out, _ = H.apply(base, u)
        if not out.strip():
            bad.append(u["id"])
    except Exception:
        bad.append(u["id"])
check("24/24 utils aplicam", not bad, str(bad))
check("emojize funciona", H.apply("oi :wave:", H.get("U005"))[0].strip() != "")

print("\n=== 6. kill switches e degradação ===")
_saved_h, _saved_t, _saved_alias = H.ENABLED, T.ENABLED, T.HUMAN_ON
# O env var é lido no import; em runtime o toolkit guarda o alias HUMAN_ON.
H.ENABLED, T.HUMAN_ON = False, False
out_off, _langs_off, meta_off = T.prepare_post("Good morning, the weather is beautiful today.")
check("BOT_HUMAN=0 preserva o texto", "Good morning" in out_off)
check("BOT_HUMAN=0 marca humano inativo", meta_off["humano"]["ativo"] is False)
check("apply() direto também respeita", H.apply("texto qualquer")[1]["aplicada"] is False)
H.ENABLED, T.HUMAN_ON = True, True
T.ENABLED = False
off2, off2l, off2m = T.prepare_post("Good morning, the weather is beautiful today.")
check("BOT_TOOLKIT=0 mantém langs antigo", off2l == ["pt-BR"], str(off2l))
H.ENABLED, T.ENABLED, T.HUMAN_ON = _saved_h, _saved_t, _saved_alias

print("\n=== 7. integração real com prepare_post ===")
CLICHE = ("Excelente pergunta! Como uma IA, espero que isso ajude. 🚀 "
          "Vamos mergulhar em alguns pontos sobre o tempo em São Paulo hoje.")
txt, langs, meta = T.prepare_post(CLICHE)
# Duas chamadas na mesma janela de 5 min DEVEM dar o mesmo texto, senão o
# Outbox (hash do texto final) não bloqueia a duplicata.
_a, _, _ma = T.prepare_post(CLICHE)
_b, _, _mb = T.prepare_post(CLICHE)
check("perfil estável dentro do bucket de 5 min",
      _a == _b and _ma["humano"]["perfil"] == _mb["humano"]["perfil"],
      f"{_ma['humano']['perfil']} / {_mb['humano']['perfil']}")
check("idempotência do Outbox preservada", T.Outbox.key(_a) == T.Outbox.key(_b))
check("langs corretos", langs == ["pt-BR"], str(langs))
check("perfil humano escolhido", meta["humano"]["ativo"] is True)
check("clichês removidos na saída", H.ai_tell_score(txt) == 0, repr(txt[:70]))
check("≤ 300 graphemes", meta["graphemes"] <= 300, str(meta["graphemes"]))
check("score registrado no meta", "score_depois" in meta["humano"])
check("sinaliza texto que era só clichê",
      meta["humano"]["recomenda_regenerar"] is True)
check("describe() inclui bloco humano", "humano" in T.describe())

print("\n=== 8. texto real (não clichê) NÃO é sinalizado ===")
_real, _lr, _mr = T.prepare_post(
    "O céu tá bonito hoje pela manhã. A luz bate diferente nas janelas e ninguém "
    "parece ter pressa na rua. Acho que isso merece uma pausa.")
check("sem recomendação de regenerar", _mr["humano"]["recomenda_regenerar"] is False)
check("texto íntegro", "janelas" in _real)
print(f"  amostra: {_real[:100]!r}")

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

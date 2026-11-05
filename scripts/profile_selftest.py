"""Testes de core/profile.py — decisão, merge, swapRecord e rollback.

Nenhuma rede. Nenhuma credencial. putRecord é sempre mockado.

Uso: python scripts/profile_selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import profile as P       # noqa: E402
from core import influence as I     # noqa: E402

PASS = FAIL = 0
TMP = Path(tempfile.mkdtemp(prefix="bio_selftest_"))


def ck(cond, nome, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {nome} {extra}")


def st() -> P.BioState:
    s = P.BioState(path=TMP / f"state_{time.time_ns()}.json")
    return s


POSTS = [f"o café {i} esfriou na xícara azul da mesa" for i in range(14)]
BIO_RUIM = ("No mundo de hoje, bem-vindo! Sou uma IA que posta conteúdo diário. "
            "Últimas horas, não perca: me segue pra mais, link na bio. 🚀")
BIO_BOA = ("18 anos, Brasil. Falo de jogo, café, código e do que eu reparo no dia. "
           "No meu ritmo.")


class BskyFake:
    def __init__(self, description=BIO_RUIM, cid="bafyCidAntigo"):
        self.value = {
            "$type": "app.bsky.actor.profile",
            "displayName": "His",
            "description": description,
            "avatar": {"$type": "blob", "ref": {"$link": "bafyAvatar"}},
            "banner": {"$type": "blob", "ref": {"$link": "bafyBanner"}},
            "labels": {"$type": "com.atproto.label.defs#selfLabels", "values": []},
            "pinnedPost": {"uri": "at://did/x/app.bsky.feed.post/y", "cid": "bafyPin"},
            "pronouns": "ele/dele",
            "createdAt": "2026-01-01T00:00:00.000Z",
        }
        self.cid = cid
        self.writes = []
        self.descricao = description

    def get_profile_record(self):
        return {"value": dict(self.value), "cid": self.cid,
                "uri": "at://did/x/app.bsky.actor.profile/self",
                "base": "https://pds.fake"}

    def put_profile_description(self, record, cid, description, *, base=None):
        # Simula o contrato real de Bsky.put_profile_description (testado
        # separadamente na seção 7b com a classe Bsky de verdade).
        novo = dict(record)
        novo["$type"] = "app.bsky.actor.profile"
        novo["description"] = description
        self.writes.append({"record": novo, "swapRecord": cid,
                            "description": description, "base": base})
        self.descricao = description
        self.cid = "bafyCidNovo"
        return {"uri": "at://did/x/app.bsky.actor.profile/self", "cid": self.cid}


print("=== 1. contrato ===")
d = P.descrever()
ck(d["ativo"] is True, "BOT_BIO ligado")
ck(d["aplicar"] is False, "default e DRY-RUN (sem escrita)")
ck("swapRecord" in d["protecao_concorrencia"], "protecao contra escrita concorrente")
ck("avatar" in d["merge_preserva"] and "displayName" in d["merge_preserva"],
   "merge preserva avatar/nome")
ck(d["candidatas"] == 4, "4 candidatas deterministicas")
ck(P.MAX_GRAPHEMES == 256, "limite do lexicon")

print("=== 2. problemas(): bio fora do nível ===")
probs = P.problemas(BIO_RUIM)
for esperado in ("generica_ou_afirma_ser_ia", "pedido_de_engajamento",
                 "cliche_de_ia", "manipulacao_ou_urgencia_falsa"):
    ck(esperado in probs, f"detecta {esperado}", str(probs))
ck(P.problemas("") != [], "bio vazia reprovada")
ck(P.problemas("ghp_abc def") != [], "padrao de segredo reprovado")
ck(P.problemas("https://x.com") != [], "link reprovado")
ck(P.problemas("a" * 300) != [], "acima de 256 graphemes reprovado")

print("=== 3. candidatas são seguras e determinísticas ===")
for c in P.CANDIDATAS:
    ok, mot = P.bio_segura(c)
    ck(ok, f"candidata aprovada: {c[:34]}", str(mot))
    ck(P.graphemes(c) <= 256, "candidata dentro do lexicon")
    ck(not I.blocklist(c), "candidata sem CTA/manipulacao")
ck(P.candidata("curioso", agora=1000) == P.candidata("curioso", agora=1000),
   "mesma data+arquétipo → mesma candidata")
ck(len({P.CANDIDATAS[i % 4] for i in range(4)}) >= 1, "catalogo nao vazio")
vistos = {P.candidata(agora=1000 + k * 86400) for k in range(30)}
ck(len(vistos) >= 2, "dias diferentes variam a candidata", str(len(vistos)))

print("=== 4. o bot ESCOLHE NÃO MEXER (6 motivos) ===")
s = st()
r = P.avaliar(BIO_RUIM, [], s, now=time.time())
ck(not r.mudar and r.motivo == "amostra_insuficiente_nao_mexer",
   "sem amostra → nao mexe", r.motivo)
r = P.avaliar(BIO_BOA, POSTS, s, now=time.time())
ck(not r.mudar and r.motivo == "bio_ja_coerente_nao_mexer",
   "bio coerente → nao mexe (estabilidade)", r.motivo)
# candidata reprovada: monkeypatch temporario
_orig = P.candidata
P.candidata = lambda *a, **k: "me segue pra mais, último dia!"
r = P.avaliar(BIO_RUIM, POSTS, s, now=time.time())
ck(not r.mudar and r.motivo == "candidata_reprovada_nao_mexer",
   "candidata ruim → nao mexe", r.motivo)
P.candidata = _orig
# sem melhora objetiva
_orig2 = P.INF.autoridade
P.INF.autoridade = lambda *a, **k: {"score": 10.0, "componentes": {}, "razoes": [],
                                    "bloqueios": [], "desligado": False}
r = P.avaliar(BIO_RUIM, POSTS, s, now=time.time())
ck(not r.mudar, "sem melhora → nao mexe", r.motivo)
P.INF.autoridade = _orig2
# cooldown
s2 = st(); s2.ultima_troca = time.time() - 86400  # 1 dia
r = P.avaliar(BIO_RUIM, POSTS, s2, now=time.time())
ck(not r.mudar and r.motivo == "cooldown_nao_mexer", "cooldown de 30d", r.motivo)
# teto de trocas
s3 = st(); s3.ultima_troca = 0.0; s3.trocas = [time.time() - 2 * 86400]
r = P.avaliar(BIO_RUIM, POSTS, s3, now=time.time())
ck(not r.mudar and r.motivo == "teto_de_trocas_nao_mexer", "teto 1/30d", r.motivo)

print("=== 5. o bot ESCOLHE MEXER quando há evidência ===")
s4 = st(); agora = time.time()
r = P.avaliar(BIO_RUIM, POSTS, s4, now=agora)
ck(r.mudar, "bio ruim + candidata melhor → decide mudar", r.motivo)
ck(r.score_novo > r.score_atual + P.MARGEM, "melhora objetiva registrada",
   f"{r.score_atual} -> {r.score_novo}")
ck(r.dry_run is True, "decisao em dry-run por padrao")
ck(r.candidata in P.CANDIDATAS, "candidata vem do catalogo")
ck("problemas:" in " ".join(r.razoes), "razao explicita no resumo")
ck(json.dumps(r.resumo()), "resumo serializavel")
ck("candidata_hash" in r.resumo() and r.resumo()["candidata_hash"],
   "so hash da candidata no log, nao texto de terceiro")

print("=== 6. DRY-RUN: ZERO escrita ===")
b = BskyFake()
r = P.avaliar(BIO_RUIM, POSTS, st(), now=time.time())
out = P.aplicar(b, r, st())
ck(out["dry_run"] is True and out["aplicada"] is False, "nao aplica")
ck(len(b.writes) == 0, "ZERO putRecord em dry-run", str(len(b.writes)))
ck(b.descricao == BIO_RUIM, "bio real intacta")

print("=== 7. APPLY: merge completo + swapRecord ===")
b2 = BskyFake()
r2 = P.avaliar(BIO_RUIM, POSTS, st(), now=time.time())
out2 = P.aplicar(b2, r2, st(), apply=True)
ck(out2["aplicada"] is True, "aplica quando autorizado", str(out2))
ck(len(b2.writes) == 1, "exatamente 1 putRecord")
w = b2.writes[0]
rec = w["record"]
ck(w["swapRecord"] == "bafyCidAntigo", "swapRecord = CID lido")
ck(rec["$type"] == "app.bsky.actor.profile", "$type correto")
ck(rec["avatar"]["ref"]["$link"] == "bafyAvatar", "avatar PRESERVADO")
ck(rec["banner"]["ref"]["$link"] == "bafyBanner", "banner PRESERVADO")
ck(rec["displayName"] == "His", "displayName PRESERVADO")
ck(rec["pinnedPost"]["cid"] == "bafyPin", "pinnedPost PRESERVADO")
ck(rec["pronouns"] == "ele/dele", "pronouns PRESERVADO")
ck(rec["labels"]["values"] == [], "labels PRESERVADOS")
ck(rec["createdAt"] == "2026-01-01T00:00:00.000Z", "createdAt PRESERVADO")
ck(rec["description"] == r2.candidata, "so description mudou")
ck("uri" not in rec and "cid" not in rec and "base" not in rec,
   "chaves de controle nao vazam pro record")
ck(out2["anterior_hash"] and out2["nova_hash"], "hashes para auditoria")
ck(out2["cid_novo"] == "bafyCidNovo", "novo cid registrado")

print("=== 7b. merge REAL em core/atproto.Bsky (sem rede) ===")
from core.atproto import Bsky  # noqa: E402
real = Bsky("handle.test", "senha-fake")
capturado = {}


def _call_fake(method, nsid, *, auth=True, public=False, params=None,
               body=None, retry=4, base=None):
    capturado.update({"method": method, "nsid": nsid, "body": body, "base": base})
    return {"uri": "at://did/x/app.bsky.actor.profile/self", "cid": "bafyNovo"}


real._call = _call_fake
valor_original = {
    "$type": "app.bsky.actor.profile", "displayName": "His",
    "description": BIO_RUIM,
    "avatar": {"$type": "blob", "ref": {"$link": "bafyAvatar"}},
    "banner": {"$type": "blob", "ref": {"$link": "bafyBanner"}},
    "pinnedPost": {"uri": "at://x", "cid": "bafyPin"},
    "labels": {"$type": "com.atproto.label.defs#selfLabels", "values": []},
    "createdAt": "2026-01-01T00:00:00.000Z",
}
real.put_profile_description(valor_original, "bafyCidAntigo", BIO_BOA,
                             base="https://pds.fake")
body = capturado.get("body") or {}
ck(capturado["method"] == "POST" and capturado["nsid"] == "com.atproto.repo.putRecord",
   "metodo e NSID corretos", str(capturado))
ck(body.get("rkey") == "self", "rkey=self")
ck(body.get("collection") == "app.bsky.actor.profile", "collection correta")
ck(body.get("validate") is True, "validate=True")
ck(body.get("swapRecord") == "bafyCidAntigo", "swapRecord = CID lido")
rec_real = body.get("record") or {}
ck(rec_real.get("description") == BIO_BOA, "description atualizada")
ck(rec_real.get("avatar", {}).get("ref", {}).get("$link") == "bafyAvatar",
   "avatar preservado no metodo REAL")
ck(rec_real.get("banner", {}).get("ref", {}).get("$link") == "bafyBanner",
   "banner preservado no metodo REAL")
ck(rec_real.get("displayName") == "His", "displayName preservado no REAL")
ck(rec_real.get("pinnedPost", {}).get("cid") == "bafyPin",
   "pinnedPost preservado no REAL")
ck(rec_real.get("labels") == {"$type": "com.atproto.label.defs#selfLabels", "values": []},
   "labels preservados no REAL")
ck(rec_real.get("createdAt") == "2026-01-01T00:00:00.000Z",
   "createdAt preservado no REAL")
ck("uri" not in rec_real and "cid" not in rec_real,
   "chaves de controle nao entram no record")
# Sem CID (perfil ainda nao existe) nao deve enviar swapRecord vazio.
capturado.clear()
real.put_profile_description(valor_original, "", BIO_BOA)
ck("swapRecord" not in (capturado.get("body") or {}),
   "sem CID nao envia swapRecord vazio")

print("=== 8. escrita concorrente NÃO é sobrescrita ===")
b3 = BskyFake(description="bio que outra sessao colocou")
r3 = P.avaliar(BIO_RUIM, POSTS, st(), now=time.time())
out3 = P.aplicar(b3, r3, st(), apply=True)
ck(out3["aplicada"] is False and out3["motivo"] == "bio_mudou_desde_avaliacao",
   "aborta se a bio mudou", str(out3))
ck(len(b3.writes) == 0, "ZERO putRecord no conflito")

print("=== 9. rollback ===")
b4 = BskyFake(); s5 = st()
r4 = P.avaliar(BIO_RUIM, POSTS, s5, now=time.time())
P.aplicar(b4, r4, s5, apply=True)
ck(s5.anterior.get("description") == BIO_RUIM, "checkpoint gravado ANTES da escrita")
rr = P.rollback(b4, s5)
ck(rr["restaurado"] is True, "rollback executado", str(rr))
ck(len(b4.writes) == 2, "2a escrita e o rollback")
ck(b4.writes[-1]["description"] == BIO_RUIM, "bio anterior restaurada")
ck(b4.writes[-1]["swapRecord"] == "bafyCidNovo", "swapRecord do cid atual")
ck(P.rollback(BskyFake(), st())["restaurado"] is False,
   "sem checkpoint → nao inventa rollback")

print("=== 10. estado persistível e auditável ===")
s6 = st(); s6.ultima_troca = 123.0; s6.trocas = [122.0]
s6.registrar_decisao({"mudar": True, "motivo": "teste", "score_atual": 1, "score_novo": 9})
s6.save(123.0)
s7 = P.BioState(path=s6.path).load()
ck(s7.ultima_troca == 123.0, "ultima_troca persistida")
ck(len(s7.decisoes) == 1 and s7.decisoes[0]["dry_run"] is True,
   "decisao registrada como dry_run")
txt = s6.path.read_text(encoding="utf8")
ck("ghp_" not in txt and "accessJwt" not in txt, "nenhum segredo no estado")
ck(json.loads(txt), "JSON valido")

print("=== 11. kill switch ===")
P.ENABLED = False
ck(not P.avaliar(BIO_RUIM, POSTS, st(), now=time.time()).mudar,
   "BOT_BIO=0 desliga a decisao")
P.ENABLED = True

print("=== 12. robustez ===")
for estranho in (None, 123, b"x", ""):
    try:
        P.problemas(estranho)       # type: ignore
        P.bio_segura(estranho)      # type: ignore
        ck(True, f"nao lanca com {type(estranho).__name__}")
    except Exception as e:
        ck(False, f"nao lanca com {type(estranho).__name__}", repr(e))
b5 = BskyFake()
b5.get_profile_record = lambda: (_ for _ in ()).throw(RuntimeError("rede caiu"))
out5 = P.aplicar(b5, r2, st(), apply=True)
ck(out5["aplicada"] is False and "erro" in out5, "falha de rede nao derruba heartbeat")

print(f"\n===== RESULTADO: {PASS} pass, {FAIL} fail =====")
sys.exit(1 if FAIL else 0)

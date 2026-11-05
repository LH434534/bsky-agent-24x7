"""AT Protocol client for Bluesky — sessions, posts, replies, follows, feed reads."""
from __future__ import annotations

import json
import re
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ── toolkit opcional ────────────────────────────────────────────────────────
# Se core/toolkit.py ou alguma lib faltar, o bot continua EXATAMENTE como antes.
try:
    from core.toolkit import (  # type: ignore
        ENABLED as TOOLKIT_ON, Outbox, PermanentFailureGuard,
        RateLimitGuard, handle_cache, prepare_post,
    )
except Exception as _e:  # pragma: no cover
    logging.getLogger("bsky").warning("toolkit indisponível (%s) — seguindo sem ele", _e)
    TOOLKIT_ON = False
    Outbox = PermanentFailureGuard = RateLimitGuard = prepare_post = None  # type: ignore
    handle_cache = lambda **_kw: {}  # type: ignore

log = logging.getLogger("bsky")

PDS = "https://bsky.social"
PUBLIC = "https://public.api.bsky.app"

URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")
MENTION_RE = re.compile(r"(?<![\w@./])@([a-zA-Z0-9][a-zA-Z0-9._-]*)(?:\.([a-zA-Z0-9.-]+))?")
TAG_RE = re.compile(r"(?<![\w#])#([\p{L}\w]+)" if False else r"(?<![\w#])#([A-Za-z0-9_À-ɏ]{2,40})")


class RateLimited(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry in {retry_after}s")


class ATProtoError(Exception):
    pass


class AuthError(ATProtoError):
    """Credencial rejeitada pelo PDS (401/403).

    É um erro PERMANENTE: repetir a tentativa não muda o resultado. A correção
    é humana — gerar nova App Password em Settings → App Passwords.
    """

    def __init__(self, handle: str, status: int, body: str = ""):
        self.handle = handle
        self.status = status
        msg = (
            f"credencial rejeitada pelo PDS ({status}) para @{handle}. "
            "A App Password salva no secret BSKY_APP_PASSWORD está revogada, "
            "expirada ou o handle está incorreto. "
            "Gere uma nova em Settings → Privacy and Security → App Passwords "
            "e atualize o secret do repositório."
        )
        if body:
            msg += f" | resposta: {body[:120]}"
        super().__init__(msg)


@dataclass
class PostRef:
    uri: str
    cid: str


class Bsky:
    """Thin, retrying, session-refreshing ATProto client."""

    def __init__(self, handle: str, app_password: str, pds: str = PDS, session_file: str | None = None):
        self.handle = handle.lstrip("@")
        self.app_password = app_password
        self.pds = pds.rstrip("/")
        self.session_file = session_file
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "bsky-agent/1.0"})
        self.did: str | None = None
        self.access: str | None = None
        self.refresh: str | None = None
        self._last_auth: float = 0.0
        # TTLCache expira handles resolvidos; antes era um dict sem limite.
        self._handle_cache = handle_cache(maxsize=512, ttl=3600) if TOOLKIT_ON else {}
        self.guard = (PermanentFailureGuard(threshold=2, reset_s=900)
                      if TOOLKIT_ON else None)
        self.rate = RateLimitGuard() if TOOLKIT_ON else None
        # outbox.json fica ao lado de session.json (fora do git, ver gh_runner.py)
        base = Path(self.session_file).parent if self.session_file else Path.cwd() / "data"
        self.outbox = Outbox(base / "outbox.json", window_s=1800) if TOOLKIT_ON else None

    # ---------------------------------------------------------------- session
    def login(self) -> None:
        if self.guard:
            self.guard.check()
        if self.session_file and self._load_session():
            if self._whoami_ok():
                log.info("session restored from disk for %s (%s)", self.handle, self.did)
                return
        r = self.s.post(
            f"{self.pds}/xrpc/com.atproto.server.createSession",
            json={"identifier": self.handle, "password": self.app_password},
            timeout=30,
        )
        if r.status_code == 401:
            if self.guard:
                self.guard.record_failure()   # erro permanente: NÃO repetir
            raise AuthError(self.handle, r.status_code, r.text)
        if r.status_code == 403:
            # 403 pode ser o PDS (auth/rate limit) OU um proxy/policy de rede
            # ("policy_default_denied"). So e credencial se o corpo for JSON
            # ATProto com um campo 'error' conhecido; caso contrario e rede.
            try:
                err = (r.json() or {}).get("error", "")
            except Exception:
                err = ""
            if err in ("AuthenticationRequired", "InvalidToken", "ExpiredToken",
                       "AccountDeactivated", "AccountTakedown"):
                if self.guard:
                    self.guard.record_failure()
                raise AuthError(self.handle, r.status_code, r.text)
            raise ATProtoError(
                f"acesso negado {r.status_code} (possivel bloqueio de rede/proxy, "
                f"nao credencial): {r.text[:200]}")
        if r.status_code >= 400:
            raise ATProtoError(f"login failed {r.status_code}: {r.text[:300]}")
        d = r.json()
        self.did, self.access, self.refresh = d["did"], d["accessJwt"], d["refreshJwt"]
        self._last_auth = time.time()
        if self.guard:
            self.guard.record_success()
        self._save_session()
        log.info("logged in as %s (%s)", self.handle, self.did)

    def _whoami_ok(self) -> bool:
        try:
            r = self.s.get(
                f"{self.pds}/xrpc/com.atproto.server.getSession",
                headers={"Authorization": f"Bearer {self.access}"},
                timeout=20,
            )
            return r.status_code == 200
        except Exception:
            return False

    def _load_session(self) -> bool:
        try:
            with open(self.session_file) as f:
                d = json.load(f)
            self.did, self.access, self.refresh = d["did"], d["access"], d["refresh"]
            self._last_auth = time.time()
            return True
        except Exception:
            return False

    def _save_session(self) -> None:
        if not self.session_file:
            return
        try:
            with open(self.session_file, "w") as f:
                json.dump({"did": self.did, "access": self.access, "refresh": self.refresh}, f)
        except Exception as e:
            log.warning("could not persist session: %s", e)

    TOKEN_ERRS = {"ExpiredToken", "InvalidToken", "AuthenticationRequired",
                  "InvalidJwt", "TokenRevoked"}

    @staticmethod
    def _is_token_error(r) -> bool:
        """Detecta token morto tanto por status (401) quanto por corpo (400 ExpiredToken)."""
        if r.status_code == 401:
            return True
        if r.status_code != 400:
            return False
        try:
            err = (r.json() or {}).get("error", "")
        except Exception:
            err = ""
        return err in Bsky.TOKEN_ERRS

    def _reauth(self) -> bool:
        """Refresh → se falhar, login novo. Retorna True se conseguiu."""
        for fn in (self._refresh, self.login):
            try:
                fn()
                return True
            except Exception as e:
                log.warning("reauth falhou: %s", str(e)[:100])
        return False

    def ensure_fresh(self, max_age_s: int = 3600) -> None:
        """Renova proativamente antes de expirar — evita perder escritas."""
        if self._last_auth and time.time() - self._last_auth < max_age_s:
            return
        try:
            self._refresh()
        except Exception:
            try:
                self.login()
            except Exception as e:
                log.warning("ensure_fresh falhou: %s", str(e)[:100])

    def _refresh(self) -> None:
        r = self.s.post(
            f"{self.pds}/xrpc/com.atproto.server.refreshSession",
            headers={"Authorization": f"Bearer {self.refresh}"},
            timeout=30,
        )
        if r.status_code >= 400:
            raise ATProtoError(f"refresh failed {r.status_code}: {r.text[:200]}")
        d = r.json()
        self.did, self.access, self.refresh = d["did"], d["accessJwt"], d["refreshJwt"]
        self._last_auth = time.time()
        self._save_session()

    # ------------------------------------------------------------------ http
    def _call(self, method: str, nsid: str, *, auth: bool = True, public: bool = False,
              params: Dict[str, Any] | None = None, body: Dict[str, Any] | None = None,
              retry: int = 4, base: str | None = None) -> Dict[str, Any]:
        # `base` é opcional e usado SÓ pelo leitor/gravador de perfil, quando
        # o entryway não faz proxy e é preciso falar com o PDS real do didDoc.
        # Nenhuma chamada existente passa `base` → comportamento idêntico.
        base = base or (PUBLIC if public else self.pds)
        url = f"{base}/xrpc/{nsid}"
        for attempt in range(retry):
            headers = {}
            if auth:
                if not self.access:
                    self.login()
                headers["Authorization"] = f"Bearer {self.access}"
            try:
                r = self.s.request(method, url, headers=headers, params=params, json=body, timeout=45)
                if self.rate:
                    self.rate.observe(r.headers)
            except requests.RequestException as e:
                log.warning("network error on %s: %s", nsid, e)
                time.sleep(min(60, 5 * 2 ** attempt))
                continue

            if r.status_code == 429:
                ra = int(r.headers.get("ratelimit-reset", 0) or 0)
                wait = max(20, ra - int(time.time())) if ra else min(300, 30 * 2 ** attempt)
                log.warning("429 on %s — backing off %ss", nsid, wait)
                time.sleep(wait)
                continue

            # ATProto devolve 400 {"error":"ExpiredToken"} — não 401.
            # Sem isso o refresh nunca dispara e todas as escritas falham.
            if auth and self._is_token_error(r):
                log.info("token expirou em %s — renovando", nsid)
                if self._reauth():
                    continue
                continue

            if r.status_code == 401 and auth and attempt == 0:
                try:
                    self._refresh()
                    continue
                except ATProtoError:
                    self.login()
                    continue

            if r.status_code >= 500:
                time.sleep(min(120, 5 * 2 ** attempt))
                continue

            if r.status_code >= 400:
                raise ATProtoError(f"{nsid} -> {r.status_code}: {r.text[:400]}")
            return r.json() if r.content else {}
        raise ATProtoError(f"{nsid} failed after {retry} attempts")

    # ------------------------------------------------------------- rich text
    def _resolve_handle(self, handle: str) -> str | None:
        if handle in self._handle_cache:
            return self._handle_cache[handle]
        try:
            d = self._call("GET", "com.atproto.identity.resolveHandle", auth=False,
                           params={"handle": handle}, public=True)
            did = d.get("did")
            self._handle_cache[handle] = did
            return did
        except Exception:
            return None

    def parse_facets(self, text: str) -> List[Dict[str, Any]]:
        """Build app.bsky richtext facets (links, mentions, hashtags) with UTF-8 byte offsets."""
        facets: List[Dict[str, Any]] = []

        def boff(i: int) -> int:
            return len(text[:i].encode("utf-8"))

        for m in URL_RE.finditer(text):
            uri = m.group(0).rstrip(".,;:!?)")
            start, end = boff(m.start()), boff(m.start()) + len(uri.encode("utf-8"))
            facets.append({"index": {"byteStart": start, "byteEnd": end},
                           "features": [{"$type": "app.bsky.richtext.facet#link", "uri": uri}]})

        for m in MENTION_RE.finditer(text):
            handle = m.group(0).lstrip("@")
            did = self._resolve_handle(handle)
            if not did:
                continue
            start, end = boff(m.start()), boff(m.end())
            facets.append({"index": {"byteStart": start, "byteEnd": end},
                           "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}]})

        for m in TAG_RE.finditer(text):
            start, end = boff(m.start()), boff(m.end())
            facets.append({"index": {"byteStart": start, "byteEnd": end},
                           "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})

        facets.sort(key=lambda f: f["index"]["byteStart"])
        return facets

    @staticmethod
    def graphemes(text: str) -> int:
        """Bluesky counts Unicode grapheme clusters; approximate with code points (safe ceiling)."""
        return len(text)

    # ----------------------------------------------------------------- write
    def upload_blob(self, data: bytes, mime: str = "image/jpeg") -> Dict[str, Any]:
        """Sobe a imagem e devolve o blob que o post referencia.

        com.atproto.repo.uploadBlob recebe os bytes crus, não JSON. Por isso
        não passa pelo _call — precisa de Content-Type próprio e corpo binário.
        """
        self.ensure_fresh()
        url = f"{self.pds}/xrpc/com.atproto.repo.uploadBlob"
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self.s.post(url,
                                headers={"Authorization": f"Bearer {self.access}",
                                         "Content-Type": mime,
                                         "User-Agent": "bsky-agent/1.0"},
                                data=data, timeout=120)
            except requests.RequestException as e:
                last = e
                time.sleep(min(60, 5 * 2 ** attempt))
                continue

            if r.status_code == 429:
                ra = int(r.headers.get("ratelimit-reset", 0) or 0)
                time.sleep(max(20, ra - int(time.time())) if ra
                           else min(300, 30 * 2 ** attempt))
                continue

            # ATProto manda 400 {"error":"ExpiredToken"} — renovar e repetir
            if self._is_token_error(r):
                if self._reauth():
                    continue
                continue

            if r.status_code >= 500:
                time.sleep(min(120, 5 * 2 ** attempt))
                continue

            if r.status_code >= 400:
                raise ATProtoError(f"uploadBlob -> {r.status_code}: {r.text[:300]}")

            d = r.json()
            blob = d.get("blob")
            if not blob or not blob.get("ref"):
                raise ATProtoError(f"uploadBlob sem blob: {str(d)[:200]}")
            return blob

        raise ATProtoError(f"uploadBlob failed after 4 attempts: {last}")

    def post(self, text: str, *, reply_to: PostRef | None = None,
             langs: List[str] | None = None,
             images: List[Dict[str, Any]] | None = None) -> PostRef:
        self.ensure_fresh()
        # ── toolkit: mojibake, idioma real e idempotência ─────────────────
        # Antes: langs era SEMPRE ["pt-BR"], mesmo para texto em inglês.
        if TOOLKIT_ON:
            text, langs, meta = prepare_post(text, langs)
            log.info("toolkit lang=%s tom=%s corrigido=%s",
                     meta["lang"], meta["tone"], meta["changed"])
            if self.outbox and self.outbox.seen(text):
                raise ATProtoError(
                    "post duplicado bloqueado pelo outbox: um createRecord que "
                    "expira pode ter publicado e o retry postaria duas vezes")
        text = text.strip()
        if not text:
            raise ValueError("empty post")
        if self.graphemes(text) > 300:
            raise ValueError(f"post too long: {self.graphemes(text)} graphemes")

        rec: Dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "langs": langs or ["pt-BR"],
        }
        f = self.parse_facets(text)
        if f:
            rec["facets"] = f
        if reply_to:
            root, parent = reply_to, reply_to
            try:
                d = self._call("GET", "app.bsky.feed.getPostThread", auth=False,
                               params={"uri": reply_to.uri, "depth": 1}, public=True)
                thread = d.get("thread", {})
                if thread.get("post", {}).get("record", {}).get("reply"):
                    rp = thread["post"]["record"]["reply"]["root"]
                    root = PostRef(rp["uri"], rp["cid"])
            except Exception:
                pass
            rec["reply"] = {"root": {"uri": root.uri, "cid": root.cid},
                            "parent": {"uri": parent.uri, "cid": parent.cid}}

        # imagens já sobem como blob antes de chegar aqui; o post só referencia
        if images:
            rec["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": [
                    {"alt": (im.get("alt") or "")[:300],
                     "image": im["blob"],
                     **({"aspectRatio": im["aspect"]} if im.get("aspect") else {})}
                    for im in images[:4]        # Bluesky aceita no máximo 4
                ],
            }

        d = self._call("POST", "com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.feed.post", "record": rec})
        if self.outbox:
            self.outbox.mark(text)   # só após confirmação do PDS
        return PostRef(d["uri"], d["cid"])

    def thread(self, texts: List[str]) -> List[PostRef]:
        """Post a self-reply thread."""
        refs: List[PostRef] = []
        parent: PostRef | None = None
        for t in texts:
            refs.append(self.post(t, reply_to=parent))
            parent = refs[-1]
        return refs

    def follow(self, did: str) -> str:
        self.ensure_fresh()
        d = self._call("POST", "com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.graph.follow",
            "record": {"$type": "app.bsky.graph.follow", "subject": did,
                       "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}})
        return d["uri"]

    def unfollow(self, follow_uri: str) -> None:
        rkey = follow_uri.rsplit("/", 1)[-1]
        self._call("POST", "com.atproto.repo.deleteRecord", body={
            "repo": self.did, "collection": "app.bsky.graph.follow", "rkey": rkey})

    def like(self, uri: str, cid: str) -> str:
        self.ensure_fresh()
        d = self._call("POST", "com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.feed.like",
            "record": {"$type": "app.bsky.feed.like",
                       "subject": {"uri": uri, "cid": cid},
                       "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}})
        return d["uri"]

    def repost(self, uri: str, cid: str) -> str:
        self.ensure_fresh()
        d = self._call("POST", "com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.feed.repost",
            "record": {"$type": "app.bsky.feed.repost",
                       "subject": {"uri": uri, "cid": cid},
                       "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}})
        return d["uri"]

    # ------------------------------------------------------------------ read
    def search_posts(self, query: str, limit: int = 25, sort: str = "latest") -> List[Dict[str, Any]]:
        d = self._call("GET", "app.bsky.feed.searchPosts", auth=False, public=True,
                       params={"q": query, "limit": limit, "sort": sort})
        return d.get("posts", [])

    def timeline(self, limit: int = 50) -> List[Dict[str, Any]]:
        d = self._call("GET", "app.bsky.feed.getTimeline", params={"limit": limit})
        return d.get("feed", [])

    def author_feed(self, actor: str, limit: int = 30) -> List[Dict[str, Any]]:
        d = self._call("GET", "app.bsky.feed.getAuthorFeed", auth=False, public=True,
                       params={"actor": actor, "limit": limit})
        return d.get("feed", [])

    def notifications(self, limit: int = 30) -> List[Dict[str, Any]]:
        d = self._call("GET", "app.bsky.notification.listNotifications", params={"limit": limit})
        return d.get("notifications", [])

    def profile(self, actor: str) -> Dict[str, Any]:
        return self._call("GET", "app.bsky.actor.getProfile", auth=False, public=True,
                          params={"actor": actor})

    # ── perfil: ler → mesclar → gravar (NUNCA substituir o record) ──────────
    PROFILE_COLLECTION = "app.bsky.actor.profile"
    PROFILE_RKEY = "self"

    def pds_real(self) -> str:
        """PDS real do didDoc, com fallback para o entryway configurado.

        O chat exige proxy e devolve 501 em bsky.social; para `getRecord` o
        entryway costuma rotear, mas não presumimos — tentamos o PDS real se
        a primeira chamada falhar. Cache por objeto, zero escrita.
        """
        cached = getattr(self, "_pds_real", "")
        if cached:
            return cached
        did = self.did or ""
        if did.startswith("did:plc:"):
            try:
                r = self.s.get(f"https://plc.directory/{did}", timeout=8)
                if r.status_code == 200:
                    for srv in (r.json().get("service") or []):
                        sid = str(srv.get("id") or "")
                        ep = str(srv.get("serviceEndpoint") or "")
                        if ep.startswith("https://") and (
                                "pds" in sid.lower()
                                or "AtprotoPersonalDataServer" in str(srv.get("type") or "")):
                            self._pds_real = ep.rstrip("/")
                            return self._pds_real
            except Exception as e:
                log.debug("didDoc indisponível (%s) — usando entryway", type(e).__name__)
        return self.pds

    def get_profile_record(self) -> Dict[str, Any]:
        """Devolve {"value": {...}, "cid": "...", "uri": "...", "base": "..."}.

        Leitura autenticada, somente leitura. Tenta o entryway e, se falhar,
        o PDS real do didDoc. Nenhuma escrita acontece aqui.
        """
        did = self.did or self.handle
        params = {"repo": did, "collection": self.PROFILE_COLLECTION,
                  "rkey": self.PROFILE_RKEY}
        last: Exception | None = None
        for base in dict.fromkeys([self.pds, self.pds_real()]):
            try:
                d = self._call("GET", "com.atproto.repo.getRecord", params=params,
                               base=base, retry=2)
                d["base"] = base
                return d
            except Exception as e:
                last = e
                log.debug("getRecord via %s falhou (%s)", base, type(e).__name__)
        raise ATProtoError(f"getRecord falhou em ambas as rotas: {last}")

    def put_profile_description(self, record: Dict[str, Any], cid: str,
                                description: str, *, base: str | None = None) -> Dict[str, Any]:
        """Atualiza SÓ `description`, preservando avatar/banner/nome/labels.

        `swapRecord` = CID lido => se outra pessoa (ou outra sessão) alterou
        o perfil nesse meio-tempo, a escrita FALHA em vez de apagar a mudança.
        É um upsert de merge, não um replace — o lexicon exige isso.
        """
        novo = dict(record or {})
        novo["$type"] = self.PROFILE_COLLECTION
        novo["description"] = description
        # Remove apenas chaves de controle que não pertencem ao record.
        for k in ("uri", "cid", "base"):
            novo.pop(k, None)
        body = {
            "repo": self.did or self.handle,
            "collection": self.PROFILE_COLLECTION,
            "rkey": self.PROFILE_RKEY,
            "validate": True,
            "record": novo,
        }
        if cid:
            body["swapRecord"] = cid       # proteção contra escrita concorrente
        return self._call("POST", "com.atproto.repo.putRecord", body=body,
                          base=base, retry=2)

    def followers(self, actor: str, limit: int = 50) -> List[Dict[str, Any]]:
        d = self._call("GET", "app.bsky.graph.getFollowers", auth=False, public=True,
                       params={"actor": actor, "limit": limit})
        return d.get("followers", [])

    def suggested_follows(self, actor: str, limit: int = 30) -> List[Dict[str, Any]]:
        try:
            d = self._call("GET", "app.bsky.graph.getSuggestedFollowsByActor", auth=False,
                           public=True, params={"actor": actor})
            return d.get("suggestions", [])[:limit]
        except Exception:
            return []


def uri_parts(uri: str):
    """at://did:plc:xxx/app.bsky.feed.post/rkey -> (did, rkey)"""
    p = uri.replace("at://", "").split("/")
    return p[0], p[-1]


def did_from_uri(uri: str) -> str:
    return uri_parts(uri)[0]

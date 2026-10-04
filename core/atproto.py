"""AT Protocol client for Bluesky — sessions, posts, replies, follows, feed reads."""
from __future__ import annotations

import json
import re
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

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
        self._handle_cache: Dict[str, str] = {}

    # ---------------------------------------------------------------- session
    def login(self) -> None:
        if self.session_file and self._load_session():
            if self._whoami_ok():
                log.info("session restored from disk for %s (%s)", self.handle, self.did)
                return
        r = self.s.post(
            f"{self.pds}/xrpc/com.atproto.server.createSession",
            json={"identifier": self.handle, "password": self.app_password},
            timeout=30,
        )
        if r.status_code >= 400:
            raise ATProtoError(f"login failed {r.status_code}: {r.text[:300]}")
        d = r.json()
        self.did, self.access, self.refresh = d["did"], d["accessJwt"], d["refreshJwt"]
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
        self._save_session()

    # ------------------------------------------------------------------ http
    def _call(self, method: str, nsid: str, *, auth: bool = True, public: bool = False,
              params: Dict[str, Any] | None = None, body: Dict[str, Any] | None = None,
              retry: int = 4) -> Dict[str, Any]:
        base = PUBLIC if public else self.pds
        url = f"{base}/xrpc/{nsid}"
        for attempt in range(retry):
            headers = {}
            if auth:
                if not self.access:
                    self.login()
                headers["Authorization"] = f"Bearer {self.access}"
            try:
                r = self.s.request(method, url, headers=headers, params=params, json=body, timeout=45)
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
    def post(self, text: str, *, reply_to: PostRef | None = None,
             langs: List[str] | None = None) -> PostRef:
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

        d = self._call("POST", "com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.feed.post", "record": rec})
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
        d = self._call("POST", "com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.feed.like",
            "record": {"$type": "app.bsky.feed.like",
                       "subject": {"uri": uri, "cid": cid},
                       "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}})
        return d["uri"]

    def repost(self, uri: str, cid: str) -> str:
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

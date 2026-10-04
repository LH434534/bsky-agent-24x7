"""SQLite memory — everything the agent has ever done, for dedupe + self-improvement."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DATA = Path(__file__).resolve().parent.parent / "data"
DB = DATA / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,          -- post | reply | follow | like | repost
  text TEXT,
  target_did TEXT,
  target_handle TEXT,
  uri TEXT,
  topic TEXT,
  ok INTEGER DEFAULT 1,
  err TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);
CREATE INDEX IF NOT EXISTS idx_actions_kind ON actions(kind);
CREATE INDEX IF NOT EXISTS idx_actions_target ON actions(target_did);

CREATE TABLE IF NOT EXISTS follows (
  did TEXT PRIMARY KEY,
  handle TEXT,
  ts REAL,
  unfollowed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_posts (
  uri TEXT PRIMARY KEY,
  ts REAL
);

CREATE TABLE IF NOT EXISTS topics (
  topic TEXT PRIMARY KEY,
  uses INTEGER DEFAULT 0,
  last REAL
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uri TEXT,
  ts REAL,
  likes INTEGER DEFAULT 0,
  replies INTEGER DEFAULT 0,
  reposts INTEGER DEFAULT 0,
  text TEXT
);
"""


class Memory:
    def __init__(self, path: Path | str = DB):
        DATA.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._c() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _c(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ───────────────────────────────────────────────────────────── write
    def log_action(self, kind: str, *, text: str = "", target_did: str = "",
                   target_handle: str = "", uri: str = "", topic: str = "",
                   ok: bool = True, err: str = "") -> None:
        with self._c() as c:
            c.execute("INSERT INTO actions (ts,kind,text,target_did,target_handle,uri,topic,ok,err)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (time.time(), kind, text, target_did, target_handle, uri, topic,
                       int(ok), err[:400]))

    def mark_follow(self, did: str, handle: str = "") -> None:
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO follows (did,handle,ts,unfollowed)"
                      " VALUES (?,?,COALESCE((SELECT ts FROM follows WHERE did=?),?),0)",
                      (did, handle, did, time.time()))

    def mark_seen(self, uri: str) -> None:
        with self._c() as c:
            c.execute("INSERT OR IGNORE INTO seen_posts (uri,ts) VALUES (?,?)", (uri, time.time()))

    def seen(self, uri: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM seen_posts WHERE uri=?", (uri,)).fetchone() is not None

    def use_topic(self, topic: str) -> None:
        with self._c() as c:
            c.execute("INSERT INTO topics (topic,uses,last) VALUES (?,1,?) "
                      "ON CONFLICT(topic) DO UPDATE SET uses=uses+1,last=excluded.last",
                      (topic, time.time()))

    def record_reach(self, uri: str, text: str, likes: int = 0, replies: int = 0,
                     reposts: int = 0) -> None:
        with self._c() as c:
            c.execute("INSERT INTO feedback (uri,ts,likes,replies,reposts,text)"
                      " VALUES (?,?,?,?,?,?)", (uri, time.time(), likes, replies, reposts, text))

    # ────────────────────────────────────────────────────────────── read
    def recent_texts(self, n: int = 200, kind: str | None = None) -> List[str]:
        with self._c() as c:
            if kind:
                rows = c.execute("SELECT text FROM actions WHERE kind=? AND text!='' "
                                 "ORDER BY ts DESC LIMIT ?", (kind, n)).fetchall()
            else:
                rows = c.execute("SELECT text FROM actions WHERE text!='' "
                                 "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [r["text"] for r in rows]

    def counts_since(self, kind: str, since: float) -> int:
        with self._c() as c:
            return c.execute("SELECT COUNT(*) n FROM actions WHERE kind=? AND ts>?",
                             (kind, since)).fetchone()["n"]

    def follows_today(self) -> int:
        return self.counts_since("follow", time.time() - 86400)

    def followed_recently(self, days: int = 7) -> int:
        return self.counts_since("follow", time.time() - days * 86400)

    def is_following(self, did: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM follows WHERE did=? AND unfollowed=0",
                             (did,)).fetchone() is not None

    def hot_topics(self, n: int = 5) -> List[str]:
        with self._c() as c:
            rows = c.execute("SELECT topic FROM topics ORDER BY uses DESC, last DESC LIMIT ?",
                             (n,)).fetchall()
        return [r["topic"] for r in rows]

    def topics_used(self) -> List[str]:
        with self._c() as c:
            return [r["topic"] for r in c.execute("SELECT topic FROM topics").fetchall()]

    def stats(self, hours: int = 24) -> Dict[str, Any]:
        since = time.time() - hours * 3600
        out: Dict[str, Any] = {}
        with self._c() as c:
            for k in ("post", "reply", "follow", "like", "repost"):
                out[k] = c.execute("SELECT COUNT(*) n FROM actions WHERE kind=? AND ts>?",
                                   (k, since)).fetchone()["n"]
            out["total_all_time"] = c.execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
            out["follows_total"] = c.execute(
                "SELECT COUNT(*) n FROM follows WHERE unfollowed=0").fetchone()["n"]
            out["errors_24h"] = c.execute(
                "SELECT COUNT(*) n FROM actions WHERE ok=0 AND ts>?", (since,)).fetchone()["n"]
            row = c.execute("SELECT SUM(likes) l, SUM(replies) r, SUM(reposts) p FROM feedback"
                            " WHERE ts>?", (since,)).fetchone()
            out["engagement"] = {"likes": row["l"] or 0, "replies": row["r"] or 0,
                                 "reposts": row["p"] or 0}
        return out

    def export(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._c() as c:
            rows = c.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

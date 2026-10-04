"""Mock AT Protocol server for offline end-to-end testing."""
import json, random, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

POSTS = [
    {"uri": "at://did:plc:mock1/app.bsky.feed.post/r1", "cid": "cid1",
     "author": {"did": "did:plc:mock1", "handle": "dev.one.bsky.social"},
     "record": {"text": "Finally finished the self-hosted backup pipeline. Restic + cron + a 20-line alert script beats any SaaS I tried."},
     "likeCount": 12, "replyCount": 4, "repostCount": 2},
    {"uri": "at://did:plc:mock2/app.bsky.feed.post/r2", "cid": "cid2",
     "author": {"did": "did:plc:mock2", "handle": "pythonista.bsky.social"},
     "record": {"text": "Unpopular opinion: most automation fails not because the tool is bad but because nobody owns it after week three."},
     "likeCount": 30, "replyCount": 9, "repostCount": 5},
    {"uri": "at://did:plc:mock3/app.bsky.feed.post/r3", "cid": "cid3",
     "author": {"did": "did:plc:mock3", "handle": "infra.girl.bsky.social"},
     "record": {"text": "Spent the whole afternoon debugging a Docker network. The fix was one flag. It is always one flag."},
     "likeCount": 8, "replyCount": 2, "repostCount": 1},
    {"uri": "at://did:plc:mock4/app.bsky.feed.post/r4", "cid": "cid4",
     "author": {"did": "did:plc:mock4", "handle": "spam.bot.bsky.social"},
     "record": {"text": "FREE AIRDROP 100x join my telegram channel now dm me for casino promo code"},
     "likeCount": 0, "replyCount": 0, "repostCount": 0},
]

LOG = []
CALL = [0]

SEEDS = [
    "Finally finished the self-hosted backup pipeline. Restic plus cron plus a tiny alert script beats every SaaS I tried.",
    "Unpopular opinion: most automation dies not because the tool is bad but because nobody owns it after week three.",
    "Spent the afternoon debugging a container network. The fix was one flag. It is always one flag.",
    "How do you keep alerting useful instead of just noisy? Genuinely asking, ours is unusable now.",
    "Anyone else shipping a small thing every day instead of planning something big? It changed how I work.",
    "Free airdrop 100x join my telegram dm me for casino promo code now now now",
    "Should I self host postgres or just pay for managed? Budget is tight but downtime is worse.",
    "Rewrote our deploy script in python. Half the size, twice as readable, nobody misses the yaml.",
    "The log line I skipped was the one that would have saved me an hour. Every single time.",
]

def fresh(n=12):
    """Fresh posts each call so the agent always has candidates."""
    CALL[0] += 1
    out = []
    for i in range(n):
        out.append({
            "uri": f"at://did:plc:m{i%7}/app.bsky.feed.post/c{CALL[0]}r{i}",
            "cid": f"cid{CALL[0]}{i}",
            "author": {"did": f"did:plc:m{i%7}", "handle": f"user{i%7}.bsky.social",
                       "displayName": f"User {i%7}"},
            "record": {"text": SEEDS[i % len(SEEDS)]},
            "likeCount": (i * 7) % 40, "replyCount": (i * 3) % 12, "repostCount": i % 6,
        })
    return out

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path
        if "getSession" in p:
            return self._json(200, {"did": "did:plc:me", "handle": "his08.bsky.social"})
        if "resolveHandle" in p:
            return self._json(200, {"did": "did:plc:resolved"})
        if "searchPosts" in p or "getTimeline" in p:
            if "getTimeline" in p:
                return self._json(200, {"feed": [{"post": x} for x in fresh(10)]})
            return self._json(200, {"posts": fresh(12)})
        if "getAuthorFeed" in p:
            return self._json(200, {"feed": [{"post": x} for x in fresh(3)]})
        if "getProfile" in p:
            return self._json(200, {"did": "did:plc:mock2", "handle": "pythonista.bsky.social",
                                    "description": "python dev, automation nerd",
                                    "followersCount": 1200, "followsCount": 300})
        if "listNotifications" in p:
            return self._json(200, {"notifications": [{
                "uri": "at://did:plc:mock1/app.bsky.feed.post/r9", "cid": "cid9",
                "reason": "reply", "isRead": False,
                "author": {"did": "did:plc:mock1", "handle": "dev.one.bsky.social"},
                "record": {"text": "how do you keep your backup alerts from going stale?"}}]})
        if "getPostThread" in p:
            return self._json(200, {"thread": {"post": {"record": {}}}})
        return self._json(200, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = self.path
        if "createSession" in p:
            return self._json(200, {"did": "did:plc:me", "handle": body.get("identifier"),
                                    "accessJwt": "tok", "refreshJwt": "ref"})
        if "refreshSession" in p:
            return self._json(200, {"did": "did:plc:me", "accessJwt": "tok2", "refreshJwt": "ref2"})
        if "createRecord" in p:
            coll = body.get("collection", "")
            LOG.append((coll, (body.get("record") or {}).get("text", "")))
            print(f"  [MOCK WRITE] {coll.split('.')[-1]:<7} :: {(body.get('record') or {}).get('text','')[:90]}")
            return self._json(200, {"uri": f"at://did:plc:me/{coll}/r{len(LOG)}", "cid": f"c{len(LOG)}"})
        return self._json(200, {})

if __name__ == "__main__":
    s = HTTPServer(("127.0.0.1", 8899), H)
    print("mock atproto on :8899")
    s.serve_forever()

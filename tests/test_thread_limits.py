"""Regression test for per-record accounting in threaded posts."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.actions import Actions
from core.antispam import Limits
from core.atproto import PostRef


class FakeGuard:
    def __init__(self, cap):
        self.limits = Limits(day_posts=cap)
        self.posts = 0
        self.texts = []

    def ok(self, action):
        return True, "ok"

    def used_today(self, action):
        return self.posts

    def record(self, action):
        if action == "post":
            self.posts += 1

    def remember_text(self, text):
        self.texts.append(text)

    def screen(self, text):
        return True, "ok"


class FakeBrain:
    def write_post(self, topic, style=""):
        return f"texto {style} sobre {topic}"


class FakeBsky:
    def __init__(self):
        self.calls = []

    def post(self, text, reply_to=None):
        self.calls.append((text, reply_to))
        return PostRef(f"at://post/{len(self.calls)}", f"cid-{len(self.calls)}")


class FakeMemory:
    def __init__(self):
        self.logs = []

    def log_action(self, *args, **kwargs):
        self.logs.append(kwargs)

    def use_topic(self, topic):
        pass


def test_thread_counts_each_post_against_daily_cap():
    guard = FakeGuard(cap=2)
    bsky = FakeBsky()
    actions = Actions(bsky, FakeBrain(), guard, FakeMemory())

    assert actions.do_thread("teste") is True
    assert len(bsky.calls) == 2
    assert guard.posts == 2


if __name__ == "__main__":
    test_thread_counts_each_post_against_daily_cap()
    print("thread cap regression: ok")

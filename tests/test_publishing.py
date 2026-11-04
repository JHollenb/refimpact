from types import SimpleNamespace

import pytest

from refimpact.outbox import Outbox
from refimpact.publisher import publish
from refimpact.tweets import render_tweet


class Client:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def create_tweet(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(data={"id": "123456789"})


@pytest.fixture
def store(tmp_path):
    return Outbox(tmp_path / "outbox.sqlite")


def test_duplicate_replay_and_successful_post(store, impact):
    first = store.enqueue(impact, render_tweet(impact))
    assert store.enqueue(impact, render_tweet(impact)) == first
    assert len(store.list()) == 1
    client = Client()
    publish(store, client, 10)
    publish(store, client, 10)
    assert client.calls == 1
    assert store.list()[0]["status"] == "posted"


def test_timeout_is_not_retried(store, impact):
    post_id = store.enqueue(impact, render_tweet(impact))
    client = Client(TimeoutError())
    publish(store, client, 10)
    publish(store, client, 10)
    assert client.calls == 1
    assert store.list()[0]["status"] == "uncertain"
    store.reconcile(post_id, tweet_id="123", confirmed_absent=False)
    assert store.list()[0]["status"] == "posted"


def test_rate_limit_keeps_pending_for_later_cycle(store, impact):
    store.enqueue(impact, render_tweet(impact))
    error = RuntimeError("rate limited")
    error.response = SimpleNamespace(status_code=429)
    client = Client(error)
    publish(store, client, 10)
    assert client.calls == 1
    assert store.list()[0]["status"] == "pending"


def test_revised_published_call_needs_review(store, impact):
    store.enqueue(impact, render_tweet(impact))
    publish(store, Client(), 1)
    impact.model_id = "revised-model"
    store.enqueue(impact, render_tweet(impact))
    assert [p["status"] for p in store.list()] == ["posted", "review"]


def test_pending_revision_supersedes_old(store, impact):
    original = store.enqueue(impact, render_tweet(impact))
    impact.model_id = "updated-model"
    revised = store.enqueue(impact, render_tweet(impact))
    assert original != revised
    assert [p["status"] for p in store.list()] == ["superseded", "pending"]


def test_atomic_claim_across_connections(tmp_path, impact):
    a, b = Outbox(tmp_path / "shared.sqlite"), Outbox(tmp_path / "shared.sqlite")
    a.enqueue(impact, render_tweet(impact))
    assert a.claim() is not None
    assert b.claim() is None
    # Crash leaves sending held for explicit reconciliation; opening a new DB does not retry.
    assert Outbox(tmp_path / "shared.sqlite").claim() is None


def test_retry_requires_operator_confirmation(store, impact):
    post_id = store.enqueue(impact, render_tweet(impact))
    publish(store, Client(TimeoutError()), 1)
    with pytest.raises(ValueError):
        store.reconcile(post_id, None, False)
    store.reconcile(post_id, None, True)
    assert store.list()[0]["status"] == "pending"


def test_missing_success_id_stays_uncertain(store, impact):
    store.enqueue(impact, render_tweet(impact))
    client = SimpleNamespace(create_tweet=lambda **kwargs: SimpleNamespace(data={}))
    publish(store, client, 1)
    assert store.list()[0]["status"] == "uncertain"

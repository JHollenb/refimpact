from __future__ import annotations

import os
from functools import partial

import tweepy

from refimpact.outbox import Outbox


def x_client():
    names = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
        "X_EXPECTED_USER_ID",
    ]
    missing = [key for key in names if not os.environ.get(key)]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=False,
    )
    client.session.request = partial(client.session.request, timeout=(10, 30))
    try:
        me = client.get_me(user_auth=True)
    except Exception as exc:
        raise ValueError(f"X account verification failed ({type(exc).__name__})") from None
    if str(me.data.id) != os.environ["X_EXPECTED_USER_ID"]:
        raise ValueError("Authenticated X account does not match X_EXPECTED_USER_ID")
    return client


def publish(store: Outbox, client, limit: int) -> list[dict]:
    outcomes = []
    for _ in range(limit):
        post = store.claim()
        if post is None:
            break
        try:
            response = client.create_tweet(text=post["text"], user_auth=True)
            tweet_id = str(response.data["id"])
            if not tweet_id.isdigit():
                raise ValueError("Missing numeric post ID")
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            # Rate rejection is safe to retry on a later invocation. A timeout, 5xx or
            # interrupted request can have committed remotely: never retry it blindly.
            status = (
                "pending"
                if code == 429
                else ("failed" if code in {400, 401, 403, 404, 422} else "uncertain")
            )
            error = f"{type(exc).__name__}; HTTP {code or 'unknown'}"
            store.finish(post["id"], status, error=error)
            outcomes.append({"id": post["id"], "status": status, "error": error})
            break
        store.finish(post["id"], "posted", tweet_id=tweet_id)
        outcomes.append({"id": post["id"], "status": "posted", "tweet_id": tweet_id})
    return outcomes

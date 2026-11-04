from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from refimpact.schema import Impact


class Outbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY, event_key TEXT NOT NULL, payload TEXT NOT NULL,
            text TEXT NOT NULL, status TEXT NOT NULL, tweet_id TEXT, error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY, post_id TEXT NOT NULL, action TEXT NOT NULL,
            detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def log(self, post_id: str, action: str, detail: str = ""):
        self.db.execute(
            "INSERT INTO audit(post_id,action,detail) VALUES(?,?,?)", (post_id, action, detail)
        )

    def enqueue(self, impact: Impact, text: str) -> str:
        payload = impact.model_dump_json()
        canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
        post_id = hashlib.sha256((canonical + "\n" + text).encode()).hexdigest()
        c = impact.comparison
        event = f"{c.game_id}:{c.play_id}:{c.reference_team}"
        with self.transaction():
            if self.db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
                return post_id
            prior = self.db.execute("SELECT status FROM posts WHERE event_key=?", (event,))
            needs_review = any(
                r["status"] in {"posted", "sending", "uncertain", "review"} for r in prior
            )
            self.db.execute(
                "UPDATE posts SET status='superseded', updated_at=CURRENT_TIMESTAMP "
                "WHERE event_key=? AND status IN ('pending','failed')",
                (event,),
            )
            status = "review" if needs_review else "pending"
            self.db.execute(
                "INSERT INTO posts(id,event_key,payload,text,status) VALUES(?,?,?,?,?)",
                (post_id, event, payload, text, status),
            )
            self.log(post_id, "enqueued", status)
        return post_id

    def list(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM posts" + (" WHERE status=?" if status else "")
        return [
            dict(r)
            for r in self.db.execute(
                sql + " ORDER BY created_at,rowid", (status,) if status else ()
            )
        ]

    def claim(self) -> dict | None:
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM posts WHERE status='pending' ORDER BY rowid LIMIT 1"
            )
            row = row.fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE posts SET status='sending', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
            self.log(row["id"], "sending")
            return dict(row)

    def finish(
        self, post_id: str, status: str, tweet_id: str | None = None, error: str | None = None
    ):
        with self.transaction():
            row = self.db.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
            if row is None or row["status"] != "sending":
                raise ValueError("Only a claimed post can finish")
            self.db.execute(
                "UPDATE posts SET status=?,tweet_id=?,error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, tweet_id, error, post_id),
            )
            self.log(post_id, status, tweet_id or error or "")

    def reconcile(self, post_id: str, tweet_id: str | None, confirmed_absent: bool):
        if bool(tweet_id) == confirmed_absent:
            raise ValueError("Supply either an observed tweet ID or --confirm-not-posted")
        if tweet_id and not tweet_id.isdigit():
            raise ValueError("Tweet ID must be numeric")
        with self.transaction():
            row = self.db.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
            if row is None or row["status"] not in {"sending", "uncertain", "failed", "review"}:
                raise ValueError("Post does not need reconciliation")
            status = "posted" if tweet_id else "pending"
            self.db.execute(
                "UPDATE posts SET status=?,tweet_id=?,error=NULL,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, tweet_id, post_id),
            )
            self.log(
                post_id, "operator_reconciled", tweet_id or "confirmed absent; retry authorized"
            )

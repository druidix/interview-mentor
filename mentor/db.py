"""SQLite storage: scenarios, gap items (spaced repetition), sessions, API usage."""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("MENTOR_DB", Path(__file__).parent.parent / "data" / "mentor.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario (
    id INTEGER PRIMARY KEY, category TEXT NOT NULL, prompt TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'invented', my_answer TEXT DEFAULT '', notes TEXT DEFAULT '',
    last_asked TEXT
);
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY, scenario_id INTEGER REFERENCES scenario(id),
    started_at TEXT NOT NULL, scorecard_json TEXT
);
CREATE TABLE IF NOT EXISTS gap_item (
    id INTEGER PRIMARY KEY, topic TEXT NOT NULL UNIQUE, correction TEXT NOT NULL,
    first_missed_at TEXT NOT NULL, next_due TEXT NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
    cost_usd REAL NOT NULL
);
"""

SEED_GAPS = [
    ("Kafka consumer lag", "Native lag is an offset/message-count delta (latest offset minus last committed offset), "
     "not a time delta. Time-based lag can be derived from throughput but is not the primitive."),
    ("Tokenizer vs word chunking", "Chunking on tokenizer tokens (encode/slice/decode) mangles case and punctuation; "
     "split on words when the unit you care about is words. add_special_tokens does not fix this."),
    ("FCM registration token vs OAuth token", "The registration token identifies the device and lives in the DB; "
     "the OAuth access token authenticates your Lambda to FCM, lives in the Lambda's memory and expires in ~1 hour. "
     "UNREGISTERED concerns the registration token only."),
]


def connect(path=None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def today():
    return date.today().isoformat()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed_gaps(conn):
    for topic, correction in SEED_GAPS:
        conn.execute(
            "INSERT OR IGNORE INTO gap_item(topic, correction, first_missed_at, next_due) VALUES (?,?,?,?)",
            (topic, correction, today(), today()))
    conn.commit()


def due_items(conn, limit=3):
    rows = conn.execute(
        "SELECT * FROM gap_item WHERE status='open' AND next_due <= ? ORDER BY next_due LIMIT ?",
        (today(), limit)).fetchall()
    return [dict(r) for r in rows]


def pick_scenario(conn, category=None):
    """Least-recently-asked scenario (never-asked first), optionally filtered by category."""
    q = "SELECT * FROM scenario"
    args = []
    if category:
        q += " WHERE category = ?"
        args.append(category)
    q += " ORDER BY (last_asked IS NOT NULL), last_asked, id LIMIT 1"
    row = conn.execute(q, args).fetchone()
    if row:
        conn.execute("UPDATE scenario SET last_asked=? WHERE id=?", (now(), row["id"]))
        conn.commit()
    return dict(row) if row else None


def add_gap(conn, topic, correction):
    conn.execute(
        "INSERT INTO gap_item(topic, correction, first_missed_at, next_due) VALUES (?,?,?,?) "
        "ON CONFLICT(topic) DO UPDATE SET correction=excluded.correction, status='open', "
        "interval_days=1, next_due=excluded.next_due",
        (topic, correction, today(), (date.today() + timedelta(days=1)).isoformat()))
    conn.commit()


def record_quiz(conn, gap_id, correct):
    """Correct: interval doubles. Miss: resets to 1 day. Always updates next_due."""
    row = conn.execute("SELECT interval_days FROM gap_item WHERE id=?", (gap_id,)).fetchone()
    if row is None:
        raise ValueError(f"no gap item {gap_id}")
    interval = row["interval_days"] * 2 if correct else 1
    conn.execute("UPDATE gap_item SET interval_days=?, next_due=? WHERE id=?",
                 (interval, (date.today() + timedelta(days=interval)).isoformat(), gap_id))
    conn.commit()
    return interval


def save_session(conn, scenario_id, scorecard):
    cur = conn.execute("INSERT INTO session(scenario_id, started_at, scorecard_json) VALUES (?,?,?)",
                       (scenario_id, now(), json.dumps(scorecard)))
    conn.commit()
    return cur.lastrowid

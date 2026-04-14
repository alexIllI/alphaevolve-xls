"""
alphaevolve/database.py
───────────────────────
SQLite-backed database of all evaluated candidates in the evolution run.

Schema:
  candidates — one row per evaluated candidate (algorithm variant)
  run_meta   — run-level metadata (config, start time, best score)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class Candidate:
    iteration: int
    island_id: int
    parent_id: int | None
    mutation_type: str          # e.g. 'sdc_objective', 'delay_constraints'
    target_file: str            # relative path of mutated file
    source_diff: str            # unified diff applied to target_file
    generated_code: str         # full text of the mutated function
    build_status: str           # 'success' | 'build_failed' | 'run_failed'
    num_stages: int = 0
    pipeline_reg_bits: int = 0
    max_stage_delay_ps: int = 0
    min_clock_period_ps: int = 0
    ppa_score: float = float("inf")
    build_duration_s: float = 0.0
    total_duration_s: float = 0.0
    notes: str = ""
    id: int | None = None       # set by DB on insert
    created_at: str = ""        # set by DB on insert


class CandidateDB:
    """Thread-safe SQLite candidate store."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS candidates (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        iteration           INTEGER NOT NULL,
        island_id           INTEGER NOT NULL,
        parent_id           INTEGER,
        mutation_type       TEXT NOT NULL,
        target_file         TEXT NOT NULL,
        source_diff         TEXT NOT NULL,
        generated_code      TEXT NOT NULL,
        build_status        TEXT NOT NULL,
        num_stages          INTEGER DEFAULT 0,
        pipeline_reg_bits   INTEGER DEFAULT 0,
        max_stage_delay_ps  INTEGER DEFAULT 0,
        min_clock_period_ps INTEGER DEFAULT 0,
        ppa_score           REAL DEFAULT 1e18,
        build_duration_s    REAL DEFAULT 0,
        total_duration_s    REAL DEFAULT 0,
        notes               TEXT DEFAULT '',
        created_at          TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS run_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_island ON candidates(island_id);
    CREATE INDEX IF NOT EXISTS idx_score  ON candidates(ppa_score);
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    # ── CRUD ───────────────────────────────────────────────────────────────────

    def insert(self, c: Candidate) -> int:
        """Insert a candidate and return its assigned ID."""
        row = asdict(c)
        row.pop("id", None)
        row.pop("created_at", None)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        cur = self._conn.execute(
            f"INSERT INTO candidates ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get(self, candidate_id: int) -> Candidate | None:
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return self._row_to_candidate(row) if row else None

    def best(self, n: int = 1, island_id: int | None = None) -> list[Candidate]:
        """Return the N best candidates (lowest ppa_score) with build_status='success'."""
        where = "build_status = 'success'"
        params: list = []
        if island_id is not None:
            where += " AND island_id = ?"
            params.append(island_id)
        rows = self._conn.execute(
            f"SELECT * FROM candidates WHERE {where} ORDER BY ppa_score ASC LIMIT ?",
            params + [n],
        ).fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def island_population(self, island_id: int, limit: int = 10) -> list[Candidate]:
        """Return most-recent successful candidates for an island."""
        rows = self._conn.execute(
            """SELECT * FROM candidates
               WHERE island_id = ? AND build_status = 'success'
               ORDER BY ppa_score ASC LIMIT ?""",
            (island_id, limit),
        ).fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def all_successful(self) -> list[Candidate]:
        rows = self._conn.execute(
            "SELECT * FROM candidates WHERE build_status='success' ORDER BY ppa_score ASC"
        ).fetchall()
        return [self._row_to_candidate(r) for r in rows]

    def iteration_summary(self) -> list[dict]:
        """Per-iteration: best score, number evaluated, number successful."""
        rows = self._conn.execute(
            """SELECT iteration,
                      COUNT(*) as evaluated,
                      SUM(CASE WHEN build_status='success' THEN 1 ELSE 0 END) as succeeded,
                      MIN(CASE WHEN build_status='success' THEN ppa_score END) as best_score
               FROM candidates GROUP BY iteration ORDER BY iteration"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Run metadata ────────────────────────────────────────────────────────────

    def set_meta(self, key: str, value) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO run_meta (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    def get_meta(self, key: str, default=None):
        row = self._conn.execute(
            "SELECT value FROM run_meta WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    # ── Export ──────────────────────────────────────────────────────────────────

    def to_csv(self, csv_path: Path | str) -> None:
        """Dump all candidates to a CSV file (for analysis)."""
        import csv
        rows = self._conn.execute(
            "SELECT * FROM candidates ORDER BY id"
        ).fetchall()
        if not rows:
            return
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows([dict(r) for r in rows])

    def close(self) -> None:
        self._conn.close()

    # ── Internal ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> Candidate:
        d = dict(row)
        return Candidate(**{k: d[k] for k in Candidate.__dataclass_fields__})

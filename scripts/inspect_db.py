"""Inspect the candidates database to diagnose failures."""
import sqlite3, sys
from pathlib import Path

db = Path("results/exp_001/candidates_db.sqlite")
if not db.exists():
    print("DB not found:", db)
    sys.exit(1)

con = sqlite3.connect(db)
rows = con.execute(
    "SELECT id, build_status, build_duration_s, notes, substr(generated_code,1,500) FROM candidates"
).fetchall()

for cid, status, dur, notes, code in rows:
    print(f"\n{'='*60}")
    print(f"ID={cid}  status={status}  build_time={dur:.1f}s")
    print(f"--- BUILD ERROR ---\n{notes[:800] if notes else '(none)'}")
    print(f"--- GENERATED CODE (first 500 chars) ---\n{code}")

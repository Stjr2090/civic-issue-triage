import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
"""Seed the authorities routing table (routing as data)."""
import sqlite3

from app.db import DB_PATH, SCHEMA

SEED = [
    # (name, category, contact, priority)
    ("Ministry of Works & Transport - District Roads", "roads", "roads@example.gov", 1),
    ("National Water & Sewerage Corp", "water", "water@example.gov", 1),
    ("Municipal Waste Management Dept", "waste", "waste@example.gov", 1),
    ("Town Clerk Office - Public Services", "public_services", "clerk@example.gov", 1),
]

conn = sqlite3.connect(DB_PATH)
conn.executescript(SCHEMA)
conn.executemany(
    "INSERT OR IGNORE INTO authorities (name, category, contact, priority)"
    " VALUES (?,?,?,?)",
    SEED,
)
conn.commit()
print(f"Seeded {conn.execute('SELECT COUNT(*) FROM authorities').fetchone()[0]} authorities.")
conn.close()

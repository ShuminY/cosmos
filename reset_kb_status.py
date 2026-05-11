"""Reset KB status for all documents from 'indexing' to 'pending'."""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/index.db")

if not DB_PATH.exists():
    print("Database not found!")
    exit(1)

conn = sqlite3.connect(str(DB_PATH))
cursor = conn.cursor()

# Check documents with status 'indexing'
cursor.execute("SELECT id, filename FROM documents WHERE kb_status = 'indexing'")
stuck = cursor.fetchall()

if not stuck:
    print("No documents stuck in 'indexing' status.")
else:
    print(f"Found {len(stuck)} documents stuck in 'indexing' status:")
    for doc_id, filename in stuck:
        print(f"  {doc_id}: {filename}")

    # Reset to 'pending'
    cursor.execute("UPDATE documents SET kb_status = 'pending' WHERE kb_status = 'indexing'")
    conn.commit()
    print(f"\nReset {cursor.rowcount} documents to 'pending' status.")

cursor.execute("SELECT kb_status, COUNT(*) FROM documents GROUP BY kb_status")
print("\nCurrent status summary:")
for status, count in cursor.fetchall():
    print(f"  {status}: {count}")

conn.close()

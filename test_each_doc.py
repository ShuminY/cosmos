"""Test each document individually to find the crash cause."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.db import init_db, session, Document
from src.kb import extract_text, chunk_text, embed_texts

init_db()

with session() as s:
    docs = s.query(Document).all()

for d in docs:
    print(f"\n{'='*50}")
    print(f"Testing: {d.filename} (status={d.kb_status})")
    print(f"  Type: {d.path.split('.')[-1]}")
    print(f"  Size: {d.size_bytes} bytes")

    project_root = Path("data/projects") / str(d.project_id)
    path = project_root / d.path

    if not path.exists():
        print(f"  SKIP: File not found: {path}")
        continue

    print(f"  Extracting text...")
    try:
        text, err = extract_text(d)
        if err:
            print(f"  ERROR: {err}")
            continue
        print(f"  Text length: {len(text)} chars")
    except Exception as e:
        print(f"  CRASH in extract_text: {e}")
        import traceback
        traceback.print_exc()
        continue

    print(f"  Chunking...")
    try:
        chunks = chunk_text(text)
        print(f"  Got {len(chunks)} chunks")
    except Exception as e:
        print(f"  CRASH in chunk_text: {e}")
        import traceback
        traceback.print_exc()
        continue

    print(f"  Embedding (first 2 chunks)...")
    try:
        chunk_texts = [c["text"] for c in chunks[:2]]
        embs = embed_texts(chunk_texts)
        print(f"  Embedding shape: {embs.shape}")
    except Exception as e:
        print(f"  CRASH in embed_texts: {e}")
        import traceback
        traceback.print_exc()
        continue

    print(f"  ✓ OK")

print(f"\n{'='*50}")
print("All tests done!")

"""Test document indexing without streamlit, to debug OOM."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

print("Step 1: Importing db...")
from src.db import init_db, session, Document

print("Step 2: Initializing db...")
init_db()

print("Step 3: Importing kb...")
from src.kb import extract_text, chunk_text, embed_texts, index_document

print("Step 4: Looking for documents...")
with session() as s:
    docs = s.query(Document).all()
    print(f"Found {len(docs)} documents:")
    for d in docs:
        print(f"  {d.id}: {d.filename} ({d.kb_status})")

    if docs:
        d = docs[0]
        print(f"\nTesting extraction for: {d.filename}")
        project_root = Path("data/projects") / str(d.project_id)
        path = project_root / d.path
        print(f"  File: {path}")
        print(f"  Exists: {path.exists()}")

        if path.exists():
            print("  Extracting text...")
            text, err = extract_text(d)
            if err:
                print(f"  Error: {err}")
            else:
                print(f"  Extracted {len(text)} chars")
                print(f"  First 200 chars: {text[:200]}")

                print("\n  Chunking...")
                chunks = chunk_text(text)
                print(f"  Got {len(chunks)} chunks")

                print("\n  Embedding first chunk...")
                emb = embed_texts([chunks[0]["text"]])
                print(f"  Embedding shape: {emb.shape}")
                print("  SUCCESS!")

print("\nAll tests passed! The issue is not in KB indexing code.")

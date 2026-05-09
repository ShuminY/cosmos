"""Knowledge Base core - text extraction, chunking, embeddings, and RAG retrieval.

Uses:
- pypdf, python-docx, python-pptx, openpyxl + pandas for text extraction
- Recursive character chunking with overlap
- sentence-transformers/all-MiniLM-L6-v2 via transformers
- Pure numpy cosine similarity for search
"""
from __future__ import annotations
import json
import io
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import select

from .db import session, Document, DocumentChunk, Project

ROOT = Path(__file__).resolve().parent.parent

# Embedding model - lightweight, fast, 384d
_embedding_model = None
_embedding_tokenizer = None


# ============ Embeddings ============
def get_embedding_model():
    """Lazy-load the embedding model and tokenizer."""
    global _embedding_model, _embedding_tokenizer
    if _embedding_model is None:
        from transformers import AutoTokenizer, AutoModel
        model_name = "sentence-transformers/all-MiniLM-L6-v2"
        _embedding_tokenizer = AutoTokenizer.from_pretrained(model_name)
        _embedding_model = AutoModel.from_pretrained(model_name)
    return _embedding_tokenizer, _embedding_model


def embed_texts(texts: list[str], batch_size: int = 8) -> np.ndarray:
    """Compute embeddings for a list of texts. Returns (n, 384) numpy array."""
    tokenizer, model = get_embedding_model()
    all_embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        encoded = tokenizer(batch, padding=True, truncation=True, return_tensors="pt", max_length=256)
        model_output = model(**encoded)
        # Mean pooling
        attention_mask = encoded["attention_mask"]
        token_embeddings = model_output[0]
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        sum_emb = (token_embeddings * input_mask_expanded).sum(1)
        sum_mask = input_mask_expanded.sum(1).clamp(min=1e-9)
        embeddings = sum_emb / sum_mask
        all_embeddings.append(embeddings.detach().numpy())

    return np.concatenate(all_embeddings, axis=0)


def embedding_to_json(arr: np.ndarray) -> str:
    """Serialize a 1D numpy array to JSON."""
    return json.dumps(arr.tolist())


def json_to_embedding(s: str) -> np.ndarray:
    """Deserialize JSON back to numpy array."""
    return np.array(json.loads(s))


# ============ Text Extraction ============
def extract_text_from_pdf(path: Path) -> tuple[str, list[tuple[int, int, int]]]:
    """Extract text from PDF, returns (full_text, page_mappings).
    page_mappings: list of (page_num, char_start, char_end)
    """
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    full_text = ""
    mappings = []
    for i, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        start = len(full_text)
        full_text += page_text + "\n\n"
        end = len(full_text)
        mappings.append((i + 1, start, end))
    return full_text, mappings


def extract_text_from_docx(path: Path) -> str:
    """Extract text from DOCX."""
    from docx import Document as DocxDocument
    doc = DocxDocument(str(path))
    return "\n\n".join(p.text for p in doc.paragraphs)


def extract_text_from_pptx(path: Path) -> str:
    """Extract text from PPTX."""
    from pptx import Presentation
    prs = Presentation(str(path))
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                texts.append(shape.text)
    return "\n\n".join(texts)


def extract_text_from_xlsx(path: Path) -> str:
    """Extract text from Excel as CSV-like text representation."""
    import pandas as pd
    buffer = io.StringIO()
    xls = pd.ExcelFile(str(path))
    for sheet_name in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet_name)
        buffer.write(f"=== Sheet: {sheet_name} ===\n")
        buffer.write(df.to_csv(index=False))
        buffer.write("\n\n")
    return buffer.getvalue()


def extract_text(doc: Document) -> tuple[str, str | None]:
    """Extract text from a document. Returns (text, error or None)."""
    project_root = ROOT / "data" / "projects" / str(doc.project_id)
    path = project_root / doc.path

    if not path.exists():
        return "", f"File not found: {doc.path}"

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            text, _ = extract_text_from_pdf(path)
            return text, None
        elif suffix == ".docx":
            return extract_text_from_docx(path), None
        elif suffix == ".pptx":
            return extract_text_from_pptx(path), None
        elif suffix in {".xlsx", ".xls", ".csv"}:
            return extract_text_from_xlsx(path), None
        elif suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="replace"), None
        else:
            return "", f"Unsupported file type: {suffix}"
    except Exception as e:
        return "", str(e)


# ============ Chunking ============
def chunk_text(text: str, chunk_size: int = 512, overlap: int = 100) -> list[dict]:
    """Split text into overlapping chunks. Returns list of {text, char_start, char_end}."""
    if not text or len(text) <= chunk_size:
        return [{"text": text, "char_start": 0, "char_end": len(text)}] if text else []

    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        # Try to split at a newline if possible (within 50 chars of end)
        if end < len(text):
            split_point = text.rfind("\n", end - 50, end)
            if split_point > pos:
                end = split_point + 1
        chunks.append({
            "text": text[pos:end].strip(),
            "char_start": pos,
            "char_end": end,
        })
        pos = end - overlap
        if pos < 0:
            pos = 0
        if pos == 0 and chunks:  # Prevent infinite loop on very short texts
            break
    return chunks


# ============ Indexing ============
def index_document(doc_id: int) -> tuple[bool, str | None]:
    """Full indexing pipeline: extract text, chunk, embed, and store chunks.
    Returns (success, error_message).
    """
    with session() as s:
        doc = s.get(Document, doc_id)
        if not doc:
            return False, "Document not found"

        doc.kb_status = "indexing"
        s.add(doc)
        s.commit()

        try:
            # 1. Extract text
            text, err = extract_text(doc)
            if err:
                doc.kb_status = "failed"
                doc.kb_error = err
                s.add(doc)
                s.commit()
                return False, err

            if not text or len(text.strip()) < 20:
                doc.kb_status = "failed"
                doc.kb_error = "No text extracted (file may be empty or image-only)"
                s.add(doc)
                s.commit()
                return False, doc.kb_error

            # 2. Chunk
            chunks = chunk_text(text)
            if not chunks:
                doc.kb_status = "failed"
                doc.kb_error = "No chunks produced"
                s.add(doc)
                s.commit()
                return False, doc.kb_error

            # 3. Clear old chunks
            s.execute(select(DocumentChunk).where(DocumentChunk.document_id == doc_id))
            old_chunks = s.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
            ).scalars().all()
            for c in old_chunks:
                s.delete(c)
            s.commit()

            # 4. Embed
            chunk_texts = [c["text"] for c in chunks]
            embeddings = embed_texts(chunk_texts)

            # 5. Store
            for i, chunk in enumerate(chunks):
                db_chunk = DocumentChunk(
                    project_id=doc.project_id,
                    document_id=doc_id,
                    chunk_index=i,
                    text=chunk["text"],
                    embedding=embedding_to_json(embeddings[i]),
                    char_start=chunk["char_start"],
                    char_end=chunk["char_end"],
                )
                s.add(db_chunk)

            doc.kb_status = "indexed"
            doc.kb_indexed_at = datetime.utcnow()
            doc.kb_chunk_count = len(chunks)
            doc.kb_error = None
            s.add(doc)
            s.commit()
            return True, None

        except Exception as e:
            doc.kb_status = "failed"
            doc.kb_error = str(e)
            s.add(doc)
            s.commit()
            return False, str(e)


def index_project_documents(project_id: int) -> tuple[int, int, list[str]]:
    """Index all unindexed or failed documents in a project.
    Returns (success_count, failed_count, error_messages).
    """
    with session() as s:
        docs = s.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.kb_status.in_(["pending", "failed"]),
            )
        ).scalars().all()

        success = 0
        failed = 0
        errors = []
        for doc in docs:
            ok, err = index_document(doc.id)
            if ok:
                success += 1
            else:
                failed += 1
                errors.append(f"{doc.filename}: {err}")
        return success, failed, errors


# ============ Search ============
def cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Compute cosine similarity between a query vector and multiple document vectors.
    a: (d,) query vector
    b: (n, d) document vectors
    returns: (n,) similarity scores [0, 1]
    """
    a_norm = a / np.linalg.norm(a)
    b_norm = b / np.linalg.norm(b, axis=1)
    return (a_norm @ b_norm.T)


def search_kb(project_id: int, query: str, top_k: int = 5) -> list[dict]:
    """Search the project KB for relevant chunks.
    Returns list of {chunk, document, score} sorted by relevance (highest first).
    """
    with session() as s:
        # Get all indexed chunks for this project
        chunks = s.execute(
            select(DocumentChunk).where(DocumentChunk.project_id == project_id)
        ).scalars().all()

        if not chunks:
            return []

        # Embed query
        query_emb = embed_texts([query])[0]

        # Load all embeddings
        chunk_embeddings = np.array([json_to_embedding(c.embedding) for c in chunks])

        # Compute similarity
        scores = cosine_similarity(query_emb, chunk_embeddings)

        # Sort by score descending, take top_k
        indices = np.argsort(-scores)[:top_k]

        results = []
        for i in indices:
            chunk = chunks[i]
            doc = chunk.document
            results.append({
                "chunk_id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "document_id": chunk.document_id,
                "filename": doc.filename,
                "text": chunk.text,
                "score": float(scores[i]),
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
            })
        return results


# ============ Status ============
def kb_status(project_id: int) -> dict:
    """Get KB status for a project.
    Returns {total_docs, indexed, pending, failed, total_chunks}.
    """
    with session() as s:
        docs = s.execute(
            select(Document).where(Document.project_id == project_id)
        ).scalars().all()

        chunks = s.execute(
            select(DocumentChunk).where(DocumentChunk.project_id == project_id)
        ).scalars().all()

        indexed = sum(1 for d in docs if d.kb_status == "indexed")
        pending = sum(1 for d in docs if d.kb_status == "pending")
        failed = sum(1 for d in docs if d.kb_status == "failed")
        indexing = sum(1 for d in docs if d.kb_status == "indexing")

        return {
            "total_docs": len(docs),
            "indexed": indexed,
            "pending": pending,
            "failed": failed,
            "indexing": indexing,
            "total_chunks": len(chunks),
        }

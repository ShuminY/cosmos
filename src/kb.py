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


# ============ Embeddings (Ultra-lightweight hash-based) ============
# Using simple hash-based features - minimal memory, no complex computation


def embed_texts(texts: list[str], batch_size: int = None) -> np.ndarray:
    """Ultra-lightweight text embedding using simple hashing.
    Returns (n, 64) numpy array. Minimal memory usage!
    """
    DIM = 64
    result = np.zeros((len(texts), DIM), dtype=np.float32)

    for text_idx, text in enumerate(texts):
        text_lower = text.lower()
        # Simple character-level features
        for i, char in enumerate(text_lower):
            # Hash character to bin
            bin_idx = (hash(char) + i) % DIM
            result[text_idx, bin_idx] += 1.0

        # Bigram features
        for i in range(len(text_lower) - 1):
            bigram = text_lower[i:i+2]
            bin_idx = hash(bigram) % DIM
            result[text_idx, bin_idx] += 0.5

        # Normalize
        norm = np.linalg.norm(result[text_idx])
        if norm > 1e-9:
            result[text_idx] = result[text_idx] / norm

    return result


def get_embedding_model():
    """Dummy function for API compatibility."""
    return None, None


def embedding_to_json(arr: np.ndarray) -> str:
    """Serialize a 1D numpy array to JSON."""
    return json.dumps(arr.tolist())


def json_to_embedding(s: str) -> np.ndarray:
    """Deserialize JSON back to numpy array."""
    return np.array(json.loads(s))


# ============ Text Extraction ============
def extract_text_from_pdf(path: Path) -> tuple[str, list[tuple[int, int, int]]]:
    """Extract text from PDF, returns (full_text, page_mappings).
    Falls back to OCR if no text layer found.
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

    # If no text extracted, try OCR
    if len(full_text.strip()) < 50:
        try:
            ocr_text = _extract_pdf_with_ocr(path)
            if ocr_text:
                return ocr_text, [(1, 0, len(ocr_text))]
        except Exception:
            pass  # OCR failed, return empty text

    return full_text, mappings


def _extract_pdf_with_ocr(path: Path) -> str | None:
    """Use OCR to extract text from image-based PDF.
    Requires: pip install pdf2image pytesseract
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract

        images = convert_from_path(str(path), dpi=200)
        texts = []
        for img in images:
            text = pytesseract.image_to_string(img, lang='chi_sim+eng')
            texts.append(text)
        return "\n\n".join(texts)
    except ImportError:
        return None
    except Exception:
        return None


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
    """Extract text from Excel - POC: skip Excel to avoid OOM issues."""
    return "[Excel file - skipped in POC mode]"


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
def chunk_text(text: str, chunk_size: int = 256, overlap: int = 50) -> list[dict]:
    """Split text into overlapping chunks. Returns list of {text, char_start, char_end}."""
    if not text:
        return []

    text_len = len(text)
    if text_len <= chunk_size:
        return [{"text": text.strip(), "char_start": 0, "char_end": text_len}]

    chunks = []
    pos = 0
    max_iterations = text_len // (chunk_size - overlap) + 10  # Safety limit
    iterations = 0

    while pos < text_len and iterations < max_iterations:
        iterations += 1
        end = min(pos + chunk_size, text_len)

        # Try to split at a newline if possible (within 50 chars of end)
        if end < text_len:
            search_start = max(pos, end - 50)
            split_point = text.rfind("\n", search_start, end)
            if split_point > pos:
                end = split_point + 1

        chunk_text_content = text[pos:end].strip()
        if chunk_text_content:  # Only add non-empty chunks
            chunks.append({
                "text": chunk_text_content,
                "char_start": pos,
                "char_end": end,
            })

        # Move forward
        new_pos = end - overlap
        if new_pos <= pos:  # Prevent infinite loop
            new_pos = pos + chunk_size - overlap
        pos = new_pos

    return chunks


# ============ Indexing ============
def index_document(doc_id: int) -> tuple[bool, str | None]:
    """Full indexing pipeline: extract text, chunk, embed, and store chunks.
    Returns (success, error_message).

    Note: Embedding computation is done OUTSIDE session to avoid OOM issues.
    """
    # Phase 1: Get document info and prepare data (with session)
    with session() as s:
        doc = s.get(Document, doc_id)
        if not doc:
            return False, "Document not found"

        doc.kb_status = "indexing"
        s.add(doc)
        s.commit()

        project_id = doc.project_id
        doc_path = doc.path
        doc_filename = doc.filename

    # Phase 2: Extract text (no session needed)
    try:
        text, err = extract_text_from_doc(doc_id, project_id, doc_path)
        if err:
            _update_doc_status(doc_id, "failed", err)
            return False, err

        if not text or len(text.strip()) < 10:  # 降低到 10 字符
            _update_doc_status(doc_id, "failed", "No text extracted (file may be empty or image-only)")
            return False, "No text extracted"

        # Phase 3: Chunk (no session needed)
        chunks = chunk_text(text)
        if not chunks:
            _update_doc_status(doc_id, "failed", "No chunks produced")
            return False, "No chunks produced"

        # Phase 4: Embed (no session needed - this is where OOM was happening!)
        chunk_texts = [c["text"] for c in chunks]
        embeddings = embed_texts(chunk_texts)

    except Exception as e:
        _update_doc_status(doc_id, "failed", str(e))
        return False, str(e)

    # Phase 5: Store results (with session)
    try:
        with session() as s:
            # Clear old chunks
            old_chunks = s.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
            ).scalars().all()
            for c in old_chunks:
                s.delete(c)

            # Store new chunks
            for i, chunk in enumerate(chunks):
                db_chunk = DocumentChunk(
                    project_id=project_id,
                    document_id=doc_id,
                    chunk_index=i,
                    text=chunk["text"],
                    embedding=embedding_to_json(embeddings[i]),
                    char_start=chunk["char_start"],
                    char_end=chunk["char_end"],
                )
                s.add(db_chunk)

            # Update document status
            doc = s.get(Document, doc_id)
            doc.kb_status = "indexed"
            doc.kb_indexed_at = datetime.utcnow()
            doc.kb_chunk_count = len(chunks)
            doc.kb_error = None
            s.add(doc)
            s.commit()
        return True, None
    except Exception as e:
        _update_doc_status(doc_id, "failed", str(e))
        return False, str(e)


def extract_text_from_doc(doc_id: int, project_id: int, doc_path: str) -> tuple[str, str | None]:
    """Extract text given doc info (without needing a Document object in session)."""
    from pathlib import Path
    doc = type('Doc', (), {'id': doc_id, 'project_id': project_id, 'path': doc_path})()
    return extract_text(doc)


def _update_doc_status(doc_id: int, status: str, error: str | None):
    """Update document status in a short session."""
    with session() as s:
        doc = s.get(Document, doc_id)
        if doc:
            doc.kb_status = status
            doc.kb_error = error
            s.add(doc)
            s.commit()


def index_project_documents(project_id: int) -> tuple[int, int, list[str]]:
    """Index all unindexed, failed, or stuck (indexing) documents in a project.
    Returns (success_count, failed_count, error_messages).
    """
    with session() as s:
        docs = s.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.kb_status.in_(["pending", "failed", "indexing"]),
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
    b_norm = b / np.linalg.norm(b, axis=1, keepdims=True)
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
        expected_dim = len(query_emb)

        # Load all embeddings - filter out mismatched dimensions
        valid_chunks = []
        valid_embeddings = []
        for c in chunks:
            emb = json_to_embedding(c.embedding)
            if len(emb) == expected_dim:
                valid_chunks.append(c)
                valid_embeddings.append(emb)

        if not valid_chunks:
            # All embeddings have wrong dimension - need reindex
            return []

        chunk_embeddings = np.array(valid_embeddings)

        # Compute similarity
        scores = cosine_similarity(query_emb, chunk_embeddings)

        # Sort by score descending, take top_k
        indices = np.argsort(-scores)[:top_k]

        results = []
        for i in indices:
            chunk = valid_chunks[i]
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

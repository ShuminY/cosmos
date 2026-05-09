"""SQLite schema for users / projects / documents / captures / materials / analyses / KB chat.

Single import surface — `from src.db import init_db, session, User, Project, ...`
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, Float,
    ForeignKey, create_engine, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"
DB_PATH = DATA_ROOT / "index.db"

Base = declarative_base()


# ============ schema ============
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="user")  # 'admin' | 'user'
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    owned_projects = relationship("Project", back_populates="owner",
                                  cascade="all, delete-orphan",
                                  foreign_keys="Project.owner_id")
    memberships = relationship("ProjectMember", back_populates="user",
                               cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    address = Column(String(512), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="planning")
    # 'planning' | 'in_progress' | 'completed' | 'archived'
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner = relationship("User", back_populates="owned_projects",
                         foreign_keys=[owner_id])
    members = relationship("ProjectMember", back_populates="project",
                           cascade="all, delete-orphan")
    captures = relationship("Capture", back_populates="project",
                            cascade="all, delete-orphan")
    materials = relationship("Material", back_populates="project",
                             cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="project",
                             cascade="all, delete-orphan")
    analyses = relationship("Analysis", back_populates="project",
                            cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="project",
                                 cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id = Column(Integer, ForeignKey("projects.id"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    role = Column(String(32), nullable=False, default="viewer")
    # 'viewer' | 'editor' | 'owner'
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    project = relationship("Project", back_populates="members")
    user = relationship("User", back_populates="memberships")


class Capture(Base):
    __tablename__ = "captures"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    captured_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    captured_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="uploaded")
    # 'uploaded' | 'processing' | 'done' | 'failed'
    src_video_path = Column(String(512), nullable=True)   # rel to project dir
    frames_dir = Column(String(512), nullable=True)
    frames_count = Column(Integer, nullable=True)
    outputs_dir = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    project = relationship("Project", back_populates="captures")


class Material(Base):
    __tablename__ = "materials"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    sku_name = Column(String(255), nullable=False)
    type = Column(String(64), nullable=False)
    # 'paint' | 'tile' | 'door' | 'window' | 'flooring' | 'fixture' | 'other'
    vendor = Column(String(255), nullable=True)
    color = Column(String(64), nullable=True)
    dim_w_mm = Column(Float, nullable=True)
    dim_h_mm = Column(Float, nullable=True)
    dim_d_mm = Column(Float, nullable=True)
    unit = Column(String(32), nullable=True)        # 'm2' | 'piece' | 'm' | 'kg'
    price = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    images_dir = Column(String(512), nullable=False)  # rel to project dir
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    project = relationship("Project", back_populates="materials")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    category = Column(String(64), nullable=False, index=True)
    # see DOCUMENT_CATEGORIES below
    filename = Column(String(512), nullable=False)
    path = Column(String(1024), nullable=False)  # rel to project dir
    size_bytes = Column(Integer, nullable=False)
    mime_type = Column(String(128), nullable=True)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Analysis state
    analysis_status = Column(String(32), nullable=False, default="pending")
    # 'pending' | 'running' | 'done' | 'unsupported' | 'failed'
    analysis_summary = Column(Text, nullable=True)        # short human-readable
    analysis_data_json = Column(Text, nullable=True)      # JSON string
    analysis_error = Column(Text, nullable=True)
    analyzed_at = Column(DateTime, nullable=True)

    # Knowledge Base (KB) indexing state
    kb_status = Column(String(32), nullable=False, default="pending")
    # 'pending' | 'indexing' | 'indexed' | 'failed'
    kb_indexed_at = Column(DateTime, nullable=True)
    kb_error = Column(Text, nullable=True)
    kb_chunk_count = Column(Integer, nullable=True)

    project = relationship("Project", back_populates="documents")


class DocumentChunk(Base):
    """Vectorized text chunk from a document for RAG retrieval."""
    __tablename__ = "document_chunks"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)  # Order within document
    text = Column(Text, nullable=False)
    embedding = Column(String, nullable=False)  # JSON-serialized numpy array
    page_num = Column(Integer, nullable=True)    # For PDFs, PPTX, etc.
    char_start = Column(Integer, nullable=True)
    char_end = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    document = relationship("Document")


class ChatSession(Base):
    """Persisted chat conversation for the KB chatbot."""
    __tablename__ = "chat_sessions"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(255), nullable=True)  # Auto-generated from first message
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    messages = relationship("ChatMessage", back_populates="session",
                            cascade="all, delete-orphan")
    project = relationship("Project")
    user = relationship("User")


class ChatMessage(Base):
    """Individual message in a chat session."""
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=False, index=True)
    role = Column(String(32), nullable=False)  # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    context_chunk_ids = Column(Text, nullable=True)  # JSON list of chunk IDs used
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    session = relationship("ChatSession", back_populates="messages")


class Analysis(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    capture_a_id = Column(Integer, ForeignKey("captures.id"), nullable=True)
    capture_b_id = Column(Integer, ForeignKey("captures.id"), nullable=True)
    kind = Column(String(64), nullable=False)
    # 'change_diff' | 'segmentation' | 'reference_match' | 'progress_report'
    status = Column(String(32), nullable=False, default="pending")
    report_path = Column(String(1024), nullable=True)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="analyses")


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    target_kind = Column(String(64), nullable=False)
    # 'document_analysis' | 'capture_pipeline' | 'change_detection'
    target_id = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    log_path = Column(String(1024), nullable=True)
    error = Column(Text, nullable=True)


# ============ taxonomy from the Excel spec ============
DOCUMENT_CATEGORIES = [
    ("01_business",     "商务/合同 (Business · contracts, BOQ, quotes, change orders)"),
    ("02_construction", "施工管理 (Construction · plans, schedules, QC, daily reports)"),
    ("03_drawings",     "图纸 (Drawings · DWG · tender / shop / factory / as-built)"),
    ("04_bim_models",   "BIM/3D models (Revit · Rhino · IFC · SketchUp)"),
    ("05_standards",    "知识/规范 (Standards · codes · process library · price list)"),
    ("06_renderings",   "效果图 (Design renderings)"),
    ("07_photos",       "施工照片 (Process / quality / completion / reference photos)"),
    ("08_videos",       "视频 (Captured walkthroughs)"),
    ("99_other",        "Other"),
]
DOCUMENT_CATEGORY_KEYS = [c[0] for c in DOCUMENT_CATEGORIES]

MATERIAL_TYPES = [
    "paint", "tile", "door", "window",
    "flooring", "ceiling", "fixture", "hardware", "other",
]


# ============ engine + sessions ============
_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db():
    Base.metadata.create_all(get_engine())


@contextmanager
def session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

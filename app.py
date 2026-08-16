import json
import mimetypes
import os
import random
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

import bleach
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
import logging
import markdown
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from studygraph_ai import (
    build_multi_topic_answer,
    generate_graph_fragment_from_uploads,
    get_openai_client_for_user,
)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv() -> None:
        return None


# -----------------------------------------------------------------------------
# Flask setup
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = BASE_DIR / "uploads"
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".csv",
    ".json",
}

DEFAULT_GRAPH_TEMPLATE: dict[str, Any] = {
    "lectures": [],
    "topics": {},
    "lecture_meta": [],
    "files": [],
}

BATCH_SIZE = 2

app = Flask(__name__)
load_dotenv()

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise ValueError("FLASK_SECRET_KEY not set")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


# -----------------------------------------------------------------------------
# Database models
# -----------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    openai_api_key: Mapped[str | None] = mapped_column("gemini_api_key", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    files: Mapped[list["StudyFile"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    topics: Mapped[list["Topic"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    lecture_metas: Mapped[list["LectureMeta"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class StudyFile(Base):
    __tablename__ = "study_files"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="processed")

    user: Mapped[User] = relationship(back_populates="files")
    topics: Mapped[list["Topic"]] = relationship(back_populates="file", cascade="all, delete-orphan")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    file_id: Mapped[str | None] = mapped_column(ForeignKey("study_files.id"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, default="")
    lecture_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lectures_json: Mapped[str] = mapped_column(Text, default="[]")

    user: Mapped[User] = relationship(back_populates="topics")
    file: Mapped[StudyFile | None] = relationship(back_populates="topics")
    outgoing_connections: Mapped[list["Connection"]] = relationship(
        back_populates="source_topic",
        foreign_keys="Connection.source_topic_id",
        cascade="all, delete-orphan",
    )


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"), index=True)
    target_topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"), index=True)
    type: Mapped[str] = mapped_column(String(40), default="related")
    strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="These topics are connected.")

    source_topic: Mapped[Topic] = relationship(foreign_keys=[source_topic_id], back_populates="outgoing_connections")


class LectureMeta(Base):
    __tablename__ = "lecture_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    lecture_id: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(255))
    order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="lecture_metas")


Base.metadata.create_all(engine)


# -----------------------------------------------------------------------------
# DB/session/auth helpers
# -----------------------------------------------------------------------------
def get_db() -> Session:
    if "db" not in g:
        g.db = SessionLocal()
    return g.db


@app.teardown_appcontext
def close_db(_: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def load_signed_in_user() -> None:
    g.current_user = None
    user_id = session.get("user_id")
    if not user_id:
        return

    db = get_db()
    g.current_user = db.get(User, user_id)
    if g.current_user is None:
        session.clear()


@app.context_processor
def inject_template_globals() -> dict[str, Any]:
    return {"current_user": getattr(g, "current_user", None)}


PUBLIC_ENDPOINTS = {"login_page", "signup_page", "static"}


@app.before_request
def require_auth_for_private_pages() -> Any:
    endpoint = request.endpoint
    if not endpoint or endpoint in PUBLIC_ENDPOINTS:
        return None

    if getattr(g, "current_user", None) is not None:
        return None

    return redirect(url_for("login_page", next=request.path))


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if getattr(g, "current_user", None) is None:
            return redirect(url_for("login_page", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


# -----------------------------------------------------------------------------
# JSON <-> DB conversion helpers
# -----------------------------------------------------------------------------
def get_connection_target(connection: dict[str, Any]) -> str | None:
    return connection.get("target") or connection.get("topic")


def set_connection_target(connection: dict[str, Any], target_id: str) -> dict[str, Any]:
    normalized = dict(connection)
    normalized.pop("topic", None)
    normalized["target"] = target_id
    return normalized


def normalize_graph_schema(lecture_graph: dict[str, Any]) -> dict[str, Any]:
    topics = lecture_graph.setdefault("topics", {})
    lecture_graph.setdefault("files", [])
    lecture_graph.setdefault("lectures", [])
    lecture_graph.setdefault("lecture_meta", [])

    for topic_id, topic_data in list(topics.items()):
        if not isinstance(topic_data, dict):
            topics.pop(topic_id, None)
            continue

        topic_data.setdefault("title", "Untitled Topic")
        topic_data.setdefault("summary", "")
        topic_data.setdefault("lectures", [])
        topic_data.setdefault("lecture_priority", None)
        topic_data.setdefault("difficulty", None)
        topic_data.setdefault("file_id", None)

        normalized_connections = []
        for connection in topic_data.get("connections", []):
            if not isinstance(connection, dict):
                continue
            target_id = get_connection_target(connection)
            if not target_id:
                continue
            normalized_connections.append(set_connection_target(connection, target_id))
        topic_data["connections"] = normalized_connections

    return lecture_graph


def db_to_graph(db: Session, user: User) -> dict[str, Any]:
    files = db.scalars(select(StudyFile).where(StudyFile.user_id == user.id).order_by(StudyFile.name.asc())).all()
    topics = db.scalars(select(Topic).where(Topic.user_id == user.id).order_by(Topic.title.asc())).all()
    connections = db.scalars(select(Connection).where(Connection.user_id == user.id).order_by(Connection.id.asc())).all()
    lecture_metas = db.scalars(
        select(LectureMeta).where(LectureMeta.user_id == user.id).order_by(LectureMeta.order.asc(), LectureMeta.id.asc())
    ).all()

    topic_map: dict[str, dict[str, Any]] = {}
    file_topics_map: dict[str, list[str]] = {file.id: [] for file in files}

    for topic in topics:
        lectures = json.loads(topic.lectures_json or "[]")
        topic_map[topic.id] = {
            "title": topic.title,
            "summary": topic.summary,
            "lectures": lectures,
            "lecture_priority": topic.lecture_priority,
            "difficulty": topic.difficulty,
            "connections": [],
            "file_id": topic.file_id,
        }
        if topic.file_id:
            file_topics_map.setdefault(topic.file_id, []).append(topic.id)

    for connection in connections:
        source_topic = topic_map.get(connection.source_topic_id)
        if source_topic is None:
            continue
        source_topic["connections"].append(
            {
                "type": connection.type,
                "strength": connection.strength,
                "reason": connection.reason,
                "target": connection.target_topic_id,
            }
        )

    lectures: list[str] = []
    if lecture_metas:
        lectures = [meta.title for meta in lecture_metas]
    else:
        seen = set()
        for topic_data in topic_map.values():
            for lecture_name in topic_data.get("lectures", []):
                if lecture_name and lecture_name not in seen:
                    seen.add(lecture_name)
                    lectures.append(lecture_name)

    graph = {
        "lectures": lectures,
        "topics": topic_map,
        "lecture_meta": [{"id": meta.lecture_id, "title": meta.title, "order": meta.order} for meta in lecture_metas],
        "files": [
            {
                "id": file.id,
                "name": file.name,
                "uploaded_at": file.uploaded_at,
                "status": file.status,
                "topics": file_topics_map.get(file.id, []),
            }
            for file in files
        ],
    }
    return validate_graph(graph)


def clear_user_graph(db: Session, user: User) -> None:
    db.query(Connection).filter(Connection.user_id == user.id).delete(synchronize_session=False)
    db.query(Topic).filter(Topic.user_id == user.id).delete(synchronize_session=False)
    db.query(StudyFile).filter(StudyFile.user_id == user.id).delete(synchronize_session=False)
    db.query(LectureMeta).filter(LectureMeta.user_id == user.id).delete(synchronize_session=False)


def graph_to_db(db: Session, user: User, lecture_graph: dict[str, Any]) -> None:
    lecture_graph = validate_graph(lecture_graph)
    clear_user_graph(db, user)

    for file_data in lecture_graph.get("files", []):
        db.add(
            StudyFile(
                id=file_data.get("id") or f"file_{secrets.token_hex(8)}",
                user_id=user.id,
                name=file_data.get("name", "Untitled File"),
                uploaded_at=file_data.get("uploaded_at"),
                status=file_data.get("status", "processed"),
            )
        )

    db.flush()

    for topic_id, topic_data in lecture_graph.get("topics", {}).items():
        db.add(
            Topic(
                id=topic_id,
                user_id=user.id,
                file_id=topic_data.get("file_id"),
                title=topic_data.get("title", "Untitled Topic"),
                summary=topic_data.get("summary", ""),
                lecture_priority=topic_data.get("lecture_priority"),
                difficulty=topic_data.get("difficulty"),
                lectures_json=json.dumps(topic_data.get("lectures", []), ensure_ascii=False),
            )
        )

    db.flush()

    for topic_id, topic_data in lecture_graph.get("topics", {}).items():
        for connection in topic_data.get("connections", []):
            target_id = get_connection_target(connection)
            if not target_id or target_id not in lecture_graph.get("topics", {}):
                continue
            db.add(
                Connection(
                    user_id=user.id,
                    source_topic_id=topic_id,
                    target_topic_id=target_id,
                    type=connection.get("type", "related"),
                    strength=connection.get("strength"),
                    reason=connection.get("reason", "These topics are connected."),
                )
            )

    for lecture_meta in lecture_graph.get("lecture_meta", []):
        db.add(
            LectureMeta(
                user_id=user.id,
                lecture_id=lecture_meta.get("id", ""),
                title=lecture_meta.get("title", "Untitled Lecture"),
                order=lecture_meta.get("order"),
            )
        )

    db.commit()


# -----------------------------------------------------------------------------
# Pure graph helpers kept from your current JSON design
# -----------------------------------------------------------------------------
def remove_topic_references(topics: dict[str, dict[str, Any]], removed_topic_ids: set[str]) -> int:
    removed_connections = 0
    for topic_id, topic_data in topics.items():
        if topic_id in removed_topic_ids:
            continue
        original_connections = topic_data.get("connections", [])
        filtered_connections = [
            connection for connection in original_connections if get_connection_target(connection) not in removed_topic_ids
        ]
        removed_connections += len(original_connections) - len(filtered_connections)
        topic_data["connections"] = filtered_connections
    return removed_connections


def prune_unused_lectures(lecture_graph: dict[str, Any]) -> None:
    topics = lecture_graph.get("topics", {})
    used_lectures: set[str] = set()
    for topic_data in topics.values():
        for lecture_name in topic_data.get("lectures", []):
            if lecture_name:
                used_lectures.add(lecture_name)

    lecture_graph["lectures"] = [lecture_name for lecture_name in lecture_graph.get("lectures", []) if lecture_name in used_lectures]
    lecture_graph["lecture_meta"] = [
        lecture_meta for lecture_meta in lecture_graph.get("lecture_meta", []) if lecture_meta.get("title") in used_lectures
    ]


def delete_topics_from_graph(lecture_graph: dict[str, Any], topic_ids_to_delete: list[str]) -> dict[str, int]:
    topics = lecture_graph.setdefault("topics", {})
    removed_topic_ids = {topic_id for topic_id in topic_ids_to_delete if topic_id in topics}
    if not removed_topic_ids:
        return {"deleted_topics": 0, "deleted_connections": 0}

    deleted_connections = remove_topic_references(topics, removed_topic_ids)
    for topic_id in removed_topic_ids:
        topics.pop(topic_id, None)

    for file_data in lecture_graph.setdefault("files", []):
        file_data["topics"] = [topic_id for topic_id in file_data.get("topics", []) if topic_id not in removed_topic_ids]

    prune_unused_lectures(lecture_graph)
    return {"deleted_topics": len(removed_topic_ids), "deleted_connections": deleted_connections}


def delete_file_from_graph(lecture_graph: dict[str, Any], file_id: str) -> dict[str, int | str]:
    files = lecture_graph.setdefault("files", [])
    file_record = next((item for item in files if item.get("id") == file_id), None)
    if not file_record:
        return {"deleted_files": 0, "deleted_topics": 0, "deleted_connections": 0, "file_name": ""}

    topic_ids_to_delete = [topic_id for topic_id in file_record.get("topics", []) if topic_id]
    delete_result = delete_topics_from_graph(lecture_graph, topic_ids_to_delete)
    lecture_graph["files"] = [item for item in files if item.get("id") != file_id]
    return {
        "deleted_files": 1,
        "deleted_topics": delete_result["deleted_topics"],
        "deleted_connections": delete_result["deleted_connections"],
        "file_name": file_record.get("name", "Untitled File"),
    }


def invert_connection_type(connection_type: str) -> str:
    if connection_type == "leads_to":
        return "prerequisite"
    if connection_type == "prerequisite":
        return "leads_to"
    return "related"


def build_card_topics(topics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    inferred_connections = {topic_id: [] for topic_id in topics}
    for source_id, source_data in topics.items():
        for connection in source_data.get("connections", []):
            target_id = get_connection_target(connection)
            if not target_id or target_id not in topics:
                continue
            inferred_connections[source_id].append(
                {
                    "topic": target_id,
                    "type": connection.get("type", "related"),
                    "strength": connection.get("strength"),
                    "reason": connection.get("reason", "These topics are connected."),
                }
            )
            reverse_type = invert_connection_type(connection.get("type", "related"))
            inferred_connections[target_id].append(
                {
                    "topic": source_id,
                    "type": reverse_type,
                    "strength": connection.get("strength"),
                    "reason": connection.get("reason", "These topics are connected."),
                }
            )

    card_topics = []
    for topic_id, topic_data in topics.items():
        deduped_connections = []
        seen = set()
        for connection in inferred_connections.get(topic_id, []):
            key = (connection.get("topic"), connection.get("type"))
            if key in seen:
                continue
            seen.add(key)
            deduped_connections.append(connection)

        card_topics.append(
            {
                "id": topic_id,
                "title": topic_data.get("title", "Untitled Topic"),
                "summary": topic_data.get("summary", "No summary available yet."),
                "lectures": topic_data.get("lectures", []),
                "file_id": topic_data.get("file_id"),
                "connections": deduped_connections,
            }
        )
    return card_topics


def build_quiz_questions(topics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    topic_items = list(topics.items())
    questions = []
    for index, (topic_id, topic_data) in enumerate(topic_items):
        correct_summary = topic_data.get("summary", "No summary available yet.")
        distractor_pool = [
            other_data.get("summary", "No summary available yet.")
            for other_id, other_data in topic_items
            if other_id != topic_id
        ]
        rng = random.Random(f"studygraph-{topic_id}")
        distractors = distractor_pool[:]
        rng.shuffle(distractors)
        option_texts = distractors[:3] + [correct_summary]
        rng.shuffle(option_texts)

        options = []
        correct_option_index = 0
        for option_index, option_text in enumerate(option_texts):
            is_correct = option_text == correct_summary
            if is_correct:
                correct_option_index = option_index
            options.append({"index": option_index, "text": option_text, "is_correct": is_correct})

        questions.append(
            {
                "index": index,
                "topic_id": topic_id,
                "topic_title": topic_data.get("title", "Untitled Topic"),
                "prompt": f"Which summary best matches {topic_data.get('title', 'this topic')}?",
                "options": options,
                "correct_option_index": correct_option_index,
            }
        )
    return questions


def build_library_files(lecture_graph: dict[str, Any]) -> list[dict[str, Any]]:
    topics = lecture_graph.get("topics", {})
    library_files = []
    for file_data in lecture_graph.get("files", []):
        topic_ids = [topic_id for topic_id in file_data.get("topics", []) if topic_id in topics]
        file_topics = []
        for topic_id in topic_ids:
            topic_data = topics[topic_id]
            file_topics.append(
                {
                    "id": topic_id,
                    "title": topic_data.get("title", "Untitled Topic"),
                    "summary": topic_data.get("summary", "No summary available yet."),
                    "lectures": topic_data.get("lectures", []),
                }
            )
        library_files.append(
            {
                "id": file_data.get("id", ""),
                "name": file_data.get("name", "Untitled File"),
                "uploaded_at": file_data.get("uploaded_at"),
                "status": file_data.get("status", "processed"),
                "topic_count": len(file_topics),
                "topics": file_topics,
            }
        )
    return library_files


# -----------------------------------------------------------------------------
# Upload and graph helpers
# -----------------------------------------------------------------------------
def slugify(value: str) -> str:
    cleaned = []
    previous_was_separator = False
    for char in value.lower().strip():
        if char.isalnum():
            cleaned.append(char)
            previous_was_separator = False
            continue
        if not previous_was_separator:
            cleaned.append("_")
            previous_was_separator = True
    slug = "".join(cleaned).strip("_")
    return slug or f"item_{secrets.token_hex(4)}"


def get_uploaded_files_from_request() -> list[Any]:
    candidates: list[Any] = []
    for field_name in ("files", "file", "documents", "uploads"):
        candidates.extend(request.files.getlist(field_name))

    unique_files = []
    seen = set()
    for storage in candidates:
        if not storage or not getattr(storage, "filename", ""):
            continue
        identity = (storage.name, storage.filename)
        if identity in seen:
            continue
        seen.add(identity)
        unique_files.append(storage)
    return unique_files


def is_allowed_upload(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS


def save_uploaded_files_locally(user: User, uploaded_files: list[Any]) -> list[dict[str, Any]]:
    user_upload_dir = UPLOAD_ROOT / f"user_{user.id}"
    user_upload_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[dict[str, Any]] = []
    uploaded_at = datetime.now().date().isoformat()

    for uploaded_file in uploaded_files:
        original_name = secure_filename(uploaded_file.filename) or f"upload_{secrets.token_hex(4)}"
        extension = Path(original_name).suffix.lower()
        generated_file_id = f"file_{slugify(Path(original_name).stem)}_{secrets.token_hex(4)}"
        disk_name = f"{generated_file_id}{extension}"
        local_path = user_upload_dir / disk_name
        uploaded_file.save(local_path)
        saved_files.append(
            {
                "id": generated_file_id,
                "name": original_name,
                "uploaded_at": uploaded_at,
                "status": "processed",
                "local_path": local_path,
                "mime_type": mimetypes.guess_type(str(local_path))[0] or "application/octet-stream",
            }
        )

    return saved_files


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def dedupe_topic_connections(topic_id: str, topic_data: dict[str, Any]) -> None:
    unique_connections = []
    seen_exact = set()

    for connection in topic_data.get("connections", []):
        if not isinstance(connection, dict):
            continue

        target = get_connection_target(connection)
        relation_type = connection.get("type")
        reason = (connection.get("reason") or "").strip()
        strength = connection.get("strength")

        if not target or not relation_type:
            continue

        exact_key = (relation_type, target, reason, strength)
        if exact_key in seen_exact:
            continue

        seen_exact.add(exact_key)
        unique_connections.append(set_connection_target(connection, target))

    topic_data["connections"] = unique_connections


def remove_bidirectional_duplicates(lecture_graph: dict[str, Any]) -> dict[str, Any]:
    topics = lecture_graph.get("topics", {})

    for topic_id, topic_data in topics.items():
        dedupe_topic_connections(topic_id, topic_data)

    reverse_pair_choices: dict[tuple[str, str, str], tuple[str, str]] = {}

    for source_id in sorted(topics.keys()):
        connections = topics[source_id].get("connections", [])
        for connection in connections:
            target_id = get_connection_target(connection)
            relation_type = connection.get("type")
            if not target_id or not relation_type or target_id not in topics:
                continue

            pair_key = tuple(sorted((source_id, target_id)) + [relation_type])
            chosen = reverse_pair_choices.get(pair_key)

            if chosen is None:
                reverse_pair_choices[pair_key] = (source_id, target_id)
                continue

            kept_source, kept_target = chosen
            if (source_id, target_id) < (kept_source, kept_target):
                reverse_pair_choices[pair_key] = (source_id, target_id)

    for source_id, topic_data in topics.items():
        filtered_connections = []
        for connection in topic_data.get("connections", []):
            target_id = get_connection_target(connection)
            relation_type = connection.get("type")
            if not target_id or not relation_type or target_id not in topics:
                filtered_connections.append(connection)
                continue

            pair_key = tuple(sorted((source_id, target_id)) + [relation_type])
            kept_source, kept_target = reverse_pair_choices[pair_key]

            if (source_id, target_id) == (kept_source, kept_target):
                filtered_connections.append(connection)

        topic_data["connections"] = filtered_connections

    return lecture_graph


def limit_topic_connections(topic_data: dict[str, Any], max_connections: int = 2) -> None:
    connections = []
    for connection in topic_data.get("connections", []):
        if not isinstance(connection, dict):
            continue
        target = get_connection_target(connection)
        relation_type = connection.get("type")
        if not target or not relation_type:
            continue
        connections.append(set_connection_target(connection, target))

    def score(connection: dict[str, Any]) -> tuple[int, float]:
        relation_type = connection.get("type")
        strength = connection.get("strength")
        if not isinstance(strength, (int, float)):
            strength = 0.0

        type_priority = {
            "prerequisite": 3,
            "leads_to": 2,
            "related": 1,
        }.get(relation_type, 0)

        return (type_priority, float(strength))

    connections.sort(key=score, reverse=True)
    topic_data["connections"] = connections[:max_connections]


def validate_graph(lecture_graph: dict[str, Any]) -> dict[str, Any]:
    lecture_graph = normalize_graph_schema(lecture_graph)
    prune_unused_lectures(lecture_graph)
    remove_bidirectional_duplicates(lecture_graph)

    for topic_data in lecture_graph.get("topics", {}).values():
        limit_topic_connections(topic_data, max_connections=2)

    return lecture_graph


def unique_topic_id(candidate_id: str, existing_topics: dict[str, Any], new_topics: dict[str, Any]) -> str:
    base_id = slugify(candidate_id)
    if base_id not in existing_topics and base_id not in new_topics:
        return base_id

    suffix = 2
    while f"{base_id}_{suffix}" in existing_topics or f"{base_id}_{suffix}" in new_topics:
        suffix += 1
    return f"{base_id}_{suffix}"


def merge_graph_fragments(base_graph: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    base_graph = normalize_graph_schema(json.loads(json.dumps(base_graph)))
    fragment = normalize_graph_schema(json.loads(json.dumps(fragment)))

    existing_topics = base_graph.setdefault("topics", {})
    new_topics: dict[str, dict[str, Any]] = {}
    topic_id_map: dict[str, str] = {}

    for original_topic_id, topic_data in fragment.get("topics", {}).items():
        final_topic_id = unique_topic_id(original_topic_id, existing_topics, new_topics)
        topic_id_map[original_topic_id] = final_topic_id
        copied_topic = dict(topic_data)
        copied_topic["connections"] = [dict(connection) for connection in topic_data.get("connections", [])]
        new_topics[final_topic_id] = copied_topic

    for final_topic_id, topic_data in new_topics.items():
        fixed_connections = []
        for connection in topic_data.get("connections", []):
            target = get_connection_target(connection)
            if not target:
                continue
            remapped_target = topic_id_map.get(target, target)
            if remapped_target not in existing_topics and remapped_target not in new_topics:
                continue
            fixed_connections.append(set_connection_target(connection, remapped_target))
        topic_data["connections"] = fixed_connections

    existing_topics.update(new_topics)

    existing_file_ids = {file_data.get("id") for file_data in base_graph.get("files", [])}
    for file_data in fragment.get("files", []):
        if file_data.get("id") in existing_file_ids:
            continue
        remapped_topics = [topic_id_map.get(topic_id, topic_id) for topic_id in file_data.get("topics", [])]
        base_graph.setdefault("files", []).append({**file_data, "topics": remapped_topics})

    lecture_set = set(base_graph.get("lectures", []))
    for lecture in fragment.get("lectures", []):
        if lecture and lecture not in lecture_set:
            lecture_set.add(lecture)
            base_graph.setdefault("lectures", []).append(lecture)

    existing_lecture_meta_ids = {meta.get("id") for meta in base_graph.get("lecture_meta", [])}
    existing_lecture_titles = {meta.get("title") for meta in base_graph.get("lecture_meta", [])}
    for lecture_meta in fragment.get("lecture_meta", []):
        lecture_title = lecture_meta.get("title")
        lecture_id = lecture_meta.get("id") or slugify(lecture_title or "lecture")
        if lecture_id in existing_lecture_meta_ids or lecture_title in existing_lecture_titles:
            continue
        base_graph.setdefault("lecture_meta", []).append(
            {"id": lecture_id, "title": lecture_title or "Untitled Lecture", "order": lecture_meta.get("order")}
        )
        existing_lecture_meta_ids.add(lecture_id)
        existing_lecture_titles.add(lecture_title)

    prune_unused_lectures(base_graph)
    return validate_graph(base_graph)


# -----------------------------------------------------------------------------
# Routes: auth
# -----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if getattr(g, "current_user", None) is not None:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        next_url = request.form.get("next") or url_for("home")

        db = get_db()
        user = db.scalar(select(User).where(User.email == email))

        if user is None or not check_password_hash(user.password_hash, password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html", next_url=next_url)

        session.clear()
        session["user_id"] = user.id
        flash("Welcome back.", "success")
        return redirect(next_url)

    return render_template("login.html", next_url=request.args.get("next") or url_for("home"))


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if getattr(g, "current_user", None) is not None:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("signup.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("signup.html")

        db = get_db()
        existing_user = db.scalar(select(User).where(User.email == email))
        if existing_user is not None:
            flash("That email is already in use.", "error")
            return render_template("signup.html")

        api_key = request.form.get("api_key", "").strip() or None
        user = User(email=email, password_hash=generate_password_hash(password), openai_api_key=api_key)

        db.add(user)
        db.commit()
        db.refresh(user)

        session.clear()
        session["user_id"] = user.id
        flash("Account created.", "success")
        return redirect(url_for("home"))

    return render_template("signup.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout_page():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login_page"))


@app.route("/settings/api-key", methods=["POST"])
@login_required
def save_api_key_page():
    db = get_db()
    api_key = request.form.get("api_key", "").strip() or None
    g.current_user.openai_api_key = api_key
    db.add(g.current_user)
    db.commit()
    flash("API key saved.", "success")
    return redirect(request.referrer or url_for("home"))


# -----------------------------------------------------------------------------
# Routes: app pages
# -----------------------------------------------------------------------------
@app.route("/")
@login_required
def home():
    return redirect(url_for("upload_page"))


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_page():
    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        uploaded_files = get_uploaded_files_from_request()
        if not uploaded_files:
            message = "Please upload at least one supported file."
            if is_ajax:
                return jsonify({"ok": False, "error": message}), 400
            flash(message, "error")
            return redirect(url_for("upload_page"))

        unsupported = [file.filename for file in uploaded_files if not is_allowed_upload(file.filename)]
        if unsupported:
            message = "Unsupported file type(s): " + ", ".join(unsupported)
            if is_ajax:
                return jsonify({"ok": False, "error": message}), 400
            flash(message, "error")
            return redirect(url_for("upload_page"))

        db = None
        try:
            client = get_openai_client_for_user(g.current_user, OPENAI_API_KEY)
            saved_files = save_uploaded_files_locally(g.current_user, uploaded_files)
            db = get_db()
            current_graph = db_to_graph(db, g.current_user)

            for batch_files in chunked(saved_files, BATCH_SIZE):
                fragment = generate_graph_fragment_from_uploads(client, current_graph, batch_files)
                current_graph = merge_graph_fragments(current_graph, fragment)

            graph_to_db(db, g.current_user, current_graph)

            if is_ajax:
                return jsonify(
                    {
                        "ok": True,
                        "processed_files": len(saved_files),
                        "redirect_url": url_for("library_page"),
                    }
                )

            flash(f"Processed {len(saved_files)} file(s) and updated your StudyGraph.", "success")
            return redirect(url_for("library_page"))
        except Exception as exc:
            if db is not None:
                db.rollback()

            error_text = str(exc)
            if "context_length_exceeded" in error_text:
                message = (
                    "Upload processing failed: this file batch is too large for the current AI request. "
                    "Try one file at a time, or split large PDFs/PPTX files into smaller parts."
                )
                status_code = 400
            else:
                message = f"Upload processing failed: {exc}"
                status_code = 500

            if is_ajax:
                return jsonify({"ok": False, "error": message}), status_code

            flash(message, "error")
            return redirect(url_for("upload_page"))

    return render_template("upload.html", active_page="upload")


@app.route("/library", methods=["GET", "POST"])
@login_required
def library_page():
    db = get_db()
    lecture_graph = db_to_graph(db, g.current_user)

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        file_id = request.form.get("file_id", "").strip()

        if not file_id:
            flash("Select a file first.", "error")
            return redirect(url_for("library_page"))

        if action == "delete":
            result = delete_file_from_graph(lecture_graph, file_id)
            if result["deleted_files"] == 0:
                flash("That file could not be found.", "error")
                return redirect(url_for("library_page"))

            graph_to_db(db, g.current_user, lecture_graph)
            flash(
                f"Deleted {result['file_name']} and removed {result['deleted_topics']} topic{'s' if result['deleted_topics'] != 1 else ''}.",
                "success",
            )
            return redirect(url_for("library_page"))

        if action == "reprocess":
            flash("Reprocess is not wired yet. Re-upload the file for now.", "error")
            return redirect(url_for("library_page"))

        flash("Unknown file action.", "error")
        return redirect(url_for("library_page"))

    library_files = build_library_files(lecture_graph)
    return render_template("library.html", active_page="library", library_files=library_files, total_files=len(library_files))


@app.route("/matrix")
@login_required
def matrix_page():
    db = get_db()
    lecture_graph = db_to_graph(db, g.current_user)
    return render_template(
        "matrix.html",
        active_page="matrix",
        lecture_graph=lecture_graph,
        lectures=lecture_graph.get("lectures", []),
        topics=lecture_graph.get("topics", {}),
    )


@app.route("/cards")
@login_required
def cards_page():
    db = get_db()
    lecture_graph = db_to_graph(db, g.current_user)
    topics = lecture_graph.get("topics", {})
    card_topics = build_card_topics(topics)
    return render_template(
        "cards.html",
        active_page="cards",
        card_topics=card_topics,
        topics_lookup=topics,
        total_cards=len(card_topics),
    )


@app.route("/quiz", methods=["GET", "POST"])
@login_required
def quiz_page():
    db = get_db()
    lecture_graph = db_to_graph(db, g.current_user)
    topics = lecture_graph.get("topics", {})
    questions = build_quiz_questions(topics)

    if not questions:
        return render_template(
            "quiz.html",
            active_page="quiz",
            question=None,
            total_questions=0,
            current_number=0,
            result=None,
            selected_option_index=None,
            next_question_number=None,
        )

    requested_index = request.args.get("q", default=0, type=int)
    requested_index = max(0, min(requested_index, len(questions) - 1))
    question = questions[requested_index]

    result = None
    selected_option_index = None
    next_question_number = requested_index + 1 if requested_index + 1 < len(questions) else 0

    if request.method == "POST":
        selected_option_index = request.form.get("answer", type=int)
        is_correct = selected_option_index == question["correct_option_index"]
        result = {
            "is_correct": is_correct,
            "correct_option_index": question["correct_option_index"],
            "correct_text": question["options"][question["correct_option_index"]]["text"],
        }

    return render_template(
        "quiz.html",
        active_page="quiz",
        question=question,
        total_questions=len(questions),
        current_number=requested_index + 1,
        result=result,
        selected_option_index=selected_option_index,
        next_question_number=next_question_number,
    )


@app.route("/ask-ai", methods=["GET", "POST"])
@login_required
def ask_ai_page():
    db = get_db()
    lecture_graph = db_to_graph(db, g.current_user)
    topics = lecture_graph.get("topics", {})

    topic_list = [{"id": topic_id, "title": topic_data.get("title", "Untitled Topic")} for topic_id, topic_data in topics.items()]

    selected_topic_ids: list[str] = []
    question_text = ""
    answer_text = None
    answer_html = None

    if request.method == "POST":
        selected_topic_ids = request.form.getlist("topics")
        question_text = request.form.get("question", "").strip()
        try:
            answer_text = build_multi_topic_answer(g.current_user, selected_topic_ids, topics, question_text)

            if answer_text:
                raw_html = markdown.markdown(
                    answer_text,
                    extensions=["extra", "sane_lists", "nl2br"]
                )

                allowed_tags = list(bleach.sanitizer.ALLOWED_TAGS) + [
                    "p", "br", "ul", "ol", "li",
                    "strong", "em", "h1", "h2", "h3", "h4",
                    "blockquote", "code", "pre"
                ]

                answer_html = bleach.clean(raw_html, tags=allowed_tags, strip=True)

        except Exception as exc:
            flash(f"Ask AI failed: {exc}", "error")

    return render_template(
        "ask_ai.html",
        active_page="ask_ai",
        topic_list=topic_list,
        selected_topic_ids=selected_topic_ids,
        question_text=question_text,
        answer_text=answer_text,
        answer_html=answer_html,
    )


@app.route("/edit-topics", methods=["GET", "POST"])
@login_required
def edit_topics_page():
    db = get_db()
    if request.method == "POST":
        selected_topic_ids = request.form.getlist("topics")
        if not selected_topic_ids:
            flash("Select at least one topic to delete.", "error")
            return redirect(url_for("edit_topics_page"))

        lecture_graph = db_to_graph(db, g.current_user)
        deletion_result = delete_topics_from_graph(lecture_graph, selected_topic_ids)
        if deletion_result["deleted_topics"] == 0:
            flash("No matching topics were found to delete.", "error")
            return redirect(url_for("edit_topics_page"))

        graph_to_db(db, g.current_user, lecture_graph)
        flash(
            f"Deleted {deletion_result['deleted_topics']} topic{'s' if deletion_result['deleted_topics'] != 1 else ''} and removed {deletion_result['deleted_connections']} connection{'s' if deletion_result['deleted_connections'] != 1 else ''}.",
            "success",
        )
        return redirect(url_for("edit_topics_page"))

    lecture_graph = db_to_graph(db, g.current_user)
    topics = lecture_graph.get("topics", {})
    topic_list = [
        {
            "id": topic_id,
            "title": topic_data.get("title", "Untitled Topic"),
            "summary": topic_data.get("summary", ""),
            "lectures": topic_data.get("lectures", []),
            "file_id": topic_data.get("file_id"),
        }
        for topic_id, topic_data in topics.items()
    ]
    return render_template("edit_topics.html", active_page="edit_topics", topic_list=topic_list)


@app.route("/admin/import-json", methods=["POST"])
@login_required
def import_graph_json_page():
    raw_json = request.form.get("lecture_graph_json", "").strip()
    if not raw_json:
        flash("No JSON payload was provided.", "error")
        return redirect(url_for("library_page"))

    try:
        lecture_graph = validate_graph(json.loads(raw_json))
    except json.JSONDecodeError:
        flash("Invalid JSON payload.", "error")
        return redirect(url_for("library_page"))

    db = get_db()
    graph_to_db(db, g.current_user, lecture_graph)
    flash("Lecture graph imported into PostgreSQL.", "success")
    return redirect(url_for("library_page"))


@app.route("/admin/export-json")
@login_required
def export_graph_json_page():
    db = get_db()
    lecture_graph = db_to_graph(db, g.current_user)
    return app.response_class(response=json.dumps(lecture_graph, indent=2, ensure_ascii=False), status=200, mimetype="application/json")


@app.route("/templates/lecture-graph-template.json")
@login_required
def lecture_graph_template_page():
    return app.response_class(
        response=json.dumps(DEFAULT_GRAPH_TEMPLATE, indent=2, ensure_ascii=False),
        status=200,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=lecture_graph_template.json"},
    )


if __name__ == "__main__":
    from waitress import serve

    print("StudyGraph running on: http://127.0.0.1:5000")
    serve(app, host="0.0.0.0", port=5000, threads=8)

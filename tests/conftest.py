"""Shared fixtures for the StudyGraph test suite.

The app reads its config from environment variables at import time, so they are
set here before `app` is imported. Tests run against a throwaway SQLite file and
never call the real OpenAI API.
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="studygraph-tests-"))
os.environ["FLASK_SECRET_KEY"] = "test-secret"
# Set TEST_DATABASE_URL to run the suite against a real (empty, disposable) PostgreSQL database.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR / 'test.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP_DIR / "uploads")
os.environ.pop("OPENAI_API_KEY", None)

import app as studygraph_app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    studygraph_app.Base.metadata.drop_all(studygraph_app.engine)
    studygraph_app.Base.metadata.create_all(studygraph_app.engine)
    yield


@pytest.fixture
def flask_app():
    studygraph_app.app.config.update(TESTING=True)
    return studygraph_app.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def signup(client, email="student@example.com", password="pw123456", api_key=""):
    return client.post(
        "/signup",
        data={"email": email, "password": password, "confirm_password": password, "api_key": api_key},
    )


@pytest.fixture
def logged_in_client(client):
    signup(client)
    return client


def make_graph(topic_ids, file_id="file_a", lecture="Lecture A", connections=None):
    """Small helper that builds a graph dict in the app's internal format."""
    connections = connections or {}
    return {
        "lectures": [lecture],
        "lecture_meta": [{"id": "lec_a", "title": lecture, "order": 1}],
        "files": [{"id": file_id, "name": f"{file_id}.pdf", "uploaded_at": "2026-01-01", "status": "processed", "topics": list(topic_ids)}],
        "topics": {
            topic_id: {
                "title": topic_id.replace("_", " ").title(),
                "summary": f"Summary of {topic_id}",
                "lectures": [lecture],
                "lecture_priority": index,
                "difficulty": 2,
                "file_id": file_id,
                "connections": connections.get(topic_id, []),
            }
            for index, topic_id in enumerate(topic_ids)
        },
    }


class FakeOpenAI:
    """Records calls and returns canned Responses API output."""

    def __init__(self, output_text):
        self.output_text = output_text
        self.uploaded = []
        self.deleted = []
        self.requests = []
        self.files = SimpleNamespace(create=self._create_file, delete=self._delete_file)
        self.responses = SimpleNamespace(create=self._create_response)

    def _create_file(self, file, purpose):
        file_id = f"oa_file_{len(self.uploaded)}"
        self.uploaded.append((file_id, purpose))
        return SimpleNamespace(id=file_id)

    def _delete_file(self, file_id):
        self.deleted.append(file_id)

    def _create_response(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(output_parsed=None, output_text=self.output_text, output=[])

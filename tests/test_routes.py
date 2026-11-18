"""Route-level tests through Flask's test client. OpenAI calls are mocked."""
import io
import json

import app as sg
from conftest import FakeOpenAI, make_graph, signup


def import_graph(client, graph):
    return client.post("/admin/import-json", data={"lecture_graph_json": json.dumps(graph)})


def test_private_pages_redirect_to_login(client):
    for path in ["/", "/upload", "/library", "/matrix", "/cards", "/quiz", "/ask-ai", "/edit-topics", "/admin/export-json"]:
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"]


def test_signup_login_logout_flow(client):
    assert signup(client).status_code == 302
    client.post("/logout")
    bad = client.post("/login", data={"email": "student@example.com", "password": "wrong"})
    assert b"Incorrect email or password" in bad.data
    good = client.post("/login", data={"email": "Student@Example.com ", "password": "pw123456"})
    assert good.status_code == 302


def test_signup_rejects_mismatched_and_duplicate(client):
    r = client.post("/signup", data={"email": "a@b.c", "password": "x", "confirm_password": "y"})
    assert b"Passwords do not match" in r.data
    signup(client, "a@b.c")
    client.post("/logout")
    assert b"already in use" in signup(client, "a@b.c").data


def test_login_next_param_cannot_redirect_off_site(client):
    signup(client)
    client.post("/logout")
    for evil in ["https://evil.example", "//evil.example", "/\\evil.example"]:
        r = client.post("/login", data={"email": "student@example.com", "password": "pw123456", "next": evil})
        assert "evil.example" not in r.headers["Location"], evil
        client.post("/logout")
    r = client.post("/login", data={"email": "student@example.com", "password": "pw123456", "next": "/cards"})
    assert r.headers["Location"].endswith("/cards")


def test_all_pages_render_with_a_graph(logged_in_client):
    graph = make_graph(["stack", "queue", "deque"], connections={"stack": [{"type": "leads_to", "strength": 0.7, "reason": "r", "target": "queue"}]})
    import_graph(logged_in_client, graph)
    for path in ["/upload", "/library", "/matrix", "/cards", "/quiz", "/ask-ai", "/edit-topics", "/templates/lecture-graph-template.json"]:
        assert logged_in_client.get(path).status_code == 200, path


def test_import_export_round_trip(logged_in_client):
    import_graph(logged_in_client, make_graph(["a", "b"]))
    exported = logged_in_client.get("/admin/export-json").get_json()
    assert set(exported["topics"]) == {"a", "b"}
    assert exported["files"][0]["topics"] == ["a", "b"]


def test_import_rejects_non_object_json(logged_in_client):
    r = logged_in_client.post("/admin/import-json", data={"lecture_graph_json": "[1, 2]"}, follow_redirects=True)
    assert r.status_code == 200
    assert b"must be an object" in r.data


def test_two_users_can_store_the_same_topic_ids(client):
    """The AI gives every user the same ids for the same lecture."""
    graph = make_graph(["binary_search_tree", "bst_runtime"], file_id="file_bst")
    signup(client, "one@example.com")
    import_graph(client, graph)
    client.post("/logout")

    signup(client, "two@example.com")
    r = import_graph(client, graph)
    assert r.status_code == 302
    exported = client.get("/admin/export-json").get_json()
    assert len(exported["topics"]) == 2
    # second user's ids were namespaced, links/file refs stayed consistent
    assert all(tid in exported["topics"] for tid in exported["files"][0]["topics"])


def test_quiz_marks_answers(logged_in_client):
    import_graph(logged_in_client, make_graph(["a", "b", "c", "d"]))
    question = sg.build_quiz_questions(logged_in_client.get("/admin/export-json").get_json()["topics"])[0]
    r = logged_in_client.post("/quiz?q=0", data={"answer": question["correct_option_index"]})
    assert r.status_code == 200 and b"Correct" in r.data


def test_edit_topics_deletes_selected(logged_in_client):
    import_graph(logged_in_client, make_graph(["a", "b"]))
    logged_in_client.post("/edit-topics", data={"topics": ["a"]})
    assert set(logged_in_client.get("/admin/export-json").get_json()["topics"]) == {"b"}


def test_library_delete_file_removes_its_topics(logged_in_client):
    import_graph(logged_in_client, make_graph(["a", "b"], file_id="file_x"))
    logged_in_client.post("/library", data={"action": "delete", "file_id": "file_x"})
    exported = logged_in_client.get("/admin/export-json").get_json()
    assert exported["topics"] == {} and exported["files"] == []


def test_upload_rejects_missing_and_unsupported_files(logged_in_client):
    headers = {"X-Requested-With": "XMLHttpRequest"}
    r = logged_in_client.post("/upload", data={}, headers=headers)
    assert r.status_code == 400
    r = logged_in_client.post("/upload", data={"files": (io.BytesIO(b"x"), "virus.exe")}, headers=headers, content_type="multipart/form-data")
    assert r.status_code == 400 and "Unsupported" in r.get_json()["error"]


def test_upload_accepts_all_nine_file_types():
    assert sorted(sg.ALLOWED_UPLOAD_EXTENSIONS) == sorted([".pdf", ".txt", ".md", ".doc", ".docx", ".ppt", ".pptx", ".csv", ".json"])
    assert sg.is_allowed_upload("Lecture 1.PDF") and not sg.is_allowed_upload("notes.zip")


def test_upload_builds_graph_in_batches(logged_in_client, monkeypatch):
    calls = []

    def fake_generate(client, current_graph, batch_files):
        calls.append([f["name"] for f in batch_files])
        return {
            "lectures": ["L"], "lecture_meta": [],
            "topics": {f"topic_{f['id']}": {"title": f["name"], "summary": "s", "lectures": ["L"], "file_id": f["id"], "connections": []} for f in batch_files},
            "files": [{"id": f["id"], "name": f["name"], "uploaded_at": f["uploaded_at"], "status": "processed", "topics": [f"topic_{f['id']}"]} for f in batch_files],
        }

    monkeypatch.setattr(sg, "get_openai_client_for_user", lambda user, key: FakeOpenAI("{}"))
    monkeypatch.setattr(sg, "generate_graph_fragment_from_uploads", fake_generate)

    files = [(io.BytesIO(b"text"), f"notes{i}.txt") for i in range(3)]
    r = logged_in_client.post("/upload", data={"files": files}, headers={"X-Requested-With": "XMLHttpRequest"}, content_type="multipart/form-data")
    assert r.get_json()["ok"] is True and r.get_json()["processed_files"] == 3
    assert [len(batch) for batch in calls] == [2, 1]  # BATCH_SIZE = 2
    assert len(logged_in_client.get("/admin/export-json").get_json()["topics"]) == 3


def test_upload_reports_ai_errors_as_json(logged_in_client, monkeypatch):
    def boom(*args):
        raise RuntimeError("context_length_exceeded")

    monkeypatch.setattr(sg, "get_openai_client_for_user", lambda user, key: FakeOpenAI("{}"))
    monkeypatch.setattr(sg, "generate_graph_fragment_from_uploads", boom)
    r = logged_in_client.post("/upload", data={"files": (io.BytesIO(b"x"), "a.txt")}, headers={"X-Requested-With": "XMLHttpRequest"}, content_type="multipart/form-data")
    assert r.status_code == 400 and "too large" in r.get_json()["error"]


def test_ask_ai_uses_server_key_when_user_has_none(logged_in_client, monkeypatch):
    import studygraph_ai

    import_graph(logged_in_client, make_graph(["a"]))
    monkeypatch.setattr(sg, "OPENAI_API_KEY", "sk-server")
    seen = {}

    def fake_client(user, default_api_key=None):
        seen["key"] = default_api_key
        return FakeOpenAI("**Answer** from graph")

    monkeypatch.setattr(studygraph_ai, "get_openai_client_for_user", fake_client)
    r = logged_in_client.post("/ask-ai", data={"topics": ["a"], "question": "What is a?"})
    assert seen["key"] == "sk-server"
    assert b"<strong>Answer</strong>" in r.data


def test_ask_ai_output_is_sanitized(logged_in_client, monkeypatch):
    import studygraph_ai

    import_graph(logged_in_client, make_graph(["a"]))
    monkeypatch.setattr(studygraph_ai, "get_openai_client_for_user", lambda u, k=None: FakeOpenAI("<script>alert(1)</script>hi"))
    r = logged_in_client.post("/ask-ai", data={"topics": ["a"], "question": "q"})
    assert b"<script>alert(1)</script>" not in r.data


def test_api_key_can_be_saved(logged_in_client):
    logged_in_client.post("/settings/api-key", data={"api_key": "sk-user"})
    with sg.SessionLocal() as db:
        assert db.query(sg.User).one().openai_api_key == "sk-user"

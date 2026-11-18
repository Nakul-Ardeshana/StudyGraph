"""OpenAI integration layer, with the client mocked out."""
import json

import pytest

import studygraph_ai as ai
from conftest import FakeOpenAI


def saved_file(tmp_path, file_id="file_lec1_abcd", name="lec1.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake")
    return {"id": file_id, "name": name, "uploaded_at": "2026-01-01", "local_path": path}


def ai_fragment(file_id="file_lec1_abcd"):
    return {
        "lectures": ["Binary Search Trees"],
        "lecture_meta": [{"id": "bst", "title": "Binary Search Trees", "order": 1}],
        "files": [],
        "topics": [
            {"id": "bst_search", "title": "BST Search", "summary": "Search", "lectures": ["Binary Search Trees"],
             "lecture_priority": 1, "difficulty": 2, "file_id": file_id,
             "connections": [{"type": "leads_to", "strength": 0.8, "reason": "r", "target": "bst_runtime"}]},
            {"id": "bst_runtime", "title": "BST Runtime", "summary": "Runtime", "lectures": ["Binary Search Trees"],
             "lecture_priority": 2, "difficulty": 3, "file_id": file_id, "connections": []},
        ],
    }


def test_generate_fragment_parses_schema_output_and_cleans_up_files(tmp_path):
    client = FakeOpenAI(json.dumps(ai_fragment()))
    fragment = ai.generate_graph_fragment_from_uploads(client, {"topics": {}}, [saved_file(tmp_path)])

    assert set(fragment["topics"]) == {"bst_search", "bst_runtime"}
    assert "id" not in fragment["topics"]["bst_search"]
    assert fragment["files"][0]["topics"] == ["bst_search", "bst_runtime"]
    # uploaded files are always deleted from OpenAI afterwards
    assert client.deleted == [fid for fid, _ in client.uploaded]
    request = client.requests[0]
    assert request["model"] == ai.OPENAI_GRAPH_MODEL
    assert request["text"]["format"]["strict"] is True


def test_unexpected_file_id_is_rejected_and_files_still_deleted(tmp_path):
    client = FakeOpenAI(json.dumps(ai_fragment(file_id="file_hallucinated")))
    with pytest.raises(ValueError, match="unexpected file_id"):
        ai.generate_graph_fragment_from_uploads(client, {"topics": {}}, [saved_file(tmp_path)])
    assert client.deleted == ["oa_file_0"]


def test_empty_response_raises():
    class Empty:
        output_parsed = None
        output_text = ""
        output = []

    with pytest.raises(ValueError):
        ai.extract_json_response(Empty())


def test_prompt_caps_existing_topics_and_summary_length():
    topics = {f"t{i:02d}": {"title": f"T{i}", "summary": "s" * 500, "position": i} for i in range(50)}
    prompt = json.loads(ai.build_graph_generation_prompt({"topics": topics}, []))
    ref = prompt["existing_topic_reference"]
    assert len(ref) == ai.MAX_EXISTING_TOPICS == 20
    assert all(len(item["summary"]) == ai.MAX_SUMMARY_CHARS == 80 for item in ref)


def test_prompt_sends_most_recent_topics_not_alphabetically_last():
    # Older topics have titles late in the alphabet; newer ones early.
    # The DB returns topics sorted by title, so order the dict that way too.
    old = {f"zz_old_{i:02d}": {"title": f"Zeta {i}", "summary": "", "position": i} for i in range(1, 31)}
    new = {f"aa_new_{i:02d}": {"title": f"Alpha {i}", "summary": "", "position": 100 + i} for i in range(1, 11)}
    title_sorted = dict(sorted({**old, **new}.items(), key=lambda kv: kv[1]["title"]))

    prompt = json.loads(ai.build_graph_generation_prompt({"topics": title_sorted}, []))
    sent = {item["id"] for item in prompt["existing_topic_reference"]}
    assert set(new) <= sent  # all 10 newest topics are included


def test_client_falls_back_to_server_key():
    class User:
        openai_api_key = None

    with pytest.raises(ValueError):
        ai.get_openai_client_for_user(User(), None)
    assert ai.get_openai_client_for_user(User(), "sk-test") is not None


def test_ask_ai_requires_selected_topics():
    assert ai.build_multi_topic_answer(None, [], {}, "What is a BST?") == "Please select at least one topic first."
    assert ai.build_multi_topic_answer(None, ["x"], {}, "   ") is None

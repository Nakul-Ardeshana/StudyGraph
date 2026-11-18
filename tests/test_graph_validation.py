"""The JSON validation layer that runs on every AI fragment and every DB load."""
import app as sg
from conftest import make_graph


def conn(target, type_="related", strength=0.5, reason="r"):
    return {"type": type_, "strength": strength, "reason": reason, "target": target}


def test_normalize_drops_bad_topics_and_accepts_legacy_topic_key():
    graph = {
        "topics": {
            "good": {"connections": [{"type": "related", "topic": "other"}, "junk", {"type": "related"}]},
            "bad": "not a dict",
        }
    }
    out = sg.normalize_graph_schema(graph)
    assert set(out["topics"]) == {"good"}
    assert out["topics"]["good"]["title"] == "Untitled Topic"
    # legacy "topic" key is rewritten to "target"; entries without a target are dropped
    assert out["topics"]["good"]["connections"] == [{"type": "related", "target": "other"}]
    assert out["files"] == [] and out["lectures"] == [] and out["lecture_meta"] == []


def test_exact_duplicate_connections_are_removed():
    topic = {"connections": [conn("b"), conn("b"), conn("b", reason="different")]}
    sg.dedupe_topic_connections("a", topic)
    assert len(topic["connections"]) == 2


def test_reverse_duplicate_link_keeps_one_direction():
    graph = make_graph(["a", "b"], connections={"a": [conn("b")], "b": [conn("a")]})
    sg.remove_bidirectional_duplicates(graph)
    assert graph["topics"]["a"]["connections"] == [conn("b")]
    assert graph["topics"]["b"]["connections"] == []


def test_reverse_links_with_different_types_are_both_kept():
    graph = make_graph(["a", "b"], connections={"a": [conn("b", "leads_to")], "b": [conn("a", "related")]})
    sg.remove_bidirectional_duplicates(graph)
    assert len(graph["topics"]["a"]["connections"]) == 1
    assert len(graph["topics"]["b"]["connections"]) == 1


def test_each_topic_keeps_its_two_strongest_connections():
    topic = {
        "connections": [
            conn("w", "related", 0.9),
            conn("x", "prerequisite", 0.1),
            conn("y", "leads_to", 0.8),
            conn("z", "leads_to", 0.2),
        ]
    }
    sg.limit_topic_connections(topic, max_connections=2)
    # type priority first (prerequisite > leads_to > related), then strength
    assert [c["target"] for c in topic["connections"]] == ["x", "y"]


def test_validate_graph_caps_every_topic_at_two_links():
    many = [conn(t, "related", s) for t, s in [("b", 0.1), ("c", 0.9), ("d", 0.5)]]
    graph = sg.validate_graph(make_graph(["a", "b", "c", "d"], connections={"a": many}))
    assert len(graph["topics"]["a"]["connections"]) == 2
    assert {c["target"] for c in graph["topics"]["a"]["connections"]} == {"c", "d"}


def test_unused_lectures_are_pruned():
    graph = make_graph(["a"])
    graph["lectures"].append("Orphan Lecture")
    graph["lecture_meta"].append({"id": "orphan", "title": "Orphan Lecture", "order": 2})
    sg.prune_unused_lectures(graph)
    assert graph["lectures"] == ["Lecture A"]
    assert [m["id"] for m in graph["lecture_meta"]] == ["lec_a"]


def test_merge_renames_colliding_topic_ids_and_remaps_links():
    base = make_graph(["stack"])
    fragment = make_graph(["stack", "queue"], file_id="file_b", connections={"queue": [conn("stack")]})
    merged = sg.merge_graph_fragments(base, fragment)
    assert set(merged["topics"]) == {"stack", "stack_2", "queue"}
    # the fragment's own "stack" became stack_2, and queue's link follows it
    assert merged["topics"]["queue"]["connections"][0]["target"] == "stack_2"
    file_b = next(f for f in merged["files"] if f["id"] == "file_b")
    assert set(file_b["topics"]) == {"stack_2", "queue"}


def test_merge_drops_links_to_unknown_topics():
    fragment = make_graph(["a"], connections={"a": [conn("does_not_exist")]})
    merged = sg.merge_graph_fragments(sg.DEFAULT_GRAPH_TEMPLATE, fragment)
    assert merged["topics"]["a"]["connections"] == []


def test_merge_assigns_increasing_positions():
    merged = sg.merge_graph_fragments(sg.DEFAULT_GRAPH_TEMPLATE, make_graph(["a", "b"]))
    merged = sg.merge_graph_fragments(merged, make_graph(["c"], file_id="file_c"))
    positions = {tid: t["position"] for tid, t in merged["topics"].items()}
    assert positions == {"a": 1, "b": 2, "c": 3}


def test_unique_topic_id_fits_column_length():
    long_id = "x" * 300
    assert len(sg.unique_topic_id(long_id, {}, {})) <= sg.MAX_ID_LENGTH


def test_delete_topics_removes_incoming_links_and_file_refs():
    graph = make_graph(["a", "b", "c"], connections={"a": [conn("b")], "c": [conn("b")]})
    result = sg.delete_topics_from_graph(graph, ["b"])
    assert result == {"deleted_topics": 1, "deleted_connections": 2}
    assert graph["files"][0]["topics"] == ["a", "c"]


def test_cards_show_links_from_both_sides():
    topics = make_graph(["a", "b"], connections={"a": [conn("b", "leads_to")]})["topics"]
    cards = {c["id"]: c for c in sg.build_card_topics(topics)}
    assert cards["a"]["connections"][0]["type"] == "leads_to"
    assert cards["b"]["connections"][0] == {**cards["b"]["connections"][0], "topic": "a", "type": "prerequisite"}


def test_quiz_never_has_two_correct_options():
    topics = make_graph(["a", "b", "c", "d"])["topics"]
    topics["b"]["summary"] = topics["a"]["summary"]  # two topics with identical summaries
    for question in sg.build_quiz_questions(topics):
        correct = [o for o in question["options"] if o["is_correct"]]
        texts = [o["text"] for o in question["options"]]
        assert len(correct) == 1
        assert len(texts) == len(set(texts))

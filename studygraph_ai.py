import json
import os
from typing import Any

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover
    raise SystemExit("openai is not installed. Run: pip install openai") from exc


OPENAI_GRAPH_MODEL = os.environ.get("OPENAI_GRAPH_MODEL", "gpt-5.4-mini")
OPENAI_ASK_MODEL = os.environ.get("OPENAI_ASK_MODEL", "gpt-5.4-nano")

MAX_EXISTING_TOPICS = 20
MAX_SUMMARY_CHARS = 80

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lectures", "topics", "lecture_meta", "files"],
    "properties": {
        "lectures": {
            "type": "array",
            "items": {"type": "string"},
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "title",
                    "summary",
                    "lectures",
                    "lecture_priority",
                    "difficulty",
                    "connections",
                    "file_id",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "lectures": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "lecture_priority": {"type": ["integer", "null"]},
                    "difficulty": {"type": ["integer", "null"]},
                    "connections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "strength", "reason", "target"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["leads_to", "prerequisite", "related"],
                                },
                                "strength": {"type": ["number", "null"]},
                                "reason": {"type": "string"},
                                "target": {"type": "string"},
                            },
                        },
                    },
                    "file_id": {"type": ["string", "null"]},
                },
            },
        },
        "lecture_meta": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "order"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "order": {"type": ["integer", "null"]},
                },
            },
        },
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "uploaded_at", "status", "topics"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "uploaded_at": {"type": ["string", "null"]},
                    "status": {"type": "string"},
                    "topics": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

GRAPH_SYSTEM_PROMPT = """
You convert uploaded study materials into a StudyGraph JSON fragment.

Return only valid JSON matching the provided schema.
Do not wrap the JSON in markdown.
Do not include commentary.

Your goal is to build a useful and readable study graph, not a shallow file summary.
Prefer clarity over completeness.
Avoid clutter.

Core requirements:
1. Output only the NEW graph fragment for the uploaded files.
2. Reuse the exact file_id values provided in the instructions.
3. Extract MULTIPLE TOPICS per file whenever the material contains multiple distinct ideas.
4. Do NOT collapse an entire lecture or PDF into a single topic unless the file is truly very short and covers only one concept.
5. Prefer smaller, teachable concepts over large umbrella summaries.
6. Topic ids must be lowercase snake_case and descriptive.
7. Each topic must have:
   - a concise title
   - a short but meaningful summary
   - the correct file_id
   - lecture names in human-readable form, not raw filenames
8. lecture_priority should reflect teaching order within the lecture/material.
9. difficulty should be an integer from 1 to 5.
10. Use only these connection types:
   - leads_to
   - prerequisite
   - related
11. Only create connections that are strongly supported by the material.
12. Prefer forward learning flow:
   foundational ideas -> methods -> analysis -> applications
13. Create only the strongest and most instructionally useful connections.
14. Prefer sparse graphs over cluttered graphs.
15. Most topics should have 0 to 2 outgoing connections.
16. Only add a connection when it adds clear study value.
17. You may connect to existing topic ids if there is a strong relationship, but avoid duplicates.
18. Keep all summaries grounded only in the uploaded material.
19. Use the existing topic reference context to connect new topics to earlier batches when justified.
20. Keep output concise enough to fit in one response. Focus on the strongest topics and strongest links first if the material is very dense.

Granularity rules:
- If a lecture introduces definitions, operations, properties, runtimes, variants, applications, or comparisons, these should usually become separate topics.
- Good topics are things like:
  "Map ADT", "Key-Value Entries", "put/get/remove", "Comparable Keys", "BST Search", "BST Runtime", "Balanced Trees", "2-4 Tree", "Red-Black Properties"
- Bad topics are oversized summaries like:
  "Binary Search Trees Lecture", "Everything about Maps", "Tree Concepts"

Lecture naming rules:
- Use clean lecture titles like:
  "Map Abstract Data Type"
  "Binary Search Trees"
- Do NOT use raw filenames like:
  "MapAbstractDataType.pdf"

Connection rules:
- Use one-sided meaningful links.
- Avoid storing both directions unless both are genuinely useful and distinct.
- Prefer links like:
  "Map ADT" leads_to "Binary Search Tree"
  instead of also adding the reverse unless needed.
- prerequisite means the target should be known first.
- leads_to means this concept naturally progresses into the target.
- related means conceptually connected but not a strict sequence.

Quality bar:
- The graph should feel like a study guide broken into digestible concepts.
- A file with several major subtopics should usually produce several topic nodes.
- Be specific, structured, and educational.
""".strip()

ASK_AI_SYSTEM_PROMPT = """
You are helping a student answer questions using only the selected StudyGraph topics.
Use only the provided topic titles, summaries, lectures, and connection reasons.
If the answer is not supported by the provided topics, say that clearly.

Return the answer in Markdown.
Use short sections and bullet points when helpful.
Do not use code fences unless they are actually needed.
""".strip()


def get_openai_client_for_user(user: Any, default_api_key: str | None = None) -> OpenAI:
    api_key = (
        getattr(user, "openai_api_key", None)
        or getattr(user, "gemini_api_key", None)
        or default_api_key
        or ""
    ).strip()
    if not api_key:
        raise ValueError("No OpenAI API key is set for this user.")
    return OpenAI(api_key=api_key)


def upload_files_to_openai(client: OpenAI, saved_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uploaded_files: list[dict[str, Any]] = []
    for file_info in saved_files:
        with open(file_info["local_path"], "rb") as handle:
            uploaded_file = client.files.create(file=handle, purpose="user_data")
        uploaded_files.append({**file_info, "openai_file_id": uploaded_file.id})
    return uploaded_files


def extract_json_response(response: Any) -> dict[str, Any]:
    if hasattr(response, "output_parsed") and isinstance(response.output_parsed, dict):
        return response.output_parsed

    text = getattr(response, "output_text", None)
    if text:
        return json.loads(text)

    output = getattr(response, "output", None) or []
    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "output_text":
                return json.loads(content.text)

    raise ValueError("OpenAI returned an empty or unparseable response.")


def convert_topics_array_to_map(fragment: dict[str, Any]) -> dict[str, Any]:
    raw_topics = fragment.get("topics", [])
    if isinstance(raw_topics, list):
        topic_map: dict[str, dict[str, Any]] = {}
        for topic in raw_topics:
            if not isinstance(topic, dict):
                continue
            topic_id = topic.get("id")
            if not topic_id:
                continue
            topic_copy = dict(topic)
            topic_copy.pop("id", None)
            topic_map[topic_id] = topic_copy
        fragment["topics"] = topic_map
    return fragment


def ensure_expected_file_ids(fragment: dict[str, Any], saved_files: list[dict[str, Any]]) -> None:
    expected_ids = {file_info["id"] for file_info in saved_files}
    for topic_id, topic_data in fragment.get("topics", {}).items():
        file_id = topic_data.get("file_id")
        if file_id is None:
            continue
        if file_id not in expected_ids:
            raise ValueError(f"Generated topic {topic_id} used unexpected file_id {file_id}.")


def normalize_fragment_files(fragment: dict[str, Any], saved_files: list[dict[str, Any]]) -> dict[str, Any]:
    topics_by_file: dict[str, list[str]] = {file_info["id"]: [] for file_info in saved_files}
    for topic_id, topic_data in fragment.get("topics", {}).items():
        file_id = topic_data.get("file_id")
        if file_id in topics_by_file:
            topics_by_file[file_id].append(topic_id)

    fragment["files"] = [
        {
            "id": file_info["id"],
            "name": file_info["name"],
            "uploaded_at": file_info["uploaded_at"],
            "status": "processed",
            "topics": topics_by_file[file_info["id"]],
        }
        for file_info in saved_files
    ]
    return fragment


def recent_topic_items(topics: dict[str, dict[str, Any]], limit: int = MAX_EXISTING_TOPICS) -> list[tuple[str, dict[str, Any]]]:
    """The most recently added topics, oldest first. The graph loaded from the DB is
    sorted by title, so slicing it directly would send the alphabetically-last
    topics instead of the recent ones. Topics without a position (created before
    positions were tracked) count as oldest and keep their existing order."""
    items = list(topics.items())
    items.sort(key=lambda item: item[1].get("position") if isinstance(item[1].get("position"), int) else 0)
    return items[-limit:] if limit > 0 else []


def build_graph_generation_prompt(current_graph: dict[str, Any], saved_files: list[dict[str, Any]]) -> str:
    source_files = [file_info["name"] for file_info in saved_files]
    uploaded_files_payload = [
        {
            "file_id": file_info["id"],
            "name": file_info["name"],
        }
        for file_info in saved_files
    ]

    existing_topic_reference = [
        {
            "id": topic_id,
            "title": topic_data.get("title"),
            "summary": (topic_data.get("summary") or "")[:MAX_SUMMARY_CHARS],
        }
        for topic_id, topic_data in recent_topic_items(current_graph.get("topics", {}))
    ]

    prompt = {
        "task": "Build a detailed StudyGraph JSON fragment for the uploaded batch.",
        "file_roles": {
            "source": source_files,
            "context": ["Existing in-app StudyGraph state"],
        },
        "uploaded_files": uploaded_files_payload,
        "existing_topic_reference": existing_topic_reference,
        "notes": [
            "Output only the NEW graph fragment for this batch.",
            "Only create topics from the current batch source materials.",
            "Reuse the exact file_id values provided for the current batch files.",
            "Extract multiple distinct teachable topics when justified.",
            "Avoid one-topic-per-file outputs.",
            "Use human-readable lecture names, not filenames.",
            "Prefer fewer connections over more connections.",
            "Most topics should have at most 2 outgoing connections.",
            "Topics may have 0 connections if no strong link is necessary.",
            "Only include cross-batch links when they are clearly important.",
            "Do not create connections just because two topics are related.",
            "Avoid dense local chains unless they are necessary for learning flow.",
            "A full existing lecture_graph.json file may also be attached in this request.",
            "The attached lecture_graph.json is CONTEXT ONLY.",
            "DO NOT extract topics from lecture_graph.json.",
            "DO NOT summarize lecture_graph.json.",
            "DO NOT modify or recreate old topics from lecture_graph.json.",
            "DO NOT include lecture_graph.json in uploaded_files processing.",
            "Use lecture_graph.json only to identify existing topic ids and create cross-batch connections when strongly justified.",
            "When a strong cross-batch relationship exists, connect new topics to existing topic ids from lecture_graph.json.",
            "Return only the NEW graph fragment for the current batch source files.",
        ],
    }
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def generate_graph_fragment_from_uploads(
    client: OpenAI,
    current_graph: dict[str, Any],
    saved_files: list[dict[str, Any]],
) -> dict[str, Any]:
    uploaded_files = upload_files_to_openai(client, saved_files)
    user_content = [
        {
            "type": "input_text",
            "text": build_graph_generation_prompt(current_graph, uploaded_files),
        }
    ]

    for file_info in uploaded_files:
        user_content.append(
            {
                "type": "input_file",
                "file_id": file_info["openai_file_id"],
            }
        )

    try:
        response = client.responses.create(
            model=OPENAI_GRAPH_MODEL,
            store=False,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": GRAPH_SYSTEM_PROMPT}]},
                {"role": "user", "content": user_content},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "studygraph_fragment",
                    "schema": RESPONSE_SCHEMA,
                    "strict": True,
                }
            },
        )

        fragment = extract_json_response(response)
        fragment = convert_topics_array_to_map(fragment)
        ensure_expected_file_ids(fragment, saved_files)
        fragment = normalize_fragment_files(fragment, saved_files)
        return fragment

    finally:
        for file_info in uploaded_files:
            file_id = file_info.get("openai_file_id")
            if not file_id:
                continue
            try:
                client.files.delete(file_id)
            except Exception:
                pass


def build_multi_topic_answer(
    user: Any,
    selected_topic_ids: list[str],
    topics: dict[str, dict[str, Any]],
    user_question: str,
    default_api_key: str | None = None,
) -> str | None:
    if not user_question.strip():
        return None

    chosen_topics = []
    for topic_id in selected_topic_ids:
        topic_data = topics.get(topic_id)
        if topic_data is None:
            continue
        chosen_topics.append(
            {
                "id": topic_id,
                "title": topic_data.get("title", "Untitled Topic"),
                "summary": topic_data.get("summary", ""),
                "lectures": topic_data.get("lectures", []),
                "connections": topic_data.get("connections", []),
            }
        )

    if not chosen_topics:
        return "Please select at least one topic first."

    client = get_openai_client_for_user(user, default_api_key)
    response = client.responses.create(
        model=OPENAI_ASK_MODEL,
        store=False,
        input=[
            {
                "role": "system",
                "content": [{"type": "input_text", "text": ASK_AI_SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Selected topics:\n"
                            f"{json.dumps(chosen_topics, ensure_ascii=False, indent=2)}\n\n"
                            f"Question: {user_question.strip()}"
                        ),
                    }
                ],
            },
        ],
    )
    answer = getattr(response, "output_text", "") or ""
    return answer.strip() or None

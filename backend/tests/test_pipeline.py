import json

from app.config import Settings
from app.domain import EditCall, Graph, Issue
from app.editor import apply_edits
from app.llm import LlmResult
from app.pipeline import Pipeline


def test_repair_edits_are_applied_to_candidate_graph():
    pipeline = Pipeline(Settings())
    committed = Graph()
    candidate = apply_edits(committed, [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]},
        {"id": "wrong", "kind": "task", "name": "Wrong", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Do the right task.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Task", "sentenceIds": ["S1"]}],
        "graph": committed.model_dump(),
        "candidate": candidate.model_dump(),
        "edits": [
            EditCall(name="delete_edge", arguments={"targetId": "flow_start_to_wrong"}).model_dump(),
            EditCall(name="delete_node", arguments={"targetId": "wrong"}).model_dump(),
            EditCall(name="add_linear_sequence", arguments={
                "predecessorId": "start",
                "nodes": [{"id": "right", "kind": "task", "name": "Right", "sourceSentences": ["S1"]}],
            }).model_dump(),
        ],
        "applyingRepair": True,
    })

    stage, updated = pipeline.step("EDITS_GENERATED", state, "Do the right task.")

    assert stage == "CANDIDATE_APPLIED"
    assert {node["id"] for node in updated["candidate"]["nodes"]} == {"start", "right"}
    assert updated["candidate"]["edges"][0]["id"] == "flow_start_to_right"


def test_invalid_edit_becomes_repair_issue_instead_of_crashing():
    pipeline = Pipeline(Settings())
    state = pipeline.initial_state("Do a task.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Task", "sentenceIds": ["S1"]}],
        "edits": [EditCall(name="delete_edge", arguments={"targetId": "missing"}).model_dump()],
    })

    stage, updated = pipeline.step("EDITS_GENERATED", state, "Do a task.")

    assert stage == "REPAIRING"
    assert updated["issues"][0]["code"] == "EDIT_APPLICATION_FAILED"


def test_review_issue_severity_is_normalized_for_blocking_decisions():
    issues = Pipeline._normalize_review_issues([
        {"code": "MAYBE", "message": "Uncertain", "severity": "medium"},
        {"code": "WRONG", "message": "Contradiction", "severity": "high"},
    ])

    assert [issue.severity for issue in issues] == ["warning", "error"]


def test_non_final_open_split_completeness_review_is_non_blocking():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": []}),
        EditCall(name="add_gateway", arguments={"id": "split", "name": "Complete?", "gatewayType": "exclusive", "role": "split", "sourceSentences": []}),
        EditCall(name="add_task", arguments={"id": "notify", "name": "Notify", "sourceSentences": []}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "split", "sourceSentences": []}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "split", "target": "notify", "sourceSentences": []}),
    ], "P1")
    issue = Issue(code="INCOMPLETE_EXCLUSIVE_SPLIT", message="missing branch",
                  elementIds=["split"], severity="error")

    incremental = Pipeline._apply_incremental_review_policy([issue], graph, False)
    final = Pipeline._apply_incremental_review_policy([issue], graph, True)

    assert incremental[0].severity == "warning"
    assert final[0].severity == "error"


def test_final_validation_issues_enter_repair_and_can_be_closed():
    pipeline = Pipeline(Settings())
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]},
        {"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "normalized": {"sentences": [{"id": "S1", "normalized": "Work."}]},
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "segmentIndex": 1,
        "graph": graph.model_dump(),
    })

    stage, state = pipeline.step("SEGMENT_COMMITTED", state, "Work.")
    assert stage == "REPAIRING"
    assert {item["code"] for item in state["issues"]} == {"END_COUNT", "OPEN_NODE"}
    leaf_issue = next(item for item in state["issues"] if item["code"] == "OPEN_NODE")
    assert leaf_issue["elementIds"] == ["work"]
    assert "eventType=end" in leaf_issue["message"]

    state["edits"] = [EditCall(name="add_linear_sequence", arguments={
        "predecessorId": "work",
        "nodes": [{"id": "end", "kind": "event", "name": "End", "eventType": "end", "trigger": "none", "sourceSentences": ["S1"]}],
    }).model_dump()]
    state["applyingRepair"] = True
    stage, state = pipeline.step("EDITS_GENERATED", state, "Work.")

    assert stage == "FINAL_VALIDATED"
    assert state["issues"] == []
    assert state["graph"]["nodes"][-1]["id"] == "end"


def test_json_agent_retries_empty_content():
    class FakeLlm:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls += 1
            content = None if self.calls == 1 else '{"segments": []}'
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={},
                             thinking_mode="enabled" if agent == "semantic_resolver" else "disabled")

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "semantic")

    value = pipeline._complete_json(state, "planner", [{"role": "user", "content": "S1 Work."}])

    assert value == {"segments": []}
    assert state["agentFailures"] == [
        {"agent": "planner", "attempt": 1, "error": "planner returned empty content"}
    ]
    assert len(state["llmCalls"]) == 2
    assert state["llmCalls"][0]["contentChars"] == 0


def test_semantic_resolver_retries_with_structured_validation_feedback():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append(messages)
            if len(self.calls) == 1:
                content = json.dumps({"sentences": [
                    {"id": "S1", "original": "Changed", "normalized": "First."},
                    {"id": "S1", "original": "First.", "normalized": "First."},
                ]})
            else:
                content = json.dumps({"sentences": [
                    {"id": "S1", "original": "First.", "normalized": "First.", "changes": []},
                    {"id": "S2", "original": "Second.", "normalized": "Second.", "changes": []},
                ]})
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("First. Second.", "semantic")
    _, state = pipeline.step("INPUT_READY", state, "First. Second.")

    stage, state = pipeline.step("SENTENCES_PREPARED", state, "First. Second.")

    assert stage == "SEMANTIC_RESOLVED"
    assert [item["id"] for item in state["normalized"]["sentences"]] == ["S1", "S2"]
    assert state["agentFailures"][0]["agent"] == "semantic_resolver"
    feedback = json.loads(pipeline.llm.calls[1][-1]["content"])
    assert feedback["type"] == "OUTPUT_VALIDATION_FAILED"
    assert {issue["code"] for issue in feedback["issues"]} == {
        "ORIGINAL_CHANGED", "DUPLICATE_SENTENCE_IDS", "MISSING_SENTENCE_IDS",
    }


def test_incremental_context_starts_at_earliest_open_node_segment():
    pipeline = Pipeline(Settings())
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "open_task", "kind": "task", "name": "Open task", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("One. Two. Three.", "semantic")
    state.update({
        "normalized": {"sentences": [
            {"id": "S1", "normalized": "One."},
            {"id": "S2", "normalized": "Two."},
            {"id": "S3", "normalized": "Three."},
        ]},
        "segments": [
            {"id": "P1", "start": "S1", "end": "S1", "title": "One", "sentenceIds": ["S1"]},
            {"id": "P2", "start": "S2", "end": "S2", "title": "Two", "sentenceIds": ["S2"]},
            {"id": "P3", "start": "S3", "end": "S3", "title": "Three", "sentenceIds": ["S3"]},
        ],
        "segmentIndex": 2,
    })

    context = pipeline._context(state, graph)

    assert [item["id"] for item in context["contextSegments"]] == ["P1", "P2", "P3"]
    assert [item["id"] for item in context["sentences"]] == ["S1", "S2", "S3"]
    assert context["openNodes"] == [{
        "id": "open_task",
        "name": "Open task",
        "kind": "task",
        "sourceSentences": ["S1"],
        "introducedInSegment": "P1",
    }]
    assert "openGateways" not in context


def test_semantic_resolver_rejects_unknown_or_reordered_sentence_ids():
    source = [{"id": "S1", "text": "One."}, {"id": "S2", "text": "Two."}]
    with_unknown = {"sentences": [
        {"id": "S1", "original": "One.", "normalized": "One."},
        {"id": "S3", "original": "Three.", "normalized": "Three."},
    ]}
    reordered = {"sentences": [
        {"id": "S2", "original": "Two.", "normalized": "Two."},
        {"id": "S1", "original": "One.", "normalized": "One."},
    ]}

    try:
        Pipeline._validate_semantic_resolution(with_unknown, source)
        assert False
    except ValueError as exc:
        assert "不存在的句子 ID" in str(exc)
        assert "缺少输入句子" in str(exc)

    try:
        Pipeline._validate_semantic_resolution(reordered, source)
        assert False
    except ValueError as exc:
        assert "句子顺序发生变化" in str(exc)

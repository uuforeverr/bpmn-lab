import json

import pytest

from app.config import Settings
from app.domain import EditCall, Graph
from app.editor import apply_edits
from app.llm import LlmResult, prompt
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


def test_reviewer_receives_description_prefix_candidate_graph_and_directed_edges():
    class FakeLlm:
        def __init__(self):
            self.messages = None

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.messages = messages
            return LlmResult(content='{"approved":true,"findings":[]}', tool_calls=[],
                             usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = FakeLlm()
    graph = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "normalized": {"sentences": [
            {"id": "S1", "original": "Work.", "normalized": "Perform work."},
            {"id": "S2", "original": "Future.", "normalized": "Future action."},
        ]},
        "context": {"isFinalSegment": True},
        "candidate": graph.model_dump(),
        "reviewerEnabled": False,
    })

    stage, updated = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")
    payload = json.loads(pipeline.llm.messages[1]["content"])

    assert stage == "SEMANTIC_REVIEWED"
    assert payload == {
        "descriptionPrefix": [{"id": "S1", "text": "Perform work."}],
        "candidateGraph": graph.model_dump(),
        "directedEdges": [],
    }
    assert updated["reviewHistory"][-1] == {
        "segmentId": "P1", "findings": [], "previousFindings": [],
    }


def test_disabled_reviewer_commits_after_structure_validation_without_an_llm_call():
    settings = Settings()
    settings.pipeline.reviewer_enabled = False
    pipeline = Pipeline(settings)
    candidate = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "normalized": {"sentences": [{"id": "S1", "normalized": "Work."}]},
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
        "issues": [],
        "reviewerEnabled": True,
    })

    stage, state = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["review"] is None
    assert state["reviewHistory"] == []
    assert state["llmCalls"] == []
    assert state["reviewerEnabled"] is False
    assert state["graph"]["metadata"]["processedSegments"] == ["P1"]


def test_reviewer_finding_is_sent_to_generate_and_can_be_rejected():
    finding = {
        "id": "R1", "code": "SEMANTIC_ORDER", "message": "The order is unsupported.",
        "elementIds": ["work"], "sourceSentences": ["S1"],
    }

    class FakeLlm:
        def complete(self, messages, tools=False, repair=False, agent=""):
            if agent == "reviewer":
                content = json.dumps({"approved": False, "findings": [finding]})
            else:
                content = json.dumps({
                    "decisions": [{
                        "findingId": "R1", "decision": "reject",
                        "reason": "The candidate is consistent with the only stated action.",
                    }],
                    "revisionPlan": "",
                })
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = FakeLlm()
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "normalized": {"sentences": [{"id": "S1", "original": "Work.", "normalized": "Work."}]},
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
    })

    stage, state = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")
    assert stage == "SEMANTIC_REVIEWED"
    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "GENERATOR_RECONSIDERING"
    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "GENERATOR_DECIDED"
    stage, state = pipeline.step(stage, state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["graph"] == candidate.model_copy(update={
        "metadata": candidate.metadata.model_copy(update={"processedSegments": ["P1"]})
    }).model_dump()
    assert state["rejectedFindings"][0]["finding"]["id"] == "R1"
    assert state["repairHistory"] == []


def test_generate_can_accept_a_finding_and_rewrite_an_existing_edge_with_a_patch():
    finding = {
        "id": "R1", "code": "SEMANTIC_PARALLELISM_MISSING",
        "message": "Packaging and documents must be independent.",
        "elementIds": ["package", "f_quality_package"],
        "sourceSentences": ["S2", "S3"],
    }

    class FakeLlm:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=False, repair=False, agent=""):
            assert agent == "generator"
            self.calls += 1
            if self.calls == 1:
                content = {
                    "decisions": [{
                        "findingId": "R1", "decision": "accept",
                        "reason": "Meanwhile and the later synchronization require parallel branches.",
                    }],
                    "revisionPlan": "Insert a parallel split after quality and start package and documents independently.",
                }
            else:
                content = {
                    "nodes": [
                        {"id": "prepare_split", "kind": "gateway", "name": "Prepare in parallel",
                         "gatewayType": "parallel", "role": "split", "sourceSentences": ["S2", "S3"]},
                        {"id": "docs", "kind": "task", "name": "Prepare documents", "sourceSentences": ["S3"]},
                    ],
                    "edges": [
                        {"id": "f_quality_package", "source": "quality", "target": "prepare_split",
                         "sourceSentences": ["S2", "S3"]},
                        {"id": "f_split_package", "source": "prepare_split", "target": "package",
                         "sourceSentences": ["S2"]},
                        {"id": "f_split_docs", "source": "prepare_split", "target": "docs",
                         "sourceSentences": ["S3"]},
                    ],
                    "removeNodeIds": [], "removeEdgeIds": [],
                }
            return LlmResult(content=json.dumps(content), tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = FakeLlm()
    candidate = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "quality", "name": "Quality", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "package", "name": "Package", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={
            "id": "f_quality_package", "source": "quality", "target": "package", "sourceSentences": ["S2"],
        }),
    ], "P1")
    state = pipeline.initial_state("Quality. Package. Meanwhile prepare documents.", "semantic")
    state.update({
        "segments": [
            {"id": "P1", "start": "S1", "end": "S2", "title": "Package", "sentenceIds": ["S1", "S2"]},
            {"id": "P2", "start": "S3", "end": "S3", "title": "Documents", "sentenceIds": ["S3"]},
        ],
        "segmentIndex": 1,
        "normalized": {"sentences": [
            {"id": "S1", "normalized": "Quality."},
            {"id": "S2", "normalized": "Package."},
            {"id": "S3", "normalized": "Meanwhile prepare documents."},
        ]},
        "candidate": candidate.model_dump(),
        "reviewFindings": [finding],
    })

    stage, state = pipeline.step("GENERATOR_RECONSIDERING", state, state["strategy"])
    assert stage == "GENERATOR_DECIDED"
    stage, state = pipeline.step(stage, state, state["strategy"])

    assert stage == "CANDIDATE_APPLIED"
    rewired = next(edge for edge in state["candidate"]["edges"] if edge["id"] == "f_quality_package")
    assert rewired["target"] == "prepare_split"
    assert state["reviewResolutions"][0]["decision"] == "accept"
    assert state["repairHistory"] == []


def test_generator_patch_retry_rejects_a_revision_that_breaks_structure():
    candidate = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_task", arguments={"id": "package", "name": "Package"}),
        EditCall(name="add_edge", arguments={"id": "f_start_package", "source": "start", "target": "package"}),
    ], "P1")
    with pytest.raises(ValueError, match="不可达"):
        Pipeline._validate_generator_revision_patch({
            "nodes": [],
            "edges": [{
                "id": "f_start_package", "source": "start", "target": "start",
                "sourceSentences": ["S1"],
            }],
            "removeNodeIds": [], "removeEdgeIds": [],
        }, candidate)


def test_generator_patch_rejects_a_no_op_revision():
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    with pytest.raises(ValueError, match="必须实际改变"):
        Pipeline._validate_generator_revision_patch({
            "nodes": [{"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]}],
            "edges": [], "removeNodeIds": [], "removeEdgeIds": [],
        }, candidate)


def test_disabling_reviewer_exits_an_existing_review_stage_and_commits_candidate():
    settings = Settings()
    settings.pipeline.reviewer_enabled = False
    pipeline = Pipeline(settings)
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "normalized": {"sentences": [{"id": "S1", "normalized": "Work."}]},
        "candidate": candidate.model_dump(),
        "reviewerEnabled": True,
        "reviewFindings": [{"id": "R1", "code": "WRONG", "message": "Old finding"}],
        "generatorReview": {
            "decisions": [{"findingId": "R1", "decision": "accept", "reason": "Old decision"}],
            "revisionPlan": "Old plan",
        },
    })

    stage, state = pipeline.step("GENERATOR_DECIDED", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["reviewerEnabled"] is False
    assert state["reviewFindings"] == []
    assert state["graph"]["metadata"]["processedSegments"] == ["P1"]


def test_generator_revision_exhaustion_keeps_candidate_and_continues():
    finding = {
        "id": "R1", "code": "SEMANTIC_ORDER", "message": "Change the order.",
        "elementIds": ["work"], "sourceSentences": ["S1"],
    }

    class NoOpLlm:
        def complete(self, messages, tools=False, repair=False, agent=""):
            return LlmResult(content=json.dumps({
                "nodes": [{"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]}],
                "edges": [], "removeNodeIds": [], "removeEdgeIds": [],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = NoOpLlm()
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "normalized": {"sentences": [{"id": "S1", "normalized": "Work."}]},
        "candidate": candidate.model_dump(),
        "reviewFindings": [finding],
        "generatorReview": {
            "decisions": [{"findingId": "R1", "decision": "accept", "reason": "Accept"}],
            "revisionPlan": "Change the order.",
        },
    })

    stage, state = pipeline.step("GENERATOR_DECIDED", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["graph"]["nodes"] == candidate.model_dump()["nodes"]
    assert len([item for item in state["agentFailures"] if item["agent"] == "generator"]) == 3
    assert state["reviewRevisionFailures"][-1]["candidateRetained"] is True
    assert state["reviewWarnings"][-1]["code"] == "REVIEW_REVISION_FAILED"


def test_review_revision_cycle_limit_keeps_candidate_and_continues():
    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    settings.pipeline.max_semantic_repairs = 2
    pipeline = Pipeline(settings)
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "reviewFindings": [{"id": "R1", "code": "WRONG", "message": "Still wrong"}],
        "reviewRevisionCount": 2,
    })

    stage, state = pipeline.step("SEMANTIC_REVIEWED", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["reviewRevisionFailures"][-1]["candidateRetained"] is True
    assert "达到上限 2" in state["reviewRevisionFailures"][-1]["message"]


def test_generator_returns_an_atomic_subgraph_without_edit_tools():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"messages": messages, "tools": tools, "repair": repair, "agent": agent})
            return LlmResult(content=json.dumps({
                "nodes": [
                    {"id": "start", "kind": "event", "name": "Start", "eventType": "start",
                     "trigger": "none", "sourceSentences": ["S1"]},
                    {"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]},
                ],
                "edges": [
                    {"id": "flow_start_work", "source": "start", "target": "work",
                     "sourceSentences": ["S1"]},
                ],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "context": {"currentSegment": {"id": "P1"}},
    })

    stage, state = pipeline.step("CONTEXT_BUILT", state, "Work.")

    assert stage == "SUBGRAPH_GENERATED"
    assert pipeline.llm.calls[0]["agent"] == "generator"
    assert pipeline.llm.calls[0]["tools"] is False
    assert pipeline.llm.calls[0]["repair"] is False
    assert state["edits"] == []
    assert state["subgraph"]["removeNodeIds"] == []
    assert state["subgraph"]["removeEdgeIds"] == []

    stage, state = pipeline.step("SUBGRAPH_GENERATED", state, "Work.")

    assert stage == "CANDIDATE_APPLIED"
    assert {node["id"] for node in state["candidate"]["nodes"]} == {"start", "work"}
    assert state["candidate"]["edges"][0]["id"] == "flow_start_work"


def test_generated_graph_patch_validation_rejects_unknown_references_and_fields():
    with pytest.raises(ValueError, match="role"):
        Pipeline._validate_generated_patch({
            "nodes": [{"id": "work", "kind": "task", "name": "Work",
                       "sourceSentences": ["S1"], "role": "invalid"}],
            "edges": [],
        }, Graph())


def test_generated_graph_patch_strips_system_owned_introduced_in_segment():
    patch = Pipeline._validate_generated_patch({
        "nodes": [{
            "id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"],
            "introducedInSegment": "MODEL_VALUE",
        }],
        "edges": [],
    }, Graph())

    assert "introducedInSegment" not in patch["nodes"][0]


def test_generated_graph_patch_strips_serialized_null_variant_fields():
    patch = Pipeline._validate_generated_patch({
        "nodes": [{
            "id": "choice", "kind": "gateway", "name": "Choice",
            "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"],
            "taskType": None, "eventType": None, "trigger": None, "eventRole": None,
        }],
        "edges": [],
    }, Graph())

    assert patch["nodes"] == [{
        "id": "choice", "kind": "gateway", "name": "Choice",
        "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"],
    }]

    with pytest.raises(ValueError, match="missing node"):
        Pipeline._validate_generated_patch({
            "nodes": [{"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]}],
            "edges": [{"id": "bad", "source": "work", "target": "missing", "sourceSentences": ["S1"]}],
        }, Graph())


def test_candidate_structure_validation_keeps_non_final_gateway_degree_non_blocking():
    pipeline = Pipeline(Settings())
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "review", "kind": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_edits(previous, [
        EditCall(name="add_gateway", arguments={"id": "choice", "name": "Result", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S2"]}),
        EditCall(name="add_task", arguments={"id": "approved", "name": "Approve", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "review", "target": "choice", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "approved", "sourceSentences": ["S2"]}),
    ], "P2")
    state = pipeline.initial_state("Review. Approve.", "semantic")
    state.update({
        "segments": [
            {"id": "P1", "start": "S1", "end": "S1", "title": "Review", "sentenceIds": ["S1"]},
            {"id": "P2", "start": "S2", "end": "S2", "title": "Result", "sentenceIds": ["S2"]},
        ],
        "segmentIndex": 1,
        "graph": previous.model_dump(),
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
    })

    stage, state = pipeline.step("CANDIDATE_APPLIED", state, "Review. Approve.")

    assert stage == "STRUCTURE_VALIDATED"
    assert state["issues"] == []
    assert any(warning["code"] == "BATCH_GATEWAY_DEGREE"
               and warning["severity"] == "warning"
               for warning in state["structureWarnings"])


def test_repair_can_complete_an_invalid_generated_batch_and_pass_structure_validation():
    class FakeRepairLlm:
        def complete(self, messages, tools=False, repair=False, agent=""):
            if not tools:
                return LlmResult(
                    content=json.dumps({"repairPlan": "Add the missing rejected branch from choice."}),
                    tool_calls=[], usage={}, latency_ms=1, raw={},
                )
            assert tools is True and repair is True and agent == "repair"
            return LlmResult(content=None, tool_calls=[
                EditCall(name="add_task", arguments={
                    "id": "rejected", "name": "Reject", "sourceSentences": ["S2"],
                }),
                EditCall(name="add_edge", arguments={
                    "id": "f_choice_rejected", "source": "choice", "target": "rejected",
                    "condition": {"label": "not approved"}, "sourceSentences": ["S2"],
                }),
            ], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeRepairLlm()
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "review", "kind": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_edits(previous, [
        EditCall(name="add_gateway", arguments={"id": "choice", "name": "Result", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S2"]}),
        EditCall(name="add_task", arguments={"id": "approved", "name": "Approve", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f_review_choice", "source": "review", "target": "choice", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={
            "id": "f_choice_approved", "source": "choice", "target": "approved",
            "condition": {"label": "approved"}, "sourceSentences": ["S2"],
        }),
    ], "P2")
    state = pipeline.initial_state("Review. Decide.", "semantic")
    state.update({
        "segments": [
            {"id": "P1", "start": "S1", "end": "S1", "title": "Review", "sentenceIds": ["S1"]},
            {"id": "P2", "start": "S2", "end": "S2", "title": "Decision", "sentenceIds": ["S2"]},
        ],
        "segmentIndex": 1,
        "graph": previous.model_dump(),
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
        "issues": [{"code": "BATCH_GATEWAY_DEGREE", "message": "complete the split"}],
    })

    stage, state = pipeline.step("REPAIRING", state, "Review. Decide.")
    assert stage == "EDITS_GENERATED"
    stage, state = pipeline.step("EDITS_GENERATED", state, "Review. Decide.")
    assert stage == "CANDIDATE_APPLIED"
    stage, state = pipeline.step("CANDIDATE_APPLIED", state, "Review. Decide.")

    assert stage == "STRUCTURE_VALIDATED"
    assert state["issues"] == []
    assert {edge["id"] for edge in state["candidate"]["edges"]}.issuperset({
        "f_choice_approved", "f_choice_rejected",
    })


def test_final_segment_candidate_receives_final_structure_validation_before_review():
    pipeline = Pipeline(Settings())
    candidate = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "work", "kind": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": True},
    })

    stage, state = pipeline.step("CANDIDATE_APPLIED", state, "Work.")

    assert stage == "STRUCTURE_VALIDATED"
    assert {issue["code"] for issue in state["issues"]} == {"END_COUNT", "OPEN_NODE"}
    stage, _ = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")
    assert stage == "REPAIRING"


def test_final_segment_keeps_incomplete_gateway_degree_blocking():
    pipeline = Pipeline(Settings())
    candidate = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Result", "gatewayType": "exclusive", "role": "split",
        }),
        EditCall(name="add_event", arguments={"id": "end_failed", "name": "End", "eventType": "end"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "end_failed"}),
    ], "P1")
    state = pipeline.initial_state("If it fails, end.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Result", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": True},
    })

    stage, state = pipeline.step("CANDIDATE_APPLIED", state, "If it fails, end.")

    assert stage == "STRUCTURE_VALIDATED"
    assert any(issue["code"] == "GATEWAY_DEGREE" and issue["severity"] == "error"
               for issue in state["issues"])
    assert not any(warning["code"] == "BATCH_GATEWAY_DEGREE"
                   for warning in state["structureWarnings"])


def test_generate_prompt_contains_type_schemas_and_both_continuation_examples():
    content = prompt("generator")

    assert '"kind":{"const":"task"}' in content
    assert '"kind":{"const":"event"}' in content
    assert '"kind":{"const":"gateway"}' in content
    assert "历史分支已由 Join 闭合" in content
    assert "历史 Gateway 尚未汇合" in content
    assert '"source":"gw_checks_join"' in content
    assert '"source":"task_record_approval"' in content
    assert "gatewayRefs 是全部历史 Gateway 的无状态紧凑索引" in content
    assert "即使已有 Gateway 当前已有两条或更多分支" in content
    assert "只有确认当前表达不属于任何已有 Gateway 时才新建 Gateway" in content
    assert "复用已有 ID 原子替换" in content
    assert "removeNodeIds/removeEdgeIds" in content
    assert "后文证明历史串行边应改为并行" in content


def test_generator_review_prompt_allows_accepting_or_rejecting_findings():
    content = prompt("generator_review")

    assert "不是必须执行的命令" in content
    assert "accept 或 reject" in content
    assert "revisionPlan" in content
    assert "不输出 Graph Patch" in content
    assert "source -> target" in content
    assert "目标拓扑其实已经存在时，必须 reject" in content
    assert '"decisions"' in content


def test_generator_patch_prompt_turns_the_accepted_plan_into_an_atomic_patch():
    content = prompt("generator_patch")

    assert "不要重新争论 finding" in content
    assert "A、B 可以是多节点业务分支" in content
    assert "将其替换成 `P -> Parallel Split`" in content
    assert "不得用 `Split -> Join` 空分支" in content
    assert "每个保留节点都可达" in content
    assert '"removeNodeIds"' in content


def test_planner_prompt_prioritizes_business_semantics_and_keeps_obvious_control_structures_together():
    content = prompt("planner")

    assert "首先保证业务事项或业务阶段的语义完整" in content
    assert "同一个决策、并行或汇聚结构" in content
    assert "不得仅为了形式上闭合 Gateway 而破坏业务语义" in content
    assert '"start":"S1","end":"S3"' in content


def test_reviewer_prompt_requires_direct_evidence_and_accepts_reasonable_equivalent_models():
    content = prompt("reviewer")

    assert "默认结论是通过" in content
    assert "不可等价的业务行为" in content
    assert "存在合理解释或只是另一种建模偏好时必须通过" in content
    assert "单独的 and、may、can、if、while" in content
    assert "source -> target" in content
    assert "无法从 directedEdges 复现所称冲突路径" in content
    assert "已解决时不得换一种措辞重复提出" in content
    assert "不得在没有新直接证据时重复 Generate 已拒绝的同一意见" in content
    assert '"findings"' in content
    assert "JSON 对象" in content


def test_repair_uses_edit_tools_and_a_distinct_prompt():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"messages": messages, "tools": tools, "repair": repair, "agent": agent})
            if not tools:
                return LlmResult(
                    content=json.dumps({"repairPlan": "Add and connect the missing task."}),
                    tool_calls=[], usage={}, latency_ms=1, raw={},
                )
            return LlmResult(content=None, tool_calls=[EditCall(name="add_task", arguments={
                "id": "fixed", "name": "Fixed", "sourceSentences": ["S1"],
            })], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "context": {"isFinalSegment": True},
        "candidate": Graph().model_dump(),
        "issues": [{"code": "WRONG", "message": "Fix it"}],
    })

    stage, updated = pipeline.step("REPAIRING", state, "Work.")
    plan_call, edit_call = pipeline.llm.calls
    system_prompt = edit_call["messages"][0]["content"]
    repair_payload = json.loads(edit_call["messages"][1]["content"])

    assert stage == "EDITS_GENERATED"
    assert plan_call["messages"][0]["content"] == prompt("repair_plan")
    assert plan_call["agent"] == "repair_plan"
    assert plan_call["tools"] is False
    assert edit_call["agent"] == "repair"
    assert edit_call["tools"] is True
    assert edit_call["repair"] is True
    assert system_prompt == prompt("repair")
    assert repair_payload["context"]["isFinalization"] is True
    assert repair_payload["context"]["contextSegments"] == state["segments"]
    assert repair_payload["repairPlan"] == "Add and connect the missing task."
    assert repair_payload["directedEdges"] == []
    assert prompt("repair") != prompt("generator")
    assert "update_edge" in prompt("repair")
    assert "按 repairPlan 修复 issues" in prompt("repair")
    assert "只提供“问题”和“原因”" in prompt("repair_plan")
    assert "最小拓扑修改" in prompt("repair_plan")
    assert "保留有文本证据的业务路径" in prompt("repair_plan")
    assert "source -> target" in prompt("repair_plan")
    assert "不得把更早的串行活动带入并行" in prompt("repair_plan")
    assert "JSON 对象" in prompt("repair_plan")
    assert '"nodes"' in prompt("generator") and '"edges"' in prompt("generator")
    assert updated["repairPlan"] == "Add and connect the missing task."
    assert updated["repairHistory"][-1]["repairPlan"] == updated["repairPlan"]
    assert updated["repairHistory"][-1]["edits"] == updated["edits"]


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

    stage, state = pipeline.step("FINAL_VALIDATED", state, "Work.")

    assert stage == "BPMN_GENERATED"
    assert state["snapshots"][-1]["segment"] == {
        "id": "FINALIZATION", "title": "最终流程", "sentenceIds": [],
    }
    assert state["snapshots"][-1]["graph"] == state["graph"]
    assert state["snapshots"][-1]["bpmnXml"] == state["bpmnXml"]
    assert 'id="end"' in state["snapshots"][-1]["bpmnXml"]


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
    assert context["gatewayRefs"] == []
    assert "openGateways" not in context


def test_gateway_refs_do_not_expand_the_continuous_text_window():
    pipeline = Pipeline(Settings())
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_gateway", arguments={
            "id": "payment_result", "name": "Payment result",
            "gatewayType": "exclusive", "role": "split",
        }),
        EditCall(name="add_event", arguments={"id": "end_failed", "name": "End", "eventType": "end"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "payment_result"}),
        EditCall(name="add_edge", arguments={
            "id": "f2", "source": "payment_result", "target": "end_failed",
            "condition": {"label": "failed"},
        }),
    ], "P1")
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

    assert [item["id"] for item in context["contextSegments"]] == ["P3"]
    assert [item["id"] for item in context["sentences"]] == ["S3"]
    assert context["openNodes"] == []
    assert context["gatewayRefs"] == [{
        "id": "payment_result", "gatewayType": "exclusive", "role": "split",
        "question": "Payment result",
    }]


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

import json

import pytest

from app.config import Settings
from app.domain import EditCall, Graph
from app.editor import apply_edits, apply_graph_patch
from app.llm import LlmResult, prompt
from app.pipeline import AgentOutputValidationError, Pipeline
from app.segmentation import split_sentences
from app.validation import validate_graph


def test_structure_repair_patch_is_applied_to_candidate_graph():
    class FakeRepair:
        def complete(self, messages, tools=False, repair=False, agent=""):
            assert agent == "repair"
            assert tools is False
            return LlmResult(content=json.dumps({
                "addNodes": [{
                    "id": "right", "type": "task", "name": "Right",
                    "sourceSentences": ["S1"],
                }],
                "addEdges": [{
                    "id": "flow_start_to_right", "source": "start", "target": "right",
                    "sourceSentences": ["S1"],
                }],
                "removeNodeIds": ["wrong"],
                "removeEdgeIds": ["flow_start_to_wrong"],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = FakeRepair()
    committed = Graph()
    candidate = apply_edits(committed, [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "wrong", "type": "task", "name": "Wrong", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Do the right task.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Task", "sentenceIds": ["S1"]}],
        "sentences": [{"id": "S1", "text": "Do the right task."}],
        "graph": committed.model_dump(),
        "candidate": candidate.model_dump(),
        "issues": [{"code": "WRONG_TASK", "message": "Replace wrong task."}],
    })

    stage, updated = pipeline.step("STRUCTURE_REPAIRING", state, "Do the right task.")

    assert stage == "CANDIDATE_APPLIED"
    assert {node["id"] for node in updated["candidate"]["nodes"]} == {"start", "right"}
    assert updated["candidate"]["edges"][0]["id"] == "flow_start_to_right"
    assert updated["repairHistory"][-1]["patch"] == updated["structureRepairPatch"]


def test_invalid_structure_repair_patch_is_retried_without_changing_candidate():
    class InvalidRepair:
        def complete(self, messages, tools=False, repair=False, agent=""):
            return LlmResult(content=json.dumps({
                "addNodes": [], "addEdges": [],
                "removeNodeIds": ["missing"], "removeEdgeIds": [],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = InvalidRepair()
    state = pipeline.initial_state("Do a task.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Task", "sentenceIds": ["S1"]}],
        "sentences": [{"id": "S1", "text": "Do a task."}],
        "candidate": Graph().model_dump(),
        "issues": [{"code": "MISSING_TASK", "message": "Add task."}],
    })

    with pytest.raises(ValueError, match="repair failed after"):
        pipeline.step("STRUCTURE_REPAIRING", state, "Do a task.")
    assert state["candidate"] == Graph().model_dump()
    assert len(state["agentFailures"]) == 3


def test_reviewer_receives_only_description_prefix_and_compact_candidate_graph():
    previous_finding = {
        "finding": {
            "id": "R0", "message": "Earlier issue.",
            "elementIds": ["work"], "sourceSentences": ["S1"],
        },
        "findingId": "R0", "decision": "accept", "reason": "Corrected.",
    }

    class FakeLlm:
        def __init__(self):
            self.messages = None

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.messages = messages
            return LlmResult(content='{"findings":[]}', tool_calls=[],
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
        "sentences": [
            {"id": "S1", "text": "Work."},
            {"id": "S2", "text": "Future."},
        ],
        "context": {"isFinalSegment": True},
        "candidate": graph.model_dump(),
        "reviewResolutions": [previous_finding],
        "reviewerEnabled": False,
    })

    stage, updated = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")
    payload = json.loads(pipeline.llm.messages[1]["content"])

    assert stage == "SEMANTIC_REVIEWED"
    assert payload == {
        "descriptionPrefix": [{"id": "S1", "text": "Work."}],
        "candidateGraph": {
            "nodes": graph.model_dump()["nodes"],
            "edges": graph.model_dump()["edges"],
        },
        "previousFindings": [previous_finding],
    }
    assert updated["reviewHistory"][-1] == {
        "segmentId": "P1", "findings": [], "previousFindings": [previous_finding],
        "discussionRound": 1,
    }


def test_disabled_reviewer_commits_after_structure_validation_without_an_llm_call():
    settings = Settings()
    settings.pipeline.reviewer_enabled = False
    pipeline = Pipeline(settings)
    candidate = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "sentences": [{"id": "S1", "text": "Work."}],
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


def test_reviewer_finding_is_sent_to_repair_and_can_be_rejected():
    finding = {
        "id": "R1", "message": "The order is unsupported.",
        "elementIds": ["work"], "sourceSentences": ["S1"],
    }

    class FakeLlm:
        def __init__(self):
            self.review_calls = 0

        def complete(self, messages, tools=False, repair=False, agent=""):
            if agent == "reviewer":
                self.review_calls += 1
                content = json.dumps({"findings": [finding] if self.review_calls == 1 else []})
            else:
                content = json.dumps({
                    "decisions": [{
                        "findingId": "R1", "decision": "reject",
                        "reason": "The candidate is consistent with the only stated action.",
                    }],
                    "patch": None,
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
        "sentences": [{"id": "S1", "text": "Work."}],
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
    })

    stage, state = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")
    assert stage == "SEMANTIC_REVIEWED"
    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "SEMANTIC_REPAIRING"
    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "SEMANTIC_REPAIR_DECIDED"
    stage, state = pipeline.step(stage, state, "Work.")

    assert stage == "STRUCTURE_VALIDATED"
    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "SEMANTIC_REVIEWED"
    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "SEGMENT_COMMITTED"
    assert state["graph"] == candidate.model_copy(update={
        "metadata": candidate.metadata.model_copy(update={"processedSegments": ["P1"]})
    }).model_dump()
    assert state["reviewDiscussionHistory"][0]["repairDecisions"][0]["reason"]
    assert state["repairHistory"] == []


def test_three_unresolved_discussion_rounds_are_sent_to_arbitration():
    finding = {
        "id": "R1", "message": "The action should be optional.",
        "elementIds": ["work"], "sourceSentences": ["S1"],
    }

    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"agent": agent, "messages": messages})
            if agent == "reviewer":
                content = {"findings": [finding]}
            elif agent == "arbitrator":
                content = {"decisions": [{
                    "findingId": "R1", "decision": "reject",
                    "reason": "The text does not make the action optional.",
                }]}
            else:
                content = {
                    "decisions": [{
                        "findingId": "R1", "decision": "reject",
                        "reason": "The optionality is not stated in S1.",
                    }],
                    "patch": None,
                }
            return LlmResult(content=json.dumps(content), tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    settings.pipeline.max_review_discussion_rounds = 3
    pipeline = Pipeline(settings)
    pipeline.llm = FakeLlm()
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "sentences": [{"id": "S1", "text": "Work."}],
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
    })

    stage = "STRUCTURE_VALIDATED"
    for _ in range(3):
        stage, state = pipeline.step(stage, state, "Work.")
        assert stage == "SEMANTIC_REVIEWED"
        stage, state = pipeline.step(stage, state, "Work.")
        assert stage == "SEMANTIC_REPAIRING"
        stage, state = pipeline.step(stage, state, "Work.")
        assert stage == "SEMANTIC_REPAIR_DECIDED"
        stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "ARBITRATING"

    stage, state = pipeline.step(stage, state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert len(state["reviewDiscussionHistory"]) == 3
    assert state["arbitrationHistory"][0]["decisions"][0]["decision"] == "reject"
    arbitration_call = next(call for call in pipeline.llm.calls if call["agent"] == "arbitrator")
    arbitration_payload = json.loads(arbitration_call["messages"][1]["content"])
    assert len(arbitration_payload["discussionHistory"]) == 3


def test_arbitrator_acceptance_is_binding_and_uses_the_arbitrator_client():
    finding = {
        "id": "R1", "message": "Rename the task to match the explicit action.",
        "elementIds": ["work"], "sourceSentences": ["S1"],
    }

    class FakeArbitrator:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=False, repair=False, agent=""):
            assert agent == "arbitrator"
            self.calls += 1
            return LlmResult(content=json.dumps({"decisions": [{
                "findingId": "R1", "decision": "accept", "reason": "S1 uses the explicit action name.",
            }]}), tool_calls=[], usage={}, latency_ms=1, raw={})

    class FakeRepair:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=False, repair=False, agent=""):
            assert agent == "repair"
            self.calls += 1
            payload = json.loads(messages[1]["content"])
            assert payload["arbitrationDecisions"][0]["decision"] == "accept"
            return LlmResult(content=json.dumps({
                "decisions": [{
                    "findingId": "R1", "decision": "accept", "reason": "Following final arbitration.",
                }],
                "patch": {
                    "addNodes": [{"id": "work", "type": "task", "name": "Perform work", "sourceSentences": ["S1"]}],
                    "addEdges": [], "removeNodeIds": ["work"], "removeEdgeIds": [],
                },
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = FakeRepair()
    pipeline.arbitrator_llm = FakeArbitrator()
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    state = pipeline.initial_state("Perform work.", "single_segment")
    state.update({
        "sentences": [{"id": "S1", "text": "Perform work."}],
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "reviewFindings": [finding],
        "pendingArbitrationFindings": [finding],
        "reviewDiscussionHistory": [{
            "segmentId": "P1", "round": 3, "reviewFindings": [finding],
            "repairDecisions": [{"findingId": "R1", "decision": "reject", "reason": "Old reason"}],
        }],
    })

    stage, state = pipeline.step("ARBITRATING", state, "Perform work.")
    assert stage == "SEMANTIC_REPAIRING"
    stage, state = pipeline.step(stage, state, "Perform work.")
    assert stage == "SEMANTIC_REPAIR_DECIDED"
    stage, state = pipeline.step(stage, state, "Perform work.")

    assert stage == "CANDIDATE_APPLIED"
    assert state["candidate"]["nodes"][0]["name"] == "Perform work"
    assert pipeline.arbitrator_llm.calls == 1

    state["issues"] = []
    stage, state = pipeline.step("STRUCTURE_VALIDATED", state, "Perform work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["graph"]["nodes"][0]["name"] == "Perform work"
    assert pipeline.llm.calls == 1


def test_invalid_repair_semantic_cannot_bypass_binding_arbitration():
    class InvalidRepair:
        def complete(self, messages, tools=False, repair=False, agent="default"):
            assert agent == "repair"
            return LlmResult(content=json.dumps({
                "decisions": [{
                    "findingId": "f1",
                    "decision": "reject",
                    "reason": "I still disagree.",
                }],
                "patch": None,
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = InvalidRepair()
    graph = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    finding = {
        "id": "f1",
        "type": "missing_behavior",
        "message": "The approval task is missing.",
        "elementIds": [],
        "sourceSentences": ["S1"],
    }
    state = pipeline.initial_state("Perform work.", "single_segment")
    state.update({
        "sentences": [{"id": "S1", "text": "Perform work."}],
        "segments": [{"id": "P1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": graph.model_dump(),
        "reviewFindings": [finding],
        "arbitrationDecisions": [{
            "findingId": "f1",
            "decision": "accept",
            "reason": "The text requires this task.",
        }],
    })

    with pytest.raises(ValueError, match="repair failed after"):
        pipeline.step("SEMANTIC_REPAIRING", state, "Perform work.")
    assert state["candidate"] == graph.model_dump()
    assert "P1" not in state["graph"]["metadata"]["processedSegments"]


def test_repair_can_accept_a_finding_and_rewrite_an_existing_edge_with_a_patch():
    finding = {
        "id": "R1",
        "message": "Packaging and documents must be independent.",
        "elementIds": ["package", "f_quality_package"],
        "sourceSentences": ["S2", "S3"],
    }

    class FakeLlm:
        def __init__(self):
            self.calls = 0
            self.messages = None

        def complete(self, messages, tools=False, repair=False, agent=""):
            assert agent == "repair"
            self.calls += 1
            self.messages = messages
            content = {
                "decisions": [{
                    "findingId": "R1", "decision": "accept",
                    "reason": "Meanwhile and the later synchronization require parallel branches.",
                }],
                "patch": {
                    "addNodes": [
                        {"id": "prepare_split", "name": "Prepare in parallel",
                         "type": "parallelGateway", "role": "split", "sourceSentences": ["S2", "S3"]},
                        {"id": "docs", "type": "task", "name": "Prepare documents", "sourceSentences": ["S3"]},
                    ],
                    "addEdges": [
                        {"id": "f_quality_package", "source": "quality", "target": "prepare_split",
                         "sourceSentences": ["S2", "S3"]},
                        {"id": "f_split_package", "source": "prepare_split", "target": "package",
                         "sourceSentences": ["S2"]},
                        {"id": "f_split_docs", "source": "prepare_split", "target": "docs",
                         "sourceSentences": ["S3"]},
                    ],
                    "removeNodeIds": [], "removeEdgeIds": ["f_quality_package"],
                },
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
        "sentences": [
            {"id": "S1", "text": "Quality."},
            {"id": "S2", "text": "Package."},
            {"id": "S3", "text": "Meanwhile prepare documents."},
        ],
        "candidate": candidate.model_dump(),
        "reviewFindings": [finding],
    })

    stage, state = pipeline.step("SEMANTIC_REPAIRING", state, state["strategy"])
    assert stage == "SEMANTIC_REPAIR_DECIDED"
    stage, state = pipeline.step(stage, state, state["strategy"])

    assert stage == "CANDIDATE_APPLIED"
    rewired = next(edge for edge in state["candidate"]["edges"] if edge["id"] == "f_quality_package")
    assert rewired["target"] == "prepare_split"
    assert state["reviewResolutions"][0]["decision"] == "accept"
    assert state["repairHistory"] == []
    assert pipeline.llm.calls == 1
    revision_payload = json.loads(pipeline.llm.messages[1]["content"])
    assert set(revision_payload) == {"descriptionPrefix", "candidateGraph", "findings"}
    assert set(revision_payload["candidateGraph"]) == {"nodes", "edges"}
    assert pipeline.llm.messages[0]["content"] == prompt("repair_semantic")


def test_repair_semantic_patch_leaves_graph_structure_to_the_shared_validator():
    candidate = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_task", arguments={"id": "package", "name": "Package"}),
        EditCall(name="add_edge", arguments={"id": "f_start_package", "source": "start", "target": "package"}),
    ], "P1")
    patch = Pipeline._validate_repair_patch({
        "addNodes": [],
        "addEdges": [{
            "id": "f_start_package", "source": "start", "target": "start",
            "sourceSentences": ["S1"],
        }],
        "removeNodeIds": [], "removeEdgeIds": ["f_start_package"],
    }, candidate)
    revised = apply_graph_patch(
        candidate, patch["addNodes"], patch["addEdges"], "P1",
        patch["removeNodeIds"], patch["removeEdgeIds"],
    )

    assert any(issue.code == "UNREACHABLE" for issue in validate_graph(revised))

    pipeline = Pipeline(Settings())
    state = pipeline.initial_state("Package.", "single_segment")
    state.update({
        "graph": candidate.model_dump(),
        "candidate": revised.model_dump(),
        "segments": [{
            "id": "P1", "start": "S1", "end": "S1",
            "title": "Package", "sentenceIds": ["S1"],
        }],
        "context": {"isFinalSegment": False},
    })
    stage, state = pipeline.step("CANDIDATE_APPLIED", state, "Package.")
    assert stage == "STRUCTURE_VALIDATED"
    assert any(issue["code"] == "UNREACHABLE" for issue in state["issues"])
    stage, _ = pipeline.step(stage, state, "Package.")
    assert stage == "STRUCTURE_REPAIRING"


def test_repair_semantic_patch_rejects_a_no_op_revision():
    candidate = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "name": "Work", "sourceSentences": ["S1"],
    })], "P1")
    with pytest.raises(ValueError, match="必须实际改变"):
        Pipeline._validate_repair_patch({
            "addNodes": [{"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]}],
            "addEdges": [], "removeNodeIds": ["work"], "removeEdgeIds": [],
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
        "sentences": [{"id": "S1", "text": "Work."}],
        "candidate": candidate.model_dump(),
        "reviewerEnabled": True,
        "reviewFindings": [{"id": "R1", "message": "Old finding"}],
        "semanticRepair": {
            "decisions": [{"findingId": "R1", "decision": "accept", "reason": "Old decision"}],
            "patch": {"addNodes": [], "addEdges": [], "removeNodeIds": [], "removeEdgeIds": []},
        },
    })

    stage, state = pipeline.step("SEMANTIC_REPAIR_DECIDED", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["reviewerEnabled"] is False
    assert state["reviewFindings"] == []
    assert state["graph"]["metadata"]["processedSegments"] == ["P1"]


def test_repair_semantic_exhaustion_keeps_candidate_and_continues():
    finding = {
        "id": "R1", "message": "Change the order.",
        "elementIds": ["work"], "sourceSentences": ["S1"],
    }

    class NoOpLlm:
        def complete(self, messages, tools=False, repair=False, agent=""):
            return LlmResult(content=json.dumps({
                "decisions": [{
                    "findingId": "R1", "decision": "accept", "reason": "Accept",
                }],
                "patch": {
                    "addNodes": [{"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]}],
                    "addEdges": [], "removeNodeIds": ["work"], "removeEdgeIds": [],
                },
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
        "sentences": [{"id": "S1", "text": "Work."}],
        "candidate": candidate.model_dump(),
        "reviewFindings": [finding],
    })

    stage, state = pipeline.step("SEMANTIC_REPAIRING", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["graph"]["nodes"] == candidate.model_dump()["nodes"]
    assert len([item for item in state["agentFailures"] if item["agent"] == "repair"]) == 3
    assert state["semanticRepairFailures"][-1]["candidateRetained"] is True
    assert state["reviewWarnings"][-1]["code"] == "SEMANTIC_REPAIR_FAILED"


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
        "reviewFindings": [{"id": "R1", "message": "Still wrong"}],
        "semanticRepairCount": 2,
    })

    stage, state = pipeline.step("SEMANTIC_REVIEWED", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["semanticRepairFailures"][-1]["candidateRetained"] is True
    assert "达到上限 2" in state["semanticRepairFailures"][-1]["message"]


def test_generator_returns_an_atomic_subgraph_without_edit_tools():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"messages": messages, "tools": tools, "repair": repair, "agent": agent})
            return LlmResult(content=json.dumps({
                "addNodes": [
                    {"id": "start", "name": "Start", "type": "startEvent",
                     "sourceSentences": ["S1"]},
                    {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
                ],
                "addEdges": [
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
    assert "edits" not in state
    assert state["subgraph"]["removeNodeIds"] == []
    assert state["subgraph"]["removeEdgeIds"] == []

    stage, state = pipeline.step("SUBGRAPH_GENERATED", state, "Work.")

    assert stage == "CANDIDATE_APPLIED"
    assert {node["id"] for node in state["candidate"]["nodes"]} == {"start", "work"}
    assert state["candidate"]["edges"][0]["id"] == "flow_start_work"


def test_generated_graph_patch_validation_rejects_unknown_references_and_fields():
    with pytest.raises(ValueError, match="role"):
        Pipeline._validate_generated_patch({
            "addNodes": [{"id": "work", "type": "task", "name": "Work",
                       "sourceSentences": ["S1"], "role": "invalid"}],
            "addEdges": [],
        }, Graph())


def test_generated_graph_patch_reports_exact_missing_reference_elements():
    graph = apply_edits(Graph(), [EditCall(name="add_event", arguments={
        "id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"],
    })], "P1")

    with pytest.raises(AgentOutputValidationError) as caught:
        Pipeline._validate_generated_patch({
            "addNodes": [{
                "id": "work", "type": "task", "name": "Work",
                "sourceSentences": ["S1"],
            }],
            "addEdges": [{
                "id": "flow_work_finish", "source": "work", "target": "finish_missing",
                "sourceSentences": ["S1"],
            }],
        }, graph)

    issue = caught.value.issues[0]
    assert issue["code"] == "GRAPH_PATCH_MISSING_NODE_REFERENCE"
    assert issue["message"] == (
        "边 flow_work_finish 的 target=finish_missing 引用了 Patch 应用后不存在的节点。"
        "每条新增边的 source 和 target 都必须在最终图中存在；若仍需使用被删除节点，"
        "必须在同一 Patch 的 addNodes 中用同一 ID 重建，否则不要删除该节点或改为引用真实存在的节点"
    )
    assert issue["elementIds"] == ["flow_work_finish", "finish_missing"]


@pytest.mark.parametrize("invalid_type", ["activity", "intermediateEvent", "xorGateway"])
def test_generated_graph_patch_invalid_node_type_reports_allowed_types_and_definitions(
        invalid_type: str):
    with pytest.raises(AgentOutputValidationError) as caught:
        Pipeline._validate_generated_patch({
            "addNodes": [{
                "id": "invalid", "type": invalid_type, "name": "Invalid",
                "sourceSentences": ["S1"],
            }],
            "addEdges": [],
            "removeNodeIds": [],
            "removeEdgeIds": [],
        }, Graph())

    issue = caught.value.issues[0]
    assert issue["code"] == "SUBGRAPH_NODE_TYPE_INVALID"
    assert issue["invalidType"] == invalid_type
    assert issue["allowedTypes"] == {
        "task": [
            "task", "userTask", "serviceTask", "sendTask", "receiveTask",
            "businessRuleTask", "manualTask", "scriptTask",
        ],
        "event": [
            "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
        ],
        "gateway": ["exclusiveGateway", "parallelGateway", "inclusiveGateway"],
    }
    assert set(issue["typeDefinitions"]) == {
        node_type
        for types in issue["allowedTypes"].values()
        for node_type in types
    }
    assert "intermediateEvent" not in issue["allowedTypes"]["event"]
    for node_type, definition in issue["typeDefinitions"].items():
        assert node_type in issue["message"]
        assert definition in issue["message"]


def test_json_retry_repeats_the_original_prompt_and_data_verbatim():
    invalid_patch = {
        "addNodes": [{
            "id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"],
        }],
        "addEdges": [{
            "id": "flow_work_finish", "source": "work", "target": "finish_missing",
            "sourceSentences": ["S1"],
        }],
    }
    valid_patch = {
        "addNodes": [
            {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
            {"id": "finish", "type": "endEvent", "name": "Finish", "sourceSentences": ["S1"]},
        ],
        "addEdges": [{
            "id": "flow_work_finish", "source": "work", "target": "finish",
            "sourceSentences": ["S1"],
        }],
    }

    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent="", **kwargs):
            self.calls.append(messages)
            content = json.dumps(invalid_patch if len(self.calls) == 1 else valid_patch)
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "semantic")

    result = pipeline._complete_json(
        state,
        "generator",
        [{"role": "user", "content": "generate"}],
        validator=lambda value: pipeline._validate_generated_patch(value, Graph()),
    )

    assert result["addNodes"][1]["id"] == "finish"
    assert pipeline.llm.calls == [
        [{"role": "user", "content": "generate"}],
        [{"role": "user", "content": "generate"}],
    ]

    diagnostic = state["agentAttemptDiagnostics"][0]
    assert diagnostic["agent"] == "generator"
    assert diagnostic["attempt"] == 1
    assert diagnostic["invalidOutput"] == invalid_patch
    assert json.loads(diagnostic["invalidContent"]) == invalid_patch
    assert diagnostic["issues"][0]["edgeId"] == "flow_work_finish"
    assert diagnostic["issues"][0]["source"] == "work"
    assert diagnostic["issues"][0]["target"] == "finish_missing"
    assert diagnostic["issues"][0]["missingEndpoints"] == ["finish_missing"]
    assert diagnostic["issues"][0]["availableCommittedNodeIds"] == []
    assert diagnostic["issues"][0]["nodeIdsAddedOrReplacedByThisPatch"] == ["work"]


def test_json_retry_keeps_invalid_element_details_internal():
    invalid_patch = {"addNodes": ["not-an-object"], "addEdges": []}
    valid_patch = {
        "addNodes": [{
            "id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"],
        }],
        "addEdges": [],
    }

    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent="", **kwargs):
            self.calls.append(messages)
            content = json.dumps(invalid_patch if len(self.calls) == 1 else valid_patch)
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "semantic")
    pipeline._complete_json(
        state, "generator", [{"role": "user", "content": "generate"}],
        validator=lambda value: pipeline._validate_generated_patch(value, Graph()),
    )

    assert pipeline.llm.calls == [
        [{"role": "user", "content": "generate"}],
        [{"role": "user", "content": "generate"}],
    ]
    elements = state["agentAttemptDiagnostics"][0]["issues"][0]["elements"]
    assert elements == [{"location": "addNodes[0]", "content": "not-an-object"}]


def test_patch_removal_of_a_node_that_exists_only_in_the_patch_is_structured():
    with pytest.raises(AgentOutputValidationError) as caught:
        Pipeline._validate_generated_patch({
            "addNodes": [{
                "id": "new_work", "type": "task", "name": "New work",
                "sourceSentences": ["S1"],
            }],
            "addEdges": [],
            "removeNodeIds": ["new_work"],
            "removeEdgeIds": [],
        }, Graph())

    issue = caught.value.issues[0]
    assert issue["code"] == "GRAPH_PATCH_REMOVE_MISSING_NODES"
    assert issue["elementIds"] == ["new_work"]
    assert "当前 graph 中不存在" in issue["message"]


def test_generated_graph_patch_rejects_existing_ids_without_matching_removal():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_edge", arguments={"id": "f", "source": "a", "target": "b"}),
    ], "P1")

    with pytest.raises(AgentOutputValidationError) as caught:
        Pipeline._validate_generated_patch({
            "addNodes": [{"id": "a", "type": "task", "name": "Changed A",
                          "sourceSentences": ["S1"]}],
            "addEdges": [{"id": "f", "source": "b", "target": "a",
                          "sourceSentences": ["S1"]}],
            "removeNodeIds": [],
            "removeEdgeIds": [],
        }, graph)

    assert {issue["code"] for issue in caught.value.issues} == {
        "GRAPH_PATCH_ADD_EXISTING_NODES_WITHOUT_REMOVE",
        "GRAPH_PATCH_ADD_EXISTING_EDGES_WITHOUT_REMOVE",
    }


def test_generator_retry_exhaustion_routes_last_invalid_patch_to_repair_loop():
    invalid_patch = {
        "addNodes": [{
            "id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"],
        }],
        "addEdges": [{
            "id": "flow_work_finish", "source": "work", "target": "finish_missing",
            "sourceSentences": ["S1"],
        }],
    }

    class FakeLlm:
        def complete(self, messages, tools=False, repair=False, agent="", **kwargs):
            return LlmResult(
                content=json.dumps(invalid_patch), tool_calls=[], usage={}, latency_ms=1, raw={}
            )

    settings = Settings()
    settings.pipeline.max_agent_retries = 1
    pipeline = Pipeline(settings)
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "semantic")
    state.update({
        "sentences": [{"id": "S1", "text": "Work."}],
        "segments": [{
            "id": "P1", "start": "S1", "end": "S1", "title": "Work",
            "sentenceIds": ["S1"],
        }],
        "context": {
            "currentSegment": {"id": "P1", "sentenceIds": ["S1"]},
            "descriptionPrefix": [{"id": "S1", "text": "Work."}],
            "graph": {"nodes": [], "edges": []},
            "isFinalSegment": True,
        },
    })

    stage, updated = pipeline.step("CONTEXT_BUILT", state, "Work.")

    assert stage == "STRUCTURE_REPAIRING"
    assert updated["candidate"] == updated["graph"]
    assert updated["subgraph"] == invalid_patch
    assert updated["issues"][0]["missingEndpoints"] == ["finish_missing"]
    assert updated["generatorPatchFailure"]["lastInvalidPatch"] == invalid_patch
    assert updated["terminalAgentFailures"][-1]["attempts"] == 2


def test_generated_graph_patch_strips_system_owned_introduced_in_segment():
    patch = Pipeline._validate_generated_patch({
        "addNodes": [{
            "id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"],
            "introducedInSegment": "MODEL_VALUE",
        }],
        "addEdges": [],
    }, Graph())

    assert "introducedInSegment" not in patch["addNodes"][0]


def test_generated_graph_patch_strips_optional_null_variant_fields():
    patch = Pipeline._validate_generated_patch({
        "addNodes": [{
            "id": "choice", "name": "Choice",
            "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"],
            "eventDefinition": None,
        }],
        "addEdges": [],
    }, Graph())

    assert patch["addNodes"] == [{
        "id": "choice", "name": "Choice",
        "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"],
    }]

    with pytest.raises(AgentOutputValidationError) as caught:
        Pipeline._validate_generated_patch({
            "addNodes": [{"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]}],
            "addEdges": [{"id": "bad", "source": "work", "target": "missing", "sourceSentences": ["S1"]}],
        }, Graph())
    assert caught.value.issues[0]["missingEndpoints"] == ["missing"]


def test_candidate_structure_validation_keeps_non_final_gateway_degree_non_blocking():
    pipeline = Pipeline(Settings())
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "review", "type": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_edits(previous, [
        EditCall(name="add_gateway", arguments={"id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S2"]}),
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
            assert tools is False and agent == "repair"
            return LlmResult(content=json.dumps({
                "addNodes": [{
                    "id": "rejected", "type": "task", "name": "Reject",
                    "sourceSentences": ["S2"],
                }],
                "addEdges": [{
                    "id": "f_choice_rejected", "source": "choice", "target": "rejected",
                    "condition": {"label": "not approved"}, "sourceSentences": ["S2"],
                }],
                "removeNodeIds": [], "removeEdgeIds": [],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeRepairLlm()
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "review", "type": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_edits(previous, [
        EditCall(name="add_gateway", arguments={"id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S2"]}),
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
        "sentences": [
            {"id": "S1", "text": "Review."},
            {"id": "S2", "text": "Decide."},
        ],
        "graph": previous.model_dump(),
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": False},
        "issues": [{"code": "BATCH_GATEWAY_DEGREE", "message": "complete the split"}],
    })

    stage, state = pipeline.step("STRUCTURE_REPAIRING", state, "Review. Decide.")
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
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "context": {"isFinalSegment": True},
    })

    stage, state = pipeline.step("CANDIDATE_APPLIED", state, "Work.")

    assert stage == "STRUCTURE_VALIDATED"
    assert {issue["code"] for issue in state["issues"]} == {
        "END_COUNT", "TASK_MISSING_OUTGOING",
    }
    stage, _ = pipeline.step("STRUCTURE_VALIDATED", state, "Work.")
    assert stage == "STRUCTURE_REPAIRING"


def test_final_segment_keeps_incomplete_gateway_degree_blocking():
    pipeline = Pipeline(Settings())
    candidate = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_event", arguments={"id": "end_failed", "name": "End", "type": "endEvent"}),
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


def test_generate_prompt_contains_minimal_input_graph_knowledge_and_three_examples():
    content = prompt("generator")

    assert "currentSegment（当前业务片段）" in content
    assert "descriptionPrefix（截至当前的完整流程描述）" in content
    assert "graph（此前已经提交的流程图）" in content
    assert "isFinalSegment（当前业务片段是否为最后一个片段）" in content
    assert '{"nodes":[],"edges":[]}' in content
    assert "startEvent 表示流程触发；无入边，可以有一条或多条出边" in content
    assert "endEvent 表示一条路径的结束结果；无出边，可以接收一条或多条入边" in content
    assert "语义原子性要求每个 Task 表示一个可独立执行或观察的业务动作" in content
    assert "原文锚定性要求名称优先沿用流程描述中的动作词和业务对象" in content
    assert "不得用“处理”“管理”“执行”等宽泛词语" in content
    assert "也不把同一动作机械拆分" in content
    assert "Task 在最终流程中必须恰好有一条入边和一条出边" in content
    assert "先 join、后 split" in content
    assert "Event 表示业务动作边界上的流程事实，不执行业务工作" in content
    assert "actionMappings" not in content
    assert "currentSegment.actions" not in content
    assert "currentSegment.controlFlow" not in content
    assert "必须保留原文明示的可选、备选、并行和循环语义" in content
    assert "只在语义上属于同一判断或同步区域时复用历史 Gateway" in content
    assert '"addNodes":[],"addEdges":[]' in content
    assert "同一已有 ID 必须同时出现在对应 remove 列表和 add 列表中" in content
    assert "removeNodeIds" in content and "removeEdgeIds" in content
    assert "衔接已经汇合的历史流程" in content
    assert "回溯历史审核并为循环增加入口 Join" in content
    assert "gateway_review_entry_join" in content
    assert content.count("## Example") == 2


def test_repair_semantic_prompt_adjudicates_findings_and_returns_a_patch_directly():
    content = prompt("repair_semantic")

    assert "独立判断每个语义问题是否成立" in content
    assert "不得盲从语义审查结果" in content
    assert "不得重新生成完整流程图" in content
    assert '"patch"' in content
    assert "source -> target" in content
    assert "当前流程图能够合理表达原文" in content
    assert "全部 decision 为 `reject` 时" in content
    assert "存在 `accept` 时" in content
    assert "早期片段或早期修订创建的节点和边" in content
    assert "可以回溯删除、替换或重新连接任何已有元素" in content
    assert "在当前整图上回溯修订审核循环" in content
    assert content.count("## Example") == 1
    assert '"decisions"' in content
    assert '"removeNodeIds"' in content


def test_planner_prompt_prioritizes_business_semantics_and_keeps_obvious_control_structures_together():
    content = prompt("planner")

    assert "不破坏业务语义边界的前提下尽量保持局部控制结构完整" in content
    assert "同一判断及其互补结果" in content
    assert "业务语义完整性优先于控制结构形式完整性" in content
    assert content.count("## Example") == 0
    assert '"actions"' not in content
    assert '"controlFlow"' not in content
    assert "语义契约" not in content


def test_reviewer_prompt_requires_direct_evidence_and_accepts_reasonable_equivalent_models():
    content = prompt("reviewer")

    assert "不要吹毛求疵" in content
    assert "没有直接证据证明存在明显且实质性的语义错误" in content
    assert "文本存在多种合理解释时" in content
    assert "source -> target" in content
    assert "错误行为能够真实复现" in content
    assert "历史 finding 已被合理拒绝" in content
    assert '"findings"' in content
    assert "JSON 对象" in content
    assert "多个具有独立业务效果的动作被错误合并" in content
    assert "证据不足、语义不确定或只是建模方式不同" in content
    assert "semanticContracts" not in content
    assert "Event 吞并 Task" not in content
    assert "Start 或 End" not in content


def test_structure_repair_uses_direct_graph_patch_and_a_distinct_prompt():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"messages": messages, "tools": tools, "repair": repair, "agent": agent})
            return LlmResult(content=json.dumps({
                "addNodes": [{
                    "id": "fixed", "type": "task", "name": "Fixed",
                    "sourceSentences": ["S1"],
                }],
                "addEdges": [], "removeNodeIds": [], "removeEdgeIds": [],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "sentences": [{"id": "S1", "text": "Work."}],
        "context": {"isFinalSegment": True},
        "candidate": Graph().model_dump(),
        "issues": [{"code": "WRONG", "message": "Fix it"}],
    })

    stage, updated = pipeline.step("STRUCTURE_REPAIRING", state, "Work.")
    repair_call = pipeline.llm.calls[0]
    repair_payload = json.loads(repair_call["messages"][1]["content"])

    assert stage == "CANDIDATE_APPLIED"
    assert repair_call["agent"] == "repair"
    assert repair_call["tools"] is False
    assert repair_call["messages"][0]["content"] == prompt("repair_structure")
    assert set(repair_payload) == {"descriptionPrefix", "candidateGraph", "issues"}
    assert set(repair_payload["candidateGraph"]) == {"nodes", "edges"}
    assert prompt("repair_structure") != prompt("generator")
    assert "最小增删操作" in prompt("repair_structure")
    assert "结构校验结果是必须处理的事实" in prompt("repair_structure")
    assert "source -> target" in prompt("repair_structure")
    assert "不进行语义审查" in prompt("repair_structure")
    assert '"nodes"' in prompt("generator") and '"edges"' in prompt("generator")
    assert updated["repairHistory"][-1]["mode"] == "structure"
    assert updated["repairHistory"][-1]["patch"] == updated["structureRepairPatch"]


def test_final_validation_issues_enter_repair_and_can_be_closed():
    class FakeRepair:
        def complete(self, messages, tools=False, repair=False, agent=""):
            assert agent == "repair"
            return LlmResult(content=json.dumps({
                "addNodes": [{
                    "id": "end", "name": "End", "type": "endEvent",
                    "sourceSentences": ["S1"],
                }],
                "addEdges": [{
                    "id": "flow_work_to_end", "source": "work", "target": "end",
                    "sourceSentences": ["S1"],
                }],
                "removeNodeIds": [], "removeEdgeIds": [],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeRepair()
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "sentences": [{"id": "S1", "text": "Work."}],
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "segmentIndex": 1,
        "graph": graph.model_dump(),
    })

    stage, state = pipeline.step("SEGMENT_COMMITTED", state, "Work.")
    assert stage == "STRUCTURE_REPAIRING"
    assert {item["code"] for item in state["issues"]} == {
        "END_COUNT", "TASK_MISSING_OUTGOING",
    }
    leaf_issue = next(
        item for item in state["issues"] if item["code"] == "TASK_MISSING_OUTGOING"
    )
    assert leaf_issue["elementIds"] == ["work"]
    assert "End Event" in leaf_issue["message"]

    stage, state = pipeline.step("STRUCTURE_REPAIRING", state, "Work.")

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


def test_final_unsound_graph_enters_woflan_guided_repair_and_rechecks(monkeypatch):
    class FakeRepair:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"agent": agent, "messages": messages})
            assert agent == "repair"
            return LlmResult(content=json.dumps({
                "addNodes": [{
                    "id": "work", "name": "Work repaired", "type": "task",
                    "sourceSentences": ["S1"],
                }],
                "addEdges": [], "removeNodeIds": ["work"], "removeEdgeIds": [],
            }), tool_calls=[], usage={}, latency_ms=1, raw={})

    results = iter([
        {
            "protocol": "test-woflan",
            "calculationSucceeded": True,
            "sound": False,
            "diagnostics": {
                "Outputs.DIAGNOSTIC_MESSAGES": ["Places are not covered."],
                "uncovered_places_s_component": ["flow_loop"],
                "dead_tasks": [],
                "s_c_net": "large raw Petri net omitted from prompt",
            },
            "conversion": {"protocol": "test-conversion"},
        },
        {
            "protocol": "test-woflan",
            "calculationSucceeded": True,
            "sound": True,
            "diagnostics": {
                "Outputs.DIAGNOSTIC_MESSAGES": ["All tasks are live."],
                "uncovered_places_s_component": [],
                "dead_tasks": [],
            },
            "conversion": {"protocol": "test-conversion"},
        },
    ])
    monkeypatch.setattr("app.pipeline.assess_soundness", lambda _xml: next(results))
    settings = Settings()
    settings.pipeline.reviewer_enabled = True
    pipeline = Pipeline(settings)
    pipeline.llm = FakeRepair()
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "work", "name": "Work", "type": "task", "sourceSentences": ["S1"]},
        {"id": "end", "name": "End", "type": "endEvent", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "semantic")
    state.update({
        "sentences": [{"id": "S1", "text": "Work."}],
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "segmentIndex": 1,
        "graph": graph.model_dump(),
    })

    stage, state = pipeline.step("FINAL_VALIDATED", state, "Work.")

    assert stage == "STRUCTURE_REPAIRING"
    assert state["soundnessRepairing"] is True
    assert state["issues"][0]["code"] == "WORKFLOW_NET_UNSOUND"
    assert state["soundnessPromptDiagnostics"]["uncoveredPlacesSComponent"] == ["flow_loop"]
    assert "s_c_net" not in state["soundnessPromptDiagnostics"]
    assert state["context"]["descriptionPrefix"] == [{"id": "S1", "text": "Work."}]

    stage, state = pipeline.step("STRUCTURE_REPAIRING", state, "Work.")
    assert stage == "FINAL_VALIDATED"

    stage, state = pipeline.step(stage, state, "Work.")

    assert stage == "BPMN_GENERATED"
    assert state["soundness"]["sound"] is True
    assert state["soundnessRepairCount"] == 1
    assert len(state["soundnessHistory"]) == 2
    assert state["snapshots"][-1]["soundness"]["sound"] is True
    assert [call["agent"] for call in pipeline.llm.calls] == ["repair"]


def test_soundness_repair_limit_preserves_completion_and_delivers_unsound_artifact(monkeypatch):
    monkeypatch.setattr("app.pipeline.assess_soundness", lambda _xml: {
        "protocol": "test-woflan",
        "calculationSucceeded": True,
        "sound": False,
        "diagnostics": {
            "Outputs.DIAGNOSTIC_MESSAGES": ["Not sound."],
            "uncovered_places_s_component": ["flow_loop"],
        },
        "conversion": {},
    })
    settings = Settings()
    settings.pipeline.max_soundness_repairs = 0
    pipeline = Pipeline(settings)
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent"},
        {"id": "work", "name": "Work", "type": "task"},
        {"id": "end", "name": "End", "type": "endEvent"},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "semantic")
    state["graph"] = graph.model_dump()

    stage, state = pipeline.step("FINAL_VALIDATED", state, "Work.")

    assert stage == "BPMN_GENERATED"
    assert state["bpmnXml"]
    assert state["soundness"]["sound"] is False
    assert state["soundnessRepairExhausted"] is True
    assert state["structureWarnings"][-1]["code"] == "SOUNDNESS_REPAIR_LIMIT_REACHED"

    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "COMPLETED"


def test_soundness_repair_agent_failure_preserves_completion(monkeypatch):
    monkeypatch.setattr("app.pipeline.assess_soundness", lambda _xml: {
        "protocol": "test-woflan",
        "calculationSucceeded": True,
        "sound": False,
        "diagnostics": {
            "Outputs.DIAGNOSTIC_MESSAGES": ["Not sound."],
            "uncovered_places_s_component": ["flow_loop"],
        },
        "conversion": {},
    })
    pipeline = Pipeline(Settings())  # no configured LLM: final repair cannot run
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent"},
        {"id": "work", "name": "Work", "type": "task"},
        {"id": "end", "name": "End", "type": "endEvent"},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "semantic")
    state["graph"] = graph.model_dump()

    stage, state = pipeline.step("FINAL_VALIDATED", state, "Work.")
    assert stage == "STRUCTURE_REPAIRING"

    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "FINAL_VALIDATED"
    assert state["soundnessRepairBlocked"] is True
    assert state["structureWarnings"][-1]["code"] == "SOUNDNESS_REPAIR_AGENT_FAILED"

    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "BPMN_GENERATED"
    assert state["soundness"]["sound"] is False
    assert state["bpmnXml"]


def test_repair_limit_retains_latest_segment_candidate_and_continues():
    settings = Settings()
    settings.pipeline.max_structure_repairs = 2
    pipeline = Pipeline(settings)
    candidate = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
        {"id": "end", "name": "End", "type": "endEvent", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "candidate": candidate.model_dump(),
        "issues": [{"code": "UNRESOLVED", "message": "Still unresolved"}],
        "repairCount": 2,
    })

    stage, state = pipeline.step("STRUCTURE_REPAIRING", state, "Work.")

    assert stage == "SEGMENT_COMMITTED"
    assert state["graph"]["nodes"] == candidate.model_dump()["nodes"]
    assert state["graph"]["edges"] == candidate.model_dump()["edges"]
    assert state["graph"]["metadata"]["processedSegments"] == ["P1"]
    assert state["segmentIndex"] == 1
    assert state["repairLimitEvents"][-1]["candidateRetained"] is True
    assert state["structureWarnings"][-1]["code"] == "REPAIR_LIMIT_REACHED"


def test_finalization_repair_limit_retains_latest_candidate_and_generates_output():
    settings = Settings()
    settings.pipeline.max_structure_repairs = 1
    settings.pipeline.max_soundness_repairs = 0
    pipeline = Pipeline(settings)
    candidate = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "work", "type": "task", "name": "Work", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("Work.", "single_segment")
    state.update({
        "segments": [{"id": "P1", "start": "S1", "end": "S1", "title": "Work", "sentenceIds": ["S1"]}],
        "segmentIndex": 1,
        "candidate": candidate.model_dump(),
        "issues": [{"code": "OPEN_NODE", "message": "Missing end"}],
        "repairCount": 1,
        "finalizing": True,
    })

    stage, state = pipeline.step("STRUCTURE_REPAIRING", state, "Work.")
    assert stage == "FINAL_VALIDATED"
    assert state["graph"] == candidate.model_dump()
    assert state["repairLimitEvents"][-1]["finalization"] is True

    stage, state = pipeline.step(stage, state, "Work.")
    assert stage == "BPMN_GENERATED"
    assert state["bpmnXml"]


def test_json_agent_retries_empty_content():
    class FakeLlm:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls += 1
            content = None if self.calls == 1 else '{"segments": []}'
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={},
                             thinking_mode="enabled" if agent == "planner" else "disabled")

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


def test_repair_empty_content_retries_with_the_same_request_options():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent="", **kwargs):
            self.calls.append({"agent": agent, **kwargs})
            content = None if len(self.calls) == 1 else '{"strategy":"retry","rationale":"ok"}'
            return LlmResult(
                content=content, tool_calls=[], usage={}, latency_ms=1, raw={},
                thinking_mode=kwargs.get("thinking_override", "enabled"),
            )

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Work.", "semantic")

    value = pipeline._complete_json(
        state, "repair", [{"role": "user", "content": "repair"}]
    )

    assert value == {"strategy": "retry", "rationale": "ok"}
    assert pipeline.llm.calls == [
        {"agent": "repair"},
        {"agent": "repair"},
    ]
    assert [call["thinkingMode"] for call in state["llmCalls"]] == ["enabled", "enabled"]


def test_repair_patch_retry_repeats_the_original_prompt_and_data_verbatim():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent="", **kwargs):
            self.calls.append(messages)
            content = "not json" if len(self.calls) == 1 else json.dumps({
                "addNodes": [{
                    "id": "task_fixed", "type": "task", "name": "Fixed",
                    "sourceSentences": ["S1"],
                }],
                "addEdges": [], "removeNodeIds": [], "removeEdgeIds": [],
            })
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("Fix.", "single_segment")
    messages = [{"role": "user", "content": "repair original graph"}]

    result = pipeline._complete_json(
        state, "repair", messages,
        validator=lambda value: pipeline._validate_repair_patch(value, Graph()),
    )

    assert result["addNodes"][0]["id"] == "task_fixed"
    assert pipeline.llm.calls == [messages, messages]


def test_transport_failure_does_not_consume_agent_output_retries():
    class FailingLlm:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=False, repair=False, agent="", **kwargs):
            self.calls += 1
            raise RuntimeError("transport unavailable")

    pipeline = Pipeline(Settings())
    pipeline.llm = FailingLlm()
    state = pipeline.initial_state("Work.", "single_segment")

    with pytest.raises(RuntimeError, match="transport unavailable"):
        pipeline._complete_json(state, "generator", [{"role": "user", "content": "generate"}])

    assert pipeline.llm.calls == 1
    assert state["agentFailures"] == []


def test_code_split_sentences_are_sent_directly_to_planner():
    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools=False, repair=False, agent=""):
            self.calls.append({"messages": messages, "agent": agent})
            content = json.dumps({"segments": [{
                "id": "P1", "start": "S1", "end": "S2", "title": "Work",
            }]})
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("First. Second.", "semantic")
    _, state = pipeline.step("INPUT_READY", state, "First. Second.")

    stage, state = pipeline.step("SENTENCES_PREPARED", state, "First. Second.")

    assert stage == "SEGMENTS_CREATED"
    assert pipeline.llm.calls[0]["agent"] == "planner"
    assert pipeline.llm.calls[0]["messages"][1]["content"] == "S1 First.\nS2 Second."
    assert state["segments"][0]["sentenceIds"] == ["S1", "S2"]


def test_semantic_planner_tolerates_trailing_sentence_id_punctuation():
    class FakeLlm:
        def complete(self, messages, tools=False, repair=False, agent=""):
            content = json.dumps({"segments": [{
                "id": "P1", "start": "S1", "end": "S3,", "title": "Work",
            }]})
            return LlmResult(content=content, tool_calls=[], usage={}, latency_ms=1, raw={})

    pipeline = Pipeline(Settings())
    pipeline.llm = FakeLlm()
    state = pipeline.initial_state("First. Second. Third.", "semantic")
    _, state = pipeline.step("INPUT_READY", state, "First. Second. Third.")

    stage, state = pipeline.step("SENTENCES_PREPARED", state, "First. Second. Third.")

    assert stage == "SEGMENTS_CREATED"
    assert state["segments"][0]["end"] == "S3"
    assert state["segments"][0]["sentenceIds"] == ["S1", "S2", "S3"]


def test_incremental_context_always_contains_the_complete_description_prefix():
    pipeline = Pipeline(Settings())
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "open_task", "type": "task", "name": "Open task", "sourceSentences": ["S1"]},
    ]})], "P1")
    state = pipeline.initial_state("One. Two. Three.", "semantic")
    state.update({
        "sentences": [
            {"id": "S1", "text": "One."},
            {"id": "S2", "text": "Two."},
            {"id": "S3", "text": "Three."},
        ],
        "segments": [
            {"id": "P1", "start": "S1", "end": "S1", "title": "One", "sentenceIds": ["S1"]},
            {"id": "P2", "start": "S2", "end": "S2", "title": "Two", "sentenceIds": ["S2"]},
            {"id": "P3", "start": "S3", "end": "S3", "title": "Three", "sentenceIds": ["S3"]},
        ],
        "segmentIndex": 2,
    })

    context = pipeline._context(state, graph)

    assert context["currentSegment"]["id"] == "P3"
    assert context["descriptionPrefix"] == [
        {"id": "S1", "text": "One."},
        {"id": "S2", "text": "Two."},
        {"id": "S3", "text": "Three."},
    ]
    assert context["graph"] == {
        "nodes": graph.model_dump()["nodes"],
        "edges": graph.model_dump()["edges"],
    }
    assert context["isFinalSegment"] is True
    assert "contextSegments" not in context
    assert "sentences" not in context
    assert "openNodes" not in context
    assert "gatewayRefs" not in context


def test_context_does_not_depend_on_frontier_or_gateway_indexes():
    pipeline = Pipeline(Settings())
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_gateway", arguments={
            "id": "payment_result", "name": "Payment result",
            "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_event", arguments={"id": "end_failed", "name": "End", "type": "endEvent"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "payment_result"}),
        EditCall(name="add_edge", arguments={
            "id": "f2", "source": "payment_result", "target": "end_failed",
            "condition": {"label": "failed"},
        }),
    ], "P1")
    state = pipeline.initial_state("One. Two. Three.", "semantic")
    state.update({
        "sentences": [
            {"id": "S1", "text": "One."},
            {"id": "S2", "text": "Two."},
            {"id": "S3", "text": "Three."},
        ],
        "segments": [
            {"id": "P1", "start": "S1", "end": "S1", "title": "One", "sentenceIds": ["S1"]},
            {"id": "P2", "start": "S2", "end": "S2", "title": "Two", "sentenceIds": ["S2"]},
            {"id": "P3", "start": "S3", "end": "S3", "title": "Three", "sentenceIds": ["S3"]},
        ],
        "segmentIndex": 2,
    })

    context = pipeline._context(state, graph)

    assert context["currentSegment"]["id"] == "P3"
    assert [item["id"] for item in context["descriptionPrefix"]] == ["S1", "S2", "S3"]
    assert context["graph"] == {
        "nodes": graph.model_dump()["nodes"],
        "edges": graph.model_dump()["edges"],
    }
    assert "contextSegments" not in context
    assert "openNodes" not in context
    assert "gatewayRefs" not in context


def test_code_sentence_split_preserves_order_and_unique_ids():
    pipeline = Pipeline(Settings())
    state = pipeline.initial_state("One. Two.", "semantic")

    stage, state = pipeline.step("INPUT_READY", state, "One. Two.")

    assert stage == "SENTENCES_PREPARED"
    assert state["sentences"] == [
        {"id": "S1", "text": "One."},
        {"id": "S2", "text": "Two."},
    ]


def test_sentence_split_treats_newlines_as_spaces_and_keeps_colon_content_together():
    sentences = split_sentences(
        "Title: Inspection\n"
        "The inspection can begin:\n"
        "* Questions are asked.\n"
        "* Buttons are pressed."
    )

    assert [item.text for item in sentences] == [
        "Title: Inspection The inspection can begin: * Questions are asked.",
        "* Buttons are pressed.",
    ]


def test_sentence_split_supports_chinese_and_english_question_and_exclamation_marks():
    sentences = split_sentences(
        "Is the application complete? Yes! Continue. 是否完成？是的！继续处理。"
    )

    assert [item.text for item in sentences] == [
        "Is the application complete?", "Yes!", "Continue.",
        "是否完成？", "是的！", "继续处理。",
    ]


def test_sentence_split_protects_common_abbreviations_initialisms_and_numbers():
    sentences = split_sentences(
        "Dr. Smith at Acme Inc. uses e.g. BPMN 2.0 in the U.S. office. "
        "Tools etc. are prepared. See Fig. 3 and contact A. Brown. Continue."
    )

    assert [item.text for item in sentences] == [
        "Dr. Smith at Acme Inc. uses e.g. BPMN 2.0 in the U.S. office.",
        "Tools etc. are prepared.",
        "See Fig. 3 and contact A. Brown.",
        "Continue.",
    ]

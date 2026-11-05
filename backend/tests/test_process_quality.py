from __future__ import annotations

import pytest

from app.bpmn import graph_to_bpmn
from app.domain import Graph
from app.process_quality import (
    assess_soundness,
    calculate_control_flow_relations,
    calculate_global_control_flow_relations,
    calculate_gateway_type_role_counts,
    calculate_pmo_task_name_dice,
    calculate_task_alignment,
)


def label_similarity(left: str, right: str) -> float:
    table = {
        ("Check application", "Review application"): 0.82,
        ("Store record", "Archive record"): 0.76,
    }
    return 1.0 if left == right else table.get((left, right), 0.0)


def pme(tasks=None, gateways=None, flows=None):
    return {
        "tasks": tasks or [], "events": [], "gateways": gateways or [], "pools": [],
        "sequenceFlows": flows or [], "messageFlows": [],
    }


def test_task_alignment_uses_global_one_to_one_matching_and_counts_unmatched_as_zero():
    generated = pme(tasks=[
        {"id": "g1", "name": "Check application", "type": "Task"},
        {"id": "g2", "name": "Store record", "type": "Task"},
    ])
    reference = pme(tasks=[
        {"id": "r1", "name": "Review application", "type": "Task"},
        {"id": "r2", "name": "Archive record", "type": "Task"},
        {"id": "r3", "name": "Notify applicant", "type": "Task"},
    ])
    result = calculate_task_alignment(generated, reference, label_similarity, threshold=0.7)
    assert result["matchedCount"] == 2
    assert result["precision"] == 1.0
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["f1"] == pytest.approx(0.8)
    assert result["semanticAlignmentScore"] == pytest.approx((0.82 + 0.76) / 3)
    assert [item["threshold"] for item in result["sensitivity"]] == [0.6, 0.7, 0.8]
    assert [item["matched"] for item in result["sensitivity"]] == [2, 2, 1]
    assert {pair["generatedId"]: pair["referenceId"] for pair in result["pairs"]} == {
        "g1": "r1", "g2": "r2"
    }
    assert {pair["generatedId"]: pair["referenceId"] for pair in result["matchedPairs"]} == {
        "g1": "r1", "g2": "r2"
    }


def test_threshold_alignment_maximizes_match_count_before_total_similarity():
    generated = pme(tasks=[
        {"id": "g0", "name": "g0", "type": "Task"},
        {"id": "g1", "name": "g1", "type": "Task"},
        {"id": "g2", "name": "g2", "type": "Task"},
    ])
    reference = pme(tasks=[
        {"id": "r0", "name": "r0", "type": "Task"},
        {"id": "r1", "name": "r1", "type": "Task"},
        {"id": "r2", "name": "r2", "type": "Task"},
    ])
    matrix = {
        ("g0", "r0"): 0.99, ("g0", "r1"): 0.70, ("g0", "r2"): 0.00,
        ("g1", "r0"): 0.70, ("g1", "r1"): 0.00, ("g1", "r2"): 0.69,
        ("g2", "r0"): 0.00, ("g2", "r1"): 0.69, ("g2", "r2"): 0.00,
    }

    result = calculate_task_alignment(
        generated, reference, lambda left, right: matrix[(left, right)], threshold=0.7
    )

    assert sum(pair["matched"] for pair in result["continuousPairs"]) == 1
    assert result["matchedCount"] == 2
    assert {(pair["generatedId"], pair["referenceId"]) for pair in result["matchedPairs"]} == {
        ("g0", "r1"), ("g1", "r0")
    }
    assert result["f1"] == pytest.approx(2 / 3)


def test_pmo_task_name_dice_uses_official_greedy_frequency_protocol():
    generated = pme(tasks=[
        {"id": "g1", "name": "A", "type": "Task"},
        {"id": "g2", "name": "B", "type": "Task"},
    ])
    reference = pme(tasks=[
        {"id": "r1", "name": "A", "type": "Task"},
        {"id": "r2", "name": "A", "type": "Task"},
    ])

    result = calculate_pmo_task_name_dice(
        generated, reference, lambda left, right: 1.0 if left == right else 0.0
    )

    assert result["protocol"] == "PMo semantic-frequency-aware Task-name Dice"
    assert result["matchedCount"] == 1
    assert result["denominator"] == 4
    assert result["score"] == pytest.approx(0.5)


def test_task_alignment_clamps_cosine_scores_to_the_reported_zero_one_range():
    generated = pme(tasks=[{"id": "g1", "name": "G", "type": "Task"}])
    reference = pme(tasks=[{"id": "r1", "name": "R", "type": "Task"}])

    result = calculate_task_alignment(
        generated, reference, lambda _left, _right: -0.25, threshold=0.7
    )

    assert result["pairs"][0]["similarity"] == 0.0
    assert result["semanticAlignmentScore"] == 0.0


def test_gateway_type_role_counts_ignore_task_boundaries():
    generated = pme(
        tasks=[
            {"id": "ga", "name": "A", "type": "Task"},
            {"id": "gb", "name": "B", "type": "Task"},
            {"id": "gc", "name": "C", "type": "Task"},
        ],
        gateways=[{"id": "gx", "name": "", "type": "ExclusiveGateway", "role": "split"}],
        flows=[
            {"id": "g1", "sourceRef": "ga", "targetRef": "gx"},
            {"id": "g2", "sourceRef": "gx", "targetRef": "gb"},
            {"id": "g3", "sourceRef": "gx", "targetRef": "gc"},
        ],
    )
    reference = pme(
        tasks=[
            {"id": "ra", "name": "A", "type": "Task"},
            {"id": "rb", "name": "B", "type": "Task"},
            {"id": "rc", "name": "C", "type": "Task"},
        ],
        gateways=[{"id": "rx", "name": "", "type": "ExclusiveGateway"}],
        flows=[
            {"id": "r1", "sourceRef": "ra", "targetRef": "rx"},
            {"id": "r2", "sourceRef": "rx", "targetRef": "rb"},
            {"id": "r3", "sourceRef": "rx", "targetRef": "rc"},
        ],
    )
    result = calculate_gateway_type_role_counts(generated, reference)
    assert result["matchedCount"] == 1
    assert result["f1"] == 1.0
    assert result["counts"][0] == {
        "category": "exclusive-split",
        "generatedCount": 1,
        "referenceCount": 1,
        "matchedCount": 1,
        "signedCountError": 0,
        "absoluteCountError": 0,
    }


def test_gateway_type_role_counts_expand_reference_mixed_gateway():
    generated = pme(
        tasks=[{"id": item, "name": item.upper(), "type": "Task"} for item in "abcd"],
        gateways=[
            {"id": "join", "name": "", "type": "ExclusiveGateway", "role": "join"},
            {"id": "split", "name": "", "type": "ExclusiveGateway", "role": "split"},
        ],
        flows=[
            {"id": "g1", "sourceRef": "a", "targetRef": "join"},
            {"id": "g2", "sourceRef": "b", "targetRef": "join"},
            {"id": "g3", "sourceRef": "join", "targetRef": "split"},
            {"id": "g4", "sourceRef": "split", "targetRef": "c"},
            {"id": "g5", "sourceRef": "split", "targetRef": "d"},
        ],
    )
    reference = pme(
        tasks=[{"id": item, "name": item.upper(), "type": "Task"} for item in "abcd"],
        gateways=[{"id": "mixed", "name": "", "type": "ExclusiveGateway"}],
        flows=[
            {"id": "r1", "sourceRef": "a", "targetRef": "mixed"},
            {"id": "r2", "sourceRef": "b", "targetRef": "mixed"},
            {"id": "r3", "sourceRef": "mixed", "targetRef": "c"},
            {"id": "r4", "sourceRef": "mixed", "targetRef": "d"},
        ],
    )

    result = calculate_gateway_type_role_counts(generated, reference)

    assert result["generatedCount"] == 2
    assert result["referenceCount"] == 2
    assert result["matchedCount"] == 2
    assert result["f1"] == 1.0


def _two_branch_pme(gateway_type: str) -> dict:
    return pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
        ],
        gateways=[
            {"id": "split", "name": "", "type": gateway_type, "role": "split"},
            {"id": "join", "name": "", "type": gateway_type, "role": "join"},
        ],
        flows=[
            {"id": "f1", "sourceRef": "split", "targetRef": "a"},
            {"id": "f2", "sourceRef": "split", "targetRef": "b"},
            {"id": "f3", "sourceRef": "a", "targetRef": "join"},
            {"id": "f4", "sourceRef": "b", "targetRef": "join"},
        ],
    )


def test_graph_behavior_relations_classify_shared_parallel_split():
    generated = _two_branch_pme("ParallelGateway")
    reference = _two_branch_pme("ParallelGateway")
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    result = calculate_control_flow_relations(generated, reference, alignment)

    assert result["f1"] == 1.0
    assert result["matchedTaskPairCoverage"] == 1.0
    assert result["pairs"][0]["generatedRelation"] == "parallel"
    assert result["pairs"][0]["referenceRelation"] == "parallel"


def test_graph_behavior_relations_detect_gateway_semantic_mismatch():
    generated = _two_branch_pme("ExclusiveGateway")
    reference = _two_branch_pme("ParallelGateway")
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    result = calculate_control_flow_relations(generated, reference, alignment)

    assert result["precision"] == 0.0
    assert result["recall"] == 0.0
    assert result["f1"] == 0.0
    assert result["pairs"][0]["generatedRelation"] == "exclusive"
    assert result["pairs"][0]["referenceRelation"] == "parallel"


def test_graph_behavior_relations_use_mutual_reachability_for_cycles():
    generated = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
        ],
        flows=[
            {"id": "f1", "sourceRef": "a", "targetRef": "b"},
            {"id": "f2", "sourceRef": "b", "targetRef": "a"},
        ],
    )
    reference = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
        ],
        flows=[
            {"id": "f1", "sourceRef": "a", "targetRef": "b"},
            {"id": "f2", "sourceRef": "b", "targetRef": "a"},
        ],
    )
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    result = calculate_control_flow_relations(generated, reference, alignment)

    assert result["pairs"][0]["generatedRelation"] == "cycle"
    assert result["pairs"][0]["referenceRelation"] == "cycle"
    assert result["f1"] == 1.0


def test_global_relations_penalize_relations_of_missing_reference_task():
    generated = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
        ],
        flows=[{"id": "f1", "sourceRef": "a", "targetRef": "b"}],
    )
    reference = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
            {"id": "c", "name": "C", "type": "Task"},
        ],
        flows=[
            {"id": "f1", "sourceRef": "a", "targetRef": "b"},
            {"id": "f2", "sourceRef": "b", "targetRef": "c"},
        ],
    )
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    conditional = calculate_control_flow_relations(generated, reference, alignment)
    global_result = calculate_global_control_flow_relations(generated, reference, alignment)

    assert conditional["f1"] == 1.0
    assert global_result["generatedRelationCount"] == 1
    assert global_result["referenceRelationCount"] == 3
    assert global_result["matchedRelationCount"] == 1
    assert global_result["precision"] == 1.0
    assert global_result["recall"] == 1 / 3
    assert global_result["f1"] == 0.5


def test_global_relations_penalize_relations_of_extra_generated_task():
    generated = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
            {"id": "extra", "name": "Extra", "type": "Task"},
        ],
        flows=[
            {"id": "f1", "sourceRef": "a", "targetRef": "b"},
            {"id": "f2", "sourceRef": "b", "targetRef": "extra"},
        ],
    )
    reference = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
        ],
        flows=[{"id": "f1", "sourceRef": "a", "targetRef": "b"}],
    )
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    result = calculate_global_control_flow_relations(generated, reference, alignment)

    assert result["generatedRelationCount"] == 3
    assert result["referenceRelationCount"] == 1
    assert result["matchedRelationCount"] == 1
    assert result["precision"] == 1 / 3
    assert result["recall"] == 1.0
    assert result["f1"] == 0.5


def test_global_relations_detect_wrong_relation_for_matched_tasks():
    generated = _two_branch_pme("ExclusiveGateway")
    reference = _two_branch_pme("ParallelGateway")
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    result = calculate_global_control_flow_relations(generated, reference, alignment)

    assert result["generatedRelationCount"] == 1
    assert result["referenceRelationCount"] == 1
    assert result["matchedRelationCount"] == 0
    assert result["f1"] == 0.0


def test_global_relations_orient_generated_pair_by_aligned_reference_ids():
    generated = pme(
        tasks=[
            {"id": "z", "name": "A", "type": "Task"},
            {"id": "y", "name": "B", "type": "Task"},
        ],
        flows=[{"id": "f1", "sourceRef": "z", "targetRef": "y"}],
    )
    reference = pme(
        tasks=[
            {"id": "a", "name": "A", "type": "Task"},
            {"id": "b", "name": "B", "type": "Task"},
        ],
        flows=[{"id": "f1", "sourceRef": "a", "targetRef": "b"}],
    )
    alignment = calculate_task_alignment(generated, reference, label_similarity)

    result = calculate_global_control_flow_relations(generated, reference, alignment)

    assert result["matchedRelationCount"] == 1
    assert result["f1"] == 1.0


def _linear_graph(task_name: str = "Review application") -> Graph:
    return Graph.model_validate({
        "process": {"id": "process_main", "name": "Example"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "task", "type": "task", "name": task_name},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "task"},
            {"id": "f2", "source": "task", "target": "end"},
        ],
    })


def test_soundness_on_linear_model():
    xml = graph_to_bpmn(_linear_graph())
    soundness = assess_soundness(xml)
    assert soundness["calculationSucceeded"] is True
    assert soundness["sound"] is True
    assert "validity" not in soundness



def test_soundness_conversion_preflight_rejects_unresolved_flow_endpoint():
    xml = graph_to_bpmn(_linear_graph()).replace('targetRef="end"', 'targetRef="missing"')

    result = assess_soundness(xml)

    assert result["calculationSucceeded"] is False
    assert result["sound"] is False
    assert result["reason"]


@pytest.mark.parametrize(("node_type", "definition"), [
    ("intermediateCatchEvent", None),
    ("intermediateCatchEvent", "message"),
    ("intermediateCatchEvent", "timer"),
    ("intermediateCatchEvent", "signal"),
    ("intermediateCatchEvent", "link"),
    ("intermediateCatchEvent", "conditional"),
    ("intermediateThrowEvent", None),
    ("intermediateThrowEvent", "message"),
    ("intermediateThrowEvent", "signal"),
    ("intermediateThrowEvent", "link"),
    ("intermediateThrowEvent", "escalation"),
    ("intermediateThrowEvent", "compensation"),
])
def test_soundness_treats_graph_json_intermediate_events_as_silent_pass_through(
    node_type, definition
):
    event = {"id": "wait", "type": node_type, "name": "Wait"}
    if definition is not None:
        event["eventDefinition"] = definition
    graph = Graph.model_validate({
        "process": {"id": "process_main", "name": "Intermediate event"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            event,
            {"id": "task", "type": "task", "name": "Continue"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "wait"},
            {"id": "f2", "source": "wait", "target": "task"},
            {"id": "f3", "source": "task", "target": "end"},
        ],
    })

    result = assess_soundness(graph_to_bpmn(graph))

    assert result["calculationSucceeded"] is True
    assert result["sound"] is True
    normalized = result["conversion"]["normalizedIntermediateEvents"]
    assert normalized == [{
        "id": "wait",
        "originalType": node_type,
        "eventDefinitions": [definition or "none"],
        "incoming": 1,
        "outgoing": 1,
        "petriAbstraction": "silent pass-through transition",
    }]


@pytest.mark.parametrize(("event_type", "definition"), [
    ("startEvent", "message"),
    ("startEvent", "timer"),
    ("startEvent", "signal"),
    ("startEvent", "conditional"),
    ("endEvent", "message"),
    ("endEvent", "signal"),
    ("endEvent", "error"),
    ("endEvent", "escalation"),
    ("endEvent", "terminate"),
    ("endEvent", "compensation"),
])
def test_soundness_accepts_graph_json_start_and_end_event_definitions(
    event_type, definition
):
    graph = _linear_graph().model_dump()
    target = graph["nodes"][0 if event_type == "startEvent" else 2]
    target["eventDefinition"] = definition

    result = assess_soundness(graph_to_bpmn(Graph.model_validate(graph)))

    assert result["calculationSucceeded"] is True
    assert result["sound"] is True


def test_soundness_preserves_parallel_multi_out_intermediate_event_semantics():
    graph = Graph.model_validate({
        "process": {"id": "process_main", "name": "Intermediate event parallel split"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "wait", "type": "intermediateCatchEvent", "name": "Wait"},
            {"id": "a", "type": "task", "name": "A"},
            {"id": "b", "type": "task", "name": "B"},
            {"id": "join", "type": "parallelGateway", "name": "Join", "role": "join"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "wait"},
            {"id": "f2", "source": "wait", "target": "a"},
            {"id": "f3", "source": "wait", "target": "b"},
            {"id": "f4", "source": "a", "target": "join"},
            {"id": "f5", "source": "b", "target": "join"},
            {"id": "f6", "source": "join", "target": "end"},
        ],
    })

    result = assess_soundness(graph_to_bpmn(graph))

    assert result["calculationSucceeded"] is True
    assert result["sound"] is True
    assert result["conversion"]["normalizedIntermediateEvents"][0]["incoming"] == 1
    assert result["conversion"]["normalizedIntermediateEvents"][0]["outgoing"] == 2
    assert result["conversion"]["normalizedIntermediateEvents"][0]["petriAbstraction"] == (
        "silent parallel split"
    )


def test_soundness_preserves_parallel_multi_out_start_event_semantics():
    graph = Graph.model_validate({
        "process": {"id": "process_main", "name": "Start event parallel split"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "a", "type": "task", "name": "A"},
            {"id": "b", "type": "task", "name": "B"},
            {"id": "join", "type": "parallelGateway", "name": "Join", "role": "join"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "a"},
            {"id": "f2", "source": "start", "target": "b"},
            {"id": "f3", "source": "a", "target": "join"},
            {"id": "f4", "source": "b", "target": "join"},
            {"id": "f5", "source": "join", "target": "end"},
        ],
    })

    result = assess_soundness(graph_to_bpmn(graph))

    assert result["calculationSucceeded"] is True
    assert result["sound"] is True
    assert result["protocol"] == "pm4py_workflow_net_soundness_with_event_abstraction_v4"
    assert result["conversion"]["normalizedStartEvents"] == [{
        "id": "start",
        "outgoing": 2,
        "insertedParallelSplit": "start__pm4py_parallel_split",
        "petriAbstraction": "start followed by silent parallel split",
    }]


def test_soundness_preserves_uncontrolled_multi_in_intermediate_event_semantics():
    graph = Graph.model_validate({
        "process": {"id": "process_main", "name": "Intermediate event simple merge"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "choice", "type": "exclusiveGateway", "name": "Choice", "role": "split"},
            {"id": "a", "type": "task", "name": "A"},
            {"id": "b", "type": "task", "name": "B"},
            {"id": "wait", "type": "intermediateCatchEvent", "name": "Wait"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "choice"},
            {"id": "f2", "source": "choice", "target": "a", "condition": {"label": "A"}},
            {"id": "f3", "source": "choice", "target": "b", "condition": {"label": "B"}},
            {"id": "f4", "source": "a", "target": "wait"},
            {"id": "f5", "source": "b", "target": "wait"},
            {"id": "f6", "source": "wait", "target": "end"},
        ],
    })

    result = assess_soundness(graph_to_bpmn(graph))

    assert result["calculationSucceeded"] is True
    assert result["sound"] is True
    assert result["conversion"]["normalizedIntermediateEvents"][0]["petriAbstraction"] == (
        "silent simple merge"
    )


def test_soundness_accepts_exclusive_join_then_split_gateways():
    graph = Graph.model_validate({
        "process": {"id": "process_main", "name": "Join then split gateways"},
        "nodes": [
            {"id": "start", "type": "startEvent", "name": "Start"},
            {"id": "first", "type": "exclusiveGateway", "name": "First", "role": "split"},
            {"id": "a", "type": "task", "name": "A"},
            {"id": "b", "type": "task", "name": "B"},
            {"id": "second_join", "type": "exclusiveGateway", "name": "Merge", "role": "join"},
            {"id": "second_split", "type": "exclusiveGateway", "name": "Next", "role": "split"},
            {"id": "c", "type": "task", "name": "C"},
            {"id": "d", "type": "task", "name": "D"},
            {"id": "end", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "f1", "source": "start", "target": "first"},
            {"id": "f2", "source": "first", "target": "a", "condition": {"label": "A"}},
            {"id": "f3", "source": "first", "target": "b", "condition": {"label": "B"}},
            {"id": "f4", "source": "a", "target": "second_join"},
            {"id": "f5", "source": "b", "target": "second_join"},
            {"id": "f_join_split", "source": "second_join", "target": "second_split"},
            {"id": "f6", "source": "second_split", "target": "c", "condition": {"label": "C"}},
            {"id": "f7", "source": "second_split", "target": "d", "condition": {"label": "D"}},
            {"id": "f8", "source": "c", "target": "end"},
            {"id": "f9", "source": "d", "target": "end"},
        ],
    })

    result = assess_soundness(graph_to_bpmn(graph))

    assert result["calculationSucceeded"] is True
    assert result["sound"] is True

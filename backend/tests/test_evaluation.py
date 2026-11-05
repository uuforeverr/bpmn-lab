from __future__ import annotations

import pytest

from app.evaluation import (
    aggregate_token_usage,
    assess_bpmn_reference_scope,
    bpmn_to_reference,
    calculate_behavioral_similarity,
    calculate_pme_similarity,
    calculate_task_nesting_complexity,
    complexity_profile,
    graph_to_pme,
    natc_tertile_thresholds,
    natc_tier,
    semantic_frequency_dice,
    summarize_runs,
)


def exact_similarity(left: str, right: str) -> float:
    return 1.0 if left == right else 0.0


def empty_pme() -> dict:
    return {
        "tasks": [], "events": [], "gateways": [], "pools": [],
        "sequenceFlows": [], "messageFlows": [],
    }


def test_semantic_frequency_dice_preserves_duplicate_counts():
    result = semantic_frequency_dice(["A", "A"], ["A", "B"], exact_similarity)
    assert result.matched == 1
    assert result.denominator == 4
    assert result.score == 0.5


def test_pme_similarity_matches_official_weighted_dice_shape():
    reference = empty_pme()
    reference["tasks"] = [
        {"id": "r1", "name": "Approve order", "type": "User"},
        {"id": "r2", "name": "Archive order", "type": "Service"},
    ]
    generated = empty_pme()
    generated["tasks"] = [
        {"id": "g1", "name": "Approve order", "type": "User"},
    ]
    result = calculate_pme_similarity(generated, reference, exact_similarity)
    assert result["scores"]["taskNames"] == pytest.approx(2 / 3)
    assert result["scores"]["taskTypes"] == pytest.approx(2 / 3)
    assert result["scores"]["tasksOverall"] == pytest.approx(2 / 3)
    assert result["scores"]["overall"] == pytest.approx(2 / 3)
    assert result["diagnostics"]["taskNames"]["elementRecall"] == 0.5


def test_graph_to_pme_uses_dataset_type_names_and_flow_conditions():
    graph = {
        "nodes": [
            {"id": "s", "type": "startEvent", "name": "start"},
            {"id": "t", "type": "userTask", "name": "Review"},
            {"id": "g", "type": "exclusiveGateway", "name": "", "role": "split"},
        ],
        "edges": [
            {"id": "f", "source": "t", "target": "g", "condition": {"label": "yes"}},
        ],
    }
    pme = graph_to_pme(graph)
    assert pme["tasks"][0]["type"] == "User"
    assert pme["events"][0]["type"] == "StartNoneEvent"
    assert pme["gateways"][0]["type"] == "ExclusiveGateway"
    assert pme["gateways"][0]["role"] == "split"
    assert pme["sequenceFlows"][0]["condition"] == "yes"


def test_reference_bpmn_is_the_structural_source_for_scope_and_complexity():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
      <process id="p">
        <startEvent id="s"/>
        <task id="a" name="A"/>
        <exclusiveGateway id="x"/>
        <task id="b" name="B"/>
        <task id="c" name="C"/>
        <endEvent id="e"/>
        <sequenceFlow id="f1" sourceRef="s" targetRef="a"/>
        <sequenceFlow id="f2" sourceRef="a" targetRef="x"/>
        <sequenceFlow id="f3" sourceRef="x" targetRef="b"/>
        <sequenceFlow id="f4" sourceRef="x" targetRef="c"/>
        <sequenceFlow id="f5" sourceRef="b" targetRef="e"/>
        <sequenceFlow id="f6" sourceRef="c" targetRef="e"/>
      </process>
    </definitions>"""
    scope = assess_bpmn_reference_scope(xml)
    reference = bpmn_to_reference(xml)
    profile = complexity_profile(reference)

    assert scope["eligible"] is True
    assert [task["name"] for task in reference["tasks"]] == ["A", "B", "C"]
    assert profile["totalNodes"] == 6
    assert profile["controlFlowComplexity"] == 2


def test_task_nesting_complexity_counts_task_control_contexts():
    reference = empty_pme()
    reference["tasks"] = [
        {"id": task_id, "name": task_id.upper(), "type": "Task"}
        for task_id in ("a", "b", "c")
    ]
    reference["events"] = [
        {"id": "s", "name": "", "type": "StartNoneEvent"},
        {"id": "e", "name": "", "type": "EndNoneEvent"},
    ]
    reference["gateways"] = [
        {"id": "x", "name": "", "type": "ExclusiveGateway"},
        {"id": "j", "name": "", "type": "ExclusiveGateway"},
    ]
    reference["sequenceFlows"] = [
        {"id": "1", "sourceRef": "s", "targetRef": "x"},
        {"id": "2", "sourceRef": "x", "targetRef": "a"},
        {"id": "3", "sourceRef": "x", "targetRef": "b"},
        {"id": "4", "sourceRef": "a", "targetRef": "j"},
        {"id": "5", "sourceRef": "b", "targetRef": "j"},
        {"id": "6", "sourceRef": "j", "targetRef": "c"},
        {"id": "7", "sourceRef": "c", "targetRef": "e"},
    ]

    result = calculate_task_nesting_complexity(reference)

    assert {row["taskId"]: row["depth"] for row in result["taskDepths"]} == {
        "a": 1, "b": 1, "c": 0,
    }
    assert result["meanTaskNestingDepth"] == pytest.approx(2 / 3)
    assert result["maxTaskNestingDepth"] == 1
    assert result["natc1"] == 5
    assert result["natc2"] == 9


def test_task_nesting_complexity_accumulates_nested_regions():
    reference = empty_pme()
    reference["tasks"] = [
        {"id": task_id, "name": task_id.upper(), "type": "Task"}
        for task_id in ("a", "b", "c", "d", "z")
    ]
    reference["events"] = [
        {"id": "s", "name": "", "type": "StartNoneEvent"},
        {"id": "e", "name": "", "type": "EndNoneEvent"},
    ]
    reference["gateways"] = [
        {"id": gateway_id, "name": "", "type": "ExclusiveGateway"}
        for gateway_id in ("outer_split", "inner_split", "inner_join", "outer_join")
    ]
    reference["sequenceFlows"] = [
        {"id": "1", "sourceRef": "s", "targetRef": "outer_split"},
        {"id": "2", "sourceRef": "outer_split", "targetRef": "a"},
        {"id": "3", "sourceRef": "a", "targetRef": "outer_join"},
        {"id": "4", "sourceRef": "outer_split", "targetRef": "inner_split"},
        {"id": "5", "sourceRef": "inner_split", "targetRef": "b"},
        {"id": "6", "sourceRef": "inner_split", "targetRef": "c"},
        {"id": "7", "sourceRef": "b", "targetRef": "inner_join"},
        {"id": "8", "sourceRef": "c", "targetRef": "inner_join"},
        {"id": "9", "sourceRef": "inner_join", "targetRef": "d"},
        {"id": "10", "sourceRef": "d", "targetRef": "outer_join"},
        {"id": "11", "sourceRef": "outer_join", "targetRef": "z"},
        {"id": "12", "sourceRef": "z", "targetRef": "e"},
    ]

    result = calculate_task_nesting_complexity(reference)

    assert {row["taskId"]: row["depth"] for row in result["taskDepths"]} == {
        "a": 1, "b": 2, "c": 2, "d": 1, "z": 0,
    }
    assert result["maxTaskNestingDepth"] == 2
    assert result["natc1"] == 11
    assert result["natc2"] == 27


def test_task_nesting_complexity_uses_virtual_sink_for_terminal_branches():
    reference = empty_pme()
    reference["tasks"] = [
        {"id": "a", "name": "A", "type": "Task"},
        {"id": "b", "name": "B", "type": "Task"},
    ]
    reference["events"] = [
        {"id": "s", "name": "", "type": "StartNoneEvent"},
        {"id": "e1", "name": "", "type": "EndNoneEvent"},
        {"id": "e2", "name": "", "type": "EndNoneEvent"},
    ]
    reference["gateways"] = [
        {"id": "x", "name": "", "type": "ExclusiveGateway"},
    ]
    reference["sequenceFlows"] = [
        {"id": "1", "sourceRef": "s", "targetRef": "x"},
        {"id": "2", "sourceRef": "x", "targetRef": "a"},
        {"id": "3", "sourceRef": "x", "targetRef": "b"},
        {"id": "4", "sourceRef": "a", "targetRef": "e1"},
        {"id": "5", "sourceRef": "b", "targetRef": "e2"},
    ]

    result = calculate_task_nesting_complexity(reference)

    assert {row["taskId"]: row["depth"] for row in result["taskDepths"]} == {
        "a": 1, "b": 1,
    }
    assert result["regions"][0]["boundaryId"] is None
    assert result["natc1"] == 4
    assert result["natc2"] == 8


def test_natc_tertiles_are_reference_only_and_tie_safe():
    profiles = [{"natc1": value} for value in (3, 6, 9, 12, 15, 18)]

    thresholds = natc_tertile_thresholds(profiles)

    assert thresholds == pytest.approx((8.0, 13.0))
    assert natc_tier(8, thresholds) == "low"
    assert natc_tier(9, thresholds) == "medium"
    assert natc_tier(14, thresholds) == "high"


def test_reference_bpmn_scope_rejects_collaboration_and_unsupported_gateway():
    xml = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
      <collaboration id="c">
        <participant id="p1" processRef="a"/><participant id="p2" processRef="b"/>
        <messageFlow id="m" sourceRef="p1" targetRef="p2"/>
      </collaboration>
      <process id="a"><startEvent id="s"/><eventBasedGateway id="g"/><endEvent id="e"/></process>
      <process id="b"><startEvent id="s2"/><endEvent id="e2"/></process>
    </definitions>"""
    scope = assess_bpmn_reference_scope(xml)

    assert scope["eligible"] is False
    assert "process_count=2" in scope["reasons"]
    assert "participant_count=2" in scope["reasons"]
    assert "message_flow_count=1" in scope["reasons"]
    assert "unsupported_gateways=eventBasedGateway" in scope["reasons"]


def test_behavioral_profile_distinguishes_exclusive_branches():
    reference = empty_pme()
    reference["tasks"] = [
        {"id": "a", "name": "A", "type": "Task"},
        {"id": "b", "name": "B", "type": "Task"},
    ]
    reference["gateways"] = [
        {"id": "x1", "name": "", "type": "ExclusiveGateway"},
        {"id": "x2", "name": "", "type": "ExclusiveGateway"},
    ]
    reference["sequenceFlows"] = [
        {"id": "1", "sourceRef": "x1", "targetRef": "a"},
        {"id": "2", "sourceRef": "x1", "targetRef": "b"},
        {"id": "3", "sourceRef": "a", "targetRef": "x2"},
        {"id": "4", "sourceRef": "b", "targetRef": "x2"},
    ]
    result = calculate_behavioral_similarity(reference, reference, exact_similarity)
    assert result["scores"]["strictOrder"] == 1.0
    assert result["scores"]["exclusiveness"] == 1.0
    assert result["scores"]["interleaving"] == 1.0
    assert result["referenceRelationCounts"]["exclusiveness"] == 1


def test_token_usage_includes_retries_cache_reasoning_and_agent_breakdown():
    calls = [
        {
            "agent": "generator", "model": "dsv4-flash", "latencyMs": 12,
            "usage": {
                "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        },
        {
            "agent": "generator", "model": "dsv4-flash", "latencyMs": 8,
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    ]
    result = aggregate_token_usage(calls)
    assert result["calls"] == 2
    assert result["inputTokens"] == 15
    assert result["outputTokens"] == 6
    assert result["totalTokens"] == 21
    assert result["cachedInputTokens"] == 3
    assert result["reasoningTokens"] == 2
    assert result["byAgent"]["generator"]["latencyMs"] == 20


def test_summary_uses_one_soundness_rate_over_final_bpmn_outputs():
    base = {
        "sample": "01",
        "repetition": 1,
        "strategy": "semantic",
        "complexity": {"sizeBucket": "S", "weightedLevel": "low", "tasks": 1,
                       "events": 2, "totalNodes": 3, "gateways": 0,
                       "sequenceFlows": 2, "nestingDepth": 0,
                       "controlFlowComplexity": 0},
        "tokenUsage": {"totalTokens": 10},
        "durationSeconds": 1.0,
    }
    runs = [
        {**base, "success": True, "bpmnXml": "<definitions/>", "quality": {
            "sound": True,
            "taskSemanticAlignmentScore": 1.0, "taskPrecision": 1.0,
            "taskRecall": 1.0, "taskF1": 1.0,
            "gatewayTypeRolePrecision": 1.0, "gatewayTypeRoleRecall": 1.0,
            "gatewayTypeRoleF1": 1.0, "gatewayTypeRoleAbsoluteCountError": 0.0,
            "controlFlowRelationPrecision": 1.0, "controlFlowRelationRecall": 1.0,
            "controlFlowRelationF1": 1.0,
            "controlFlowRelationMatchedTaskPairCoverage": 1.0,
        }},
        {**base, "repetition": 2, "success": True, "bpmnXml": "<definitions/>",
         "quality": {"sound": False}},
        {**base, "repetition": 3, "success": False},
    ]

    trend = summarize_runs(runs)["trendsBySize"][0]

    assert trend["soundnessOutputs"] == 2
    assert trend["soundnessPassed"] == 1
    assert trend["soundnessRate"] == 0.5
    assert "soundnessRateAssessable" not in trend
    assert "soundnessRateEndToEnd" not in trend
    assert trend["controlFlowRelationF1N"] == 1


def test_summary_reports_task_thresholds_by_complexity_and_paired_deltas():
    def run(strategy, similarities):
        return {
            "sample": "01", "repetition": 1, "strategy": strategy,
            "success": True, "bpmnXml": "<definitions/>",
            "complexity": {"sizeBucket": "M", "weightedLevel": "medium",
                           "tasks": 2, "events": 2, "totalNodes": 4,
                           "gateways": 0, "sequenceFlows": 3, "nestingDepth": 0,
                           "controlFlowComplexity": 0},
            "tokenUsage": {"totalTokens": 10}, "durationSeconds": 1.0,
            "quality": {
                "sound": True,
                "taskAlignment": {
                    "generatedCount": 2, "referenceCount": 2,
                    "pairs": [{"similarity": value} for value in similarities],
                },
                "taskSemanticAlignmentScore": sum(similarities) / 2,
                "taskPrecision": 1.0, "taskRecall": 1.0, "taskF1": 1.0,
                "gatewayTypeRolePrecision": 1.0, "gatewayTypeRoleRecall": 1.0,
                "gatewayTypeRoleF1": 1.0, "gatewayTypeRoleAbsoluteCountError": 0.0,
                "controlFlowRelations": {
                    "sensitivity": [
                        {"threshold": threshold, "precision": 1.0, "recall": 1.0,
                         "f1": 1.0, "matchedTaskPairCoverage": 1.0}
                        for threshold in (0.6, 0.7, 0.8)
                    ]
                },
                "controlFlowRelationPrecision": 1.0,
                "controlFlowRelationRecall": 1.0,
                "controlFlowRelationF1": 1.0,
                "controlFlowRelationMatchedTaskPairCoverage": 1.0,
            },
        }

    summary = summarize_runs([
        run("single_segment", [0.65, 0.75]),
        run("semantic", [0.75, 0.85]),
    ])
    trends = {item["strategy"]: item for item in summary["trendsBySize"]}
    assert [item["threshold"] for item in trends["semantic"]["taskThresholds"]] == [0.6, 0.7, 0.8]
    assert [item["f1"] for item in trends["semantic"]["taskThresholds"]] == [1.0, 1.0, 0.5]
    assert [item["f1"] for item in trends["single_segment"]["taskThresholds"]] == [1.0, 0.5, 0.0]
    assert [item["f1"] for item in summary["pairedDifferences"][0]["taskThresholdDeltas"]] == [0.0, 0.5, 0.5]
    assert summary["pairedFlowMeanDifferences"][0]["semanticMinusSingle"]["taskF1"] == 0.0

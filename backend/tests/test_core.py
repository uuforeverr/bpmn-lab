import pytest

from app.bpmn import graph_to_bpmn
from app.domain import EditCall, Graph
from app.editor import EditError, apply_edits, apply_graph_patch, apply_subgraph
from app.llm import parse_json_object
from app.segmentation import FixedLengthStrategy, split_sentences
from app.tools import EDIT_TOOLS
from app.validation import gateway_refs, open_nodes, validate_graph, validate_incremental_batch


def test_sentence_split_ignores_chinese_semicolon():
    result = split_sentences("提交申请；然后审核。Approved. Done")
    assert [s.text for s in result] == ["提交申请；然后审核。", "Approved.", "Done"]


def test_sentence_split_preserves_common_abbreviations():
    result = split_sentences("Answer based on the result, i.e. approval or rejection. Then notify.")
    assert [s.text for s in result] == [
        "Answer based on the result, i.e. approval or rejection.",
        "Then notify.",
    ]


def test_linear_macro_generates_edges():
    calls = [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "开始", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "review", "type": "task", "name": "审核申请", "sourceSentences": ["S1"]},
        {"id": "end", "name": "结束", "type": "endEvent", "sourceSentences": ["S2"]},
    ]})]
    graph = apply_edits(Graph(), calls, "P1")
    assert [edge.id for edge in graph.edges] == ["flow_start_to_review", "flow_review_to_end"]
    assert validate_graph(graph, final=True) == []
    assert "inclusiveGateway" not in graph_to_bpmn(graph)


def test_plain_event_omits_event_definition_from_graph_json_v2():
    graph = apply_edits(Graph(), [EditCall(name="add_node", arguments={
        "id": "start", "name": "Start",
        "type": "startEvent", "sourceSentences": ["S1"],
    })], "P1")

    assert graph.model_dump()["nodes"][0] == {
        "id": "start", "type": "startEvent", "name": "Start",
        "sourceSentences": ["S1"],
    }
    assert "kind" not in graph.model_dump()["nodes"][0]
    assert "trigger" not in graph.model_dump()["nodes"][0]


def test_delete_node_does_not_cascade():
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "a", "type": "task", "name": "A", "sourceSentences": ["S1"]},
        {"id": "b", "type": "task", "name": "B", "sourceSentences": ["S1"]},
    ]})], "P1")
    try:
        apply_edits(graph, [EditCall(name="delete_node", arguments={"targetId": "a"})], "P1")
        assert False
    except EditError:
        pass


def test_inclusive_gateway_is_serialized():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "choose_checks", "name": "选择检查", "type": "inclusiveGateway", "role": "split", "sourceSentences": ["S1"]})
    ], "P1")
    assert "inclusiveGateway" in graph_to_bpmn(graph)


@pytest.mark.parametrize(("role", "direction"), [
    ("split", "Diverging"), ("join", "Converging"),
])
def test_gateway_role_is_serialized_as_bpmn_gateway_direction(role, direction):
    graph = apply_edits(Graph(), [EditCall(name="add_gateway", arguments={
        "id": "gateway", "name": "Route", "type": "exclusiveGateway", "role": role,
    })], "P1")

    assert f'gatewayDirection="{direction}"' in graph_to_bpmn(graph)


def test_activity_default_flow_is_serialized_on_the_task():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "decide", "name": "Decide"}),
        EditCall(name="add_task", arguments={"id": "accept", "name": "Accept"}),
        EditCall(name="add_task", arguments={"id": "fallback", "name": "Fallback"}),
        EditCall(name="add_edge", arguments={
            "id": "f_accept", "source": "decide", "target": "accept",
            "condition": {"label": "accepted"},
        }),
        EditCall(name="add_edge", arguments={
            "id": "f_fallback", "source": "decide", "target": "fallback", "isDefault": True,
        }),
    ], "P1")

    assert '<bpmn:task id="decide" name="Decide" default="f_fallback"' in graph_to_bpmn(graph)


@pytest.mark.parametrize("node_type", [
    "task", "userTask", "serviceTask", "sendTask", "receiveTask",
    "businessRuleTask", "manualTask", "scriptTask",
])
def test_supported_task_types_are_serialized(node_type):
    graph = apply_edits(Graph(), [EditCall(name="add_task", arguments={
        "id": "work", "type": node_type, "name": "Work",
    })], "P1")

    assert f"<bpmn:{node_type} " in graph_to_bpmn(graph)


@pytest.mark.parametrize(("node_type", "definition"), [
    ("startEvent", "message"), ("startEvent", "timer"),
    ("startEvent", "signal"), ("startEvent", "conditional"),
    ("endEvent", "message"), ("endEvent", "signal"),
    ("endEvent", "error"), ("endEvent", "escalation"),
    ("endEvent", "terminate"), ("endEvent", "compensation"),
    ("intermediateCatchEvent", "message"), ("intermediateCatchEvent", "timer"),
    ("intermediateCatchEvent", "signal"), ("intermediateCatchEvent", "link"),
    ("intermediateCatchEvent", "conditional"),
    ("intermediateThrowEvent", "message"), ("intermediateThrowEvent", "signal"),
    ("intermediateThrowEvent", "link"), ("intermediateThrowEvent", "escalation"),
    ("intermediateThrowEvent", "compensation"),
])
def test_supported_event_definitions_are_serialized(node_type, definition):
    graph = apply_edits(Graph(), [EditCall(name="add_event", arguments={
        "id": "event", "type": node_type, "name": "Event",
        "eventDefinition": definition,
    })], "P1")
    xml = graph_to_bpmn(graph)

    assert f"<bpmn:{node_type} " in xml
    assert f"<bpmn:{definition}EventDefinition " in xml


def test_parse_json_object_accepts_markdown_fence_and_leading_text():
    assert parse_json_object('```json\n{"approved": true}\n```', "reviewer") == {"approved": True}
    assert parse_json_object('Result:\n{"segments": []}\nDone', "planner") == {"segments": []}


def test_final_validation_allows_split_without_join_when_branches_end():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "choice", "name": "Choose", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "end_a", "name": "End A", "type": "endEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "end_b", "name": "End B", "type": "endEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={
            "id": "f2", "source": "choice", "target": "end_a",
            "condition": {"label": "A"}, "sourceSentences": ["S1"],
        }),
        EditCall(name="add_edge", arguments={
            "id": "f3", "source": "choice", "target": "end_b",
            "condition": {"label": "B"}, "sourceSentences": ["S1"],
        }),
    ], "P1")

    assert validate_graph(graph, final=True) == []


def test_incremental_validation_allows_open_gateway_but_final_rejects_it():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "choice", "name": "Complete?", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "incomplete", "type": "task", "name": "Request more data", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "incomplete", "sourceSentences": ["S1"]}),
    ], "P1")

    assert not any(issue.code == "GATEWAY_DEGREE" for issue in validate_graph(graph))
    final_issue = next(issue for issue in validate_graph(graph, final=True)
                       if issue.code == "GATEWAY_DEGREE")
    assert "Split Gateway choice 只有 1 条出边" in final_issue.message
    assert "f2(choice->incomplete)" in final_issue.message
    assert "role=split 至少需要 2 条出边" in final_issue.message


def test_graph_patch_can_atomically_rewire_an_existing_edge_and_add_a_gateway():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={
            "id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"],
        }),
        EditCall(name="add_task", arguments={
            "id": "quality", "name": "Quality check", "sourceSentences": ["S1"],
        }),
        EditCall(name="add_task", arguments={
            "id": "package", "name": "Package", "sourceSentences": ["S2"],
        }),
        EditCall(name="add_edge", arguments={"id": "f_start_quality", "source": "start", "target": "quality"}),
        EditCall(name="add_edge", arguments={
            "id": "f_quality_package", "source": "quality", "target": "package",
            "sourceSentences": ["S2"],
        }),
    ], "P1")

    candidate = apply_graph_patch(previous, [
        {"id": "parallel_prepare", "name": "Prepare in parallel",
         "type": "parallelGateway", "role": "split", "sourceSentences": ["S2", "S3"]},
        {"id": "shipping_docs", "type": "task", "name": "Prepare shipping documents",
         "sourceSentences": ["S3"]},
    ], [
        {"id": "f_quality_package", "source": "quality", "target": "parallel_prepare",
         "sourceSentences": ["S2", "S3"]},
        {"id": "f_split_package", "source": "parallel_prepare", "target": "package",
         "sourceSentences": ["S2"]},
        {"id": "f_split_docs", "source": "parallel_prepare", "target": "shipping_docs",
         "sourceSentences": ["S3"]},
    ], "P2", remove_edge_ids=["f_quality_package"])

    rewired = next(edge for edge in candidate.edges if edge.id == "f_quality_package")
    assert rewired.target == "parallel_prepare"
    assert rewired.sourceSentences == ["S2", "S3"]
    assert candidate.node_map()["parallel_prepare"].sourceSentences == ["S2", "S3"]
    assert {edge.id for edge in candidate.edges}.issuperset({
        "f_quality_package", "f_split_package", "f_split_docs",
    })


def test_graph_patch_node_removal_also_removes_its_incident_edges():
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent"},
        {"id": "obsolete", "type": "task", "name": "Obsolete"},
    ]})], "P1")

    candidate = apply_graph_patch(
        previous, add_nodes=[], add_edges=[], segment_id="P2", remove_node_ids=["obsolete"]
    )

    assert {node.id for node in candidate.nodes} == {"start"}
    assert candidate.edges == []


def test_graph_patch_treats_remove_and_upsert_of_the_same_edge_as_replacement():
    previous = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_task", arguments={"id": "c", "name": "C"}),
        EditCall(name="add_edge", arguments={"id": "f", "source": "a", "target": "b"}),
    ], "P1")

    candidate = apply_graph_patch(
        previous,
        add_nodes=[],
        add_edges=[{"id": "f", "source": "a", "target": "c", "sourceSentences": ["S2"]}],
        segment_id="P2",
        remove_edge_ids=["f"],
    )

    assert [(edge.id, edge.source, edge.target) for edge in candidate.edges] == [("f", "a", "c")]


def test_graph_patch_deletes_node_before_reinserting_the_same_id():
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent"},
        {"id": "work", "type": "task", "name": "Old work"},
    ]})], "P1")

    candidate = apply_graph_patch(
        previous,
        add_nodes=[{"id": "work", "type": "task", "name": "New work"}],
        add_edges=[],
        segment_id="P2",
        remove_node_ids=["work"],
    )

    assert candidate.node_map()["work"].name == "New work"
    assert [(edge.source, edge.target) for edge in candidate.edges] == [("start", "work")]


def test_graph_patch_rejects_implicit_update_without_matching_removal():
    previous = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "work", "name": "Old work"}),
    ], "P1")

    with pytest.raises(ValueError, match="without explicit removal"):
        apply_graph_patch(
            previous,
            add_nodes=[{"id": "work", "type": "task", "name": "New work"}],
            add_edges=[],
            segment_id="P2",
        )


def test_generated_batch_warns_about_a_new_single_branch_gateway():
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "review", "type": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S2"]},
        {"id": "approved", "type": "task", "name": "Approve", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_review_choice", "source": "review", "target": "choice", "sourceSentences": ["S2"]},
        {"id": "f_choice_approved", "source": "choice", "target": "approved", "condition": {"label": "approved"}, "sourceSentences": ["S2"]},
    ], "P2")

    issues = validate_incremental_batch(previous, candidate)

    assert any(issue.code == "BATCH_GATEWAY_DEGREE" and issue.elementIds == ["choice"]
               and issue.severity == "warning"
               for issue in issues)


def test_generated_batch_accepts_a_complete_split_with_open_branches():
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]},
        {"id": "review", "type": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S2"]},
        {"id": "approved", "type": "task", "name": "Approve", "sourceSentences": ["S2"]},
        {"id": "rejected", "type": "task", "name": "Reject", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_review_choice", "source": "review", "target": "choice", "sourceSentences": ["S2"]},
        {"id": "f_choice_approved", "source": "choice", "target": "approved", "condition": {"label": "approved"}, "sourceSentences": ["S2"]},
        {"id": "f_choice_rejected", "source": "choice", "target": "rejected", "condition": {"label": "not approved"}, "sourceSentences": ["S2"]},
    ], "P2")

    assert validate_incremental_batch(previous, candidate) == []
    assert validate_graph(candidate) == []


def test_generated_batch_can_continue_from_an_existing_join():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "type": "parallelGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "task_a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "task_b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "type": "parallelGateway", "role": "join", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "task_a"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork", "target": "task_b"}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "task_a", "target": "join"}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "task_b", "target": "join"}),
    ], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "archive", "type": "task", "name": "Archive", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_join_archive", "source": "join", "target": "archive", "sourceSentences": ["S2"]},
    ], "P2")

    assert validate_incremental_batch(previous, candidate) == []
    assert validate_graph(candidate) == []


def test_generated_batch_can_continue_unjoined_existing_branches_separately():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "approved", "name": "Record approval", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "rejected", "name": "Record rejection", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "approved", "condition": {"label": "approved"}}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "choice", "target": "rejected", "condition": {"label": "not approved"}}),
    ], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "deploy", "type": "task", "name": "Deploy", "sourceSentences": ["S2"]},
        {"id": "archive", "type": "task", "name": "Archive", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_approved_deploy", "source": "approved", "target": "deploy", "sourceSentences": ["S2"]},
        {"id": "f_rejected_archive", "source": "rejected", "target": "archive", "sourceSentences": ["S2"]},
    ], "P2")

    assert validate_incremental_batch(previous, candidate) == []
    assert validate_graph(candidate) == []


def test_generated_batch_rejects_an_unconnected_new_node():
    previous = apply_edits(Graph(), [EditCall(name="add_event", arguments={
        "id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"],
    })], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "orphan", "type": "task", "name": "Orphan", "sourceSentences": ["S2"]},
    ], [], "P2")

    codes = {issue.code for issue in validate_incremental_batch(previous, candidate)}

    assert "BATCH_UNCONNECTED_NODE" in codes


def test_generated_batch_warns_when_continuing_from_an_incomplete_existing_join():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "work", "name": "Work", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "type": "exclusiveGateway", "role": "join", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "work"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "work", "target": "join"}),
    ], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "next_task", "type": "task", "name": "Continue", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_join_next", "source": "join", "target": "next_task", "sourceSentences": ["S2"]},
    ], "P2")

    issues = validate_incremental_batch(previous, candidate)

    assert any(issue.code == "BATCH_GATEWAY_DEGREE" and issue.elementIds == ["join"]
               and issue.severity == "warning"
               for issue in issues)


def test_directional_gateway_roles_reject_mixed_degrees_and_recommend_join_split_pair():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_gateway", arguments={"id": "fork_a", "name": "Fork A", "type": "parallelGateway", "role": "split"}),
        EditCall(name="add_gateway", arguments={"id": "fork_b", "name": "Fork B", "type": "parallelGateway", "role": "split"}),
        EditCall(name="add_gateway", arguments={"id": "bad_split", "name": "Bad split", "type": "parallelGateway", "role": "split"}),
        EditCall(name="add_gateway", arguments={"id": "bad_join", "name": "Bad join", "type": "parallelGateway", "role": "join"}),
        EditCall(name="add_task", arguments={"id": "task_a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "task_b", "name": "B"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork_a"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork_a", "target": "fork_b"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork_a", "target": "bad_split"}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "fork_b", "target": "bad_split"}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "bad_split", "target": "bad_join"}),
        EditCall(name="add_edge", arguments={"id": "f6", "source": "bad_split", "target": "task_a"}),
        EditCall(name="add_edge", arguments={"id": "f7", "source": "bad_join", "target": "task_a"}),
        EditCall(name="add_edge", arguments={"id": "f8", "source": "bad_join", "target": "task_b"}),
    ], "P1")

    issues = validate_graph(graph)
    codes = {issue.code for issue in issues}

    assert "SPLIT_MULTIPLE_INCOMING" in codes
    assert "JOIN_MULTIPLE_OUTGOING" in codes
    join_issue = next(issue for issue in issues if issue.code == "JOIN_MULTIPLE_OUTGOING")
    assert "f7(bad_join->task_a)" in join_issue.message
    assert "f8(bad_join->task_b)" in join_issue.message
    assert "同的 role=split Gateway" in join_issue.message


def test_same_type_join_then_split_accepts_multiple_incoming_and_outgoing_flows():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_gateway", arguments={
            "id": "route_join", "name": "Route join", "type": "exclusiveGateway", "role": "join",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "route_split", "name": "Route split", "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_task", arguments={"id": "c", "name": "C"}),
        EditCall(name="add_task", arguments={"id": "d", "name": "D"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "a", "target": "route_join"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "b", "target": "route_join"}),
        EditCall(name="add_edge", arguments={"id": "f_join_split", "source": "route_join", "target": "route_split"}),
        EditCall(name="add_edge", arguments={
            "id": "f3", "source": "route_split", "target": "c", "condition": {"label": "C"},
        }),
        EditCall(name="add_edge", arguments={
            "id": "f4", "source": "route_split", "target": "d", "condition": {"label": "D"},
        }),
    ], "P1")

    codes = {issue.code for issue in validate_graph(graph)}

    assert "SPLIT_MULTIPLE_INCOMING" not in codes
    assert "JOIN_MULTIPLE_OUTGOING" not in codes
    xml = graph_to_bpmn(graph)
    assert 'gatewayDirection="Converging"' in xml
    assert 'gatewayDirection="Diverging"' in xml


def test_task_rejects_multiple_incoming_and_outgoing_flows():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_task", arguments={"id": "work", "name": "Work"}),
        EditCall(name="add_task", arguments={"id": "c", "name": "C"}),
        EditCall(name="add_task", arguments={"id": "d", "name": "D"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "a", "target": "work"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "b", "target": "work"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "work", "target": "c"}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "work", "target": "d"}),
    ], "P1")

    issues = validate_graph(graph)
    codes = {issue.code for issue in issues}

    assert "TASK_MULTIPLE_INCOMING" in codes
    assert "TASK_MULTIPLE_OUTGOING" in codes
    assert "role=join" in next(
        issue.message for issue in issues if issue.code == "TASK_MULTIPLE_INCOMING"
    )
    assert "role=split" in next(
        issue.message for issue in issues if issue.code == "TASK_MULTIPLE_OUTGOING"
    )


def test_loop_return_to_task_reports_atomic_join_rewrite():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_task", arguments={"id": "review", "name": "Review"}),
        EditCall(name="add_task", arguments={"id": "revise", "name": "Revise"}),
        EditCall(name="add_edge", arguments={"id": "flow_start_review", "source": "start", "target": "review"}),
        EditCall(name="add_edge", arguments={"id": "flow_review_revise", "source": "review", "target": "revise"}),
        EditCall(name="add_edge", arguments={"id": "flow_revise_review", "source": "revise", "target": "review"}),
    ], "P1")

    issue = next(
        item for item in validate_graph(graph) if item.code == "TASK_MULTIPLE_INCOMING"
    )

    assert "flow_start_review(start->review)" in issue.message
    assert "flow_revise_review(revise->review)" in issue.message
    assert "循环回边" in issue.message
    assert "Join -> Task" in issue.message


def test_only_join_gateway_and_end_event_may_have_multiple_incoming_flows():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_event", arguments={
            "id": "wait", "name": "Wait", "type": "intermediateCatchEvent",
        }),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "type": "endEvent"}),
        EditCall(name="add_edge", arguments={"id": "flow_a_wait", "source": "a", "target": "wait"}),
        EditCall(name="add_edge", arguments={"id": "flow_b_wait", "source": "b", "target": "wait"}),
        EditCall(name="add_edge", arguments={"id": "flow_wait_end", "source": "wait", "target": "end"}),
        EditCall(name="add_edge", arguments={"id": "flow_a_end", "source": "a", "target": "end"}),
    ], "P1")

    issues = validate_graph(graph)

    assert "INTERMEDIATE_EVENT_MULTIPLE_INCOMING" in {item.code for item in issues}
    assert not any(item.code == "END_MULTIPLE_INCOMING" for item in issues)


def test_final_task_requires_one_incoming_and_one_outgoing_flow():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "work", "name": "Work"}),
    ], "P1")

    codes = {issue.code for issue in validate_graph(graph, final=True)}

    assert "TASK_MISSING_INCOMING" in codes
    assert "TASK_MISSING_OUTGOING" in codes


def test_graph_model_exposes_only_split_and_join_gateway_roles():
    with pytest.raises(ValueError):
        Graph.model_validate({
            "nodes": [{
                "id": "gateway", "type": "exclusiveGateway", "name": "Route",
                "role": "mixed",
            }],
        })


def test_open_gateway_context_exposes_gateway_type_and_role():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Checks complete", "type": "parallelGateway", "role": "join"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "join"}),
    ], "P1")

    assert open_nodes(graph) == [{
        "id": "join", "name": "Checks complete",
        "sourceSentences": [],
        "type": "parallelGateway", "role": "join",
    }]


def test_gateway_refs_include_every_gateway_without_a_provisional_status():
    graph = apply_edits(Graph(), [
        EditCall(name="add_gateway", arguments={
            "id": "decision", "name": "Payment result",
            "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "merge", "name": "Checks complete",
            "type": "parallelGateway", "role": "join",
        }),
    ], "P1")

    assert gateway_refs(graph) == [
        {"id": "decision", "type": "exclusiveGateway", "role": "split",
         "question": "Payment result"},
        {"id": "merge", "type": "parallelGateway", "role": "join",
         "question": "Checks complete"},
    ]
    assert all("status" not in item for item in gateway_refs(graph))


def test_incremental_validation_rejects_nodes_unreachable_from_start():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "connected", "name": "Connected", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "orphan", "name": "Orphan", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "connected"}),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "UNREACHABLE" and issue.elementIds == ["orphan"]
               and issue.sourceSentences == ["S2"] for issue in issues)


def test_task_parallel_split_requires_explicit_gateway():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "dispatch", "name": "Dispatch", "sourceSentences": ["S2"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S2"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "dispatch"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "dispatch", "target": "a"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "dispatch", "target": "b"}),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "TASK_MULTIPLE_OUTGOING" for issue in issues)


def test_task_merge_requires_explicit_join_gateway():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "type": "parallelGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "continue", "name": "Continue", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "a"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork", "target": "b"}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "a", "target": "continue"}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "b", "target": "continue"}),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "TASK_MULTIPLE_INCOMING" for issue in issues)


def test_start_can_split_in_parallel_and_end_can_accept_multiple_incoming_flows():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "type": "endEvent"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "a"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "start", "target": "b"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "a", "target": "end"}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "b", "target": "end"}),
    ], "P1")

    codes = {issue.code for issue in validate_graph(graph, final=True)}

    assert "IMPLICIT_SPLIT" not in codes
    assert "IMPLICIT_MERGE" not in codes


def test_start_multiple_outgoing_flows_cannot_be_conditional():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_edge", arguments={
            "id": "f1", "source": "start", "target": "a", "condition": {"label": "A"},
        }),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "start", "target": "b"}),
    ], "P1")

    assert any(issue.code == "START_OUTGOING_CONDITION" for issue in validate_graph(graph))


def test_split_validation_rejects_conditional_default_and_parallel_default():
    graph = apply_edits(Graph(), [
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "type": "parallelGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={
            "id": "default_with_condition", "source": "fork", "target": "a",
            "condition": {"label": "fallback"}, "isDefault": True, "sourceSentences": ["S1"],
        }),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "b", "sourceSentences": ["S1"]}),
    ], "P1")

    codes = {issue.code for issue in validate_graph(graph)}

    assert {"DEFAULT_HAS_CONDITION", "PARALLEL_CONDITION", "PARALLEL_DEFAULT"}.issubset(codes)


def test_exclusive_split_rejects_unconditional_non_default_flow_once_branches_exist():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_task", arguments={"id": "approved", "name": "Approve"}),
        EditCall(name="add_task", arguments={"id": "rejected", "name": "Reject"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={
            "id": "f_approved", "source": "choice", "target": "approved",
            "condition": {"label": "approved"},
        }),
        EditCall(name="add_edge", arguments={
            "id": "f_rejected", "source": "choice", "target": "rejected",
        }),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "EXCLUSIVE_UNCONDITIONAL_FLOW"
               and issue.elementIds == ["f_rejected"] for issue in issues)


def test_exclusive_split_accepts_one_condition_and_one_explicit_default():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Result", "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_task", arguments={"id": "approved", "name": "Approve"}),
        EditCall(name="add_task", arguments={"id": "fallback", "name": "Fallback"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={
            "id": "f_approved", "source": "choice", "target": "approved",
            "condition": {"label": "approved"},
        }),
        EditCall(name="add_edge", arguments={
            "id": "f_fallback", "source": "choice", "target": "fallback", "isDefault": True,
        }),
    ], "P1")

    assert not any(issue.code == "EXCLUSIVE_UNCONDITIONAL_FLOW"
                   for issue in validate_graph(graph))


def test_final_validation_reports_every_reachable_task_without_an_outgoing_flow():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "choice", "name": "Choose", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "end_ok", "name": "Done", "type": "endEvent", "sourceSentences": ["S2"]}),
        EditCall(name="add_node", arguments={"id": "notify", "type": "task", "name": "Request more data", "sourceSentences": ["S3"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "end_ok"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "choice", "target": "notify"}),
    ], "P1")

    issues = validate_graph(graph, final=True)
    leaf_issues = [issue for issue in issues if issue.code == "TASK_MISSING_OUTGOING"]

    assert len(leaf_issues) == 1
    assert leaf_issues[0].elementIds == ["notify"]
    assert leaf_issues[0].sourceSentences == ["S3"]
    assert "End Event" in leaf_issues[0].message


def test_final_leaf_rule_ignores_unreachable_leaf_nodes():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "name": "Start", "type": "startEvent"}),
        EditCall(name="add_node", arguments={"id": "end", "name": "End", "type": "endEvent"}),
        EditCall(name="add_node", arguments={"id": "orphan", "type": "task", "name": "Orphan"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "end"}),
    ], "P1")

    issues = validate_graph(graph, final=True)

    assert any(issue.code == "UNREACHABLE" and "orphan" in issue.elementIds for issue in issues)
    assert not any(issue.code == "OPEN_NODE" and "orphan" in issue.elementIds for issue in issues)


def test_model_tools_expose_type_specific_node_functions_only():
    tools = {tool["function"]["name"]: tool["function"] for tool in EDIT_TOOLS}

    assert {"add_task", "add_event", "add_gateway"}.issubset(tools)
    assert "add_node" not in tools
    assert "eventType" not in tools["add_task"]["parameters"]["properties"]
    assert "gatewayType" not in tools["add_event"]["parameters"]["properties"]
    assert "eventType" not in tools["add_gateway"]["parameters"]["properties"]
    assert tools["add_gateway"]["parameters"]["properties"]["role"]["enum"] == [
        "split", "join",
    ]


def test_update_edge_tool_exposes_minimal_edit_fields_and_can_clear_condition():
    tools = {tool["function"]["name"]: tool["function"] for tool in EDIT_TOOLS}
    changes = tools["update_edge"]["parameters"]["properties"]["changes"]

    assert set(changes["properties"]) == {
        "source", "target", "condition", "isDefault", "sourceSentences",
    }
    assert {variant.get("type") for variant in changes["properties"]["condition"]["anyOf"]} == {
        "object", "null",
    }

    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={
            "id": "conditional", "source": "a", "target": "b",
            "condition": {"label": "if needed"}, "sourceSentences": ["S1"],
        }),
    ], "P1")
    updated = apply_edits(graph, [EditCall(name="update_edge", arguments={
        "targetId": "conditional", "changes": {"condition": None},
    })], "P1")

    assert updated.edges[0].condition is None


def test_node_variants_reject_fields_from_other_node_types():
    with pytest.raises(EditError, match="add_task only accepts task types"):
        apply_edits(Graph(), [EditCall(name="add_task", arguments={
            "id": "bad_task", "name": "Bad", "type": "endEvent", "sourceSentences": ["S1"],
        })], "P1")


def test_linear_sequence_rejects_gateway_nodes():
    with pytest.raises(EditError, match="only accepts task and event"):
        apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [{
            "id": "bad_gateway", "name": "Bad",
            "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S1"],
        }]})], "P1")


def test_loop_is_modeled_by_an_edge_back_to_an_existing_node():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "enter_review", "name": "Enter review", "type": "exclusiveGateway", "role": "join", "sourceSentences": ["S1", "S2"]}),
        EditCall(name="add_task", arguments={"id": "review", "name": "Review", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "repeat", "name": "Repeat?", "type": "exclusiveGateway", "role": "split", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "type": "endEvent", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "enter_review", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "enter_review", "target": "review", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "review", "target": "repeat", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "repeat", "target": "enter_review", "condition": {"label": "repeat"}, "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "repeat", "target": "end", "condition": {"label": "done"}, "sourceSentences": ["S2"]}),
    ], "P1")

    assert any(edge.source == "repeat" and edge.target == "enter_review" for edge in graph.edges)
    assert validate_graph(graph, final=True) == []


def test_parallel_split_requires_a_common_parallel_join_only_at_final_validation():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "type": "parallelGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_event", arguments={"id": "end_a", "name": "End A", "type": "endEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_event", arguments={"id": "end_b", "name": "End B", "type": "endEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "a", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork", "target": "b", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "a", "target": "end_a", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "b", "target": "end_b", "sourceSentences": ["S1"]}),
    ], "P1")

    assert not any(issue.code == "PARALLEL_JOIN_MISSING" for issue in validate_graph(graph))
    final_issues = validate_graph(graph, final=True)
    assert any(issue.code == "PARALLEL_JOIN_MISSING" and issue.elementIds == ["fork"]
               for issue in final_issues)


def test_parallel_split_accepts_a_join_reachable_from_every_branch():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "type": "parallelGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "type": "parallelGateway", "role": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "type": "endEvent", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "a", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork", "target": "b", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "a", "target": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "b", "target": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f6", "source": "join", "target": "end", "sourceSentences": ["S2"]}),
    ], "P1")

    assert not any(issue.code == "PARALLEL_JOIN_MISSING" for issue in validate_graph(graph, final=True))


def test_parallel_join_must_be_reachable_from_every_direct_branch():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "type": "startEvent", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "type": "parallelGateway", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "type": "parallelGateway", "role": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end_a", "name": "End A", "type": "endEvent", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end_b", "name": "End B", "type": "endEvent", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "a", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork", "target": "b", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "a", "target": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "join", "target": "end_a", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f6", "source": "b", "target": "end_b", "sourceSentences": ["S2"]}),
    ], "P1")

    issues = validate_graph(graph, final=True)

    assert any(issue.code == "PARALLEL_JOIN_MISSING" and issue.elementIds == ["fork"]
               for issue in issues)


def test_inclusive_split_requires_conditions_or_an_explicit_default():
    graph = apply_edits(Graph(), [
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Choose applicable paths",
            "type": "inclusiveGateway", "role": "split",
        }),
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_edge", arguments={
            "id": "f_a", "source": "choice", "target": "a",
            "condition": {"label": "A applies"},
        }),
        EditCall(name="add_edge", arguments={
            "id": "f_b", "source": "choice", "target": "b",
        }),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "INCLUSIVE_UNCONDITIONAL_FLOW"
               and issue.elementIds == ["f_b"] for issue in issues)


def test_final_validation_rejects_a_reachable_cycle_with_no_path_to_an_end():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={
            "id": "start", "name": "Start", "type": "startEvent",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Continue?", "type": "exclusiveGateway", "role": "split",
        }),
        EditCall(name="add_event", arguments={
            "id": "end", "name": "End", "type": "endEvent",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "loop_join", "name": "Repeat", "type": "exclusiveGateway", "role": "join",
        }),
        EditCall(name="add_task", arguments={"id": "a", "name": "A"}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={
            "id": "f2", "source": "choice", "target": "end", "condition": {"label": "done"},
        }),
        EditCall(name="add_edge", arguments={
            "id": "f3", "source": "choice", "target": "loop_join", "condition": {"label": "repeat"},
        }),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "loop_join", "target": "a"}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "a", "target": "b"}),
        EditCall(name="add_edge", arguments={"id": "f6", "source": "b", "target": "loop_join"}),
    ], "P1")

    issues = validate_graph(graph, final=True)

    no_end = next(issue for issue in issues if issue.code == "NO_END_PATH")
    assert set(no_end.elementIds) == {"loop_join", "a", "b"}


def test_join_multiple_outgoing_reports_one_clear_rule_and_action():
    graph = apply_edits(Graph(), [
        EditCall(name="add_task", arguments={
            "id": "quality", "name": "Quality", "sourceSentences": ["S9"],
        }),
        EditCall(name="add_task", arguments={
            "id": "package", "name": "Package", "sourceSentences": ["S10"],
        }),
        EditCall(name="add_gateway", arguments={
            "id": "package_join", "name": "Package done", "type": "exclusiveGateway",
            "role": "join", "sourceSentences": ["S10"],
        }),
        EditCall(name="add_task", arguments={
            "id": "docs", "name": "Prepare documents", "sourceSentences": ["S11"],
        }),
        EditCall(name="add_gateway", arguments={
            "id": "ready_join", "name": "Both ready", "type": "parallelGateway",
            "role": "join", "sourceSentences": ["S13"],
        }),
        EditCall(name="add_edge", arguments={"id": "f_quality_package", "source": "quality", "target": "package"}),
        EditCall(name="add_edge", arguments={"id": "f_package_join", "source": "package", "target": "package_join"}),
        EditCall(name="add_edge", arguments={"id": "f_join_docs", "source": "package_join", "target": "docs"}),
        EditCall(name="add_edge", arguments={"id": "f_join_ready", "source": "package_join", "target": "ready_join"}),
        EditCall(name="add_edge", arguments={"id": "f_docs_ready", "source": "docs", "target": "ready_join"}),
    ], "P1")

    issue = next(item for item in validate_graph(graph)
                 if item.code == "JOIN_MULTIPLE_OUTGOING")

    assert "f_join_docs(package_join->docs)" in issue.message
    assert "f_join_ready(package_join->ready_join)" in issue.message
    assert "role=join" in issue.message
    assert "同的 role=split Gateway" in issue.message


def _structured_gateway_pair(split_type: str, join_type: str) -> Graph:
    conditional = split_type != "parallelGateway"
    branch_a = {
        "id": "flow_split_a", "source": "gateway_split", "target": "task_a",
        **({"condition": {"label": "A"}} if conditional else {}),
    }
    branch_b = {
        "id": "flow_split_b", "source": "gateway_split", "target": "task_b",
        **({"condition": {"label": "B"}} if conditional else {}),
    }
    return Graph.model_validate({
        "nodes": [
            {"id": "start_main", "type": "startEvent", "name": "Start"},
            {"id": "gateway_split", "type": split_type, "name": "Split", "role": "split"},
            {"id": "task_a", "type": "task", "name": "A"},
            {"id": "task_b", "type": "task", "name": "B"},
            {"id": "gateway_join", "type": join_type, "name": "Join", "role": "join"},
            {"id": "end_main", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "flow_start_split", "source": "start_main", "target": "gateway_split"},
            branch_a,
            branch_b,
            {"id": "flow_a_join", "source": "task_a", "target": "gateway_join"},
            {"id": "flow_b_join", "source": "task_b", "target": "gateway_join"},
            {"id": "flow_join_end", "source": "gateway_join", "target": "end_main"},
        ],
    })


@pytest.mark.parametrize("gateway_type", [
    "exclusiveGateway", "inclusiveGateway", "parallelGateway",
])
def test_gateway_pairing_accepts_matching_split_and_join_types(gateway_type):
    issues = validate_graph(_structured_gateway_pair(gateway_type, gateway_type), final=True)

    assert not any(issue.code in {
        "GATEWAY_JOIN_TYPE_MISMATCH", "GATEWAY_PAIR_UNRESOLVED", "PARALLEL_JOIN_MISSING",
    } for issue in issues)


@pytest.mark.parametrize(("split_type", "join_type"), [
    ("exclusiveGateway", "parallelGateway"),
    ("inclusiveGateway", "parallelGateway"),
    ("inclusiveGateway", "exclusiveGateway"),
    ("parallelGateway", "exclusiveGateway"),
    ("parallelGateway", "inclusiveGateway"),
    ("exclusiveGateway", "inclusiveGateway"),
])
def test_gateway_pairing_rejects_mismatched_split_and_join_types(split_type, join_type):
    issues = validate_graph(_structured_gateway_pair(split_type, join_type), final=True)

    mismatch = next(issue for issue in issues if issue.code == "GATEWAY_JOIN_TYPE_MISMATCH")
    assert mismatch.elementIds[:2] == ["gateway_split", "gateway_join"]
    assert split_type.removesuffix("Gateway") in mismatch.message
    assert join_type.removesuffix("Gateway") in mismatch.message


def _loop_entry_graph(join_type: str) -> Graph:
    return Graph.model_validate({
        "nodes": [
            {"id": "start_loop", "type": "startEvent", "name": "Start"},
            {"id": "loop_entry", "type": join_type, "name": "Loop entry", "role": "join"},
            {"id": "task_work", "type": "task", "name": "Work"},
            {"id": "loop_choice", "type": "exclusiveGateway", "name": "Repeat?", "role": "split"},
            {"id": "end_loop", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "flow_initial_entry", "source": "start_loop", "target": "loop_entry"},
            {"id": "flow_entry_work", "source": "loop_entry", "target": "task_work"},
            {"id": "flow_work_choice", "source": "task_work", "target": "loop_choice"},
            {"id": "flow_repeat", "source": "loop_choice", "target": "loop_entry",
             "condition": {"label": "repeat"}},
            {"id": "flow_done", "source": "loop_choice", "target": "end_loop",
             "condition": {"label": "done"}},
        ],
    })


def test_gateway_pairing_treats_a_loop_return_as_an_exclusive_entry_merge():
    issues = validate_graph(_loop_entry_graph("exclusiveGateway"), final=True)

    assert not any(issue.code in {
        "LOOP_ENTRY_JOIN_TYPE_MISMATCH", "GATEWAY_PAIR_UNRESOLVED",
    } for issue in issues)


@pytest.mark.parametrize("join_type", ["parallelGateway", "inclusiveGateway"])
def test_gateway_pairing_rejects_a_synchronizing_loop_entry_join(join_type):
    issues = validate_graph(_loop_entry_graph(join_type), final=True)

    mismatch = next(issue for issue in issues if issue.code == "LOOP_ENTRY_JOIN_TYPE_MISMATCH")
    assert mismatch.elementIds == ["loop_entry", "flow_repeat"]


def test_gateway_pairing_selects_the_nearest_split_in_nested_control_blocks():
    graph = Graph.model_validate({
        "nodes": [
            {"id": "start_nested", "type": "startEvent", "name": "Start"},
            {"id": "outer_split", "type": "parallelGateway", "name": "Outer", "role": "split"},
            {"id": "inner_split", "type": "exclusiveGateway", "name": "Inner", "role": "split"},
            {"id": "inner_a", "type": "task", "name": "Inner A"},
            {"id": "inner_b", "type": "task", "name": "Inner B"},
            {"id": "inner_join", "type": "exclusiveGateway", "name": "Inner join", "role": "join"},
            {"id": "left_done", "type": "task", "name": "Left done"},
            {"id": "right_task", "type": "task", "name": "Right"},
            {"id": "outer_join", "type": "parallelGateway", "name": "Outer join", "role": "join"},
            {"id": "end_nested", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "flow_start_outer", "source": "start_nested", "target": "outer_split"},
            {"id": "flow_outer_inner", "source": "outer_split", "target": "inner_split"},
            {"id": "flow_outer_right", "source": "outer_split", "target": "right_task"},
            {"id": "flow_inner_a", "source": "inner_split", "target": "inner_a",
             "condition": {"label": "A"}},
            {"id": "flow_inner_b", "source": "inner_split", "target": "inner_b",
             "condition": {"label": "B"}},
            {"id": "flow_a_inner_join", "source": "inner_a", "target": "inner_join"},
            {"id": "flow_b_inner_join", "source": "inner_b", "target": "inner_join"},
            {"id": "flow_inner_left", "source": "inner_join", "target": "left_done"},
            {"id": "flow_left_outer_join", "source": "left_done", "target": "outer_join"},
            {"id": "flow_right_outer_join", "source": "right_task", "target": "outer_join"},
            {"id": "flow_outer_end", "source": "outer_join", "target": "end_nested"},
        ],
    })

    issues = validate_graph(graph, final=True)

    assert not any(issue.code in {
        "GATEWAY_JOIN_TYPE_MISMATCH", "GATEWAY_PAIR_UNRESOLVED", "PARALLEL_JOIN_MISSING",
    } for issue in issues)


def test_gateway_pairing_does_not_use_an_unrelated_downstream_parallel_join():
    graph = Graph.model_validate({
        "nodes": [
            {"id": "start_chain", "type": "startEvent", "name": "Start"},
            {"id": "first_split", "type": "parallelGateway", "name": "First", "role": "split"},
            {"id": "first_a", "type": "task", "name": "First A"},
            {"id": "first_b", "type": "task", "name": "First B"},
            {"id": "wrong_join", "type": "exclusiveGateway", "name": "Wrong", "role": "join"},
            {"id": "second_split", "type": "parallelGateway", "name": "Second", "role": "split"},
            {"id": "second_a", "type": "task", "name": "Second A"},
            {"id": "second_b", "type": "task", "name": "Second B"},
            {"id": "second_join", "type": "parallelGateway", "name": "Second join", "role": "join"},
            {"id": "end_chain", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "flow_start_first", "source": "start_chain", "target": "first_split"},
            {"id": "flow_first_a", "source": "first_split", "target": "first_a"},
            {"id": "flow_first_b", "source": "first_split", "target": "first_b"},
            {"id": "flow_a_wrong", "source": "first_a", "target": "wrong_join"},
            {"id": "flow_b_wrong", "source": "first_b", "target": "wrong_join"},
            {"id": "flow_wrong_second", "source": "wrong_join", "target": "second_split"},
            {"id": "flow_second_a", "source": "second_split", "target": "second_a"},
            {"id": "flow_second_b", "source": "second_split", "target": "second_b"},
            {"id": "flow_a_second_join", "source": "second_a", "target": "second_join"},
            {"id": "flow_b_second_join", "source": "second_b", "target": "second_join"},
            {"id": "flow_second_end", "source": "second_join", "target": "end_chain"},
        ],
    })

    mismatches = [
        issue for issue in validate_graph(graph, final=True)
        if issue.code == "GATEWAY_JOIN_TYPE_MISMATCH"
    ]

    assert len(mismatches) == 1
    assert mismatches[0].elementIds[:2] == ["first_split", "wrong_join"]


def test_gateway_pairing_rejects_a_final_join_without_a_common_split():
    graph = Graph.model_validate({
        "nodes": [
            {"id": "start_uncontrolled", "type": "startEvent", "name": "Start"},
            {"id": "task_left", "type": "task", "name": "Left"},
            {"id": "task_right", "type": "task", "name": "Right"},
            {"id": "join_uncontrolled", "type": "parallelGateway", "name": "Join", "role": "join"},
            {"id": "end_uncontrolled", "type": "endEvent", "name": "End"},
        ],
        "edges": [
            {"id": "flow_start_left", "source": "start_uncontrolled", "target": "task_left"},
            {"id": "flow_start_right", "source": "start_uncontrolled", "target": "task_right"},
            {"id": "flow_left_join", "source": "task_left", "target": "join_uncontrolled"},
            {"id": "flow_right_join", "source": "task_right", "target": "join_uncontrolled"},
            {"id": "flow_join_end", "source": "join_uncontrolled", "target": "end_uncontrolled"},
        ],
    })

    assert any(issue.code == "GATEWAY_PAIR_UNRESOLVED"
               for issue in validate_graph(graph, final=True))
    assert not any(issue.code == "GATEWAY_PAIR_UNRESOLVED"
                   for issue in validate_graph(graph, final=False))

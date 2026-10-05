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
        {"id": "start", "kind": "event", "name": "开始", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]},
        {"id": "review", "kind": "task", "name": "审核申请", "sourceSentences": ["S1"]},
        {"id": "end", "kind": "event", "name": "结束", "eventType": "end", "trigger": "none", "sourceSentences": ["S2"]},
    ]})]
    graph = apply_edits(Graph(), calls, "P1")
    assert [edge.id for edge in graph.edges] == ["flow_start_to_review", "flow_review_to_end"]
    assert validate_graph(graph, final=True) == []
    assert "inclusiveGateway" not in graph_to_bpmn(graph)


def test_plain_event_defaults_to_none_trigger():
    graph = apply_edits(Graph(), [EditCall(name="add_node", arguments={
        "id": "start", "kind": "event", "name": "Start",
        "eventType": "start", "sourceSentences": ["S1"],
    })], "P1")

    assert graph.nodes[0].eventType == "start"
    assert graph.nodes[0].trigger == "none"


def test_delete_node_does_not_cascade():
    graph = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "a", "kind": "task", "name": "A", "sourceSentences": ["S1"]},
        {"id": "b", "kind": "task", "name": "B", "sourceSentences": ["S1"]},
    ]})], "P1")
    try:
        apply_edits(graph, [EditCall(name="delete_node", arguments={"targetId": "a"})], "P1")
        assert False
    except EditError:
        pass


def test_inclusive_gateway_is_serialized():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "choose_checks", "kind": "gateway", "name": "选择检查", "gatewayType": "inclusive", "role": "split", "sourceSentences": ["S1"]})
    ], "P1")
    assert "inclusiveGateway" in graph_to_bpmn(graph)


def test_parse_json_object_accepts_markdown_fence_and_leading_text():
    assert parse_json_object('```json\n{"approved": true}\n```', "reviewer") == {"approved": True}
    assert parse_json_object('Result:\n{"segments": []}\nDone', "planner") == {"segments": []}


def test_final_validation_allows_split_without_join_when_branches_end():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "kind": "event", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "choice", "kind": "gateway", "name": "Choose", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "end_a", "kind": "event", "name": "End A", "eventType": "end", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "end_b", "kind": "event", "name": "End B", "eventType": "end", "trigger": "none", "sourceSentences": ["S1"]}),
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
        EditCall(name="add_node", arguments={"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "choice", "kind": "gateway", "name": "Complete?", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "incomplete", "kind": "task", "name": "Request more data", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "incomplete", "sourceSentences": ["S1"]}),
    ], "P1")

    assert not any(issue.code == "GATEWAY_DEGREE" for issue in validate_graph(graph))
    final_issue = next(issue for issue in validate_graph(graph, final=True)
                       if issue.code == "GATEWAY_DEGREE")
    assert "Split choice 只有 1 条出边" in final_issue.message
    assert "f2(choice->incomplete)" in final_issue.message
    assert "Split 至少需要 2 条出边" in final_issue.message


def test_graph_patch_can_atomically_rewire_an_existing_edge_and_add_a_gateway():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={
            "id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"],
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
        {"id": "parallel_prepare", "kind": "gateway", "name": "Prepare in parallel",
         "gatewayType": "parallel", "role": "split", "sourceSentences": ["S2", "S3"]},
        {"id": "shipping_docs", "kind": "task", "name": "Prepare shipping documents",
         "sourceSentences": ["S3"]},
    ], [
        {"id": "f_quality_package", "source": "quality", "target": "parallel_prepare",
         "sourceSentences": ["S2", "S3"]},
        {"id": "f_split_package", "source": "parallel_prepare", "target": "package",
         "sourceSentences": ["S2"]},
        {"id": "f_split_docs", "source": "parallel_prepare", "target": "shipping_docs",
         "sourceSentences": ["S3"]},
    ], "P2")

    rewired = next(edge for edge in candidate.edges if edge.id == "f_quality_package")
    assert rewired.target == "parallel_prepare"
    assert rewired.introducedInSegment == "P1"
    assert candidate.node_map()["parallel_prepare"].introducedInSegment == "P2"
    assert {edge.id for edge in candidate.edges}.issuperset({
        "f_quality_package", "f_split_package", "f_split_docs",
    })


def test_graph_patch_node_removal_also_removes_its_incident_edges():
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start"},
        {"id": "obsolete", "kind": "task", "name": "Obsolete"},
    ]})], "P1")

    candidate = apply_graph_patch(
        previous, nodes=[], edges=[], segment_id="P2", remove_node_ids=["obsolete"]
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
        nodes=[],
        edges=[{"id": "f", "source": "a", "target": "c", "sourceSentences": ["S2"]}],
        segment_id="P2",
        remove_edge_ids=["f"],
    )

    assert [(edge.id, edge.source, edge.target) for edge in candidate.edges] == [("f", "a", "c")]


def test_generated_batch_warns_about_a_new_single_branch_gateway():
    previous = apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "review", "kind": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "choice", "kind": "gateway", "name": "Result", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S2"]},
        {"id": "approved", "kind": "task", "name": "Approve", "sourceSentences": ["S2"]},
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
        {"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]},
        {"id": "review", "kind": "task", "name": "Review", "sourceSentences": ["S1"]},
    ]})], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "choice", "kind": "gateway", "name": "Result", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S2"]},
        {"id": "approved", "kind": "task", "name": "Approve", "sourceSentences": ["S2"]},
        {"id": "rejected", "kind": "task", "name": "Reject", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_review_choice", "source": "review", "target": "choice", "sourceSentences": ["S2"]},
        {"id": "f_choice_approved", "source": "choice", "target": "approved", "condition": {"label": "approved"}, "sourceSentences": ["S2"]},
        {"id": "f_choice_rejected", "source": "choice", "target": "rejected", "condition": {"label": "not approved"}, "sourceSentences": ["S2"]},
    ], "P2")

    assert validate_incremental_batch(previous, candidate) == []
    assert validate_graph(candidate) == []


def test_generated_batch_can_continue_from_an_existing_join():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "gatewayType": "parallel", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "task_a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "task_b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "gatewayType": "parallel", "role": "join", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "fork"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "fork", "target": "task_a"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "fork", "target": "task_b"}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "task_a", "target": "join"}),
        EditCall(name="add_edge", arguments={"id": "f5", "source": "task_b", "target": "join"}),
    ], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "archive", "kind": "task", "name": "Archive", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_join_archive", "source": "join", "target": "archive", "sourceSentences": ["S2"]},
    ], "P2")

    assert validate_incremental_batch(previous, candidate) == []
    assert validate_graph(candidate) == []


def test_generated_batch_can_continue_unjoined_existing_branches_separately():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "choice", "name": "Result", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "approved", "name": "Record approval", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "rejected", "name": "Record rejection", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "approved", "condition": {"label": "approved"}}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "choice", "target": "rejected", "condition": {"label": "not approved"}}),
    ], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "deploy", "kind": "task", "name": "Deploy", "sourceSentences": ["S2"]},
        {"id": "archive", "kind": "task", "name": "Archive", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_approved_deploy", "source": "approved", "target": "deploy", "sourceSentences": ["S2"]},
        {"id": "f_rejected_archive", "source": "rejected", "target": "archive", "sourceSentences": ["S2"]},
    ], "P2")

    assert validate_incremental_batch(previous, candidate) == []
    assert validate_graph(candidate) == []


def test_generated_batch_rejects_an_unconnected_new_node():
    previous = apply_edits(Graph(), [EditCall(name="add_event", arguments={
        "id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"],
    })], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "orphan", "kind": "task", "name": "Orphan", "sourceSentences": ["S2"]},
    ], [], "P2")

    codes = {issue.code for issue in validate_incremental_batch(previous, candidate)}

    assert "BATCH_UNCONNECTED_NODE" in codes


def test_generated_batch_warns_when_continuing_from_an_incomplete_existing_join():
    previous = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "work", "name": "Work", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "gatewayType": "exclusive", "role": "join", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "work"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "work", "target": "join"}),
    ], "P1")
    candidate = apply_subgraph(previous, [
        {"id": "next_task", "kind": "task", "name": "Continue", "sourceSentences": ["S2"]},
    ], [
        {"id": "f_join_next", "source": "join", "target": "next_task", "sourceSentences": ["S2"]},
    ], "P2")

    issues = validate_incremental_batch(previous, candidate)

    assert any(issue.code == "BATCH_GATEWAY_DEGREE" and issue.elementIds == ["join"]
               and issue.severity == "warning"
               for issue in issues)


def test_gateway_roles_reject_mixed_split_and_join_degrees():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_gateway", arguments={"id": "fork_a", "name": "Fork A", "gatewayType": "parallel", "role": "split"}),
        EditCall(name="add_gateway", arguments={"id": "fork_b", "name": "Fork B", "gatewayType": "parallel", "role": "split"}),
        EditCall(name="add_gateway", arguments={"id": "bad_split", "name": "Bad split", "gatewayType": "parallel", "role": "split"}),
        EditCall(name="add_gateway", arguments={"id": "bad_join", "name": "Bad join", "gatewayType": "parallel", "role": "join"}),
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
    assert "Join 负责汇合，只能有 1 条出边；Split 负责分叉，才可以多出边" in join_issue.message


def test_open_gateway_context_exposes_gateway_type_and_role():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Checks complete", "gatewayType": "parallel", "role": "join"}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "join"}),
    ], "P1")

    assert open_nodes(graph) == [{
        "id": "join", "name": "Checks complete", "kind": "gateway",
        "sourceSentences": [], "introducedInSegment": "P1",
        "gatewayType": "parallel", "role": "join",
    }]


def test_gateway_refs_include_every_gateway_without_a_provisional_status():
    graph = apply_edits(Graph(), [
        EditCall(name="add_gateway", arguments={
            "id": "decision", "name": "Payment result",
            "gatewayType": "exclusive", "role": "split",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "merge", "name": "Checks complete",
            "gatewayType": "parallel", "role": "join",
        }),
    ], "P1")

    assert gateway_refs(graph) == [
        {"id": "decision", "gatewayType": "exclusive", "role": "split",
         "question": "Payment result"},
        {"id": "merge", "gatewayType": "parallel", "role": "join",
         "question": "Checks complete"},
    ]
    assert all("status" not in item for item in gateway_refs(graph))


def test_incremental_validation_rejects_nodes_unreachable_from_start():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "connected", "name": "Connected", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "orphan", "name": "Orphan", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "connected"}),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "UNREACHABLE" and issue.elementIds == ["orphan"]
               and issue.sourceSentences == ["S2"] for issue in issues)


def test_incremental_validation_rejects_implicit_split_on_task():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "dispatch", "name": "Dispatch", "sourceSentences": ["S2"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S2"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "dispatch"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "dispatch", "target": "a"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "dispatch", "target": "b"}),
    ], "P1")

    issues = validate_graph(graph)

    assert any(issue.code == "IMPLICIT_SPLIT" and issue.elementIds == ["dispatch"]
               for issue in issues)


def test_incremental_validation_rejects_implicit_merge_on_task():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "gatewayType": "parallel", "role": "split", "sourceSentences": ["S1"]}),
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

    assert any(issue.code == "IMPLICIT_MERGE" and issue.elementIds == ["continue"]
               for issue in issues)


def test_split_validation_rejects_conditional_default_and_parallel_default():
    graph = apply_edits(Graph(), [
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "gatewayType": "parallel", "role": "split", "sourceSentences": ["S1"]}),
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
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Result", "gatewayType": "exclusive", "role": "split",
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
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start"}),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Result", "gatewayType": "exclusive", "role": "split",
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


def test_final_validation_reports_every_reachable_leaf_that_is_not_an_end_event():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "kind": "event", "name": "Start", "eventType": "start", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "choice", "kind": "gateway", "name": "Choose", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_node", arguments={"id": "end_ok", "kind": "event", "name": "Done", "eventType": "end", "sourceSentences": ["S2"]}),
        EditCall(name="add_node", arguments={"id": "notify", "kind": "task", "name": "Request more data", "sourceSentences": ["S3"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "choice"}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "end_ok"}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "choice", "target": "notify"}),
    ], "P1")

    issues = validate_graph(graph, final=True)
    leaf_issues = [issue for issue in issues if issue.code == "OPEN_NODE"]

    assert len(leaf_issues) == 1
    assert leaf_issues[0].elementIds == ["notify"]
    assert leaf_issues[0].sourceSentences == ["S3"]
    assert "eventType=end" in leaf_issues[0].message


def test_final_leaf_rule_ignores_unreachable_leaf_nodes():
    graph = apply_edits(Graph(), [
        EditCall(name="add_node", arguments={"id": "start", "kind": "event", "name": "Start", "eventType": "start"}),
        EditCall(name="add_node", arguments={"id": "end", "kind": "event", "name": "End", "eventType": "end"}),
        EditCall(name="add_node", arguments={"id": "orphan", "kind": "task", "name": "Orphan"}),
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
    with pytest.raises(ValueError, match="task cannot contain"):
        apply_edits(Graph(), [EditCall(name="add_task", arguments={
            "id": "bad_task", "name": "Bad", "eventType": "end", "sourceSentences": ["S1"],
        })], "P1")


def test_linear_sequence_rejects_gateway_nodes():
    with pytest.raises(EditError, match="only accepts task and event"):
        apply_edits(Graph(), [EditCall(name="add_linear_sequence", arguments={"nodes": [{
            "id": "bad_gateway", "kind": "gateway", "name": "Bad",
            "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S1"],
        }]})], "P1")


def test_loop_is_modeled_by_an_edge_back_to_an_existing_node():
    graph = apply_edits(Graph(), [
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "enter_review", "name": "Enter review", "gatewayType": "exclusive", "role": "join", "sourceSentences": ["S1", "S2"]}),
        EditCall(name="add_task", arguments={"id": "review", "name": "Review", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "repeat", "name": "Repeat?", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "eventType": "end", "trigger": "none", "sourceSentences": ["S2"]}),
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
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "gatewayType": "parallel", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_event", arguments={"id": "end_a", "name": "End A", "eventType": "end", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_event", arguments={"id": "end_b", "name": "End B", "eventType": "end", "trigger": "none", "sourceSentences": ["S1"]}),
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
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "gatewayType": "parallel", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "gatewayType": "parallel", "role": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "eventType": "end", "trigger": "none", "sourceSentences": ["S2"]}),
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
        EditCall(name="add_event", arguments={"id": "start", "name": "Start", "eventType": "start", "trigger": "none", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "fork", "name": "Fork", "gatewayType": "parallel", "role": "split", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "a", "name": "A", "sourceSentences": ["S1"]}),
        EditCall(name="add_task", arguments={"id": "b", "name": "B", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "join", "name": "Join", "gatewayType": "parallel", "role": "join", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end_a", "name": "End A", "eventType": "end", "trigger": "none", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end_b", "name": "End B", "eventType": "end", "trigger": "none", "sourceSentences": ["S2"]}),
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
            "gatewayType": "inclusive", "role": "split",
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
            "id": "start", "name": "Start", "eventType": "start",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "choice", "name": "Continue?", "gatewayType": "exclusive", "role": "split",
        }),
        EditCall(name="add_event", arguments={
            "id": "end", "name": "End", "eventType": "end",
        }),
        EditCall(name="add_gateway", arguments={
            "id": "loop_join", "name": "Repeat", "gatewayType": "exclusive", "role": "join",
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
            "id": "package_join", "name": "Package done", "gatewayType": "exclusive",
            "role": "join", "sourceSentences": ["S10"],
        }),
        EditCall(name="add_task", arguments={
            "id": "docs", "name": "Prepare documents", "sourceSentences": ["S11"],
        }),
        EditCall(name="add_gateway", arguments={
            "id": "ready_join", "name": "Both ready", "gatewayType": "parallel",
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

    assert issue.message == (
        "问题：Join package_join 有 2 条出边："
        "f_join_docs(package_join->docs), f_join_ready(package_join->ready_join)。"
        "原因：Join 负责汇合，只能有 1 条出边；Split 负责分叉，才可以多出边。"
    )

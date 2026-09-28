import pytest

from app.bpmn import graph_to_bpmn
from app.domain import EditCall, Graph
from app.editor import EditError, apply_edits
from app.llm import parse_json_object
from app.segmentation import FixedLengthStrategy, split_sentences
from app.tools import EDIT_TOOLS
from app.validation import validate_graph


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
        EditCall(name="add_edge", arguments={"id": "f2", "source": "choice", "target": "end_a", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "choice", "target": "end_b", "sourceSentences": ["S1"]}),
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
    assert any(issue.code == "GATEWAY_DEGREE" for issue in validate_graph(graph, final=True))


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
        EditCall(name="add_task", arguments={"id": "review", "name": "Review", "sourceSentences": ["S1"]}),
        EditCall(name="add_gateway", arguments={"id": "repeat", "name": "Repeat?", "gatewayType": "exclusive", "role": "split", "sourceSentences": ["S2"]}),
        EditCall(name="add_event", arguments={"id": "end", "name": "End", "eventType": "end", "trigger": "none", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f1", "source": "start", "target": "review", "sourceSentences": ["S1"]}),
        EditCall(name="add_edge", arguments={"id": "f2", "source": "review", "target": "repeat", "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f3", "source": "repeat", "target": "review", "condition": {"label": "repeat"}, "sourceSentences": ["S2"]}),
        EditCall(name="add_edge", arguments={"id": "f4", "source": "repeat", "target": "end", "condition": {"label": "done"}, "sourceSentences": ["S2"]}),
    ], "P1")

    assert any(edge.source == "repeat" and edge.target == "review" for edge in graph.edges)
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

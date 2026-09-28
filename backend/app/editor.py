from __future__ import annotations

from copy import deepcopy
from typing import Any

from .domain import Edge, EditCall, Graph, Node


class EditError(ValueError):
    pass


def _flow_id(source: str, target: str) -> str:
    return f"flow_{source}_to_{target}"


def _normalize_node_args(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill deterministic BPMN defaults that models commonly omit."""
    args = deepcopy(raw)
    if args.get("kind") == "event" and args.get("eventType") and not args.get("trigger"):
        args["trigger"] = "none"
    return args


def apply_edits(graph: Graph, calls: list[EditCall], segment_id: str) -> Graph:
    candidate = deepcopy(graph)
    for call in calls:
        _apply(candidate, call, segment_id)
    candidate.metadata.revision += 1
    return candidate


def _apply(graph: Graph, call: EditCall, segment_id: str) -> None:
    args = deepcopy(call.arguments)
    nodes = graph.node_map()
    edges = {edge.id: edge for edge in graph.edges}
    if call.name in {"add_task", "add_event", "add_gateway", "add_node"}:
        if call.name != "add_node":
            args["kind"] = call.name.removeprefix("add_")
        args = _normalize_node_args(args)
        args["introducedInSegment"] = segment_id
        node = Node.model_validate(args)
        if node.id in nodes:
            raise EditError(f"duplicate node id: {node.id}")
        graph.nodes.append(node)
    elif call.name == "add_edge":
        args["introducedInSegment"] = segment_id
        edge = Edge.model_validate(args)
        if edge.id in edges:
            raise EditError(f"duplicate edge id: {edge.id}")
        graph.edges.append(edge)
    elif call.name == "update_node":
        target = nodes.get(args["targetId"])
        if not target:
            raise EditError("node not found")
        changes = args.get("changes", {})
        if "id" in changes or "kind" in changes:
            raise EditError("node id and kind are immutable")
        updated = target.model_copy(update=changes)
        graph.nodes[graph.nodes.index(target)] = Node.model_validate(updated.model_dump())
    elif call.name == "update_edge":
        target = edges.get(args["targetId"])
        if not target:
            raise EditError("edge not found")
        changes = args.get("changes", {})
        if "id" in changes:
            raise EditError("edge id is immutable")
        graph.edges[graph.edges.index(target)] = Edge.model_validate(target.model_copy(update=changes).model_dump())
    elif call.name == "delete_edge":
        target = edges.get(args["targetId"])
        if not target:
            raise EditError("edge not found")
        graph.edges.remove(target)
    elif call.name == "delete_node":
        target = nodes.get(args["targetId"])
        if not target:
            raise EditError("node not found")
        if any(edge.source == target.id or edge.target == target.id for edge in graph.edges):
            raise EditError("delete connected edges before deleting node")
        graph.nodes.remove(target)
    elif call.name == "add_linear_sequence":
        raw_nodes = args.get("nodes", [])
        predecessor = args.get("predecessorId")
        sequence: list[Node] = []
        for raw in raw_nodes:
            if raw.get("kind") == "gateway":
                raise EditError("add_linear_sequence only accepts task and event nodes")
            raw = _normalize_node_args(raw)
            raw["introducedInSegment"] = segment_id
            node = Node.model_validate(raw)
            if node.id in graph.node_map() or any(item.id == node.id for item in sequence):
                raise EditError(f"duplicate node id: {node.id}")
            sequence.append(node)
        graph.nodes.extend(sequence)
        chain = ([predecessor] if predecessor else []) + [item.id for item in sequence]
        for source, target in zip(chain, chain[1:]):
            edge_id = _flow_id(source, target)
            if any(edge.id == edge_id for edge in graph.edges):
                raise EditError(f"duplicate generated edge id: {edge_id}")
            graph.edges.append(Edge(id=edge_id, source=source, target=target,
                                    sourceSentences=next(item.sourceSentences for item in sequence if item.id == target),
                                    introducedInSegment=segment_id))
    else:
        raise EditError(f"unsupported edit: {call.name}")

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


def apply_subgraph(graph: Graph, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                   segment_id: str) -> Graph:
    """Atomically merge a generator-produced subgraph into the committed graph."""
    candidate = deepcopy(graph)
    existing_node_ids = {node.id for node in candidate.nodes}
    existing_edge_ids = {edge.id for edge in candidate.edges}
    new_node_ids: set[str] = set()
    new_edge_ids: set[str] = set()

    for raw in nodes:
        args = _normalize_node_args(raw)
        args["introducedInSegment"] = segment_id
        node = Node.model_validate(args)
        if node.id in existing_node_ids or node.id in new_node_ids:
            raise EditError(f"duplicate node id: {node.id}")
        candidate.nodes.append(node)
        new_node_ids.add(node.id)

    available_node_ids = existing_node_ids | new_node_ids
    for raw in edges:
        args = deepcopy(raw)
        args["introducedInSegment"] = segment_id
        edge = Edge.model_validate(args)
        if edge.id in existing_edge_ids or edge.id in new_edge_ids:
            raise EditError(f"duplicate edge id: {edge.id}")
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")
        candidate.edges.append(edge)
        new_edge_ids.add(edge.id)

    candidate.metadata.revision += 1
    return candidate


def apply_graph_patch(graph: Graph, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                      segment_id: str, remove_node_ids: list[str] | None = None,
                      remove_edge_ids: list[str] | None = None) -> Graph:
    """Atomically apply a declarative generator patch, including local rewrites."""
    candidate = deepcopy(graph)
    remove_node_ids = list(remove_node_ids or [])
    remove_edge_ids = list(remove_edge_ids or [])
    if len(remove_node_ids) != len(set(remove_node_ids)):
        raise EditError("duplicate id in removeNodeIds")
    if len(remove_edge_ids) != len(set(remove_edge_ids)):
        raise EditError("duplicate id in removeEdgeIds")

    raw_node_ids = [item.get("id") for item in nodes if isinstance(item, dict)]
    raw_edge_ids = [item.get("id") for item in edges if isinstance(item, dict)]
    if len(raw_node_ids) != len(set(raw_node_ids)):
        raise EditError("duplicate node id in patch")
    if len(raw_edge_ids) != len(set(raw_edge_ids)):
        raise EditError("duplicate edge id in patch")
    remove_node_ids = [item for item in remove_node_ids if item not in set(raw_node_ids)]
    remove_edge_ids = [item for item in remove_edge_ids if item not in set(raw_edge_ids)]

    original_nodes = candidate.node_map()
    original_edges = {edge.id: edge for edge in candidate.edges}
    unknown_nodes = sorted(set(remove_node_ids) - set(original_nodes))
    unknown_edges = sorted(set(remove_edge_ids) - set(original_edges))
    if unknown_nodes:
        raise EditError(f"removeNodeIds references missing nodes: {', '.join(unknown_nodes)}")
    if unknown_edges:
        raise EditError(f"removeEdgeIds references missing edges: {', '.join(unknown_edges)}")

    removed_nodes = set(remove_node_ids)
    removed_edges = set(remove_edge_ids)
    candidate.edges = [
        edge for edge in candidate.edges
        if edge.id not in removed_edges
        and edge.source not in removed_nodes
        and edge.target not in removed_nodes
    ]
    candidate.nodes = [node for node in candidate.nodes if node.id not in removed_nodes]

    node_positions = {node.id: index for index, node in enumerate(candidate.nodes)}
    for raw in nodes:
        args = _normalize_node_args(raw)
        existing = original_nodes.get(args.get("id"))
        if existing and existing.kind != args.get("kind"):
            raise EditError(f"node kind is immutable for upsert: {existing.id}")
        args["introducedInSegment"] = (
            existing.introducedInSegment if existing else segment_id
        )
        node = Node.model_validate(args)
        if node.id in node_positions:
            candidate.nodes[node_positions[node.id]] = node
        else:
            node_positions[node.id] = len(candidate.nodes)
            candidate.nodes.append(node)

    available_node_ids = {node.id for node in candidate.nodes}
    edge_positions = {edge.id: index for index, edge in enumerate(candidate.edges)}
    for raw in edges:
        args = deepcopy(raw)
        existing = original_edges.get(args.get("id"))
        args["introducedInSegment"] = (
            existing.introducedInSegment if existing else segment_id
        )
        edge = Edge.model_validate(args)
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")
        if edge.id in edge_positions:
            candidate.edges[edge_positions[edge.id]] = edge
        else:
            edge_positions[edge.id] = len(candidate.edges)
            candidate.edges.append(edge)

    for edge in candidate.edges:
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")

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

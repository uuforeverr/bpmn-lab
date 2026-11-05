from __future__ import annotations

from copy import deepcopy
from typing import Any

from .domain import Edge, EditCall, Graph, Node, node_kind


class EditError(ValueError):
    pass


def _flow_id(source: str, target: str) -> str:
    return f"flow_{source}_to_{target}"


def _normalize_node_args(raw: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(raw)


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
        node = Node.model_validate(args)
        if node.id in existing_node_ids or node.id in new_node_ids:
            raise EditError(f"duplicate node id: {node.id}")
        candidate.nodes.append(node)
        new_node_ids.add(node.id)

    available_node_ids = existing_node_ids | new_node_ids
    for raw in edges:
        args = deepcopy(raw)
        edge = Edge.model_validate(args)
        if edge.id in existing_edge_ids or edge.id in new_edge_ids:
            raise EditError(f"duplicate edge id: {edge.id}")
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")
        candidate.edges.append(edge)
        new_edge_ids.add(edge.id)

    candidate.metadata.revision += 1
    return candidate


def apply_graph_patch(graph: Graph, add_nodes: list[dict[str, Any]], add_edges: list[dict[str, Any]],
                      segment_id: str, remove_node_ids: list[str] | None = None,
                      remove_edge_ids: list[str] | None = None) -> Graph:
    """Atomically apply an add/remove delta; remove+add of one ID is replacement."""
    candidate = deepcopy(graph)
    remove_node_ids = list(remove_node_ids or [])
    remove_edge_ids = list(remove_edge_ids or [])
    if len(remove_node_ids) != len(set(remove_node_ids)):
        raise EditError("duplicate id in removeNodeIds")
    if len(remove_edge_ids) != len(set(remove_edge_ids)):
        raise EditError("duplicate id in removeEdgeIds")

    raw_node_ids = [item.get("id") for item in add_nodes if isinstance(item, dict)]
    raw_edge_ids = [item.get("id") for item in add_edges if isinstance(item, dict)]
    if len(raw_node_ids) != len(set(raw_node_ids)):
        raise EditError("duplicate node id in patch")
    if len(raw_edge_ids) != len(set(raw_edge_ids)):
        raise EditError("duplicate edge id in patch")
    original_nodes = candidate.node_map()
    original_edges = {edge.id: edge for edge in candidate.edges}
    unknown_nodes = sorted(set(remove_node_ids) - set(original_nodes))
    unknown_edges = sorted(set(remove_edge_ids) - set(original_edges))
    if unknown_nodes:
        raise EditError(f"removeNodeIds references missing nodes: {', '.join(unknown_nodes)}")
    if unknown_edges:
        raise EditError(f"removeEdgeIds references missing edges: {', '.join(unknown_edges)}")

    implicit_node_replacements = sorted(
        (set(raw_node_ids) & set(original_nodes)) - set(remove_node_ids)
    )
    implicit_edge_replacements = sorted(
        (set(raw_edge_ids) & set(original_edges)) - set(remove_edge_ids)
    )
    if implicit_node_replacements:
        raise EditError(
            "addNodes reuses committed IDs without explicit removal: "
            + ", ".join(implicit_node_replacements)
        )
    if implicit_edge_replacements:
        raise EditError(
            "addEdges reuses committed IDs without explicit removal: "
            + ", ".join(implicit_edge_replacements)
        )

    removed_nodes = set(remove_node_ids)
    removed_edges = set(remove_edge_ids)
    replacement_node_ids = removed_nodes & set(raw_node_ids)
    truly_removed_nodes = removed_nodes - replacement_node_ids
    candidate.edges = [
        edge for edge in candidate.edges
        if edge.id not in removed_edges
        and edge.source not in truly_removed_nodes
        and edge.target not in truly_removed_nodes
    ]
    candidate.nodes = [node for node in candidate.nodes if node.id not in removed_nodes]

    for raw in add_nodes:
        args = _normalize_node_args(raw)
        existing = original_nodes.get(args.get("id"))
        if existing and existing.kind != node_kind(args.get("type", "")):
            raise EditError(f"node category is immutable for replacement: {existing.id}")
        node = Node.model_validate(args)
        candidate.nodes.append(node)

    available_node_ids = {node.id for node in candidate.nodes}
    for raw in add_edges:
        args = deepcopy(raw)
        edge = Edge.model_validate(args)
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")
        candidate.edges.append(edge)

    for edge in candidate.edges:
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")

    candidate.metadata.revision += 1
    return candidate


def apply_graph_operations(graph: Graph, add: list[dict[str, Any]],
                           update: list[dict[str, Any]], remove: list[dict[str, Any]],
                           segment_id: str) -> Graph:
    """Atomically apply declarative add/update/remove operations to a graph."""
    del segment_id  # Element provenance is carried by sourceSentences, not patch mechanics.
    candidate = deepcopy(graph)

    def keyed(items: list[dict[str, Any]], operation: str) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                raise EditError(f"{operation} items must be objects")
            kind = raw.get("kind")
            element_id = raw.get("id")
            if kind not in {"node", "edge"} or not isinstance(element_id, str):
                raise EditError(f"{operation} items require kind=node|edge and a string id")
            key = (kind, element_id)
            if key in result:
                raise EditError(f"duplicate {kind} id in {operation}: {element_id}")
            result[key] = deepcopy(raw)
        return result

    additions = keyed(add, "add")
    updates = keyed(update, "update")
    removals = keyed(remove, "remove")
    overlaps = sorted((set(additions) & set(updates))
                      | (set(additions) & set(removals))
                      | (set(updates) & set(removals)))
    if overlaps:
        labels = ", ".join(f"{kind}:{element_id}" for kind, element_id in overlaps)
        raise EditError(f"elements cannot occur in multiple patch operations: {labels}")

    original_nodes = candidate.node_map()
    original_edges = {edge.id: edge for edge in candidate.edges}
    for operation, items, must_exist in (
        ("add", additions, False), ("update", updates, True), ("remove", removals, True),
    ):
        for (kind, element_id) in items:
            exists = element_id in (original_nodes if kind == "node" else original_edges)
            if exists != must_exist:
                expectation = "an existing" if must_exist else "a new"
                raise EditError(f"{operation} requires {expectation} {kind} id: {element_id}")

    removed_node_ids = {
        element_id for kind, element_id in removals if kind == "node"
    }
    removed_edge_ids = {
        element_id for kind, element_id in removals if kind == "edge"
    }
    updated_edge_ids = {
        element_id for kind, element_id in updates if kind == "edge"
    }

    node_by_id = {node.id: node for node in candidate.nodes}
    for element_id in removed_node_ids:
        del node_by_id[element_id]
    for (kind, element_id), raw in updates.items():
        if kind != "node":
            continue
        args = {key: value for key, value in raw.items() if key != "kind"}
        existing = original_nodes[element_id]
        if existing.kind != node_kind(args.get("type", "")):
            raise EditError(f"node category is immutable for update: {element_id}")
        node_by_id[element_id] = Node.model_validate(args)
    for (kind, element_id), raw in additions.items():
        if kind == "node":
            args = {key: value for key, value in raw.items() if key != "kind"}
            node_by_id[element_id] = Node.model_validate(args)

    edge_by_id = {edge.id: edge for edge in candidate.edges}
    for element_id in removed_edge_ids:
        del edge_by_id[element_id]
    for edge_id, edge in list(edge_by_id.items()):
        if edge_id in updated_edge_ids:
            continue
        if edge.source in removed_node_ids or edge.target in removed_node_ids:
            del edge_by_id[edge_id]
    for (kind, element_id), raw in updates.items():
        if kind == "edge":
            args = {key: value for key, value in raw.items() if key != "kind"}
            edge_by_id[element_id] = Edge.model_validate(args)
    for (kind, element_id), raw in additions.items():
        if kind == "edge":
            args = {key: value for key, value in raw.items() if key != "kind"}
            edge_by_id[element_id] = Edge.model_validate(args)

    available_node_ids = set(node_by_id)
    for edge in edge_by_id.values():
        if edge.source not in available_node_ids or edge.target not in available_node_ids:
            raise EditError(f"edge {edge.id} references a missing node")

    candidate.nodes = list(node_by_id.values())
    candidate.edges = list(edge_by_id.values())
    candidate.metadata.revision += 1
    return candidate


def _apply(graph: Graph, call: EditCall, segment_id: str) -> None:
    args = deepcopy(call.arguments)
    nodes = graph.node_map()
    edges = {edge.id: edge for edge in graph.edges}
    if call.name in {"add_task", "add_event", "add_gateway", "add_node"}:
        if call.name == "add_task":
            args.setdefault("type", "task")
        if call.name != "add_node":
            expected_kind = call.name.removeprefix("add_")
            if node_kind(args.get("type", "")) != expected_kind:
                raise EditError(f"{call.name} only accepts {expected_kind} types")
        args = _normalize_node_args(args)
        node = Node.model_validate(args)
        if node.id in nodes:
            raise EditError(f"duplicate node id: {node.id}")
        graph.nodes.append(node)
    elif call.name == "add_edge":
        edge = Edge.model_validate(args)
        if edge.id in edges:
            raise EditError(f"duplicate edge id: {edge.id}")
        graph.edges.append(edge)
    elif call.name == "update_node":
        target = nodes.get(args["targetId"])
        if not target:
            raise EditError("node not found")
        changes = args.get("changes", {})
        if "id" in changes:
            raise EditError("node id is immutable")
        if "type" in changes and node_kind(changes["type"]) != target.kind:
            raise EditError("node category is immutable")
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
            if node_kind(raw.get("type", "")) == "gateway":
                raise EditError("add_linear_sequence only accepts task and event nodes")
            raw = _normalize_node_args(raw)
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
            graph.edges.append(Edge(
                id=edge_id, source=source, target=target,
                sourceSentences=next(item.sourceSentences for item in sequence if item.id == target),
            ))
    else:
        raise EditError(f"unsupported edit: {call.name}")

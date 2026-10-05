from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from .domain import Graph, Issue


ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{1,127}$")


def validate_graph(graph: Graph, final: bool = False) -> list[Issue]:
    """Report deterministic graph-rule violations; do not infer business semantics."""
    issues: list[Issue] = []
    node_ids = [node.id for node in graph.nodes]
    edge_ids = [edge.id for edge in graph.edges]
    for value in node_ids + edge_ids:
        if not ID_PATTERN.fullmatch(value):
            issues.append(Issue(
                code="INVALID_ID",
                message=(f"问题：ID {value} 不合法。原因：BPMN ID 必须以字母或下划线开头，"
                         "且只能包含字母、数字、点、下划线或连字符。"),
                elementIds=[value],
            ))
    for values, kind in ((node_ids, "node"), (edge_ids, "edge")):
        duplicates = {value for value in values if values.count(value) > 1}
        for value in duplicates:
            issues.append(Issue(
                code="DUPLICATE_ID",
                message=f"问题：{kind} ID {value} 重复。原因：每个 BPMN 元素的 ID 必须唯一。",
                elementIds=[value],
            ))
    node_map = graph.node_map()
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source not in node_map or edge.target not in node_map:
            issues.append(Issue(
                code="DANGLING_EDGE",
                message=(f"问题：边 {edge.id} 引用了不存在的节点。"
                         "原因：边的 source 和 target 必须都是已有节点。"),
                elementIds=[edge.id],
            ))
            continue
        outgoing[edge.source].append(edge.target)
        incoming[edge.target].append(edge.source)
    starts = [node for node in graph.nodes if node.kind == "event" and node.eventType == "start"]
    ends = [node for node in graph.nodes if node.kind == "event" and node.eventType == "end"]
    for node in starts:
        if incoming[node.id]:
            issues.append(Issue(
                code="START_HAS_INCOMING",
                message=f"问题：Start {node.id} 有入边。原因：Start 是流程入口，不能有入边。",
                elementIds=[node.id],
            ))
    for node in ends:
        if outgoing[node.id]:
            issues.append(Issue(
                code="END_HAS_OUTGOING",
                message=f"问题：End {node.id} 有出边。原因：End 是流程终点，不能有出边。",
                elementIds=[node.id],
            ))
    reachable: set[str] = {node.id for node in starts}
    queue = deque(reachable)
    while queue:
        for target in outgoing[queue.popleft()]:
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
    if starts:
        unreachable = [node for node in graph.nodes if node.id not in reachable]
        if unreachable:
            issues.append(Issue(
                code="UNREACHABLE",
                message="问题：这些节点从 Start 不可达。原因：所有流程节点都必须从 Start 可达。",
                elementIds=[node.id for node in unreachable],
                sourceSentences=sorted({sid for node in unreachable for sid in node.sourceSentences}),
            ))
    for node in graph.nodes:
        if node.kind != "gateway" and len(outgoing[node.id]) > 1:
            issues.append(Issue(
                code="IMPLICIT_SPLIT",
                message=(f"问题：非 Gateway 节点 {node.id} 有 {len(outgoing[node.id])} 条出边。"
                         "原因：只有 Split Gateway 表示分叉，才可以有多条出边。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if node.kind != "gateway" and len(incoming[node.id]) > 1:
            issues.append(Issue(
                code="IMPLICIT_MERGE",
                message=(f"问题：非 Gateway 节点 {node.id} 有 {len(incoming[node.id])} 条入边。"
                         "原因：只有 Join Gateway 表示汇合，才可以有多条入边。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if node.kind == "gateway":
            degree = len(outgoing[node.id]) if node.role == "split" else len(incoming[node.id])
            # Incremental snapshots may intentionally leave a gateway open for a
            # branch or join that is described in a later semantic segment.
            if final and degree < 2:
                control_edges = [
                    edge for edge in graph.edges
                    if (edge.source == node.id if node.role == "split" else edge.target == node.id)
                ]
                connections = ", ".join(
                    f"{edge.id}({edge.source}->{edge.target})" for edge in control_edges
                ) or "无"
                direction = "出边" if node.role == "split" else "入边"
                issues.append(Issue(
                    code="GATEWAY_DEGREE",
                    message=(f"问题：{node.role.title()} {node.id} 只有 {degree} 条{direction}：{connections}。"
                             f"原因：{node.role.title()} 至少需要 2 条{direction}，否则没有形成"
                             f"{'分叉' if node.role == 'split' else '汇合'}。"),
                    elementIds=[node.id],
                    sourceSentences=node.sourceSentences,
                ))
            if node.role == "split":
                if len(incoming[node.id]) > 1:
                    issues.append(Issue(
                        code="SPLIT_MULTIPLE_INCOMING",
                        message=(f"问题：Split {node.id} 有 {len(incoming[node.id])} 条入边。"
                                 "原因：Split 负责分叉，只能有 1 条入边；Join 负责汇合，才可以多入边。"),
                        elementIds=[node.id],
                        sourceSentences=node.sourceSentences,
                    ))
                split_edges = [edge for edge in graph.edges if edge.source == node.id]
                defaults = sum(1 for edge in split_edges if edge.isDefault)
                if defaults > 1:
                    issues.append(Issue(
                        code="MULTIPLE_DEFAULTS",
                        message=(f"问题：Split {node.id} 有 {defaults} 条默认流。"
                                 "原因：一个 Split 最多只能有 1 条默认流。"),
                        elementIds=[node.id],
                    ))
                invalid_defaults = [edge.id for edge in split_edges if edge.isDefault and edge.condition]
                if invalid_defaults:
                    issues.append(Issue(
                        code="DEFAULT_HAS_CONDITION",
                        message=("问题：默认流带有 condition。"
                                 "原因：默认流只在其他条件都不成立时执行，本身不能有 condition。"),
                        elementIds=invalid_defaults,
                    ))
                if node.gatewayType == "parallel" and any(edge.condition for edge in split_edges):
                    issues.append(Issue(
                        code="PARALLEL_CONDITION",
                        message=(f"问题：Parallel Split {node.id} 的出边带有 condition。"
                                 "原因：Parallel Split 会走全部出边，出边不能有 condition。"),
                        elementIds=[node.id],
                    ))
                if node.gatewayType == "parallel" and any(edge.isDefault for edge in split_edges):
                    issues.append(Issue(
                        code="PARALLEL_DEFAULT",
                        message=(f"问题：Parallel Split {node.id} 设置了默认流。"
                                 "原因：Parallel Split 会走全部出边，不存在默认流。"),
                        elementIds=[node.id],
                    ))
                if node.gatewayType == "exclusive" and len(split_edges) >= 2:
                    unconditional = [
                        edge for edge in split_edges
                        if edge.condition is None and not edge.isDefault
                    ]
                    if unconditional:
                        issues.append(Issue(
                            code="EXCLUSIVE_UNCONDITIONAL_FLOW",
                            message=("问题：Exclusive Split 存在无条件且非默认的出边。"
                                     "原因：每条出边必须有 condition，或是唯一默认流。"),
                            elementIds=[edge.id for edge in unconditional],
                            sourceSentences=sorted({
                                sid for edge in unconditional for sid in edge.sourceSentences
                            }),
                        ))
                if node.gatewayType == "inclusive" and len(split_edges) >= 2:
                    unconditional = [
                        edge for edge in split_edges
                        if edge.condition is None and not edge.isDefault
                    ]
                    if unconditional:
                        issues.append(Issue(
                            code="INCLUSIVE_UNCONDITIONAL_FLOW",
                            message=("问题：Inclusive Split 存在无条件且非默认的出边。"
                                     "原因：每条出边必须有 condition，或是唯一默认流。"),
                            elementIds=[edge.id for edge in unconditional],
                            sourceSentences=sorted({
                                sid for edge in unconditional for sid in edge.sourceSentences
                            }),
                        ))
            elif len(outgoing[node.id]) > 1:
                join_edges = [edge for edge in graph.edges if edge.source == node.id]
                connections = ", ".join(
                    f"{edge.id}({edge.source}->{edge.target})" for edge in join_edges
                )
                issues.append(Issue(
                    code="JOIN_MULTIPLE_OUTGOING",
                    message=(f"问题：Join {node.id} 有 {len(join_edges)} 条出边：{connections}。"
                             "原因：Join 负责汇合，只能有 1 条出边；Split 负责分叉，才可以多出边。"),
                    elementIds=[node.id],
                    sourceSentences=node.sourceSentences,
                ))
    if final:
        if len(starts) != 1:
            issues.append(Issue(
                code="START_COUNT",
                message=(f"问题：流程有 {len(starts)} 个 Start。"
                         "原因：最终流程必须恰好有 1 个 Start。"),
            ))
        if not ends:
            issues.append(Issue(
                code="END_COUNT",
                message="问题：流程没有 End。原因：最终流程至少需要 1 个 End。",
            ))
        if ends:
            can_reach_end: set[str] = {node.id for node in ends}
            reverse_queue = deque(can_reach_end)
            while reverse_queue:
                for predecessor in incoming[reverse_queue.popleft()]:
                    if predecessor not in can_reach_end:
                        can_reach_end.add(predecessor)
                        reverse_queue.append(predecessor)
            trapped = [
                node for node in graph.nodes
                if node.id in reachable and node.id not in can_reach_end and outgoing[node.id]
            ]
            if trapped:
                issues.append(Issue(
                    code="NO_END_PATH",
                    message=("问题：这些可达节点无法到达任何 End。"
                             "原因：每条流程路径最终都必须可以到达 End。"),
                    elementIds=[node.id for node in trapped],
                    sourceSentences=sorted({sid for node in trapped for sid in node.sourceSentences}),
                ))
        parallel_joins = [node for node in graph.nodes if node.kind == "gateway"
                          and node.gatewayType == "parallel" and node.role == "join"]
        for split in (node for node in graph.nodes if node.id in reachable
                      and node.kind == "gateway" and node.gatewayType == "parallel"
                      and node.role == "split"):
            branch_targets = outgoing[split.id]
            common_reachable: set[str] | None = None
            for target in branch_targets:
                branch_reachable = _reachable_from(target, outgoing)
                common_reachable = (branch_reachable if common_reachable is None
                                    else common_reachable.intersection(branch_reachable))
            matching_joins = [join.id for join in parallel_joins
                              if common_reachable is not None and join.id in common_reachable]
            if not matching_joins:
                issues.append(Issue(
                    code="PARALLEL_JOIN_MISSING",
                    message=(f"问题：Parallel Split {split.id} 的分支没有共同的 Parallel Join。"
                             "原因：进入共同后续前，全部并行分支必须先由 Parallel Join 汇合。"),
                    elementIds=[split.id],
                    sourceSentences=split.sourceSentences,
                ))
        for node in graph.nodes:
            if node.id in reachable and not outgoing[node.id] and not (
                node.kind == "event" and node.eventType == "end"
            ):
                issues.append(Issue(
                    code="OPEN_NODE",
                    message=(f"问题：叶子节点 {node.id} 不是 eventType=end 的 End Event。"
                             "原因：每条结束路径都必须以 End Event 终止。"),
                    elementIds=[node.id],
                    sourceSentences=node.sourceSentences,
                ))
    return issues


def validate_incremental_batch(previous: Graph, candidate: Graph, final: bool = False) -> list[Issue]:
    """Validate completeness properties specific to one generated subgraph batch."""
    issues: list[Issue] = []
    previous_node_ids = {node.id for node in previous.nodes}
    previous_edge_ids = {edge.id for edge in previous.edges}
    new_nodes = [node for node in candidate.nodes if node.id not in previous_node_ids]
    new_edges = [edge for edge in candidate.edges if edge.id not in previous_edge_ids]
    touched_node_ids = {endpoint for edge in new_edges for endpoint in (edge.source, edge.target)}
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    incident: dict[str, list[str]] = defaultdict(list)
    for edge in candidate.edges:
        incoming[edge.target].append(edge.id)
        outgoing[edge.source].append(edge.id)
        incident[edge.source].append(edge.id)
        incident[edge.target].append(edge.id)

    starts = [node for node in candidate.nodes
              if node.kind == "event" and node.eventType == "start"]
    if candidate.nodes and len(starts) != 1:
        issues.append(Issue(
            code="START_COUNT",
            message=(f"问题：增量图有 {len(starts)} 个 Start。"
                     "原因：流程必须恰好有 1 个 Start。"),
            elementIds=[node.id for node in starts],
        ))

    for node in new_nodes:
        if not incident[node.id]:
            issues.append(Issue(
                code="BATCH_UNCONNECTED_NODE",
                message=(f"问题：新增节点 {node.id} 没有连接。"
                         "原因：新增节点必须在当轮接入流程。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
    for node in candidate.nodes:
        if node.kind != "gateway" or (
            node.id in previous_node_ids and node.id not in touched_node_ids
        ):
            continue
        control_edges = outgoing[node.id] if node.role == "split" else incoming[node.id]
        if len(control_edges) < 2 and not final:
            direction = "出边" if node.role == "split" else "入边"
            issues.append(Issue(
                code="BATCH_GATEWAY_DEGREE",
                message=(f"问题：{node.role.title()} {node.id} 暂时只有 {len(control_edges)} 条{direction}。"
                         f"原因：最终至少需要 2 条{direction}，否则没有形成"
                         f"{'分叉' if node.role == 'split' else '汇合'}。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
                severity="warning",
            ))
    return issues


def _reachable_from(start: str, outgoing: dict[str, list[str]]) -> set[str]:
    seen = {start}
    queue = deque([start])
    while queue:
        for target in outgoing[queue.popleft()]:
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def open_nodes(graph: Graph) -> list[dict[str, Any]]:
    sources = {edge.source for edge in graph.edges}
    result: list[dict[str, Any]] = []
    for node in graph.nodes:
        if node.id in sources or (node.kind == "event" and node.eventType == "end"):
            continue
        item: dict[str, Any] = {
            "id": node.id,
            "name": node.name,
            "kind": node.kind,
            "sourceSentences": node.sourceSentences,
            "introducedInSegment": node.introducedInSegment,
        }
        if node.kind == "gateway":
            item.update({"gatewayType": node.gatewayType, "role": node.role})
        elif node.kind == "event":
            item.update({"eventType": node.eventType, "trigger": node.trigger})
        result.append(item)
    return result


def gateway_refs(graph: Graph) -> list[dict[str, str]]:
    """Return a compact, status-free index of every gateway in the committed graph."""
    return [{
        "id": node.id,
        "gatewayType": node.gatewayType,
        "role": node.role,
        "question": node.name,
    } for node in graph.nodes if node.kind == "gateway"]

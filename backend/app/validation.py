from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from .domain import Graph, Issue


ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{1,127}$")


def validate_graph(graph: Graph, final: bool = False) -> list[Issue]:
    issues: list[Issue] = []
    node_ids = [node.id for node in graph.nodes]
    edge_ids = [edge.id for edge in graph.edges]
    for value in node_ids + edge_ids:
        if not ID_PATTERN.fullmatch(value):
            issues.append(Issue(code="INVALID_ID", message=f"ID 不适合 BPMN XML: {value}", elementIds=[value]))
    for values, kind in ((node_ids, "node"), (edge_ids, "edge")):
        duplicates = {value for value in values if values.count(value) > 1}
        for value in duplicates:
            issues.append(Issue(code="DUPLICATE_ID", message=f"重复 {kind} ID: {value}", elementIds=[value]))
    node_map = graph.node_map()
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source not in node_map or edge.target not in node_map:
            issues.append(Issue(code="DANGLING_EDGE", message=f"边 {edge.id} 引用了不存在的节点", elementIds=[edge.id]))
            continue
        outgoing[edge.source].append(edge.target)
        incoming[edge.target].append(edge.source)
    starts = [node for node in graph.nodes if node.kind == "event" and node.eventType == "start"]
    ends = [node for node in graph.nodes if node.kind == "event" and node.eventType == "end"]
    for node in starts:
        if incoming[node.id]:
            issues.append(Issue(code="START_HAS_INCOMING", message="开始事件不能有入边", elementIds=[node.id]))
    for node in ends:
        if outgoing[node.id]:
            issues.append(Issue(code="END_HAS_OUTGOING", message="结束事件不能有出边", elementIds=[node.id]))
    for node in graph.nodes:
        if node.kind == "gateway":
            degree = len(outgoing[node.id]) if node.role == "split" else len(incoming[node.id])
            # Incremental snapshots may intentionally leave a gateway open for a
            # branch or join that is described in a later semantic segment.
            if final and degree < 2:
                issues.append(Issue(code="GATEWAY_DEGREE", message="已形成的分叉或汇合至少需要两条流", elementIds=[node.id]))
            if node.role == "split":
                defaults = sum(1 for edge in graph.edges if edge.source == node.id and edge.isDefault)
                if defaults > 1:
                    issues.append(Issue(code="MULTIPLE_DEFAULTS", message="分叉最多允许一条默认流", elementIds=[node.id]))
                if node.gatewayType == "parallel" and any(edge.condition for edge in graph.edges if edge.source == node.id):
                    issues.append(Issue(code="PARALLEL_CONDITION", message="并行网关的输出流不能携带条件", elementIds=[node.id]))
    if final:
        if len(starts) != 1:
            issues.append(Issue(code="START_COUNT", message="最终流程必须恰好有一个开始事件"))
        if not ends:
            issues.append(Issue(code="END_COUNT", message="最终流程至少需要一个结束事件"))
        reachable: set[str] = set()
        if starts:
            reachable = {node.id for node in starts}
            queue = deque(reachable)
            while queue:
                for target in outgoing[queue.popleft()]:
                    if target not in reachable:
                        reachable.add(target); queue.append(target)
            unreachable = [node.id for node in graph.nodes if node.id not in reachable]
            if unreachable:
                issues.append(Issue(code="UNREACHABLE", message="存在从开始事件不可达的节点", elementIds=unreachable))
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
                    message=(f"并行分叉 {split.id}（{split.name}）的所有分支没有共同可达的 "
                             "Parallel Join；请在共同后续开始前添加 parallel join 汇聚全部并行分支"),
                    elementIds=[split.id],
                    sourceSentences=split.sourceSentences,
                ))
        for node in graph.nodes:
            if node.id in reachable and not outgoing[node.id] and not (
                node.kind == "event" and node.eventType == "end"
            ):
                issues.append(Issue(
                    code="OPEN_NODE",
                    message=(f"可达叶子节点 {node.id}（{node.name}）不是结束事件；"
                             "请在该节点后连接 eventType=end 的 End Event"),
                    elementIds=[node.id],
                    sourceSentences=node.sourceSentences,
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
    return [{
        "id": node.id,
        "name": node.name,
        "kind": node.kind,
        "sourceSentences": node.sourceSentences,
        "introducedInSegment": node.introducedInSegment,
    } for node in graph.nodes
            if node.id not in sources and not (node.kind == "event" and node.eventType == "end")]

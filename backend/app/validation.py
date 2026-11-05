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
        start_edges = [edge for edge in graph.edges if edge.source == node.id]
        invalid_start_edges = [
            edge.id for edge in start_edges if edge.condition is not None or edge.isDefault
        ]
        if invalid_start_edges:
            issues.append(Issue(
                code="START_OUTGOING_CONDITION",
                message=(f"问题：Start {node.id} 的出边包含 condition 或 default。"
                         "原因：Start 的多条出边会并行启动，不能表达条件选择。"),
                elementIds=invalid_start_edges,
                sourceSentences=node.sourceSentences,
            ))
    for node in ends:
        if outgoing[node.id]:
            issues.append(Issue(
                code="END_HAS_OUTGOING",
                message=f"问题：End {node.id} 有出边。原因：End 是流程终点，不能有出边。",
                elementIds=[node.id],
            ))
    for node in graph.nodes:
        if node.kind != "event" or node.eventType != "intermediate":
            continue
        incoming_count = len(incoming[node.id])
        outgoing_count = len(outgoing[node.id])
        if incoming_count > 1:
            issues.append(Issue(
                code="INTERMEDIATE_EVENT_MULTIPLE_INCOMING",
                message=(f"问题：Intermediate Event {node.id} 有 {incoming_count} 条入边。"
                         "原因：本项目只允许 role=join Gateway 和 End Event 接收多条入边；"
                         "请先用符合路径激活语义的 Join 汇合，再以一条边连接该 Event。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if final and incoming_count == 0:
            issues.append(Issue(
                code="INTERMEDIATE_EVENT_MISSING_INCOMING",
                message=(f"问题：最终流程中的 Intermediate Event {node.id} 没有入边。"
                         "原因：Intermediate Event 必须恰好有一条入边。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if final and outgoing_count == 0:
            issues.append(Issue(
                code="INTERMEDIATE_EVENT_MISSING_OUTGOING",
                message=(f"问题：最终流程中的 Intermediate Event {node.id} 没有出边。"
                         "原因：Intermediate Event 必须恰好有一条出边。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
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
        if node.kind != "task":
            continue
        incoming_count = len(incoming[node.id])
        outgoing_count = len(outgoing[node.id])
        if incoming_count > 1:
            incoming_edges = [edge for edge in graph.edges if edge.target == node.id]
            connections = ", ".join(
                f"{edge.id}({edge.source}->{edge.target})" for edge in incoming_edges
            )
            reachable_after_task = _reachable_from(node.id, outgoing)
            loop_edges = [edge for edge in incoming_edges if edge.source in reachable_after_task]
            loop_detail = (
                " 其中循环回边为 "
                + ", ".join(f"{edge.id}({edge.source}->{edge.target})" for edge in loop_edges)
                + "；循环回边不得直接指向 Task，必须与首次进入边先汇入 Join。"
                if loop_edges else ""
            )
            issues.append(Issue(
                code="TASK_MULTIPLE_INCOMING",
                message=(f"问题：Task {node.id} 有 {incoming_count} 条入边。"
                         f"当前连接：{connections}。原因：本项目要求 Task 只有一条入边；"
                         "请在 Task 前增加由流程语义决定类型的 role=join Gateway，"
                         "将全部原入边的 target 原子地改接到该 Join，再新增 Join -> Task 的唯一入边。"
                         f"{loop_detail}"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if outgoing_count > 1:
            issues.append(Issue(
                code="TASK_MULTIPLE_OUTGOING",
                message=(f"问题：Task {node.id} 有 {outgoing_count} 条出边。"
                         "原因：本项目要求 Task 只有一条出边；请在 Task 后增加由流程语义决定类型的 "
                         "role=split Gateway，并将原出边改由该 Split 发出。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if final and incoming_count == 0:
            issues.append(Issue(
                code="TASK_MISSING_INCOMING",
                message=(f"问题：最终流程中的 Task {node.id} 没有入边。"
                         "原因：最终流程中的每个 Task 必须恰好有一条入边。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
        if final and outgoing_count == 0:
            issues.append(Issue(
                code="TASK_MISSING_OUTGOING",
                message=(f"问题：最终流程中的 Task {node.id} 没有出边。"
                         "原因：最终流程中的每个 Task 必须恰好有一条出边并连接后续节点或 End Event。"),
                elementIds=[node.id],
                sourceSentences=node.sourceSentences,
            ))
    for node in graph.nodes:
        if node.kind == "gateway":
            required_degrees = (
                [("出边", len(outgoing[node.id]), "分叉")]
                if node.role == "split" else
                [("入边", len(incoming[node.id]), "汇合")]
            )
            # Incremental snapshots may intentionally leave a gateway open for a
            # branch or join that is described in a later semantic segment.
            if final:
                for direction, degree, purpose in required_degrees:
                    if degree >= 2:
                        continue
                    control_edges = [
                        edge for edge in graph.edges
                        if (edge.source == node.id if direction == "出边" else edge.target == node.id)
                    ]
                    connections = ", ".join(
                        f"{edge.id}({edge.source}->{edge.target})" for edge in control_edges
                    ) or "无"
                    issues.append(Issue(
                        code="GATEWAY_DEGREE",
                        message=(f"问题：{node.role.title()} Gateway {node.id} 只有 {degree} 条{direction}："
                                 f"{connections}。原因：role={node.role} 至少需要 2 条{direction}，"
                                 f"否则没有形成{purpose}。"),
                        elementIds=[node.id],
                        sourceSentences=node.sourceSentences,
                    ))
            if node.role == "split":
                if len(incoming[node.id]) > 1:
                    issues.append(Issue(
                        code="SPLIT_MULTIPLE_INCOMING",
                        message=(f"问题：Split {node.id} 有 {len(incoming[node.id])} 条入边。"
                                 "原因：role=split 必须一入多出；请在该 Split 前增加一个与其 type 相同的 "
                                 "role=join Gateway，由 Join 接收原入边后再以一条边连接该 Split。"),
                        elementIds=[node.id],
                        sourceSentences=node.sourceSentences,
                    ))
                if final and not incoming[node.id]:
                    issues.append(Issue(
                        code="SPLIT_MISSING_INCOMING",
                        message=(f"问题：最终流程中的 Split {node.id} 没有入边。"
                                 "原因：role=split 必须恰好有一条入边。"),
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
                             "原因：role=join 必须多入一出；请在该 Join 后增加一个与其 type 相同的 "
                             "role=split Gateway，由 Join 以一条边连接该 Split，再由 Split 发出原出边。"),
                    elementIds=[node.id],
                    sourceSentences=node.sourceSentences,
                ))
            elif final and not outgoing[node.id]:
                issues.append(Issue(
                    code="JOIN_MISSING_OUTGOING",
                    message=(f"问题：最终流程中的 Join {node.id} 没有出边。"
                             "原因：role=join 必须恰好有一条出边。"),
                    elementIds=[node.id],
                    sourceSentences=node.sourceSentences,
                ))
            if node.role == "join":
                join_edges = [edge for edge in graph.edges if edge.source == node.id]
                conditional = [
                    edge.id for edge in join_edges if edge.condition is not None or edge.isDefault
                ]
                if conditional:
                    issues.append(Issue(
                        code="JOIN_CONDITIONAL_OUTGOING",
                        message=(f"问题：Join {node.id} 的出边包含 condition 或 default。"
                                 "原因：Join 只负责汇合，不负责分支判断；条件应位于后续 Split 的出边。"),
                        elementIds=conditional,
                        sourceSentences=node.sourceSentences,
                    ))
    issues.extend(_validate_gateway_pairing(graph, reachable, final))
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
        for node in graph.nodes:
            if node.id in reachable and not outgoing[node.id] and node.kind != "task" and not (
                node.kind == "event" and node.eventType == "end"
            ):
                issues.append(Issue(
                    code="OPEN_NODE",
                    message=(f"问题：叶子节点 {node.id} 不是 type=endEvent 的 End Event。"
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
        requirements = (
            [("出边", outgoing[node.id], "分叉")]
            if node.role == "split" else
            [("入边", incoming[node.id], "汇合")]
            if node.role == "join" else
            [("入边", incoming[node.id], "汇合"), ("出边", outgoing[node.id], "分叉")]
        )
        if not final:
            for direction, control_edges, purpose in requirements:
                if len(control_edges) >= 2:
                    continue
                issues.append(Issue(
                    code="BATCH_GATEWAY_DEGREE",
                    message=(f"问题：{node.role.title()} Gateway {node.id} 暂时只有 "
                             f"{len(control_edges)} 条{direction}。原因：最终至少需要 2 条{direction}，"
                             f"否则没有形成{purpose}。"),
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


def _validate_gateway_pairing(graph: Graph, reachable: set[str], final: bool) -> list[Issue]:
    """Pair structured joins with their controlling splits without crossing loop back-edges."""
    node_map = graph.node_map()
    valid_edges = [
        edge for edge in graph.edges
        if edge.source in node_map and edge.target in node_map
    ]
    predecessors: dict[str, list[str]] = defaultdict(list)
    for edge in valid_edges:
        predecessors[edge.target].append(edge.source)
    dominators = _dominators(reachable, predecessors, {
        node.id for node in graph.nodes
        if node.kind == "event" and node.eventType == "start" and node.id in reachable
    })
    back_edge_ids = {
        edge.id for edge in valid_edges
        if edge.source in reachable and edge.target in dominators.get(edge.source, set())
    }

    forward_edges = [edge for edge in valid_edges if edge.id not in back_edge_ids]
    forward_outgoing: dict[str, list[str]] = defaultdict(list)
    split_edges: dict[str, list[Any]] = defaultdict(list)
    join_incoming_edges: dict[str, list[Any]] = defaultdict(list)
    for edge in forward_edges:
        forward_outgoing[edge.source].append(edge.target)
        split_edges[edge.source].append(edge)
        join_incoming_edges[edge.target].append(edge)
    all_incoming_edges: dict[str, list[Any]] = defaultdict(list)
    for edge in valid_edges:
        all_incoming_edges[edge.target].append(edge)

    splits = [
        node for node in graph.nodes
        if node.id in reachable and node.kind == "gateway" and node.role == "split"
    ]
    joins = [
        node for node in graph.nodes
        if node.id in reachable and node.kind == "gateway" and node.role == "join"
    ]
    distance_cache: dict[str, dict[str, int]] = {}
    issues: list[Issue] = []
    paired_split_ids: set[str] = set()

    for join in joins:
        incoming_edges = all_incoming_edges[join.id]
        if len(incoming_edges) < 2:
            continue
        loop_returns = [edge for edge in incoming_edges if edge.id in back_edge_ids]
        if loop_returns:
            if join.gatewayType != "exclusive":
                issues.append(Issue(
                    code="LOOP_ENTRY_JOIN_TYPE_MISMATCH",
                    message=(f"问题：循环入口 Join {join.id} 的类型是 {join.gatewayType}。"
                             "原因：首次进入路径与循环回边属于不同执行轮次，"
                             "不会同时到达，必须使用 Exclusive Join 汇合。"),
                    elementIds=[join.id, *[edge.id for edge in loop_returns]],
                    sourceSentences=join.sourceSentences,
                ))
            continue

        forward_incoming = join_incoming_edges[join.id]
        if len(forward_incoming) < 2:
            continue
        candidates: list[tuple[tuple[int, int], Any, dict[str, str]]] = []
        for split in splits:
            branches = split_edges[split.id]
            if len(branches) < 2:
                continue
            arm_to_branch: dict[str, str] = {}
            arm_distances: list[int] = []
            valid_candidate = True
            for arm in forward_incoming:
                matches: list[tuple[str, int]] = []
                for branch in branches:
                    if arm.id == branch.id:
                        matches.append((branch.id, 0))
                        continue
                    distances = distance_cache.setdefault(
                        branch.id, _shortest_distances(branch.target, forward_outgoing)
                    )
                    if arm.source in distances:
                        matches.append((branch.id, distances[arm.source] + 1))
                if len(matches) != 1:
                    valid_candidate = False
                    break
                branch_id, distance = matches[0]
                arm_to_branch[arm.id] = branch_id
                arm_distances.append(distance)
            if not valid_candidate or len(set(arm_to_branch.values())) < 2:
                continue
            candidates.append(((max(arm_distances), sum(arm_distances)), split, arm_to_branch))

        if not candidates:
            if final:
                issues.append(Issue(
                    code="GATEWAY_PAIR_UNRESOLVED",
                    message=(f"问题：Join {join.id} 无法反向定位唯一的共同 Split。"
                             "原因：每条 Join 入边应分别来自同一个控制 Split 的不同分支；"
                             "若是循环入口，则必须能识别出回边。"),
                    elementIds=[join.id, *[edge.id for edge in forward_incoming]],
                    sourceSentences=join.sourceSentences,
                ))
            continue

        candidates.sort(key=lambda item: item[0])
        best_score = candidates[0][0]
        nearest = [item for item in candidates if item[0] == best_score]
        if len(nearest) != 1:
            if final:
                issues.append(Issue(
                    code="GATEWAY_PAIR_UNRESOLVED",
                    message=(f"问题：Join {join.id} 有多个同等距离的共同 Split："
                             f"{', '.join(item[1].id for item in nearest)}。"
                             "原因：无法唯一确定当前 Join 的控制范围。"),
                    elementIds=[join.id, *[item[1].id for item in nearest]],
                    sourceSentences=join.sourceSentences,
                ))
            continue

        _, split, arm_to_branch = nearest[0]
        paired_split_ids.add(split.id)
        if join.gatewayType != split.gatewayType:
            mapping = ", ".join(
                f"{arm_id}<-{branch_id}" for arm_id, branch_id in sorted(arm_to_branch.items())
            )
            issues.append(Issue(
                code="GATEWAY_JOIN_TYPE_MISMATCH",
                message=(f"问题：Join {join.id} 是 {join.gatewayType}Gateway，但其最近共同控制 "
                         f"Split {split.id} 是 {split.gatewayType}Gateway（{mapping}）。"
                         "原因：结构化控制块必须按分支激活语义配对："
                         "Parallel Split 对应 Parallel Join，Exclusive Split 对应 Exclusive Join，"
                         "Inclusive Split 对应 Inclusive Join。"),
                elementIds=[split.id, join.id, *sorted(arm_to_branch)],
                sourceSentences=sorted(set(split.sourceSentences + join.sourceSentences)),
            ))

    if final:
        for split in splits:
            if split.gatewayType != "parallel" or split.id in paired_split_ids:
                continue
            issues.append(Issue(
                code="PARALLEL_JOIN_MISSING",
                message=(f"问题：Parallel Split {split.id} 的分支没有配对的 Parallel Join。"
                         "原因：进入共同后续前，全部并行分支必须先由同一个 "
                         "Parallel Join 汇合。"),
                elementIds=[split.id],
                sourceSentences=split.sourceSentences,
            ))
    return issues


def _dominators(
    reachable: set[str], predecessors: dict[str, list[str]], roots: set[str],
) -> dict[str, set[str]]:
    if not reachable:
        return {}
    dominators = {
        node_id: ({node_id} if node_id in roots else set(reachable))
        for node_id in reachable
    }
    changed = True
    while changed:
        changed = False
        for node_id in sorted(reachable - roots):
            incoming = [item for item in predecessors[node_id] if item in reachable]
            if incoming:
                common = set.intersection(*(dominators[item] for item in incoming))
                updated = {node_id, *common}
            else:
                updated = {node_id}
            if updated != dominators[node_id]:
                dominators[node_id] = updated
                changed = True
    return dominators


def _shortest_distances(start: str, outgoing: dict[str, list[str]]) -> dict[str, int]:
    distances = {start: 0}
    queue = deque([start])
    while queue:
        source = queue.popleft()
        for target in outgoing[source]:
            if target in distances:
                continue
            distances[target] = distances[source] + 1
            queue.append(target)
    return distances


def open_nodes(graph: Graph) -> list[dict[str, Any]]:
    sources = {edge.source for edge in graph.edges}
    result: list[dict[str, Any]] = []
    for node in graph.nodes:
        if node.id in sources or (node.kind == "event" and node.eventType == "end"):
            continue
        item: dict[str, Any] = {
            "id": node.id,
            "name": node.name,
            "type": node.type,
            "sourceSentences": node.sourceSentences,
        }
        if node.kind == "gateway":
            item["role"] = node.role
        elif node.kind == "event":
            if node.eventDefinition:
                item["eventDefinition"] = node.eventDefinition
        result.append(item)
    return result


def gateway_refs(graph: Graph) -> list[dict[str, str]]:
    """Return a compact, status-free index of every gateway in the committed graph."""
    return [{
        "id": node.id,
        "type": node.type,
        "role": node.role,
        "question": node.name,
    } for node in graph.nodes if node.kind == "gateway"]

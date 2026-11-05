from __future__ import annotations

import tempfile
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.etree import ElementTree as ET


StringSimilarity = Callable[[str, str], float]


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _maximum_weight_pairs(weights: list[list[float]]) -> list[tuple[int, int, float]]:
    if not weights or not weights[0]:
        return []
    try:
        from scipy.optimize import linear_sum_assignment  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "optimal one-to-one alignment requires scipy; install the evaluation extra"
        ) from exc
    rows, columns = linear_sum_assignment(weights, maximize=True)
    return [(int(row), int(column), float(weights[row][column])) for row, column in zip(rows, columns)]


def _maximum_cardinality_threshold_pairs(
    weights: list[list[float]], threshold: float,
) -> list[tuple[int, int, float]]:
    """Maximize eligible pair count first, then their total similarity.

    Each generated Task can also be assigned to a private zero-weight dummy
    column.  Every eligible real edge receives a bonus larger than the maximum
    possible sum of cosine scores, so one additional eligible match always
    dominates any similarity-only gain among lower-cardinality assignments.
    """
    if not weights or not weights[0]:
        return []
    try:
        from scipy.optimize import linear_sum_assignment  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "thresholded one-to-one alignment requires scipy; install the evaluation extra"
        ) from exc
    generated_count = len(weights)
    reference_count = len(weights[0])
    cardinality_bonus = min(generated_count, reference_count) + 1.0
    forbidden = -cardinality_bonus
    objective = [
        [
            cardinality_bonus + float(score) if float(score) >= threshold else forbidden
            for score in row
        ]
        + [0.0] * generated_count
        for row in weights
    ]
    rows, columns = linear_sum_assignment(objective, maximize=True)
    pairs = [
        (int(row), int(column), float(weights[row][column]))
        for row, column in zip(rows, columns)
        if column < reference_count and float(weights[row][column]) >= threshold
    ]
    return sorted(pairs)


def calculate_task_alignment(
    generated: dict[str, Any],
    reference: dict[str, Any],
    similarity: StringSimilarity,
    threshold: float = 0.7,
    sensitivity_thresholds: tuple[float, ...] = (0.6, 0.7, 0.8),
) -> dict[str, Any]:
    """Align Task labels globally and report thresholded and continuous scores.

    The embedding model is used only to score labels. Generated labels are never
    rewritten. Unmatched Tasks contribute zero to the continuous alignment score.
    """
    generated_tasks = list(generated.get("tasks", []))
    reference_tasks = list(reference.get("tasks", []))
    weights = [
        [
            min(
                1.0,
                max(
                    0.0,
                    float(similarity(str(left.get("name", "")), str(right.get("name", "")))),
                ),
            )
            for right in reference_tasks
        ]
        for left in generated_tasks
    ]
    continuous_assignments = _maximum_weight_pairs(weights)
    continuous_pairs = [
        {
            "generatedIndex": left,
            "referenceIndex": right,
            "generatedId": generated_tasks[left]["id"],
            "referenceId": reference_tasks[right]["id"],
            "generatedName": str(generated_tasks[left].get("name", "")),
            "referenceName": str(reference_tasks[right].get("name", "")),
            "similarity": score,
            "matched": score >= threshold,
        }
        for left, right, score in continuous_assignments
    ]

    generated_count = len(generated_tasks)
    reference_count = len(reference_tasks)
    denominator = max(generated_count, reference_count)
    alignment_score = (
        sum(pair["similarity"] for pair in continuous_pairs) / denominator if denominator else 1.0
    )

    def scores_at(value: float) -> dict[str, Any]:
        assignments = _maximum_cardinality_threshold_pairs(weights, value)
        matched_pairs = [
            {
                "generatedIndex": left,
                "referenceIndex": right,
                "generatedId": generated_tasks[left]["id"],
                "referenceId": reference_tasks[right]["id"],
                "generatedName": str(generated_tasks[left].get("name", "")),
                "referenceName": str(reference_tasks[right].get("name", "")),
                "similarity": score,
            }
            for left, right, score in assignments
        ]
        count = len(matched_pairs)
        p = count / generated_count if generated_count else (1.0 if reference_count == 0 else 0.0)
        r = count / reference_count if reference_count else (1.0 if generated_count == 0 else 0.0)
        return {
            "threshold": value,
            "matchingProtocol": "maximum_cardinality_then_total_similarity",
            "matched": count,
            "precision": p,
            "recall": r,
            "f1": _f1(p, r),
            "totalMatchedSimilarity": sum(pair["similarity"] for pair in matched_pairs),
            "pairs": matched_pairs,
        }

    sensitivity = [
        scores_at(value) for value in sorted(set((*sensitivity_thresholds, threshold)))
    ]
    primary = next(item for item in sensitivity if item["threshold"] == threshold)
    matched = int(primary["matched"])
    precision = float(primary["precision"])
    recall = float(primary["recall"])

    return {
        "protocol": "continuous_optimal_and_threshold_cardinality_task_alignment_v2",
        "threshold": threshold,
        "generatedCount": generated_count,
        "referenceCount": reference_count,
        "generatedIds": [str(item["id"]) for item in generated_tasks],
        "referenceIds": [str(item["id"]) for item in reference_tasks],
        "matchedCount": matched,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "semanticAlignmentScore": alignment_score,
        "meanAssignedSimilarity": (
            sum(pair["similarity"] for pair in continuous_pairs) / len(continuous_pairs)
            if continuous_pairs else (1.0 if denominator == 0 else 0.0)
        ),
        "meanMatchedSimilarity": (
            float(primary["totalMatchedSimilarity"]) / matched if matched else 0.0
        ),
        "countAbsoluteError": abs(generated_count - reference_count),
        "pairs": continuous_pairs,
        "continuousPairs": continuous_pairs,
        "matchedPairs": primary["pairs"],
        "sensitivity": sensitivity,
    }


def calculate_pmo_task_name_dice(
    generated: dict[str, Any],
    reference: dict[str, Any],
    similarity: StringSimilarity,
    threshold: float = 0.7,
) -> dict[str, Any]:
    """Report PMo's official greedy semantic-frequency Task-name Dice."""
    from .evaluation import semantic_frequency_dice

    generated_tasks = list(generated.get("tasks", []))
    reference_tasks = list(reference.get("tasks", []))
    generated_names = [str(task.get("name", "")) for task in generated_tasks]
    reference_names = [str(task.get("name", "")) for task in reference_tasks]
    result = semantic_frequency_dice(reference_names, generated_names, similarity, threshold)
    pairs = [
        {
            "referenceIndex": reference_index,
            "generatedIndex": generated_index,
            "referenceId": reference_tasks[reference_index]["id"],
            "generatedId": generated_tasks[generated_index]["id"],
            "referenceName": reference_names[reference_index],
            "generatedName": generated_names[generated_index],
            "similarity": score,
        }
        for reference_index, generated_index, score in result.pairs
    ]
    return {
        "protocol": "PMo semantic-frequency-aware Task-name Dice",
        "threshold": threshold,
        "generatedCount": len(generated_tasks),
        "referenceCount": len(reference_tasks),
        "matchedCount": result.matched,
        "denominator": result.denominator,
        "score": result.score,
        "pairs": pairs,
    }


def _graph_index(pme: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, set[str]]]:
    nodes = {
        item["id"]: {**item, "kind": kind}
        for group, kind in (("tasks", "task"), ("events", "event"), ("gateways", "gateway"))
        for item in pme.get(group, [])
    }
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in pme.get("sequenceFlows", []):
        source, target = edge.get("sourceRef"), edge.get("targetRef")
        if source in nodes and target in nodes:
            outgoing[source].add(target)
            incoming[target].add(source)
    return nodes, outgoing, incoming


_GATEWAY_CATEGORIES = (
    "exclusive-split", "exclusive-join",
    "parallel-split", "parallel-join",
    "inclusive-split", "inclusive-join",
)


def _gateway_family(gateway: dict[str, Any]) -> str | None:
    value = str(gateway.get("type") or "").lower()
    for family in ("exclusive", "parallel", "inclusive"):
        if family in value:
            return family
    return None


def calculate_gateway_type_role_counts(
    generated: dict[str, Any], reference: dict[str, Any],
) -> dict[str, Any]:
    """Compare Gateway type/role multiplicities without Task-boundary matching.

    A BPMN Gateway with both multiple incoming and multiple outgoing flows is
    counted once as a join and once as a split.  This makes a reference mixed
    node comparable with the generated same-type ``join -> split`` expansion.
    """
    def counts(pme: dict[str, Any]) -> Counter[str]:
        _, outgoing, incoming = _graph_index(pme)
        result: Counter[str] = Counter()
        for gateway in pme.get("gateways", []):
            family = _gateway_family(gateway)
            if family is None:
                continue
            node_id = str(gateway.get("id"))
            roles: set[str] = set()
            if len(incoming.get(node_id, set())) > 1:
                roles.add("join")
            if len(outgoing.get(node_id, set())) > 1:
                roles.add("split")
            declared = str(gateway.get("role") or "")
            if not roles and declared in {"join", "split"}:
                roles.add(declared)
            for role in roles:
                result[f"{family}-{role}"] += 1
        return result

    generated_counts = counts(generated)
    reference_counts = counts(reference)
    rows = []
    matched_total = 0
    generated_total = 0
    reference_total = 0
    for category in _GATEWAY_CATEGORIES:
        generated_count = generated_counts[category]
        reference_count = reference_counts[category]
        matched = min(generated_count, reference_count)
        matched_total += matched
        generated_total += generated_count
        reference_total += reference_count
        rows.append({
            "category": category,
            "generatedCount": generated_count,
            "referenceCount": reference_count,
            "matchedCount": matched,
            "signedCountError": generated_count - reference_count,
            "absoluteCountError": abs(generated_count - reference_count),
        })
    precision = (
        matched_total / generated_total
        if generated_total else (1.0 if reference_total == 0 else 0.0)
    )
    recall = (
        matched_total / reference_total
        if reference_total else (1.0 if generated_total == 0 else 0.0)
    )
    return {
        "protocol": "gateway_type_role_multiset_v1",
        "categories": list(_GATEWAY_CATEGORIES),
        "generatedCount": generated_total,
        "referenceCount": reference_total,
        "matchedCount": matched_total,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "totalAbsoluteCountError": sum(row["absoluteCountError"] for row in rows),
        "counts": rows,
    }


def _reachable_nodes(start: str, outgoing: dict[str, set[str]]) -> set[str]:
    reached: set[str] = set()
    queue = deque(outgoing.get(start, set()))
    while queue:
        node_id = queue.popleft()
        if node_id in reached:
            continue
        reached.add(node_id)
        queue.extend(outgoing.get(node_id, set()) - reached)
    return reached


def _shortest_distances(start: str, outgoing: dict[str, set[str]]) -> dict[str, int]:
    distances = {start: 0}
    queue = deque([start])
    while queue:
        source = queue.popleft()
        for target in outgoing.get(source, set()):
            if target in distances:
                continue
            distances[target] = distances[source] + 1
            queue.append(target)
    return distances


def _gateway_relation_type(gateway: dict[str, Any]) -> str | None:
    value = str(gateway.get("type") or "").lower()
    if "exclusive" in value:
        return "exclusive"
    if "parallel" in value:
        return "parallel"
    if "inclusive" in value:
        return "inclusive"
    return None


def _structural_task_relation(
    left: str,
    right: str,
    nodes: dict[str, dict[str, Any]],
    outgoing: dict[str, set[str]],
    reachability: dict[str, set[str]],
) -> dict[str, Any]:
    """Classify a Task pair using directed reachability and its closest shared split."""
    left_to_right = right in reachability.get(left, set())
    right_to_left = left in reachability.get(right, set())
    if left_to_right and right_to_left:
        return {"relation": "cycle", "evidenceGatewayId": None}
    if left_to_right:
        return {"relation": "left-before-right", "evidenceGatewayId": None}
    if right_to_left:
        return {"relation": "right-before-left", "evidenceGatewayId": None}

    # A shared successor prevents an unrelated remote split from classifying two
    # disconnected Tasks as branches of the same control-flow region.
    if not (reachability.get(left, set()) & reachability.get(right, set())):
        return {"relation": "unclassified", "evidenceGatewayId": None}

    candidates: list[tuple[int, str, str]] = []
    for gateway_id, gateway in nodes.items():
        branches = sorted(outgoing.get(gateway_id, set()))
        if gateway.get("kind") != "gateway" or len(branches) < 2:
            continue
        branch_reach = [({branch} | reachability.get(branch, set())) for branch in branches]
        left_branches = {index for index, reached in enumerate(branch_reach) if left in reached}
        right_branches = {index for index, reached in enumerate(branch_reach) if right in reached}
        if not any(a != b for a in left_branches for b in right_branches):
            continue
        relation = _gateway_relation_type(gateway)
        if relation is None:
            continue
        distances = _shortest_distances(gateway_id, outgoing)
        candidates.append((distances.get(left, 10**9) + distances.get(right, 10**9), gateway_id, relation))
    if not candidates:
        return {"relation": "unclassified", "evidenceGatewayId": None}
    _, gateway_id, relation = min(candidates)
    return {"relation": relation, "evidenceGatewayId": gateway_id}


def calculate_control_flow_relations(
    generated: dict[str, Any],
    reference: dict[str, Any],
    task_alignment: dict[str, Any],
) -> dict[str, Any]:
    """Compare Task-pair control-flow relations after semantic Task alignment.

    Relations are structural rather than trace-sampled: mutual reachability is a
    cycle, one-way reachability is directed order, and otherwise the closest
    shared split classifies exclusive, parallel, or inclusive branches.
    """
    generated_nodes, generated_out, _ = _graph_index(generated)
    reference_nodes, reference_out, _ = _graph_index(reference)
    generated_reach = {
        task["id"]: _reachable_nodes(task["id"], generated_out)
        for task in generated.get("tasks", [])
    }
    reference_reach = {
        task["id"]: _reachable_nodes(task["id"], reference_out)
        for task in reference.get("tasks", [])
    }

    def score(matched_pairs: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
        aligned = sorted(
            ((str(pair["referenceId"]), str(pair["generatedId"])) for pair in matched_pairs),
            key=lambda item: item[0],
        )
        generated_relations: set[tuple[str, str, str]] = set()
        reference_relations: set[tuple[str, str, str]] = set()
        pair_rows: list[dict[str, Any]] = []
        for (reference_left, generated_left), (reference_right, generated_right) in combinations(aligned, 2):
            generated_relation = _structural_task_relation(
                generated_left, generated_right, generated_nodes, generated_out, generated_reach
            )
            reference_relation = _structural_task_relation(
                reference_left, reference_right, reference_nodes, reference_out, reference_reach
            )
            generated_label = generated_relation["relation"]
            reference_label = reference_relation["relation"]
            key = (reference_left, reference_right)
            if generated_label != "unclassified":
                generated_relations.add((*key, generated_label))
            if reference_label != "unclassified":
                reference_relations.add((*key, reference_label))
            pair_rows.append({
                "referenceTaskIds": [reference_left, reference_right],
                "generatedTaskIds": [generated_left, generated_right],
                "generatedRelation": generated_label,
                "referenceRelation": reference_label,
                "matched": generated_label == reference_label and reference_label != "unclassified",
                "generatedEvidenceGatewayId": generated_relation["evidenceGatewayId"],
                "referenceEvidenceGatewayId": reference_relation["evidenceGatewayId"],
            })
        correct = generated_relations & reference_relations
        precision = (
            len(correct) / len(generated_relations)
            if generated_relations else (1.0 if not reference_relations else 0.0)
        )
        recall = (
            len(correct) / len(reference_relations)
            if reference_relations else (1.0 if not generated_relations else 0.0)
        )
        matched_count = len(aligned)
        matched_pair_count = matched_count * (matched_count - 1) // 2
        reference_count = len(reference.get("tasks", []))
        reference_pair_count = reference_count * (reference_count - 1) // 2
        return {
            "threshold": threshold,
            "matchedTaskCount": matched_count,
            "matchedTaskPairCount": matched_pair_count,
            "referenceTaskPairCount": reference_pair_count,
            "matchedTaskPairCoverage": (
                matched_pair_count / reference_pair_count if reference_pair_count else 1.0
            ),
            "generatedRelationCount": len(generated_relations),
            "referenceRelationCount": len(reference_relations),
            "matchedRelationCount": len(correct),
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "pairs": pair_rows,
        }

    sensitivity = [
        score(list(item.get("pairs", [])), float(item["threshold"]))
        for item in task_alignment.get("sensitivity", [])
    ]
    primary_threshold = float(task_alignment.get("threshold", 0.7))
    primary = next(
        (item for item in sensitivity if item["threshold"] == primary_threshold),
        score(list(task_alignment.get("matchedPairs", [])), primary_threshold),
    )
    return {
        "protocol": "matched_task_control_flow_relation_v1",
        "relationTypes": [
            "left-before-right", "right-before-left", "exclusive", "parallel", "inclusive", "cycle",
        ],
        **primary,
        "sensitivity": sensitivity,
    }


def calculate_global_control_flow_relations(
    generated: dict[str, Any],
    reference: dict[str, Any],
    task_alignment: dict[str, Any],
) -> dict[str, Any]:
    """Compare relations over every classifiable Task pair in both graphs.

    Unlike :func:`calculate_control_flow_relations`, whose denominators contain
    only semantically matched Task pairs, this metric retains the complete
    relation inventories.  A generated relation is correct only when both of
    its endpoint Tasks are aligned one-to-one and the corresponding reference
    pair has the same relation.  Relations involving an extra generated Task
    therefore remain in the precision denominator, while relations involving
    a missing reference Task remain in the recall denominator.
    """
    generated_nodes, generated_out, _ = _graph_index(generated)
    reference_nodes, reference_out, _ = _graph_index(reference)
    generated_task_ids = sorted(str(task["id"]) for task in generated.get("tasks", []))
    reference_task_ids = sorted(str(task["id"]) for task in reference.get("tasks", []))
    generated_reach = {
        task_id: _reachable_nodes(task_id, generated_out)
        for task_id in generated_task_ids
    }
    reference_reach = {
        task_id: _reachable_nodes(task_id, reference_out)
        for task_id in reference_task_ids
    }

    reference_relations: dict[tuple[str, str], dict[str, Any]] = {}
    for left, right in combinations(reference_task_ids, 2):
        relation = _structural_task_relation(
            left, right, reference_nodes, reference_out, reference_reach
        )
        if relation["relation"] != "unclassified":
            reference_relations[(left, right)] = relation

    def score(matched_pairs: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
        generated_to_reference = {
            str(pair["generatedId"]): str(pair["referenceId"])
            for pair in matched_pairs
        }
        generated_relation_count = 0
        correct_count = 0
        generated_rows: list[dict[str, Any]] = []
        for generated_a, generated_b in combinations(generated_task_ids, 2):
            reference_a = generated_to_reference.get(generated_a)
            reference_b = generated_to_reference.get(generated_b)

            # When both Tasks are aligned, orient the generated pair using the
            # canonical reference-ID order so directed-order labels are
            # directly comparable.  Unaligned pairs still count as generated
            # relations but can never be true positives.
            if reference_a is not None and reference_b is not None and reference_b < reference_a:
                generated_left, generated_right = generated_b, generated_a
                reference_left, reference_right = reference_b, reference_a
            else:
                generated_left, generated_right = generated_a, generated_b
                reference_left, reference_right = reference_a, reference_b

            generated_relation = _structural_task_relation(
                generated_left,
                generated_right,
                generated_nodes,
                generated_out,
                generated_reach,
            )
            generated_label = generated_relation["relation"]
            if generated_label == "unclassified":
                continue
            generated_relation_count += 1

            reference_relation = None
            if reference_left is not None and reference_right is not None:
                reference_relation = reference_relations.get((reference_left, reference_right))
            reference_label = (
                reference_relation["relation"] if reference_relation is not None else "unclassified"
            )
            matched = generated_label == reference_label and reference_label != "unclassified"
            if matched:
                correct_count += 1
            generated_rows.append({
                "generatedTaskIds": [generated_left, generated_right],
                "referenceTaskIds": (
                    [reference_left, reference_right]
                    if reference_left is not None and reference_right is not None
                    else None
                ),
                "generatedRelation": generated_label,
                "referenceRelation": reference_label,
                "matched": matched,
                "generatedEvidenceGatewayId": generated_relation["evidenceGatewayId"],
                "referenceEvidenceGatewayId": (
                    reference_relation["evidenceGatewayId"]
                    if reference_relation is not None else None
                ),
            })

        reference_relation_count = len(reference_relations)
        precision = (
            correct_count / generated_relation_count
            if generated_relation_count
            else (1.0 if reference_relation_count == 0 else 0.0)
        )
        recall = (
            correct_count / reference_relation_count
            if reference_relation_count
            else (1.0 if generated_relation_count == 0 else 0.0)
        )
        return {
            "threshold": threshold,
            "matchedTaskCount": len(generated_to_reference),
            "generatedTaskCount": len(generated_task_ids),
            "referenceTaskCount": len(reference_task_ids),
            "generatedRelationCount": generated_relation_count,
            "referenceRelationCount": reference_relation_count,
            "matchedRelationCount": correct_count,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "generatedRelations": generated_rows,
        }

    sensitivity = [
        score(list(item.get("pairs", [])), float(item["threshold"]))
        for item in task_alignment.get("sensitivity", [])
    ]
    primary_threshold = float(task_alignment.get("threshold", 0.7))
    primary = next(
        (item for item in sensitivity if item["threshold"] == primary_threshold),
        score(list(task_alignment.get("matchedPairs", [])), primary_threshold),
    )
    return {
        "protocol": "global_task_control_flow_relation_v1",
        "relationTypes": [
            "left-before-right", "right-before-left", "exclusive", "parallel", "inclusive", "cycle",
        ],
        **primary,
        "sensitivity": sensitivity,
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_INTERMEDIATE_EVENT_TAGS = {"intermediateCatchEvent", "intermediateThrowEvent"}


def _normalize_bpmn_for_pm4py(xml: str) -> tuple[str, dict[str, Any]]:
    """Adapt the Graph-JSON event subset to PM4Py's BPMN converter.

    A Graph-JSON Start Event may have multiple unconditional outgoing flows;
    the project semantics start all of them in parallel.  PM4Py's converter
    instead maps a multi-out Start to one source place competing for several
    transitions, which makes the branches mutually exclusive.  Insert a
    silent Parallel Gateway after such a Start before conversion.

    PM4Py imports intermediate catch/throw events but its BPMN-to-Petri-net
    converter does not create Petri-net nodes for them.  Consequently, both
    sequence flows incident to such an event are dropped and a connected BPMN
    process becomes several Petri-net components.  For control-flow soundness
    and task-only conformance, intermediate Events are replaced by silent
    gateway structures that preserve BPMN's uncontrolled-flow semantics:
    multiple incoming flows are a simple (exclusive) merge and multiple
    outgoing flows create parallel paths.

    The original BPMN is never modified.  Event trigger/correlation semantics
    (message, timer, signal, link, conditional, escalation, compensation) are
    outside the single-process Graph-JSON control-flow abstraction.
    """
    root = ET.fromstring(xml)
    processes = [element for element in root.iter() if _local_name(element.tag) == "process"]
    elements = [
        element
        for process in processes
        for element in process.iter()
        if element is not process
    ]

    flow_node_tags = {
        "task", "userTask", "serviceTask", "manualTask", "sendTask", "receiveTask",
        "businessRuleTask", "scriptTask", "startEvent", "endEvent",
        "intermediateCatchEvent", "intermediateThrowEvent", "exclusiveGateway",
        "parallelGateway", "inclusiveGateway",
    }
    nodes = {
        str(element.get("id")): element
        for element in elements
        if _local_name(element.tag) in flow_node_tags and element.get("id")
    }
    flows = [element for element in elements if _local_name(element.tag) == "sequenceFlow"]
    incoming: dict[str, int] = defaultdict(int)
    outgoing: dict[str, int] = defaultdict(int)
    for flow in flows:
        outgoing[str(flow.get("sourceRef"))] += 1
        incoming[str(flow.get("targetRef"))] += 1

    parents = {child: parent for parent in root.iter() for child in list(parent)}
    existing_ids = {str(element.get("id")) for element in root.iter() if element.get("id")}

    def unique_id(base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in existing_ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        existing_ids.add(candidate)
        return candidate

    normalized_starts: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        if _local_name(node.tag) != "startEvent" or outgoing[node_id] <= 1:
            continue
        namespace = node.tag.rsplit("}", 1)[0] + "}" if "}" in node.tag else ""
        parent = parents[node]
        split_id = unique_id(f"{node_id}__pm4py_parallel_split")
        split = ET.Element(
            f"{namespace}parallelGateway",
            {"id": split_id, "name": "", "gatewayDirection": "Diverging"},
        )
        parent.append(split)
        outgoing_flows = [flow for flow in flows if str(flow.get("sourceRef")) == node_id]
        for flow in outgoing_flows:
            flow.set("sourceRef", split_id)
        bridge_id = unique_id(f"{node_id}__pm4py_bridge")
        parent.append(ET.Element(
            f"{namespace}sequenceFlow",
            {"id": bridge_id, "sourceRef": node_id, "targetRef": split_id},
        ))
        normalized_starts.append({
            "id": node_id,
            "outgoing": outgoing[node_id],
            "insertedParallelSplit": split_id,
            "petriAbstraction": "start followed by silent parallel split",
        })

    normalized_events: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        node_type = _local_name(node.tag)
        if node_type not in _INTERMEDIATE_EVENT_TAGS:
            continue
        if incoming[node_id] < 1 or outgoing[node_id] < 1:
            raise ValueError(
                f"intermediate event {node_id} requires at least one incoming and one outgoing "
                f"sequenceFlow for soundness abstraction; found {incoming[node_id]} incoming "
                f"and {outgoing[node_id]} outgoing"
            )
        definitions = [
            _local_name(child.tag).removesuffix("EventDefinition")
            for child in list(node)
            if _local_name(child.tag).endswith("EventDefinition")
        ]
        namespace = node.tag.rsplit("}", 1)[0] + "}" if "}" in node.tag else ""
        for child in list(node):
            if _local_name(child.tag).endswith("EventDefinition"):
                node.remove(child)
        node.attrib.pop("default", None)
        if outgoing[node_id] == 1:
            node.tag = f"{namespace}exclusiveGateway"
            node.set("gatewayDirection", "Converging" if incoming[node_id] > 1 else "Unspecified")
            abstraction = "silent pass-through transition" if incoming[node_id] == 1 else "silent simple merge"
        elif incoming[node_id] == 1:
            node.tag = f"{namespace}parallelGateway"
            node.set("gatewayDirection", "Diverging")
            abstraction = "silent parallel split"
        else:
            parent = parents[node]
            node.tag = f"{namespace}exclusiveGateway"
            node.set("gatewayDirection", "Converging")
            split_id = f"{node_id}__pm4py_parallel_split"
            split_id = unique_id(split_id)
            split = ET.Element(
                f"{namespace}parallelGateway",
                {"id": split_id, "name": "", "gatewayDirection": "Diverging"},
            )
            parent.append(split)
            outgoing_flows = [
                flow for flow in flows if str(flow.get("sourceRef")) == node_id
            ]
            for flow in outgoing_flows:
                flow.set("sourceRef", split_id)
            bridge_id = unique_id(f"{node_id}__pm4py_bridge")
            parent.append(ET.Element(
                f"{namespace}sequenceFlow",
                {"id": bridge_id, "sourceRef": node_id, "targetRef": split_id},
            ))
            abstraction = "silent simple merge followed by parallel split"
        normalized_events.append({
            "id": node_id,
            "originalType": node_type,
            "eventDefinitions": definitions or ["none"],
            "incoming": incoming[node_id],
            "outgoing": outgoing[node_id],
            "petriAbstraction": abstraction,
        })

    normalized_xml = ET.tostring(root, encoding="unicode")
    return normalized_xml, {
        "protocol": "bpmn_event_control_flow_abstraction_v3",
        "normalizedStartEvents": normalized_starts,
        "normalizedIntermediateEvents": normalized_events,
        "eventSemanticsScope": (
            "single-process token flow; multi-out Start and intermediate-event control-flow "
            "multiplicity are preserved, "
            "but external trigger/correlation and special end-event effects are not modeled"
        ),
    }


@contextmanager
def _temporary_bpmn(xml: str) -> Iterator[Path]:
    handle = tempfile.NamedTemporaryFile("w", suffix=".bpmn", encoding="utf-8", delete=False)
    try:
        with handle:
            handle.write(xml)
        yield Path(handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _load_petri_net(xml: str):
    try:
        import pm4py  # type: ignore
    except ImportError as exc:
        raise RuntimeError("soundness assessment requires pm4py") from exc
    normalized_xml, conversion = _normalize_bpmn_for_pm4py(xml)
    with _temporary_bpmn(normalized_xml) as path:
        bpmn_graph = pm4py.read_bpmn(str(path))
    net, initial_marking, final_marking = pm4py.convert_to_petri_net(bpmn_graph)
    return net, initial_marking, final_marking, conversion


def assess_soundness(xml: str) -> dict[str, Any]:
    try:
        from pm4py.algo.analysis.woflan import algorithm as woflan  # type: ignore
        net, initial_marking, final_marking, conversion = _load_petri_net(xml)
        sound, diagnostics = woflan.apply(
            net,
            initial_marking,
            final_marking,
            parameters={
                "return_asap_when_not_sound": True,
                "return_diagnostics": True,
                "print_diagnostics": False,
            },
        )
        return {
            "protocol": "pm4py_workflow_net_soundness_with_event_abstraction_v4",
            "calculationSucceeded": True,
            "sound": bool(sound),
            "diagnostics": _json_safe(diagnostics),
            "conversion": conversion,
        }
    except Exception as exc:
        return {
            "protocol": "pm4py_workflow_net_soundness_with_event_abstraction_v4",
            "calculationSucceeded": False,
            "sound": False,
            "reason": str(exc),
            "errorType": type(exc).__name__,
        }


def calculate_process_quality(
    generated_graph: dict[str, Any],
    reference_graph: dict[str, Any],
    generated_bpmn_xml: str,
    similarity: StringSimilarity,
    task_threshold: float = 0.7,
) -> dict[str, Any]:
    """Calculate the retained structure, element and relation metric set."""
    from .evaluation import graph_to_pme

    generated_pme = graph_to_pme(generated_graph)
    task = calculate_task_alignment(generated_pme, reference_graph, similarity, task_threshold)
    pmo_task_name = calculate_pmo_task_name_dice(
        generated_pme, reference_graph, similarity, threshold=0.7
    )
    gateway = calculate_gateway_type_role_counts(generated_pme, reference_graph)
    control_flow = calculate_control_flow_relations(generated_pme, reference_graph, task)
    global_control_flow = calculate_global_control_flow_relations(
        generated_pme, reference_graph, task
    )
    soundness = assess_soundness(generated_bpmn_xml)
    return {
        "taskAlignment": task,
        "pmoTaskNameDice": pmo_task_name,
        "gatewayTypeRoleCounts": gateway,
        "controlFlowRelations": control_flow,
        "conditionalControlFlowRelations": control_flow,
        "globalControlFlowRelations": global_control_flow,
        "soundness": soundness,
        "taskSemanticAlignmentScore": task["semanticAlignmentScore"],
        "pmoTaskNameDiceAt07": pmo_task_name["score"],
        "taskPrecision": task["precision"],
        "taskRecall": task["recall"],
        "taskF1": task["f1"],
        "gatewayTypeRolePrecision": gateway["precision"],
        "gatewayTypeRoleRecall": gateway["recall"],
        "gatewayTypeRoleF1": gateway["f1"],
        "gatewayTypeRoleAbsoluteCountError": gateway["totalAbsoluteCountError"],
        "controlFlowRelationPrecision": control_flow["precision"],
        "controlFlowRelationRecall": control_flow["recall"],
        "controlFlowRelationF1": control_flow["f1"],
        "controlFlowRelationMatchedTaskPairCoverage": control_flow["matchedTaskPairCoverage"],
        "conditionalControlFlowRelationPrecision": control_flow["precision"],
        "conditionalControlFlowRelationRecall": control_flow["recall"],
        "conditionalControlFlowRelationF1": control_flow["f1"],
        "globalControlFlowRelationPrecision": global_control_flow["precision"],
        "globalControlFlowRelationRecall": global_control_flow["recall"],
        "globalControlFlowRelationF1": global_control_flow["f1"],
        "sound": soundness.get("sound"),
    }

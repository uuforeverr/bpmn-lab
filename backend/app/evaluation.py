from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from statistics import mean, quantiles
from typing import Any, Callable, Iterable
from xml.etree import ElementTree as ET


StringSimilarity = Callable[[str, str], float]


_BPMN_TASK_TYPES = {
    "task": "Task",
    "userTask": "User",
    "serviceTask": "Service",
    "sendTask": "Send",
    "receiveTask": "Receive",
    "businessRuleTask": "BusinessRule",
    "manualTask": "Manual",
    "scriptTask": "Script",
}
_BPMN_GATEWAY_TYPES = {
    "exclusiveGateway": "ExclusiveGateway",
    "parallelGateway": "ParallelGateway",
    "inclusiveGateway": "InclusiveGateway",
}
_BPMN_EVENT_TYPES = {
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
}
_SUPPORTED_EVENT_DEFINITIONS = {
    "startEvent": {"none", "message", "timer", "signal", "conditional"},
    "endEvent": {
        "none", "message", "signal", "error", "escalation", "terminate", "compensation",
    },
    "intermediateCatchEvent": {"none", "message", "timer", "signal", "link", "conditional"},
    "intermediateThrowEvent": {"none", "message", "signal", "link", "escalation", "compensation"},
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _event_definition(element: ET.Element) -> str:
    for child in element:
        name = _local_name(child.tag)
        if name.endswith("EventDefinition"):
            return name.removesuffix("EventDefinition")
    return "none"


def _reference_event_type(node_type: str, definition: str) -> str:
    if node_type == "startEvent":
        return "StartNoneEvent" if definition == "none" else f"Start{definition.title()}Event"
    if node_type == "endEvent":
        return "EndNoneEvent" if definition == "none" else f"End{definition.title()}Event"
    if definition == "timer":
        return "IntermediateTimerEvent"
    if definition == "conditional":
        return "IntermediateConditionalEvent"
    if definition == "message":
        suffix = "Catching" if node_type == "intermediateCatchEvent" else "Throwing"
        return f"IntermediateMessageEvent{suffix}"
    suffix = "Catching" if node_type == "intermediateCatchEvent" else "Throwing"
    return f"Intermediate{definition.title()}Event{suffix}"


def bpmn_to_reference(xml: str) -> dict[str, Any]:
    """Parse the reference BPMN into the graph shape used by the evaluators.

    Formal structural eligibility and complexity are derived from BPMN itself.
    The returned Task names use the same values as the PMo PME files, but do not
    rely on PME/BPMN identifiers being equal.
    """
    root = ET.fromstring(xml)
    result: dict[str, Any] = {
        "tasks": [], "events": [], "gateways": [], "pools": [],
        "sequenceFlows": [], "messageFlows": [],
    }
    for element in root.iter():
        node_type = _local_name(element.tag)
        element_id = element.get("id")
        if node_type in _BPMN_TASK_TYPES and element_id:
            result["tasks"].append({
                "id": element_id,
                "name": str(element.get("name") or ""),
                "type": _BPMN_TASK_TYPES[node_type],
            })
        elif node_type in _BPMN_EVENT_TYPES and element_id:
            definition = _event_definition(element)
            result["events"].append({
                "id": element_id,
                "name": str(element.get("name") or ""),
                "type": _reference_event_type(node_type, definition),
                "bpmnType": node_type,
                "eventDefinition": definition,
            })
        elif node_type in _BPMN_GATEWAY_TYPES and element_id:
            result["gateways"].append({
                "id": element_id,
                "name": str(element.get("name") or ""),
                "type": _BPMN_GATEWAY_TYPES[node_type],
            })
        elif node_type == "sequenceFlow" and element_id:
            result["sequenceFlows"].append({
                "id": element_id,
                "sourceRef": str(element.get("sourceRef") or ""),
                "targetRef": str(element.get("targetRef") or ""),
                "condition": str(element.get("name") or ""),
            })
        elif node_type == "messageFlow" and element_id:
            result["messageFlows"].append({
                "id": element_id,
                "sourceRef": str(element.get("sourceRef") or ""),
                "targetRef": str(element.get("targetRef") or ""),
                "message": str(element.get("name") or ""),
            })
        elif node_type == "participant" and element_id:
            result["pools"].append({
                "id": element_id,
                "name": str(element.get("name") or ""),
                "processRef": str(element.get("processRef") or ""),
            })
    return result


def assess_bpmn_reference_scope(xml: str) -> dict[str, Any]:
    """Decide whether a reference BPMN is within the current Graph-JSON scope."""
    root = ET.fromstring(xml)
    elements = list(root.iter())
    tags = [_local_name(element.tag) for element in elements]
    process_count = tags.count("process")
    participants = tags.count("participant")
    message_flows = tags.count("messageFlow")
    starts = tags.count("startEvent")
    ends = tags.count("endEvent")
    unsupported_gateways = sorted({
        tag for tag in tags if tag.endswith("Gateway") and tag not in _BPMN_GATEWAY_TYPES
    })
    unsupported_flow_nodes = sorted({
        tag for tag in tags if tag in {
            "boundaryEvent", "subProcess", "adHocSubProcess", "transaction", "callActivity",
        }
    })
    unsupported_events: list[str] = []
    for element in elements:
        node_type = _local_name(element.tag)
        if node_type not in _BPMN_EVENT_TYPES:
            continue
        definition = _event_definition(element)
        if definition not in _SUPPORTED_EVENT_DEFINITIONS[node_type]:
            unsupported_events.append(f"{node_type}:{definition}")

    reasons: list[str] = []
    if process_count != 1:
        reasons.append(f"process_count={process_count}")
    if participants > 1:
        reasons.append(f"participant_count={participants}")
    if message_flows:
        reasons.append(f"message_flow_count={message_flows}")
    if starts != 1:
        reasons.append(f"start_event_count={starts}")
    if ends < 1:
        reasons.append("end_event_count=0")
    if unsupported_gateways:
        reasons.append("unsupported_gateways=" + ",".join(unsupported_gateways))
    if unsupported_flow_nodes:
        reasons.append("unsupported_flow_nodes=" + ",".join(unsupported_flow_nodes))
    if unsupported_events:
        reasons.append("unsupported_events=" + ",".join(sorted(set(unsupported_events))))
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "processCount": process_count,
        "participantCount": participants,
        "messageFlowCount": message_flows,
        "startEventCount": starts,
        "endEventCount": ends,
    }


def aggregate_token_usage(calls: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate OpenAI Chat Completions and Responses-style usage fields.

    Every recorded response is included, including validation retries. Transport failures
    cannot be counted because providers do not return usage for them.
    """

    def value(usage: dict[str, Any], *names: str) -> int:
        for name in names:
            raw = usage.get(name)
            if isinstance(raw, (int, float)):
                return int(raw)
        return 0

    def nested(usage: dict[str, Any], parents: tuple[str, ...], name: str) -> int:
        for parent in parents:
            details = usage.get(parent)
            if isinstance(details, dict) and isinstance(details.get(name), (int, float)):
                return int(details[name])
        return 0

    totals = {
        "calls": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "cachedInputTokens": 0,
        "reasoningTokens": 0,
        "latencyMs": 0,
    }
    by_agent: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}

    for call in calls:
        usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
        input_tokens = value(usage, "prompt_tokens", "input_tokens")
        output_tokens = value(usage, "completion_tokens", "output_tokens")
        total_tokens = value(usage, "total_tokens") or input_tokens + output_tokens
        row = {
            "calls": 1,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
            "cachedInputTokens": nested(
                usage, ("prompt_tokens_details", "input_tokens_details"), "cached_tokens"
            ),
            "reasoningTokens": nested(
                usage, ("completion_tokens_details", "output_tokens_details"), "reasoning_tokens"
            ),
            "latencyMs": int(call.get("latencyMs") or 0),
        }
        for key, amount in row.items():
            totals[key] += amount

        for target, label in (
            (by_agent, str(call.get("agent") or "unknown")),
            (by_model, str(call.get("model") or "unknown")),
        ):
            bucket = target.setdefault(label, {key: 0 for key in totals})
            for key, amount in row.items():
                bucket[key] += amount

    return {**totals, "byAgent": by_agent, "byModel": by_model}


class SentenceTransformerSimilarity:
    """Lazy official PMo text similarity backend."""

    def __init__(
        self, model_name: str = "sentence-transformers/stsb-mpnet-base-v2",
        local_files_only: bool = False,
    ):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PME quality metrics require the optional evaluation dependency: "
                "pip install -e '.[evaluation]'"
            ) from exc
        self.model_name = model_name
        self.model = SentenceTransformer(model_name, local_files_only=local_files_only)
        self.cache: dict[str, Any] = {}

    def __call__(self, left: str, right: str) -> float:
        if left == right:
            return 1.0
        import torch  # type: ignore

        def encode(value: str):
            if value not in self.cache:
                self.cache[value] = self.model.encode(value, convert_to_tensor=True)
            return self.cache[value]

        return float(torch.nn.functional.cosine_similarity(
            encode(left).reshape(1, -1), encode(right).reshape(1, -1)
        ).item())


@dataclass
class ListSimilarity:
    score: float
    denominator: int
    matched: int
    pairs: list[tuple[int, int, float]]


def semantic_frequency_dice(
    left: list[str], right: list[str], similarity: StringSimilarity, threshold: float = 0.7
) -> ListSimilarity:
    """Reproduce PMo's greedy, semantic- and frequency-aware Dice protocol."""
    candidates = [
        (float(similarity(left_value, right_value)), left_index, right_index)
        for left_index, left_value in enumerate(left)
        for right_index, right_value in enumerate(right)
    ]
    # PMo uses a stable descending sort by score; insertion order resolves ties.
    candidates.sort(key=lambda item: item[0], reverse=True)
    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    adjusted_right = list(right)
    for score, left_index, right_index in candidates:
        if score < threshold:
            break
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        adjusted_right[right_index] = left[left_index]
        pairs.append((left_index, right_index, score))

    def frequency_index(values: list[str]) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter()
        indexed: list[tuple[str, int]] = []
        for item in values:
            indexed.append((item, counts[item]))
            counts[item] += 1
        return indexed

    left_indexed = set(frequency_index(left))
    right_indexed = set(frequency_index(adjusted_right))
    denominator = len(left_indexed) + len(right_indexed)
    matched = len(left_indexed & right_indexed)
    score = 1.0 if denominator == 0 else 2 * matched / denominator
    return ListSimilarity(score, denominator, matched, pairs)


def _weighted(parts: Iterable[ListSimilarity]) -> float:
    rows = list(parts)
    denominator = sum(row.denominator for row in rows)
    return 0.0 if denominator == 0 else sum(row.score * row.denominator for row in rows) / denominator


def _nonempty_values(pme: dict[str, Any], group: str, attribute: str) -> list[str]:
    return [str(item[attribute]) for item in pme.get(group, []) if item.get(attribute)]


def _flow_strings(pme: dict[str, Any], group: str, middle: str) -> list[str]:
    values: dict[str, str] = {}
    for item in [*pme.get("tasks", []), *pme.get("events", []), *pme.get("pools", [])]:
        values[item.get("id", "")] = str(item.get("name", ""))
    for item in pme.get("gateways", []):
        values[item.get("id", "")] = str(item.get("type", ""))
    return [
        " ".join((
            values.get(flow.get("sourceRef", ""), ""),
            str(flow.get(middle, "")),
            values.get(flow.get("targetRef", ""), ""),
        ))
        for flow in pme.get(group, [])
    ]


def _lane_lists(pme: dict[str, Any]) -> tuple[list[str], list[str]]:
    values: dict[str, str] = {}
    for item in [*pme.get("tasks", []), *pme.get("events", [])]:
        values[item.get("id", "")] = str(item.get("name", ""))
    for item in pme.get("gateways", []):
        values[item.get("id", "")] = str(item.get("type", ""))
    names: list[str] = []
    refs: list[str] = []
    for pool in pme.get("pools", []):
        lanes = pool.get("lanes", [])
        if not lanes:
            names.append(str(pool.get("name", "")))
        for lane in lanes:
            lane_name = f"{pool.get('name', '')} - {lane.get('name', '')}"
            names.append(lane_name)
            refs.extend(f"{lane_name} - {values.get(ref, '')}" for ref in lane.get("elemRefs", []))
    return names, refs


def calculate_pme_similarity(
    generated: dict[str, Any], reference: dict[str, Any], similarity: StringSimilarity,
    threshold: float = 0.7,
) -> dict[str, Any]:
    """Calculate the official PMo list metrics and expose their counts."""
    # The official implementation treats the ground truth as list 1 and replaces
    # semantically matching items in generated list 2 with their list-1 values.
    inputs = {
        "taskNames": (_nonempty_values(reference, "tasks", "name"), _nonempty_values(generated, "tasks", "name")),
        "taskTypes": (_nonempty_values(reference, "tasks", "type"), _nonempty_values(generated, "tasks", "type")),
        "eventNames": (_nonempty_values(reference, "events", "name"), _nonempty_values(generated, "events", "name")),
        "eventTypes": (_nonempty_values(reference, "events", "type"), _nonempty_values(generated, "events", "type")),
        "gatewayNames": (_nonempty_values(reference, "gateways", "name"), _nonempty_values(generated, "gateways", "name")),
        "gatewayTypes": (_nonempty_values(reference, "gateways", "type"), _nonempty_values(generated, "gateways", "type")),
        "sequenceFlows": (_flow_strings(reference, "sequenceFlows", "condition"), _flow_strings(generated, "sequenceFlows", "condition")),
        "messageFlows": (_flow_strings(reference, "messageFlows", "message"), _flow_strings(generated, "messageFlows", "message")),
    }
    generated_lanes, generated_lane_refs = _lane_lists(generated)
    reference_lanes, reference_lane_refs = _lane_lists(reference)
    inputs["lanes"] = (reference_lanes, generated_lanes)
    inputs["laneRefs"] = (reference_lane_refs, generated_lane_refs)
    parts = {
        name: semantic_frequency_dice(left, right, similarity, threshold)
        for name, (left, right) in inputs.items()
    }
    groups = {
        "tasksOverall": ("taskNames", "taskTypes"),
        "eventsOverall": ("eventNames", "eventTypes"),
        "gatewaysOverall": ("gatewayNames", "gatewayTypes"),
        "flowsOverall": ("sequenceFlows", "messageFlows"),
        "lanesOverall": ("lanes", "laneRefs"),
    }
    scores = {name: row.score for name, row in parts.items()}
    scores.update({name: _weighted(parts[key] for key in keys) for name, keys in groups.items()})
    scores["overall"] = _weighted(parts.values())
    diagnostics = {
        name: {
            "generatedCount": len(inputs[name][1]),
            "referenceCount": len(inputs[name][0]),
            "matchedCount": row.matched,
            "elementRecall": (row.matched / len(inputs[name][0]) if inputs[name][0] else 1.0),
            "diceDenominator": row.denominator,
        }
        for name, row in parts.items()
    }
    return {
        "protocol": "PMo semantic-frequency-aware Dice",
        "embeddingModel": getattr(similarity, "model_name", "custom"),
        "threshold": threshold,
        "scores": scores,
        "diagnostics": diagnostics,
    }


_TASK_PME_TYPES = {
    "task": "Task", "userTask": "User", "serviceTask": "Service", "manualTask": "Manual",
    "sendTask": "Send", "receiveTask": "Receive", "businessRuleTask": "BusinessRule",
    "scriptTask": "Script",
}
_GATEWAY_PME_TYPES = {
    "exclusiveGateway": "ExclusiveGateway", "parallelGateway": "ParallelGateway",
    "inclusiveGateway": "InclusiveGateway",
}


def _event_pme_type(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    definition = str(node.get("eventDefinition") or "none")
    if node_type == "startEvent":
        return "StartNoneEvent" if definition == "none" else f"Start{definition.title()}Event"
    if node_type == "endEvent":
        return "EndNoneEvent" if definition == "none" else f"End{definition.title()}Event"
    if definition == "timer":
        return "IntermediateTimerEvent"
    if definition == "conditional":
        return "IntermediateConditionalEvent"
    if definition == "message":
        suffix = "Catching" if node_type == "intermediateCatchEvent" else "Throwing"
        return f"IntermediateMessageEvent{suffix}"
    prefix = "IntermediateCatch" if node_type == "intermediateCatchEvent" else "IntermediateThrow"
    return f"{prefix}{definition.title()}Event"


def graph_to_pme(graph: dict[str, Any]) -> dict[str, Any]:
    """Convert Graph-JSON v2 or the synthetic v1 reference form to PMo PME."""
    pme: dict[str, list[dict[str, Any]]] = {
        "tasks": [], "events": [], "gateways": [], "pools": [],
        "sequenceFlows": [], "messageFlows": [],
    }
    for node in graph.get("nodes", []):
        node_type = node.get("type")
        kind = node.get("kind")
        if kind == "task" and not node_type:
            node_type = "task"
        elif kind == "event" and not node_type:
            node_type = {"start": "startEvent", "end": "endEvent"}.get(
                node.get("eventType"), "intermediateCatchEvent"
            )
        elif kind == "gateway" and not node_type:
            node_type = f"{node.get('gatewayType', 'exclusive')}Gateway"
        base = {"id": node["id"], "name": str(node.get("name", ""))}
        if node_type in _TASK_PME_TYPES:
            pme["tasks"].append({**base, "type": _TASK_PME_TYPES[node_type]})
        elif node_type in {"startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent"}:
            pme["events"].append({**base, "type": _event_pme_type({**node, "type": node_type})})
        elif node_type in _GATEWAY_PME_TYPES:
            gateway = {**base, "type": _GATEWAY_PME_TYPES[node_type]}
            if node.get("role"):
                gateway["role"] = node["role"]
            pme["gateways"].append(gateway)
    for edge in graph.get("edges", []):
        condition = edge.get("condition")
        if isinstance(condition, dict):
            condition = condition.get("label") or condition.get("expression") or ""
        row = {"id": edge["id"], "sourceRef": edge["source"], "targetRef": edge["target"]}
        if condition:
            row["condition"] = str(condition)
        pme["sequenceFlows"].append(row)
    return pme


def estimate_gateway_nesting_depth(pme: dict[str, Any]) -> int:
    """Estimate block nesting by closest common reachable split/join pairing."""
    gateways = {item["id"]: item for item in pme.get("gateways", [])}
    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree: Counter[str] = Counter()
    outdegree: Counter[str] = Counter()
    for edge in pme.get("sequenceFlows", []):
        source, target = edge.get("sourceRef"), edge.get("targetRef")
        if source and target:
            adjacency[source].append(target)
            outdegree[source] += 1
            indegree[target] += 1

    def reachable(start: str) -> set[str]:
        visited: set[str] = set()
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            queue.extend(adjacency.get(node, []))
        return visited

    def distances(start: str) -> dict[str, int]:
        result = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for target in adjacency.get(node, []):
                if target not in result:
                    result[target] = result[node] + 1
                    queue.append(target)
        return result

    splits = [node for node in gateways if outdegree[node] > 1]
    joins = [node for node in gateways if indegree[node] > 1]
    reach_cache = {node: reachable(node) for node in gateways}
    pairs: list[tuple[str, str]] = []
    for split in splits:
        successors = adjacency.get(split, [])
        if len(successors) < 2:
            continue
        common = set.intersection(*(reachable(successor) for successor in successors))
        same_type = [
            join for join in joins
            if join in common and gateways[join].get("type") == gateways[split].get("type")
        ]
        candidates = same_type or [join for join in joins if join in common]
        if candidates:
            split_distances = distances(split)
            pairs.append((split, min(candidates, key=lambda item: (split_distances.get(item, 10**9), item))))
    if not pairs:
        return 0

    contains: dict[int, list[int]] = defaultdict(list)
    for outer_index, (outer_split, outer_join) in enumerate(pairs):
        for inner_index, (inner_split, inner_join) in enumerate(pairs):
            if outer_index == inner_index:
                continue
            if (
                inner_split in reach_cache.get(outer_split, set())
                and outer_join in reach_cache.get(inner_join, set())
                and outer_split not in reach_cache.get(inner_split, set())
            ):
                contains[outer_index].append(inner_index)

    memo: dict[int, int] = {}

    def depth(index: int, trail: set[int]) -> int:
        if index in memo:
            return memo[index]
        if index in trail:
            return 1
        memo[index] = 1 + max(
            (depth(child, trail | {index}) for child in contains.get(index, [])),
            default=0,
        )
        return memo[index]

    return max(depth(index, set()) for index in range(len(pairs)))


def calculate_task_nesting_complexity(pme: dict[str, Any]) -> dict[str, Any]:
    """Measure Task exposure to enclosing split regions.

    A split region starts at a Gateway with multiple outgoing flows and ends at
    its immediate common post-dominator.  A virtual process sink is used when
    branches terminate separately.  A Task belongs to a region when the split
    dominates the Task and the region boundary post-dominates it.  This avoids
    counting Tasks before a loop entry or after a branch has already merged.

    NATC1 counts one unit for the Task itself plus one unit for every enclosing
    control-flow region. NATC2 is the depth-emphasised sensitivity variant.
    """
    tasks = {str(item["id"]): item for item in pme.get("tasks", []) if item.get("id")}
    node_ids = {
        str(item["id"])
        for group in ("tasks", "events", "gateways")
        for item in pme.get(group, [])
        if item.get("id")
    }
    for edge in pme.get("sequenceFlows", []):
        if edge.get("sourceRef"):
            node_ids.add(str(edge["sourceRef"]))
        if edge.get("targetRef"):
            node_ids.add(str(edge["targetRef"]))

    if not node_ids:
        return {
            "protocol": "task_control_context_nesting_v1",
            "taskCount": 0,
            "taskDepths": [],
            "meanTaskNestingDepth": 0.0,
            "maxTaskNestingDepth": 0,
            "natc1": 0,
            "natc2": 0,
            "splitRegionCount": 0,
            "regions": [],
        }

    virtual_source = "__natc_virtual_source__"
    virtual_sink = "__natc_virtual_sink__"
    while virtual_source in node_ids:
        virtual_source += "_"
    while virtual_sink in node_ids or virtual_sink == virtual_source:
        virtual_sink += "_"

    all_nodes = set(node_ids) | {virtual_source, virtual_sink}
    adjacency: dict[str, set[str]] = {node: set() for node in all_nodes}
    predecessors: dict[str, set[str]] = {node: set() for node in all_nodes}
    for edge in pme.get("sequenceFlows", []):
        source = str(edge.get("sourceRef") or "")
        target = str(edge.get("targetRef") or "")
        if source in node_ids and target in node_ids:
            adjacency[source].add(target)
            predecessors[target].add(source)

    starts = [node for node in node_ids if not predecessors[node]]
    ends = [node for node in node_ids if not adjacency[node]]
    for node in starts:
        adjacency[virtual_source].add(node)
        predecessors[node].add(virtual_source)
    for node in ends:
        adjacency[node].add(virtual_sink)
        predecessors[virtual_sink].add(node)

    def fixed_point_dominators(
        nodes: set[str], incoming: dict[str, set[str]], root: str,
    ) -> dict[str, set[str]]:
        dominators = {
            node: ({root} if node == root else set(nodes))
            for node in nodes
        }
        changed = True
        while changed:
            changed = False
            for node in sorted(nodes - {root}):
                parents = incoming.get(node, set())
                shared = (
                    set.intersection(*(dominators[parent] for parent in parents))
                    if parents else set()
                )
                updated = {node} | shared
                if updated != dominators[node]:
                    dominators[node] = updated
                    changed = True
        return dominators

    dominators = fixed_point_dominators(all_nodes, predecessors, virtual_source)
    post_dominators = fixed_point_dominators(all_nodes, adjacency, virtual_sink)

    gateways = {str(item["id"]): item for item in pme.get("gateways", []) if item.get("id")}
    split_ids = sorted(node for node in gateways if len(adjacency[node]) > 1)

    def immediate_post_dominator(node: str) -> str:
        strict = post_dominators[node] - {node}
        for candidate in sorted(strict):
            if all(
                other == candidate or other in post_dominators[candidate]
                for other in strict
            ):
                return candidate
        return virtual_sink

    regions: list[dict[str, Any]] = []
    task_depths = {task_id: 0 for task_id in tasks}
    for split_id in split_ids:
        boundary = immediate_post_dominator(split_id)
        members = sorted(
            task_id for task_id in tasks
            if split_id in dominators.get(task_id, set())
            and boundary in post_dominators.get(task_id, set())
            and task_id != boundary
        )
        if not members:
            continue
        for task_id in members:
            task_depths[task_id] += 1
        regions.append({
            "splitId": split_id,
            "boundaryId": None if boundary == virtual_sink else boundary,
            "gatewayType": str(gateways[split_id].get("type") or ""),
            "taskIds": members,
        })

    rows = [
        {
            "taskId": task_id,
            "taskName": str(tasks[task_id].get("name") or ""),
            "depth": task_depths[task_id],
        }
        for task_id in tasks
    ]
    depths = [int(row["depth"]) for row in rows]
    return {
        "protocol": "task_control_context_nesting_v1",
        "taskCount": len(rows),
        "taskDepths": rows,
        "meanTaskNestingDepth": mean(depths) if depths else 0.0,
        "maxTaskNestingDepth": max(depths, default=0),
        "natc1": sum(1 + depth for depth in depths),
        "natc2": sum((1 + depth) ** 2 for depth in depths),
        "splitRegionCount": len(regions),
        "regions": regions,
    }


def natc_tertile_thresholds(
    profiles: Iterable[dict[str, Any]], field: str = "natc1",
) -> tuple[float, float]:
    """Return outcome-independent inclusive empirical tertile cut points."""
    values = sorted(float(profile[field]) for profile in profiles)
    if len(values) < 3:
        raise ValueError("NATC tertiles require at least three reference processes")
    lower, upper = quantiles(values, n=3, method="inclusive")
    return float(lower), float(upper)


def natc_tier(value: float, thresholds: tuple[float, float]) -> str:
    lower, upper = thresholds
    if value <= lower:
        return "low"
    if value <= upper:
        return "medium"
    return "high"


def complexity_profile(reference: dict[str, Any], nesting_depth: int | None = None) -> dict[str, Any]:
    tasks = len(reference.get("tasks", []))
    events = len(reference.get("events", []))
    gateways = len(reference.get("gateways", []))
    nodes = tasks + events + gateways
    size = "S" if nodes <= 15 else "M" if nodes <= 25 else "L" if nodes <= 35 else "XL"
    weighted = tasks + 2 * gateways
    level = "low" if weighted <= 24 else "medium" if weighted <= 33 else "high"
    if nesting_depth is None:
        nesting_depth = estimate_gateway_nesting_depth(reference)
    task_nesting = calculate_task_nesting_complexity(reference)
    outdegree: Counter[str] = Counter()
    for edge in reference.get("sequenceFlows", []):
        source = edge.get("sourceRef")
        if source:
            outdegree[str(source)] += 1
    cfc = 0
    for gateway in reference.get("gateways", []):
        branches = outdegree[str(gateway.get("id", ""))]
        if branches <= 1:
            continue
        gateway_type = str(gateway.get("type", "")).lower()
        if "parallel" in gateway_type:
            cfc += 1
        elif "exclusive" in gateway_type:
            cfc += branches
        elif "inclusive" in gateway_type:
            cfc += 2 ** branches - 1
    return {
        "tasks": tasks, "events": events, "gateways": gateways,
        "sequenceFlows": len(reference.get("sequenceFlows", [])),
        "totalNodes": nodes, "sizeBucket": size, "nestingDepth": nesting_depth,
        "taskMeanNestingDepth": task_nesting["meanTaskNestingDepth"],
        "taskMaxNestingDepth": task_nesting["maxTaskNestingDepth"],
        "natc1": task_nesting["natc1"],
        "natc2": task_nesting["natc2"],
        "taskNestingProtocol": task_nesting["protocol"],
        "controlFlowComplexity": cfc,
        "weightedScore": weighted, "weightedLevel": level,
    }


def _pme_graph(pme: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    nodes: dict[str, dict[str, Any]] = {}
    for group, kind in (("tasks", "task"), ("events", "event"), ("gateways", "gateway")):
        for node in pme.get(group, []):
            nodes[node["id"]] = {**node, "kind": kind}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for edge in pme.get("sequenceFlows", []):
        if edge.get("sourceRef") in nodes and edge.get("targetRef") in nodes:
            outgoing[edge["sourceRef"]].add(edge["targetRef"])
    return nodes, outgoing


def _transitive_reachability(
    nodes: dict[str, dict[str, Any]], outgoing: dict[str, set[str]]
) -> dict[str, set[str]]:
    reach: dict[str, set[str]] = {}
    for source in nodes:
        seen: set[str] = set()
        queue = deque(outgoing.get(source, set()))
        while queue:
            target = queue.popleft()
            if target in seen:
                continue
            seen.add(target)
            queue.extend(outgoing.get(target, set()) - seen)
        reach[source] = seen
    return reach


def behavioral_relations(pme: dict[str, Any]) -> dict[str, Any]:
    """Extract a deterministic structured-graph behavioral profile.

    This is a transparent supplemental metric, not the official PME metric. Cross-branch
    XOR and parallel relations are inferred from split regions before their common suffix.
    """
    nodes, outgoing = _pme_graph(pme)
    reach = _transitive_reachability(nodes, outgoing)
    task_ids = [node_id for node_id, node in nodes.items() if node["kind"] == "task"]
    strict: set[tuple[str, str]] = set()
    cyclic: set[tuple[str, str]] = set()
    for left in task_ids:
        for right in task_ids:
            if left == right:
                continue
            if right in reach[left] and left not in reach[right]:
                strict.add((left, right))
            elif right in reach[left] and left in reach[right]:
                cyclic.add(tuple(sorted((left, right))))

    exclusive: set[tuple[str, str]] = set()
    interleaving: set[tuple[str, str]] = set()
    for split_id, split in nodes.items():
        branches = list(outgoing.get(split_id, set()))
        if split["kind"] != "gateway" or len(branches) < 2:
            continue
        branch_reach = [{branch, *reach.get(branch, set())} for branch in branches]
        common_suffix = set.intersection(*branch_reach) if branch_reach else set()
        branch_tasks = [
            {node_id for node_id in region - common_suffix if nodes[node_id]["kind"] == "task"}
            for region in branch_reach
        ]
        gateway_type = str(split.get("type", ""))
        target = exclusive if gateway_type == "ExclusiveGateway" else (
            interleaving if gateway_type in {"ParallelGateway", "InclusiveGateway"} else None
        )
        if target is None:
            continue
        for left_index in range(len(branch_tasks)):
            for right_index in range(left_index + 1, len(branch_tasks)):
                target.update(
                    tuple(sorted((left, right)))
                    for left in branch_tasks[left_index]
                    for right in branch_tasks[right_index]
                    if left != right
                )
    interleaving -= cyclic
    return {
        "strictOrder": strict,
        "exclusiveness": exclusive,
        "interleaving": interleaving,
        "cyclicPairs": cyclic,
        "hasCycle": bool(cyclic),
    }


def _set_dice(left: set[Any], right: set[Any]) -> float:
    denominator = len(left) + len(right)
    return 1.0 if denominator == 0 else 2 * len(left & right) / denominator


def calculate_behavioral_similarity(
    generated: dict[str, Any], reference: dict[str, Any], similarity: StringSimilarity,
    threshold: float = 0.7,
) -> dict[str, Any]:
    generated_tasks = generated.get("tasks", [])
    reference_tasks = reference.get("tasks", [])
    matches = semantic_frequency_dice(
        [str(item.get("name", "")) for item in generated_tasks],
        [str(item.get("name", "")) for item in reference_tasks],
        similarity,
        threshold,
    )
    mapping = {
        generated_tasks[left]["id"]: f"REF::{reference_tasks[right]['id']}"
        for left, right, _ in matches.pairs
    }
    matched_reference_ids = {
        reference_tasks[right]["id"] for _, right, _ in matches.pairs
    }

    def canonicalize(relations: dict[str, Any], generated_side: bool) -> dict[str, set[tuple[str, str]]]:
        normalized: dict[str, set[tuple[str, str]]] = {}
        for key in ("strictOrder", "exclusiveness", "interleaving"):
            target: set[tuple[str, str]] = set()
            for left, right in relations[key]:
                if generated_side:
                    left_value = mapping.get(left, f"GEN::{left}")
                    right_value = mapping.get(right, f"GEN::{right}")
                else:
                    left_value = f"REF::{left}" if left in matched_reference_ids else f"REF-ONLY::{left}"
                    right_value = f"REF::{right}" if right in matched_reference_ids else f"REF-ONLY::{right}"
                pair = (left_value, right_value)
                target.add(pair if key == "strictOrder" else tuple(sorted(pair)))
            normalized[key] = target
        return normalized

    generated_relations = behavioral_relations(generated)
    reference_relations = behavioral_relations(reference)
    left = canonicalize(generated_relations, True)
    right = canonicalize(reference_relations, False)
    scores = {key: _set_dice(left[key], right[key]) for key in left}
    return {
        "protocol": "structured_graph_profile_v1",
        "scores": {**scores, "macroAverage": mean(scores.values())},
        "generatedRelationCounts": {key: len(value) for key, value in left.items()},
        "referenceRelationCounts": {key: len(value) for key, value in right.items()},
        "matchedTasks": len(mapping),
        "generatedHasCycle": generated_relations["hasCycle"],
        "referenceHasCycle": reference_relations["hasCycle"],
        "interpretation": (
            "Interleaving is a parallel proxy only when both models are acyclic; "
            "for cyclic models it is reported only as interleaving."
        ),
    }


def _node_alignment(
    generated: dict[str, Any], reference: dict[str, Any], similarity: StringSimilarity,
    threshold: float,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group in ("tasks", "events", "gateways"):
        left = generated.get(group, [])
        right = reference.get(group, [])
        if group == "tasks":
            left_labels = [str(item.get("name", "")) for item in left]
            right_labels = [str(item.get("name", "")) for item in right]
        else:
            left_labels = [f"{item.get('type', '')} {item.get('name', '')}" for item in left]
            right_labels = [f"{item.get('type', '')} {item.get('name', '')}" for item in right]
        matches = semantic_frequency_dice(left_labels, right_labels, similarity, threshold)
        mapping.update({left[i]["id"]: right[j]["id"] for i, j, _ in matches.pairs})
    return mapping


def calculate_graph_edit_distance(
    generated: dict[str, Any], reference: dict[str, Any], similarity: StringSimilarity,
    threshold: float = 0.7,
) -> dict[str, Any]:
    """Edit distance after semantic node alignment using unit insert/delete/substitute costs."""
    mapping = _node_alignment(generated, reference, similarity, threshold)
    generated_nodes = {
        item["id"]: item
        for group in ("tasks", "events", "gateways")
        for item in generated.get(group, [])
    }
    reference_nodes = {
        item["id"]: item
        for group in ("tasks", "events", "gateways")
        for item in reference.get(group, [])
    }
    matched_reference = set(mapping.values())
    node_insertions = len(generated_nodes) - len(mapping)
    node_deletions = len(reference_nodes) - len(matched_reference)
    node_substitutions = sum(
        generated_nodes[left].get("type") != reference_nodes[right].get("type")
        for left, right in mapping.items()
    )

    def edges(pme: dict[str, Any], generated_side: bool) -> Counter[tuple[str, str, str]]:
        rows: Counter[tuple[str, str, str]] = Counter()
        for edge in pme.get("sequenceFlows", []):
            source = edge.get("sourceRef", "")
            target = edge.get("targetRef", "")
            if generated_side:
                source = mapping.get(source, f"GEN::{source}")
                target = mapping.get(target, f"GEN::{target}")
            condition = str(edge.get("condition", ""))
            rows[(str(source), str(target), condition)] += 1
        return rows

    generated_edges = edges(generated, True)
    reference_edges = edges(reference, False)
    shared_edges = generated_edges & reference_edges
    edge_insertions = sum((generated_edges - shared_edges).values())
    edge_deletions = sum((reference_edges - shared_edges).values())
    distance = node_insertions + node_deletions + node_substitutions + edge_insertions + edge_deletions
    reference_size = len(reference_nodes) + len(reference.get("sequenceFlows", []))
    return {
        "protocol": "semantic_alignment_unit_cost_v1",
        "ged": distance,
        "rged": distance / max(1, reference_size),
        "normalization": "GED / (reference nodes + reference sequence flows)",
        "costs": {
            "nodeInsertions": node_insertions,
            "nodeDeletions": node_deletions,
            "nodeSubstitutions": node_substitutions,
            "edgeInsertions": edge_insertions,
            "edgeDeletions": edge_deletions,
        },
        "matchedNodes": len(mapping),
    }


def calculate_quality_metrics(
    generated_graph: dict[str, Any], reference_pme: dict[str, Any],
    similarity: StringSimilarity, threshold: float = 0.7,
) -> dict[str, Any]:
    generated_pme = graph_to_pme(generated_graph)
    pme = calculate_pme_similarity(generated_pme, reference_pme, similarity, threshold)
    behavior = calculate_behavioral_similarity(generated_pme, reference_pme, similarity, threshold)
    edit = calculate_graph_edit_distance(generated_pme, reference_pme, similarity, threshold)
    return {
        "pmeOverall": pme["scores"]["overall"],
        "gatewayTypeSimilarity": pme["scores"]["gatewayTypes"],
        "sequenceFlowSimilarity": pme["scores"]["sequenceFlows"],
        "strictOrderSimilarity": behavior["scores"]["strictOrder"],
        "exclusivenessSimilarity": behavior["scores"]["exclusiveness"],
        "interleavingSimilarity": behavior["scores"]["interleaving"],
        "behaviorMacroAverage": behavior["scores"]["macroAverage"],
        "ged": edit["ged"],
        "rged": edit["rged"],
        "pme": pme,
        "behavioralProfile": behavior,
        "graphEditDistance": edit,
    }


def _mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate strategy-by-complexity trends and paired semantic-minus-single deltas."""
    task_thresholds = (0.6, 0.7, 0.8)
    metrics = (
        "pmoTaskNameDiceAt07", "taskSemanticAlignmentScore",
        "taskPrecision", "taskRecall", "taskF1",
        "gatewayTypeRolePrecision", "gatewayTypeRoleRecall", "gatewayTypeRoleF1",
        "gatewayTypeRoleAbsoluteCountError",
        "controlFlowRelationPrecision", "controlFlowRelationRecall", "controlFlowRelationF1",
        "controlFlowRelationMatchedTaskPairCoverage",
        "conditionalControlFlowRelationPrecision", "conditionalControlFlowRelationRecall",
        "conditionalControlFlowRelationF1",
        "globalControlFlowRelationPrecision", "globalControlFlowRelationRecall",
        "globalControlFlowRelationF1",
    )

    def task_scores_at(row: dict[str, Any], threshold: float) -> dict[str, float] | None:
        alignment = row.get("quality", {}).get("taskAlignment")
        if not alignment:
            return None
        for item in alignment.get("sensitivity", []):
            if abs(float(item.get("threshold", -1.0)) - threshold) < 1e-12:
                return {
                    "precision": float(item["precision"]),
                    "recall": float(item["recall"]),
                    "f1": float(item["f1"]),
                }
        if "pairs" not in alignment:
            return None
        generated_count = int(alignment.get("generatedCount", 0))
        reference_count = int(alignment.get("referenceCount", 0))
        matched = sum(
            float(pair.get("similarity", 0.0)) >= threshold
            for pair in alignment.get("pairs", [])
        )
        precision = (
            matched / generated_count
            if generated_count else (1.0 if reference_count == 0 else 0.0)
        )
        recall = (
            matched / reference_count
            if reference_count else (1.0 if generated_count == 0 else 0.0)
        )
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        return {"precision": precision, "recall": recall, "f1": f1}

    def relation_scores_at(row: dict[str, Any], threshold: float) -> dict[str, float] | None:
        relation = row.get("quality", {}).get("controlFlowRelations")
        if not relation:
            return None
        for item in relation.get("sensitivity", []):
            if abs(float(item.get("threshold", -1.0)) - threshold) < 1e-12:
                return {
                    "precision": float(item["precision"]),
                    "recall": float(item["recall"]),
                    "f1": float(item["f1"]),
                    "matchedTaskPairCoverage": float(item["matchedTaskPairCoverage"]),
                }
        return None

    def global_relation_scores_at(
        row: dict[str, Any], threshold: float,
    ) -> dict[str, float] | None:
        relation = row.get("quality", {}).get("globalControlFlowRelations")
        if not relation:
            return None
        for item in relation.get("sensitivity", []):
            if abs(float(item.get("threshold", -1.0)) - threshold) < 1e-12:
                return {
                    "precision": float(item["precision"]),
                    "recall": float(item["recall"]),
                    "f1": float(item["f1"]),
                }
        return None

    def trends_for(field: str) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            if field in run.get("complexity", {}):
                grouped[(run["strategy"], str(run["complexity"][field]))].append(run)
        trends: list[dict[str, Any]] = []
        for (strategy, bucket), rows in sorted(grouped.items()):
            successful = [row for row in rows if row.get("success")]
            trend: dict[str, Any] = {
                "strategy": strategy,
                field: bucket,
                "runs": len(rows),
                "successful": len(successful),
                "completionRate": len(successful) / len(rows),
                "meanEndToEndTokens": _mean([float(row["tokenUsage"]["totalTokens"]) for row in rows]),
                "meanDurationSeconds": _mean([float(row["durationSeconds"]) for row in rows]),
            }
            output_rows = [row for row in rows if row.get("bpmnXml")]
            trend["soundnessOutputs"] = len(output_rows)
            trend["soundnessPassed"] = sum(
                row.get("quality", {}).get("sound") is True for row in output_rows
            )
            trend["soundnessRate"] = (
                trend["soundnessPassed"] / len(output_rows) if output_rows else None
            )
            for metric in metrics:
                values = [
                    float(row["quality"][metric])
                    for row in successful
                    if row.get("quality", {}).get(metric) is not None
                ]
                trend[metric] = _mean(values)
                trend[f"{metric}N"] = len(values)
                conservative = [float(row.get("quality", {}).get(metric, 0.0)) for row in rows]
                trend[f"{metric}Conservative"] = _mean(conservative)
            trend["taskThresholds"] = []
            for threshold in task_thresholds:
                scores = [
                    score for row in successful
                    if (score := task_scores_at(row, threshold)) is not None
                ]
                trend["taskThresholds"].append({
                    "threshold": threshold,
                    "runs": len(scores),
                    "precision": _mean([score["precision"] for score in scores]),
                    "recall": _mean([score["recall"] for score in scores]),
                    "f1": _mean([score["f1"] for score in scores]),
                })
            trend["controlFlowRelationThresholds"] = []
            trend["globalControlFlowRelationThresholds"] = []
            for threshold in task_thresholds:
                scores = [
                    score for row in successful
                    if (score := relation_scores_at(row, threshold)) is not None
                ]
                trend["controlFlowRelationThresholds"].append({
                    "threshold": threshold,
                    "runs": len(scores),
                    "precision": _mean([score["precision"] for score in scores]),
                    "recall": _mean([score["recall"] for score in scores]),
                    "f1": _mean([score["f1"] for score in scores]),
                    "matchedTaskPairCoverage": _mean([
                        score["matchedTaskPairCoverage"] for score in scores
                    ]),
                })
                global_scores = [
                    score for row in successful
                    if (score := global_relation_scores_at(row, threshold)) is not None
                ]
                trend["globalControlFlowRelationThresholds"].append({
                    "threshold": threshold,
                    "runs": len(global_scores),
                    "precision": _mean([score["precision"] for score in global_scores]),
                    "recall": _mean([score["recall"] for score in global_scores]),
                    "f1": _mean([score["f1"] for score in global_scores]),
                })
            trends.append(trend)
        return trends

    indexed = {(row["sample"], row["repetition"], row["strategy"]): row for row in runs}
    paired: list[dict[str, Any]] = []
    sample_repetitions = sorted({(row["sample"], row["repetition"]) for row in runs})
    for sample, repetition in sample_repetitions:
        semantic = indexed.get((sample, repetition, "semantic"))
        single = indexed.get((sample, repetition, "single_segment"))
        if not semantic or not single:
            continue
        deltas = {}
        for metric in metrics:
            left = semantic.get("quality", {}).get(metric)
            right = single.get("quality", {}).get(metric)
            if left is not None and right is not None:
                deltas[metric] = float(left) - float(right)
        paired.append({
            "sample": sample, "repetition": repetition,
            "sizeBucket": semantic["complexity"]["sizeBucket"],
            "weightedLevel": semantic["complexity"]["weightedLevel"],
            "tasks": semantic["complexity"]["tasks"],
            "totalNodes": semantic["complexity"]["totalNodes"],
            "gateways": semantic["complexity"]["gateways"],
            "sequenceFlows": semantic["complexity"]["sequenceFlows"],
            "nestingDepth": semantic["complexity"]["nestingDepth"],
            "controlFlowComplexity": semantic["complexity"]["controlFlowComplexity"],
            "semanticMinusSingle": deltas,
            "taskThresholdDeltas": [
                {
                    "threshold": threshold,
                    **{
                        metric: semantic_scores[metric] - single_scores[metric]
                        for metric in ("precision", "recall", "f1")
                    },
                }
                for threshold in task_thresholds
                if (semantic_scores := task_scores_at(semantic, threshold)) is not None
                and (single_scores := task_scores_at(single, threshold)) is not None
            ],
            "controlFlowRelationThresholdDeltas": [
                {
                    "threshold": threshold,
                    **{
                        metric: semantic_scores[metric] - single_scores[metric]
                        for metric in ("precision", "recall", "f1", "matchedTaskPairCoverage")
                    },
                }
                for threshold in task_thresholds
                if (semantic_scores := relation_scores_at(semantic, threshold)) is not None
                and (single_scores := relation_scores_at(single, threshold)) is not None
            ],
            "globalControlFlowRelationThresholdDeltas": [
                {
                    "threshold": threshold,
                    **{
                        metric: semantic_scores[metric] - single_scores[metric]
                        for metric in ("precision", "recall", "f1")
                    },
                }
                for threshold in task_thresholds
                if (semantic_scores := global_relation_scores_at(semantic, threshold)) is not None
                and (single_scores := global_relation_scores_at(single, threshold)) is not None
            ],
            "tokenDelta": semantic["tokenUsage"]["totalTokens"] - single["tokenUsage"]["totalTokens"],
            "bothSuccessful": bool(semantic.get("success") and single.get("success")),
        })

    grouped_flows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped_flows[(str(run["sample"]), str(run["strategy"]))].append(run)
    flow_means: dict[tuple[str, str], dict[str, Any]] = {}
    for (sample, strategy), rows in sorted(grouped_flows.items()):
        successful = [row for row in rows if row.get("success")]
        anchor = rows[0]
        quality_means = {
            metric: _mean([
                float(row["quality"][metric])
                for row in successful
                if row.get("quality", {}).get(metric) is not None
            ])
            for metric in metrics
        }
        flow_means[(sample, strategy)] = {
            "sample": sample,
            "strategy": strategy,
            "plannedRuns": len(rows),
            "successfulRuns": len(successful),
            "completionRate": len(successful) / len(rows),
            "sizeBucket": anchor["complexity"]["sizeBucket"],
            "complexity": anchor["complexity"],
            "qualityMeans": quality_means,
            "meanEndToEndTokens": _mean([
                float(row["tokenUsage"]["totalTokens"]) for row in rows
            ]),
            "meanDurationSeconds": _mean([float(row["durationSeconds"]) for row in rows]),
            "taskThresholdMeans": [
                {
                    "threshold": threshold,
                    **{
                        field: _mean([
                            score[field] for row in successful
                            if (score := task_scores_at(row, threshold)) is not None
                        ])
                        for field in ("precision", "recall", "f1")
                    },
                }
                for threshold in task_thresholds
            ],
            "controlFlowRelationThresholdMeans": [
                {
                    "threshold": threshold,
                    **{
                        field: _mean([
                            score[field] for row in successful
                            if (score := relation_scores_at(row, threshold)) is not None
                        ])
                        for field in (
                            "precision", "recall", "f1", "matchedTaskPairCoverage",
                        )
                    },
                }
                for threshold in task_thresholds
            ],
            "globalControlFlowRelationThresholdMeans": [
                {
                    "threshold": threshold,
                    **{
                        field: _mean([
                            score[field] for row in successful
                            if (score := global_relation_scores_at(row, threshold)) is not None
                        ])
                        for field in ("precision", "recall", "f1")
                    },
                }
                for threshold in task_thresholds
            ],
        }

    paired_flow_means: list[dict[str, Any]] = []
    for sample in sorted({key[0] for key in flow_means}):
        semantic = flow_means.get((sample, "semantic"))
        single = flow_means.get((sample, "single_segment"))
        if not semantic or not single:
            continue
        deltas = {
            metric: float(semantic["qualityMeans"][metric]) - float(single["qualityMeans"][metric])
            for metric in metrics
            if semantic["qualityMeans"].get(metric) is not None
            and single["qualityMeans"].get(metric) is not None
        }
        paired_flow_means.append({
            "sample": sample,
            "sizeBucket": semantic["sizeBucket"],
            "complexity": semantic["complexity"],
            "semantic": semantic,
            "singleSegment": single,
            "semanticMinusSingle": deltas,
            "meanTokenDelta": (
                float(semantic["meanEndToEndTokens"]) - float(single["meanEndToEndTokens"])
            ),
        })
    return {
        "trendsBySize": trends_for("sizeBucket"),
        "trendsByWeightedLevel": trends_for("weightedLevel"),
        "trendsByNatc1Tier": trends_for("natc1Tier"),
        "trendsByNatc2Tier": trends_for("natc2Tier"),
        "pairedDifferences": paired,
        "flowMeans": list(flow_means.values()),
        "pairedFlowMeanDifferences": paired_flow_means,
    }

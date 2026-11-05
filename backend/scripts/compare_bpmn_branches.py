from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from app.config import load_settings
from app.llm import LlmClient, parse_json_object


FLOW_NODE_TAGS = {
    "task", "userTask", "serviceTask", "sendTask", "receiveTask",
    "businessRuleTask", "manualTask", "scriptTask", "callActivity",
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
    "exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway",
    "complexGateway",
}
TASK_TAGS = {tag for tag in FLOW_NODE_TAGS if tag.endswith("Task") or tag == "task"}
EVENT_TAGS = {tag for tag in FLOW_NODE_TAGS if tag.endswith("Event")}
GATEWAY_TAGS = {tag for tag in FLOW_NODE_TAGS if tag.endswith("Gateway")}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_bpmn(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    nodes: dict[str, dict[str, str]] = {}
    edges: list[dict[str, str]] = []
    for element in root.iter():
        tag = local(element.tag)
        if tag in FLOW_NODE_TAGS and element.get("id"):
            nodes[element.get("id")] = {
                "id": element.get("id", ""),
                "name": element.get("name", "").strip(),
                "type": tag,
            }
        elif tag == "sequenceFlow" and element.get("id"):
            edges.append({
                "id": element.get("id", ""),
                "source": element.get("sourceRef", ""),
                "target": element.get("targetRef", ""),
                "name": element.get("name", "").strip(),
            })
    outgoing: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        outgoing[edge["source"]].append(edge["target"])
        incoming[edge["target"]].append(edge["source"])
    return {"nodes": nodes, "edges": edges, "outgoing": outgoing, "incoming": incoming}


def structural_check(graph: dict[str, Any]) -> dict[str, Any]:
    nodes, edges = graph["nodes"], graph["edges"]
    issues: list[str] = []
    for edge in edges:
        if edge["source"] not in nodes or edge["target"] not in nodes:
            issues.append(f"dangling sequenceFlow {edge['id']}")
    starts = [node_id for node_id, node in nodes.items() if node["type"] == "startEvent"]
    ends = [node_id for node_id, node in nodes.items() if node["type"] == "endEvent"]
    if not starts:
        issues.append("missing startEvent")
    if not ends:
        issues.append("missing endEvent")
    reached: set[str] = set()
    queue = deque(starts)
    while queue:
        node_id = queue.popleft()
        if node_id in reached:
            continue
        reached.add(node_id)
        queue.extend(graph["outgoing"].get(node_id, []))
    unreachable = sorted(set(nodes) - reached)
    if unreachable:
        issues.append(f"unreachable nodes: {','.join(unreachable)}")
    bad_leaves = sorted(node_id for node_id in reached
                        if not graph["outgoing"].get(node_id)
                        and nodes[node_id]["type"] != "endEvent")
    if bad_leaves:
        issues.append(f"non-end leaves: {','.join(bad_leaves)}")
    for node_id, node in nodes.items():
        if node["type"] in TASK_TAGS or node["type"].startswith("intermediate"):
            if len(graph["outgoing"].get(node_id, [])) > 1:
                issues.append(f"implicit split at {node_id}")
            if len(graph["incoming"].get(node_id, [])) > 1:
                issues.append(f"implicit merge at {node_id}")
    return {
        "valid": not issues,
        "issues": issues,
        "nodes": len(nodes),
        "sequenceFlows": len(edges),
        "startEvents": len(starts),
        "endEvents": len(ends),
    }


def typed_nodes(graph: dict[str, Any], tags: set[str]) -> list[dict[str, str]]:
    return [node for node in graph["nodes"].values() if node["type"] in tags]


def llm_task_matches(client: LlmClient, sample: str, gold: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    gold_tasks = typed_nodes(gold, TASK_TAGS)
    generated_tasks = typed_nodes(generated, TASK_TAGS)
    prompt = {
        "sample": sample,
        "goldTasks": gold_tasks,
        "generatedTasks": generated_tasks,
    }
    messages = [{
        "role": "system",
        "content": (
            "You are a strict bilingual business-process element matcher. Match a gold English BPMN task "
            "to a generated task only when both denote the same observable business action. Allow concise "
            "paraphrases and Chinese-English equivalents, but do not match merely related actions. Use each "
            "ID at most once. Do not force unmatched items. Return JSON only as "
            "{\"matches\":[{\"goldId\":\"...\",\"generatedId\":\"...\",\"confidence\":\"high|medium|low\"}],"
            "\"unmatchedGoldIds\":[\"...\"],\"unmatchedGeneratedIds\":[\"...\"]}."
        ),
    }, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]
    result = client.complete(messages, agent="element_matcher")
    value = parse_json_object(result.content, "element_matcher")
    valid_gold = {node["id"] for node in gold_tasks}
    valid_generated = {node["id"] for node in generated_tasks}
    accepted: list[dict[str, str]] = []
    used_gold: set[str] = set()
    used_generated: set[str] = set()
    for item in value.get("matches", []):
        gold_id, generated_id = item.get("goldId"), item.get("generatedId")
        if (gold_id in valid_gold and generated_id in valid_generated
                and gold_id not in used_gold and generated_id not in used_generated
                and item.get("confidence") in {"high", "medium"}):
            accepted.append({
                "goldId": gold_id,
                "generatedId": generated_id,
                "confidence": item.get("confidence", "medium"),
                "goldName": gold["nodes"][gold_id]["name"],
                "generatedName": generated["nodes"][generated_id]["name"],
            })
            used_gold.add(gold_id)
            used_generated.add(generated_id)
    return {
        "matches": accepted,
        "unmatchedGoldIds": sorted(valid_gold - used_gold),
        "unmatchedGeneratedIds": sorted(valid_generated - used_generated),
        "usage": result.usage,
    }


def event_matches(gold: dict[str, Any], generated: dict[str, Any]) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for event_type in sorted(EVENT_TAGS):
        gold_events = sorted(typed_nodes(gold, {event_type}), key=lambda node: node["id"])
        generated_events = sorted(typed_nodes(generated, {event_type}), key=lambda node: node["id"])
        for gold_event, generated_event in zip(gold_events, generated_events):
            matches.append({"goldId": gold_event["id"], "generatedId": generated_event["id"]})
    return matches


def shortest_distances(start: str, outgoing: dict[str, list[str]]) -> dict[str, int]:
    distance = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in outgoing.get(current, []):
            if target not in distance:
                distance[target] = distance[current] + 1
                queue.append(target)
    return distance


def split_branches(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes, outgoing = graph["nodes"], graph["outgoing"]
    result: list[dict[str, Any]] = []
    for split_id in sorted(nodes):
        starts = outgoing.get(split_id, [])
        if len(starts) < 2:
            continue
        distances = [shortest_distances(start, outgoing) for start in starts]
        common = set(distances[0])
        for item in distances[1:]:
            common.intersection_update(item)
        common.discard(split_id)
        join = min(common, key=lambda node_id: (
            max(item[node_id] for item in distances),
            sum(item[node_id] for item in distances),
            0 if len(graph["incoming"].get(node_id, [])) > 1 else 1,
            node_id,
        )) if common else None
        branches = []
        for start in starts:
            branch_nodes: set[str] = set()
            queue = deque([start])
            while queue:
                current = queue.popleft()
                if current == join or current in branch_nodes:
                    continue
                branch_nodes.add(current)
                queue.extend(outgoing.get(current, []))
            business = sorted(node_id for node_id in branch_nodes
                              if nodes[node_id]["type"] in TASK_TAGS | EVENT_TAGS)
            branches.append({
                "start": start,
                "nodes": sorted(branch_nodes),
                "businessElements": business,
                "businessNames": [nodes[node_id]["name"] or nodes[node_id]["type"] for node_id in business],
            })
        result.append({
            "splitId": split_id,
            "splitName": nodes[split_id]["name"],
            "splitType": nodes[split_id]["type"],
            "joinId": join,
            "branches": branches,
        })
    return result


def best_branch_pairing(gold_split: dict[str, Any], generated_split: dict[str, Any],
                        element_map: dict[str, str]) -> dict[str, Any]:
    gold_branches = gold_split["branches"]
    generated_branches = generated_split["branches"]
    pair_scores: list[list[dict[str, Any]]] = []
    for gold_branch in gold_branches:
        mapped = {element_map[node_id] for node_id in gold_branch["businessElements"] if node_id in element_map}
        denominator = len(gold_branch["businessElements"])
        row = []
        for generated_branch in generated_branches:
            present = mapped.intersection(generated_branch["businessElements"])
            row.append({
                "coverage": len(present) / denominator if denominator else 1.0,
                "covered": len(present),
                "total": denominator,
            })
        pair_scores.append(row)
    best_pairs: list[tuple[int, int]] = []
    best_total = -1.0
    if len(gold_branches) <= len(generated_branches):
        for generated_indices in itertools.permutations(range(len(generated_branches)), len(gold_branches)):
            total = sum(pair_scores[i][j]["coverage"] for i, j in enumerate(generated_indices))
            if total > best_total:
                best_total = total
                best_pairs = list(enumerate(generated_indices))
    else:
        for gold_indices in itertools.permutations(range(len(gold_branches)), len(generated_branches)):
            total = sum(pair_scores[i][j]["coverage"] for j, i in enumerate(gold_indices))
            if total > best_total:
                best_total = total
                best_pairs = [(i, j) for j, i in enumerate(gold_indices)]
    details = []
    paired_gold = set()
    for gold_index, generated_index in best_pairs:
        paired_gold.add(gold_index)
        score = pair_scores[gold_index][generated_index]
        details.append({
            "goldBranch": gold_index + 1,
            "generatedBranch": generated_index + 1,
            **score,
            "corresponding": score["coverage"] >= 0.5,
            "goldElements": gold_branches[gold_index]["businessNames"],
            "generatedElements": generated_branches[generated_index]["businessNames"],
        })
    for gold_index, branch in enumerate(gold_branches):
        if gold_index not in paired_gold:
            details.append({
                "goldBranch": gold_index + 1,
                "generatedBranch": None,
                "coverage": 0.0,
                "covered": 0,
                "total": len(branch["businessElements"]),
                "corresponding": False,
                "goldElements": branch["businessNames"],
                "generatedElements": [],
            })
    weighted_total = sum(item["total"] for item in details)
    weighted_covered = sum(item["covered"] for item in details)
    type_match = gold_split["splitType"] == generated_split["splitType"]
    return {
        "score": (best_total / len(gold_branches) if gold_branches else 0.0)
                 * (1.0 if len(gold_branches) == len(generated_branches) else 0.8)
                 * (1.0 if type_match else 0.9),
        "branchCountMatch": len(gold_branches) == len(generated_branches),
        "typeMatch": type_match,
        "correspondingBranches": sum(item["corresponding"] for item in details),
        "totalGoldBranches": len(gold_branches),
        "elementCoverage": weighted_covered / weighted_total if weighted_total else 1.0,
        "details": sorted(details, key=lambda item: item["goldBranch"]),
    }


def compare_branches(gold: dict[str, Any], generated: dict[str, Any],
                     element_map: dict[str, str]) -> dict[str, Any]:
    gold_splits = split_branches(gold)
    generated_splits = split_branches(generated)
    candidates = []
    for gold_index, gold_split in enumerate(gold_splits):
        for generated_index, generated_split in enumerate(generated_splits):
            comparison = best_branch_pairing(gold_split, generated_split, element_map)
            candidates.append((comparison["score"], gold_index, generated_index, comparison))
    assignments: dict[int, tuple[int, dict[str, Any]]] = {}
    used_generated: set[int] = set()
    for _, gold_index, generated_index, comparison in sorted(candidates, reverse=True, key=lambda item: item[0]):
        if gold_index not in assignments and generated_index not in used_generated:
            assignments[gold_index] = (generated_index, comparison)
            used_generated.add(generated_index)
    details = []
    for gold_index, gold_split in enumerate(gold_splits):
        if gold_index in assignments:
            generated_index, comparison = assignments[gold_index]
            generated_split = generated_splits[generated_index]
            details.append({
                "goldSplitId": gold_split["splitId"],
                "goldSplitType": gold_split["splitType"],
                "generatedSplitId": generated_split["splitId"],
                "generatedSplitType": generated_split["splitType"],
                **comparison,
            })
        else:
            branch_details = [{
                "goldBranch": index + 1, "generatedBranch": None, "coverage": 0.0,
                "covered": 0, "total": len(branch["businessElements"]), "corresponding": False,
                "goldElements": branch["businessNames"], "generatedElements": [],
            } for index, branch in enumerate(gold_split["branches"])]
            details.append({
                "goldSplitId": gold_split["splitId"], "goldSplitType": gold_split["splitType"],
                "generatedSplitId": None, "generatedSplitType": None, "score": 0.0,
                "branchCountMatch": False, "typeMatch": False, "correspondingBranches": 0,
                "totalGoldBranches": len(branch_details), "elementCoverage": 0.0,
                "details": branch_details,
            })
    total_branches = sum(item["totalGoldBranches"] for item in details)
    corresponding = sum(item["correspondingBranches"] for item in details)
    total_elements = sum(branch["total"] for item in details for branch in item["details"])
    covered_elements = sum(branch["covered"] for item in details for branch in item["details"])
    return {
        "goldSplits": len(gold_splits),
        "generatedSplits": len(generated_splits),
        "correspondingBranches": corresponding,
        "totalGoldBranches": total_branches,
        "consistencyRate": corresponding / total_branches if total_branches else 1.0,
        "elementCoverage": covered_elements / total_elements if total_elements else 1.0,
        "splits": details,
    }


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Semantic BPMN 11-20 comparison",
        "",
        "Element coverage uses one-to-one bilingual semantic task matching (high/medium confidence) plus event-type matching. Branches are extracted from every node with multiple outgoing sequence flows; a branch ends at the nearest common reachable node. A corresponding branch requires at least 50% of its gold business elements to occur together in one generated branch.",
        "",
        "| Sample | Structure | Task coverage | Event coverage | Business-element coverage | Branch consistency | Branch element coverage |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["samples"]:
        coverage = item["elementCoverage"]
        branch = item["branchComparison"]
        lines.append(
            f"| {item['sample']} | {'PASS' if item['structure']['valid'] else 'FAIL'} | "
            f"{coverage['matchedTasks']}/{coverage['goldTasks']} ({pct(coverage['taskCoverage'])}) | "
            f"{coverage['matchedEvents']}/{coverage['goldEvents']} ({pct(coverage['eventCoverage'])}) | "
            f"{coverage['matchedBusinessElements']}/{coverage['goldBusinessElements']} ({pct(coverage['businessElementCoverage'])}) | "
            f"{branch['correspondingBranches']}/{branch['totalGoldBranches']} ({pct(branch['consistencyRate'])}) | "
            f"{pct(branch['elementCoverage'])} |"
        )
    summary = report["summary"]
    lines.extend([
        "",
        "## Overall",
        "",
        f"- Structural completeness: {summary['structurallyValid']}/{summary['samples']} BPMN files pass deterministic XML graph checks.",
        f"- Micro business-element coverage: {summary['matchedBusinessElements']}/{summary['goldBusinessElements']} ({pct(summary['businessElementCoverage'])}).",
        f"- Micro branch consistency: {summary['correspondingBranches']}/{summary['totalGoldBranches']} ({pct(summary['branchConsistencyRate'])}).",
        f"- Micro branch element coverage: {summary['coveredBranchElements']}/{summary['totalBranchElements']} ({pct(summary['branchElementCoverage'])}).",
        "",
        "## Per-split branch details",
        "",
    ])
    for item in report["samples"]:
        lines.append(f"### {item['sample']}")
        lines.append("")
        if not item["branchComparison"]["splits"]:
            lines.append("No gold split branches.")
            lines.append("")
            continue
        for split in item["branchComparison"]["splits"]:
            lines.append(
                f"- Gold split `{split['goldSplitId']}` ({split['goldSplitType']}) -> "
                f"generated `{split['generatedSplitId'] or 'NONE'}` ({split['generatedSplitType'] or 'NONE'}); "
                f"branch count {'match' if split['branchCountMatch'] else 'differs'}."
            )
            for branch in split["details"]:
                lines.append(
                    f"  - Gold branch {branch['goldBranch']} -> generated branch "
                    f"{branch['generatedBranch'] if branch['generatedBranch'] is not None else 'NONE'}: "
                    f"{branch['covered']}/{branch['total']} ({pct(branch['coverage'])}); "
                    f"gold=[{'; '.join(branch['goldElements'])}], generated=[{'; '.join(branch['generatedElements'])}]"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold-dir", type=Path, required=True)
    parser.add_argument("--generated-dir", type=Path, required=True)
    parser.add_argument("--samples", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--match-cache", type=Path, required=True)
    args = parser.parse_args()

    cache = json.loads(args.match_cache.read_text("utf-8")) if args.match_cache.exists() else {}
    client = LlmClient(load_settings().llm)
    samples = []
    for sample in args.samples:
        gold = parse_bpmn(args.gold_dir / f"{sample}.bpmn")
        generated = parse_bpmn(args.generated_dir / f"{sample}.bpmn")
        if sample not in cache:
            cache[sample] = llm_task_matches(client, sample, gold, generated)
            args.match_cache.parent.mkdir(parents=True, exist_ok=True)
            args.match_cache.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
        task_match = cache[sample]
        events = event_matches(gold, generated)
        element_map = {item["goldId"]: item["generatedId"] for item in task_match["matches"]}
        element_map.update({item["goldId"]: item["generatedId"] for item in events})
        gold_tasks = typed_nodes(gold, TASK_TAGS)
        gold_events = typed_nodes(gold, EVENT_TAGS)
        matched_tasks = len(task_match["matches"])
        matched_events = len(events)
        element_coverage = {
            "goldTasks": len(gold_tasks), "generatedTasks": len(typed_nodes(generated, TASK_TAGS)),
            "matchedTasks": matched_tasks,
            "taskCoverage": matched_tasks / len(gold_tasks) if gold_tasks else 1.0,
            "goldEvents": len(gold_events), "generatedEvents": len(typed_nodes(generated, EVENT_TAGS)),
            "matchedEvents": matched_events,
            "eventCoverage": matched_events / len(gold_events) if gold_events else 1.0,
            "goldGateways": len(typed_nodes(gold, GATEWAY_TAGS)),
            "generatedGateways": len(typed_nodes(generated, GATEWAY_TAGS)),
            "goldSequenceFlows": len(gold["edges"]), "generatedSequenceFlows": len(generated["edges"]),
            "goldBusinessElements": len(gold_tasks) + len(gold_events),
            "matchedBusinessElements": matched_tasks + matched_events,
            "businessElementCoverage": ((matched_tasks + matched_events) / (len(gold_tasks) + len(gold_events))
                                        if gold_tasks or gold_events else 1.0),
        }
        samples.append({
            "sample": sample,
            "structure": structural_check(generated),
            "elementCoverage": element_coverage,
            "taskMatches": task_match,
            "eventMatches": events,
            "branchComparison": compare_branches(gold, generated, element_map),
        })

    summary = {
        "samples": len(samples),
        "structurallyValid": sum(item["structure"]["valid"] for item in samples),
        "matchedBusinessElements": sum(item["elementCoverage"]["matchedBusinessElements"] for item in samples),
        "goldBusinessElements": sum(item["elementCoverage"]["goldBusinessElements"] for item in samples),
        "correspondingBranches": sum(item["branchComparison"]["correspondingBranches"] for item in samples),
        "totalGoldBranches": sum(item["branchComparison"]["totalGoldBranches"] for item in samples),
        "coveredBranchElements": sum(branch["covered"] for item in samples
                                     for split in item["branchComparison"]["splits"]
                                     for branch in split["details"]),
        "totalBranchElements": sum(branch["total"] for item in samples
                                   for split in item["branchComparison"]["splits"]
                                   for branch in split["details"]),
    }
    summary["businessElementCoverage"] = summary["matchedBusinessElements"] / summary["goldBusinessElements"]
    summary["branchConsistencyRate"] = (summary["correspondingBranches"] / summary["totalGoldBranches"]
                                        if summary["totalGoldBranches"] else 1.0)
    summary["branchElementCoverage"] = (summary["coveredBranchElements"] / summary["totalBranchElements"]
                                        if summary["totalBranchElements"] else 1.0)
    report = {"method": {
        "elementMatch": "strict one-to-one bilingual semantic task match, accepting high/medium confidence; events matched by BPMN event type",
        "branchScope": "from each multi-outgoing node to the nearest common reachable node",
        "correspondingBranchThreshold": 0.5,
    }, "summary": summary, "samples": samples}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    args.markdown.write_text(render_markdown(report), "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

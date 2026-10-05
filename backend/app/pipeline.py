from __future__ import annotations

import json
from typing import Callable

from pydantic import ValidationError

from .bpmn import graph_to_bpmn
from .config import Settings
from .domain import EditCall, Graph, Issue, Segment, Sentence
from .editor import EditError, apply_edits, apply_graph_patch
from .llm import LlmClient, parse_json_object, prompt
from .layout import BpmnLayoutClient
from .segmentation import FixedLengthStrategy, SingleSegmentStrategy, split_sentences
from .validation import validate_graph, validate_incremental_batch


STAGES = ["INPUT_READY", "SENTENCES_PREPARED", "SEMANTIC_RESOLVED", "SEGMENTS_CREATED",
          "CONTEXT_BUILT", "SUBGRAPH_GENERATED", "EDITS_GENERATED", "CANDIDATE_APPLIED", "STRUCTURE_VALIDATED",
          "SEMANTIC_REVIEWED", "GENERATOR_RECONSIDERING", "GENERATOR_DECIDED", "REPAIRING",
          "SEGMENT_COMMITTED", "FINAL_VALIDATED", "BPMN_GENERATED", "COMPLETED"]


class AgentOutputValidationError(ValueError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("; ".join(issue["message"] for issue in issues))


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = LlmClient(settings.llm) if settings.llm.model and settings.llm.resolved_key() else None
        self.layout = BpmnLayoutClient(settings.layout) if settings.layout.enabled else None

    def initial_state(self, input_text: str, strategy: str) -> dict:
        graph = Graph()
        graph.metadata.segmentationStrategy = strategy
        graph.metadata.promptVersion = "v5"
        return {"strategy": strategy, "sentences": [], "normalized": None, "segments": [], "segmentIndex": 0,
                "graph": graph.model_dump(), "candidate": None, "subgraph": None,
                "edits": [], "issues": [], "snapshots": [],
                "llmCalls": [], "repairCount": 0, "repairHistory": [], "applyingRepair": False,
                "agentFailures": [], "structureWarnings": [], "reviewWarnings": [], "reviewHistory": [],
                "reviewFindings": [], "reviewResolutions": [], "rejectedFindings": [],
                "reviewRevisionCount": 0, "reviewRevisionFailures": [],
                "reviewerEnabled": self.settings.pipeline.reviewer_enabled,
                "repairPlan": None, "finalizing": False, "bpmnXml": None}

    def step(self, stage: str, state: dict, input_text: str) -> tuple[str, dict]:
        graph = Graph.model_validate(state["graph"])
        reviewer_enabled = self.settings.pipeline.reviewer_enabled
        state["reviewerEnabled"] = reviewer_enabled
        if not reviewer_enabled and stage in {
            "SEMANTIC_REVIEWED", "GENERATOR_RECONSIDERING", "GENERATOR_DECIDED",
        }:
            return self._commit_segment(state)
        if stage == "INPUT_READY":
            state["sentences"] = [s.model_dump() for s in split_sentences(input_text)]
            return "SENTENCES_PREPARED", state
        if stage == "SENTENCES_PREPARED":
            self._require_llm()
            body = "\n".join(f'{s["id"]} {s["text"]}' for s in state["sentences"])
            state["normalized"] = self._complete_json(
                state, "semantic_resolver",
                [{"role": "system", "content": prompt("semantic_resolver")}, {"role": "user", "content": body}],
                validator=lambda value: self._validate_semantic_resolution(value, state["sentences"]),
            )
            return "SEMANTIC_RESOLVED", state
        if stage == "SEMANTIC_RESOLVED":
            sentences = [Sentence(id=item["id"], text=item.get("normalized", item.get("text", "")))
                         for item in state["normalized"]["sentences"]]
            strategy = state["strategy"]
            if strategy == "fixed_length":
                segments = FixedLengthStrategy(self.settings.pipeline.fixed_length_sentences).segment(sentences)
            elif strategy == "single_segment":
                segments = SingleSegmentStrategy().segment(sentences)
            else:
                self._require_llm()
                body = "\n".join(f"{s.id} {s.text}" for s in sentences)
                raw = self._complete_json(
                    state, "planner",
                    [{"role": "system", "content": prompt("planner")}, {"role": "user", "content": body}],
                )
                by_id = {s.id: i for i, s in enumerate(sentences)}
                segments = []
                for item in raw["segments"]:
                    ids = [s.id for s in sentences[by_id[item["start"]]:by_id[item["end"]] + 1]]
                    segments.append(Segment(**item, sentenceIds=ids))
            state["segments"] = [s.model_dump() for s in segments]
            return "SEGMENTS_CREATED", state
        if stage in {"SEGMENTS_CREATED", "SEGMENT_COMMITTED"}:
            if state["segmentIndex"] >= len(state["segments"]):
                issues = validate_graph(graph, final=True)
                state["issues"] = [issue.model_dump() for issue in issues]
                if issues:
                    state["candidate"] = graph.model_dump()
                    state["context"] = self._finalization_context(state, graph)
                    state["repairCount"] = 0
                    state["finalizing"] = True
                    return "REPAIRING", state
                return "FINAL_VALIDATED", state
            state["context"] = self._context(state, graph)
            return "CONTEXT_BUILT", state
        if stage == "CONTEXT_BUILT":
            self._require_llm()
            state["subgraph"] = self._complete_json(
                state, "generator",
                [{"role": "system", "content": prompt("generator")},
                 {"role": "user", "content": json.dumps(state["context"], ensure_ascii=False)}],
                validator=lambda value: self._validate_generated_patch(value, graph),
            )
            state["edits"] = []
            return "SUBGRAPH_GENERATED", state
        if stage == "SUBGRAPH_GENERATED":
            segment = state["segments"][state["segmentIndex"]]
            try:
                candidate = apply_graph_patch(
                    graph,
                    state["subgraph"]["nodes"],
                    state["subgraph"]["edges"],
                    segment["id"],
                    state["subgraph"]["removeNodeIds"],
                    state["subgraph"]["removeEdgeIds"],
                )
            except (EditError, ValidationError, KeyError, TypeError, ValueError) as exc:
                state["candidate"] = graph.model_dump()
                state["issues"] = [Issue(
                    code="GRAPH_PATCH_APPLICATION_FAILED",
                    message=f"Generate Graph Patch 无法应用：{exc}",
                    sourceSentences=segment["sentenceIds"],
                ).model_dump()]
                return "REPAIRING", state
            state["candidate"] = candidate.model_dump()
            return "CANDIDATE_APPLIED", state
        if stage == "EDITS_GENERATED":
            segment = ({"id": "FINALIZATION", "sentenceIds": []} if state.get("finalizing") else
                       state["segments"][state["segmentIndex"]])
            repair_base = state.get("applyingRepair") and state.get("candidate")
            base = Graph.model_validate(state["candidate"]) if repair_base else graph
            try:
                candidate = apply_edits(base, [EditCall.model_validate(c) for c in state["edits"]], segment["id"])
            except (EditError, ValidationError, KeyError, TypeError, ValueError) as exc:
                state["issues"] = [Issue(
                    code="EDIT_APPLICATION_FAILED",
                    message=f"工具编辑无法应用：{exc}",
                    sourceSentences=segment["sentenceIds"],
                ).model_dump()]
                state["applyingRepair"] = False
                return "REPAIRING", state
            state["candidate"] = candidate.model_dump()
            state["applyingRepair"] = False
            if state.get("finalizing"):
                issues = validate_graph(candidate, final=True)
                state["issues"] = [issue.model_dump() for issue in issues]
                if issues:
                    return "REPAIRING", state
                state["graph"] = candidate.model_dump()
                state["candidate"] = None
                state["finalizing"] = False
                return "FINAL_VALIDATED", state
            return "CANDIDATE_APPLIED", state
        if stage == "CANDIDATE_APPLIED":
            candidate = Graph.model_validate(state["candidate"])
            is_final_segment = bool(state.get("context", {}).get("isFinalSegment"))
            issues = [
                *validate_graph(candidate, final=is_final_segment),
                *validate_incremental_batch(graph, candidate, final=is_final_segment),
            ]
            unique_issues: dict[tuple[str, tuple[str, ...]], Issue] = {}
            for issue in issues:
                unique_issues.setdefault((issue.code, tuple(issue.elementIds)), issue)
            segment_id = state["segments"][state["segmentIndex"]]["id"]
            state["structureWarnings"] = [
                warning for warning in state.get("structureWarnings", [])
                if warning.get("segmentId") != segment_id
            ]
            state["structureWarnings"].extend(
                {"segmentId": segment_id, **issue.model_dump()}
                for issue in unique_issues.values() if issue.severity == "warning"
            )
            state["issues"] = [
                issue.model_dump() for issue in unique_issues.values()
                if issue.severity == "error"
            ]
            return "STRUCTURE_VALIDATED", state
        if stage == "STRUCTURE_VALIDATED":
            if state["issues"]: return "REPAIRING", state
            if not reviewer_enabled:
                state["review"] = None
                return self._commit_segment(state)
            self._require_llm()
            segment_id = state["segments"][state["segmentIndex"]]["id"]
            payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": state["candidate"],
                "directedEdges": self._directed_edges(state["candidate"]),
            }
            if state.get("reviewResolutions"):
                payload["previousFindings"] = state["reviewResolutions"]
            review = self._complete_json(
                state, "reviewer",
                [{"role": "system", "content": prompt("reviewer")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                validator=self._validate_review,
            )
            state["review"] = review
            state["reviewFindings"] = review["findings"]
            state.setdefault("reviewHistory", []).append({
                "segmentId": segment_id,
                "findings": review["findings"],
                "previousFindings": payload.get("previousFindings", []),
            })
            state["reviewResolutions"] = []
            return "SEMANTIC_REVIEWED", state
        if stage == "SEMANTIC_REVIEWED":
            if state.get("reviewFindings"):
                if state.get("reviewRevisionCount", 0) >= self.settings.pipeline.max_semantic_repairs:
                    return self._accept_unrevised_candidate(
                        state,
                        f"语义修订已达到上限 {self.settings.pipeline.max_semantic_repairs}",
                    )
                return "GENERATOR_RECONSIDERING", state
            return self._commit_segment(state)
        if stage == "GENERATOR_RECONSIDERING":
            self._require_llm()
            findings = state.get("reviewFindings", [])
            payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": state["candidate"],
                "directedEdges": self._directed_edges(state["candidate"]),
                "findings": findings,
            }
            decision = self._complete_json(
                state, "generator",
                [{"role": "system", "content": prompt("generator_review")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                validator=lambda value: self._validate_generator_decision(value, findings),
            )
            state["generatorReview"] = decision
            return "GENERATOR_DECIDED", state
        if stage == "GENERATOR_DECIDED":
            decision = state["generatorReview"]
            findings_by_id = {item["id"]: item for item in state.get("reviewFindings", [])}
            accepted_ids = {
                item["findingId"] for item in decision["decisions"]
                if item["decision"] == "accept"
            }
            resolutions = []
            for item in decision["decisions"]:
                finding = findings_by_id[item["findingId"]]
                resolution = {"finding": finding, **item}
                resolutions.append(resolution)
            if not accepted_ids:
                for resolution in resolutions:
                    state.setdefault("rejectedFindings", []).append({
                        "segmentId": state["segments"][state["segmentIndex"]]["id"],
                        **resolution,
                    })
                state["reviewFindings"] = []
                state["reviewResolutions"] = []
                return self._commit_segment(state)
            segment = state["segments"][state["segmentIndex"]]
            revision_payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": state["candidate"],
                "acceptedFindings": [
                    findings_by_id[finding_id] for finding_id in accepted_ids
                ],
                "revisionPlan": decision["revisionPlan"],
            }
            try:
                patch = self._complete_json(
                    state, "generator",
                    [{"role": "system", "content": prompt("generator_patch")},
                     {"role": "user", "content": json.dumps(revision_payload, ensure_ascii=False)}],
                    validator=lambda value: self._validate_generator_revision_patch(
                        value, Graph.model_validate(state["candidate"])
                    ),
                )
            except ValueError as exc:
                return self._accept_unrevised_candidate(state, str(exc))
            candidate_before_revision = Graph.model_validate(state["candidate"])
            candidate = apply_graph_patch(
                candidate_before_revision,
                patch["nodes"], patch["edges"], segment["id"],
                patch["removeNodeIds"], patch["removeEdgeIds"],
            )
            for resolution in resolutions:
                if resolution["decision"] == "reject":
                    state.setdefault("rejectedFindings", []).append({
                        "segmentId": segment["id"], **resolution,
                    })
            state["reviewFindings"] = []
            state["candidate"] = candidate.model_dump()
            state["subgraph"] = patch
            state["reviewResolutions"] = resolutions
            state["reviewRevisionCount"] = state.get("reviewRevisionCount", 0) + 1
            return "CANDIDATE_APPLIED", state
        if stage == "REPAIRING":
            if state["repairCount"] >= self.settings.pipeline.max_semantic_repairs:
                raise ValueError(f"局部修订已达到上限 {self.settings.pipeline.max_semantic_repairs}")
            self._require_llm()
            repair_context = state["context"]
            if state.get("finalizing") or repair_context.get("isFinalSegment"):
                repair_graph = Graph.model_validate(state.get("candidate") or state["graph"])
                repair_context = self._finalization_context(state, repair_graph)
            payload = {"context": repair_context, "committedGraph": state["graph"],
                       "candidateGraph": state["candidate"], "previousEdits": state["edits"],
                       "generatedSubgraph": state.get("subgraph"),
                       "directedEdges": self._directed_edges(state["candidate"]),
                       "issues": state["issues"]}
            plan = self._complete_json(
                state, "repair_plan",
                [{"role": "system", "content": prompt("repair_plan")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                validator=self._validate_repair_plan,
            )
            state["repairPlan"] = plan["repairPlan"]
            payload["repairPlan"] = plan["repairPlan"]
            result = self._complete_tools(
                state, "repair",
                [{"role": "system", "content": prompt("repair")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                repair=True,
            )
            generated_edits = [call.model_dump() for call in result.tool_calls]
            state["repairHistory"].append({"attempt": state["repairCount"] + 1,
                                            "issues": state["issues"],
                                            "repairPlan": plan["repairPlan"],
                                            "generatedSubgraph": state.get("subgraph"),
                                            "edits": generated_edits})
            state["repairCount"] += 1
            state["edits"] = generated_edits
            state["issues"] = []
            state["applyingRepair"] = state.get("candidate") is not None
            return "EDITS_GENERATED", state
        if stage == "FINAL_VALIDATED":
            final_graph = Graph.model_validate(state["graph"])
            state["bpmnXml"] = self._render_bpmn(final_graph)
            final_snapshot = {
                "segment": {"id": "FINALIZATION", "title": "最终流程", "sentenceIds": []},
                "graph": final_graph.model_dump(),
                "bpmnXml": state["bpmnXml"],
            }
            snapshots = state.setdefault("snapshots", [])
            if snapshots and snapshots[-1].get("segment", {}).get("id") == "FINALIZATION":
                snapshots[-1] = final_snapshot
            else:
                snapshots.append(final_snapshot)
            return "BPMN_GENERATED", state
        if stage == "BPMN_GENERATED": return "COMPLETED", state
        return stage, state

    def _final_validate(self, state: dict) -> dict:
        issues = validate_graph(Graph.model_validate(state["graph"]), final=True)
        state["issues"] = [i.model_dump() for i in issues]
        if issues: raise ValueError("终局校验失败")
        return state

    def _commit_segment(self, state: dict) -> tuple[str, dict]:
        candidate = Graph.model_validate(state["candidate"])
        segment = state["segments"][state["segmentIndex"]]
        candidate.metadata.processedSegments.append(segment["id"])
        state["graph"] = candidate.model_dump()
        state["snapshots"].append({
            "segment": segment,
            "graph": candidate.model_dump(),
            "bpmnXml": self._render_bpmn(candidate),
        })
        state["candidate"] = None
        state["subgraph"] = None
        state["reviewFindings"] = []
        state["reviewResolutions"] = []
        state["reviewRevisionCount"] = 0
        state.pop("generatorReview", None)
        state["segmentIndex"] += 1
        state["repairCount"] = 0
        return "SEGMENT_COMMITTED", state

    def _accept_unrevised_candidate(self, state: dict, error: str) -> tuple[str, dict]:
        segment = state["segments"][state["segmentIndex"]]
        failure = {
            "segmentId": segment["id"],
            "message": error,
            "findings": state.get("reviewFindings", []),
            "revisionPlan": state.get("generatorReview", {}).get("revisionPlan", ""),
            "candidateRetained": True,
        }
        state.setdefault("reviewRevisionFailures", []).append(failure)
        state.setdefault("reviewWarnings", []).append({
            "segmentId": segment["id"],
            "code": "REVIEW_REVISION_FAILED",
            "message": error,
            "severity": "warning",
        })
        return self._commit_segment(state)

    def _render_bpmn(self, graph: Graph) -> str:
        semantic_xml = graph_to_bpmn(graph)
        return self.layout.layout(semantic_xml) if self.layout else semantic_xml

    def _context(self, state: dict, graph: Graph) -> dict:
        current_index = state["segmentIndex"]
        segment = state["segments"][current_index]
        return {"currentSegment": segment, "contextSegments": state["segments"][:current_index + 1],
                "segmentIndex": current_index, "totalSegments": len(state["segments"]),
                "isFirstSegment": current_index == 0, "isFinalSegment": current_index == len(state["segments"]) - 1,
                "descriptionPrefix": self._description_prefix(state),
                "graph": graph.model_dump()}

    def _finalization_context(self, state: dict, graph: Graph) -> dict:
        return {
            "currentSegment": {"id": "FINALIZATION", "title": "终局闭合", "sentenceIds": []},
            "contextSegments": state["segments"],
            "descriptionPrefix": [
                {"id": item["id"], "text": item.get("normalized", item.get("text", ""))}
                for item in (state.get("normalized") or {}).get("sentences", [])
            ],
            "graph": graph.model_dump(),
            "isFinalization": True,
        }

    @staticmethod
    def _description_prefix(state: dict) -> list[dict[str, str]]:
        current_index = state["segmentIndex"]
        prefix_ids = {
            sentence_id
            for segment in state["segments"][:current_index + 1]
            for sentence_id in segment["sentenceIds"]
        }
        return [
            {"id": item["id"], "text": item.get("normalized", item.get("text", ""))}
            for item in state["normalized"]["sentences"]
            if item["id"] in prefix_ids
        ]

    @staticmethod
    def _directed_edges(graph: dict) -> list[dict[str, str]]:
        """Expose the graph topology without node metadata competing for attention."""
        return [
            {"id": edge["id"], "source": edge["source"], "target": edge["target"]}
            for edge in graph.get("edges", [])
        ]

    def _record(self, state: dict, agent: str, result):
        state["llmCalls"].append({"agent": agent, "usage": result.usage, "latencyMs": result.latency_ms,
                                  "toolCallCount": len(result.tool_calls),
                                  "finishReason": result.finish_reason,
                                  "contentChars": len(result.content or ""),
                                  "thinkingMode": result.thinking_mode})

    def _complete_json(self, state: dict, agent: str, messages: list[dict],
                       validator: Callable[[dict], dict] | None = None) -> dict:
        attempts = self.settings.pipeline.max_agent_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                attempt_messages = messages if attempt == 1 else [*messages, {
                    "role": "user",
                    "content": json.dumps({
                        "type": "OUTPUT_VALIDATION_FAILED",
                        "agent": agent,
                        "issues": (last_error.issues if isinstance(last_error, AgentOutputValidationError)
                                   else [{"code": "INVALID_JSON", "message": str(last_error)}]),
                        "instruction": "修正全部问题，重新生成完整结果；只返回符合原输出格式的 JSON 对象。",
                    }, ensure_ascii=False),
                }]
                result = self.llm.complete(attempt_messages, agent=agent)
                self._record(state, agent, result)
                value = parse_json_object(result.content, agent)
                return validator(value) if validator else value
            except ValueError as exc:
                last_error = exc
                state.setdefault("agentFailures", []).append(
                    {"agent": agent, "attempt": attempt, "error": str(exc)}
                )
        raise ValueError(f"{agent} failed after {attempts} attempts: {last_error}")

    @staticmethod
    def _validate_semantic_resolution(value: dict, source_sentences: list[dict]) -> dict:
        raw_sentences = value.get("sentences")
        if not isinstance(raw_sentences, list):
            raise AgentOutputValidationError([{
                "code": "SENTENCES_NOT_ARRAY",
                "message": "sentences 必须是数组",
            }])

        expected_ids = [item["id"] for item in source_sentences]
        original_by_id = {item["id"]: item["text"] for item in source_sentences}
        returned_ids: list[str] = []
        issues: list[dict] = []
        for index, item in enumerate(raw_sentences):
            if not isinstance(item, dict):
                issues.append({
                    "code": "SENTENCE_NOT_OBJECT",
                    "message": f"sentences[{index}] 必须是对象",
                    "index": index,
                })
                continue
            sentence_id = item.get("id")
            if not isinstance(sentence_id, str):
                issues.append({
                    "code": "INVALID_SENTENCE_ID",
                    "message": f"sentences[{index}].id 必须是字符串",
                    "index": index,
                })
                continue
            returned_ids.append(sentence_id)
            if sentence_id not in original_by_id:
                issues.append({
                    "code": "UNKNOWN_SENTENCE_ID",
                    "message": f"返回了输入中不存在的句子 ID: {sentence_id}",
                    "sentenceId": sentence_id,
                })
                continue
            if item.get("original") != original_by_id[sentence_id]:
                issues.append({
                    "code": "ORIGINAL_CHANGED",
                    "message": f"{sentence_id}.original 与输入原文不一致，必须逐字保留",
                    "sentenceId": sentence_id,
                    "expectedOriginal": original_by_id[sentence_id],
                    "actualOriginal": item.get("original"),
                })
            if not isinstance(item.get("normalized"), str) or not item["normalized"].strip():
                issues.append({
                    "code": "INVALID_NORMALIZED_SENTENCE",
                    "message": f"{sentence_id}.normalized 必须是非空字符串",
                    "sentenceId": sentence_id,
                })

        duplicates = sorted({sentence_id for sentence_id in returned_ids
                             if returned_ids.count(sentence_id) > 1})
        if duplicates:
            issues.append({
                "code": "DUPLICATE_SENTENCE_IDS",
                "message": f"句子 ID 重复: {', '.join(duplicates)}",
                "sentenceIds": duplicates,
            })
        missing = [sentence_id for sentence_id in expected_ids if sentence_id not in returned_ids]
        if missing:
            issues.append({
                "code": "MISSING_SENTENCE_IDS",
                "message": f"缺少输入句子: {', '.join(missing)}",
                "sentenceIds": missing,
            })
        if not duplicates and not missing and returned_ids != expected_ids:
            issues.append({
                "code": "SENTENCE_ORDER_CHANGED",
                "message": "句子顺序发生变化，必须保持输入顺序",
                "expectedIds": expected_ids,
                "actualIds": returned_ids,
            })
        if issues:
            raise AgentOutputValidationError(issues)
        return value

    @staticmethod
    def _validate_generated_patch(value: dict, graph: Graph,
                                  allow_empty: bool = False) -> dict:
        issues: list[dict] = []
        extra_fields = sorted(set(value) - {
            "nodes", "edges", "removeNodeIds", "removeEdgeIds",
        })
        if extra_fields:
            issues.append({
                "code": "GRAPH_PATCH_EXTRA_FIELDS",
                "message": f"Graph Patch 包含未定义字段: {', '.join(extra_fields)}",
            })
        nodes = value.get("nodes")
        edges = value.get("edges")
        remove_node_ids = value.get("removeNodeIds", [])
        remove_edge_ids = value.get("removeEdgeIds", [])
        if not isinstance(nodes, list):
            issues.append({"code": "GRAPH_PATCH_NODES_NOT_ARRAY", "message": "nodes 必须是数组"})
        if not isinstance(edges, list):
            issues.append({"code": "GRAPH_PATCH_EDGES_NOT_ARRAY", "message": "edges 必须是数组"})
        if not isinstance(remove_node_ids, list) or not all(
                isinstance(item, str) for item in remove_node_ids):
            issues.append({
                "code": "GRAPH_PATCH_REMOVE_NODES_NOT_STRING_ARRAY",
                "message": "removeNodeIds 必须是字符串数组",
            })
        if not isinstance(remove_edge_ids, list) or not all(
                isinstance(item, str) for item in remove_edge_ids):
            issues.append({
                "code": "GRAPH_PATCH_REMOVE_EDGES_NOT_STRING_ARRAY",
                "message": "removeEdgeIds 必须是字符串数组",
            })
        if issues:
            raise AgentOutputValidationError(issues)
        nodes = [dict(item) if isinstance(item, dict) else item for item in nodes]
        edges = [dict(item) if isinstance(item, dict) else item for item in edges]
        for item in [*nodes, *edges]:
            if isinstance(item, dict):
                item.pop("introducedInSegment", None)
        for item in nodes:
            if not isinstance(item, dict):
                continue
            item.pop("taskType", None)
            for field in ("eventType", "trigger", "eventRole", "gatewayType", "role"):
                if item.get(field) is None:
                    item.pop(field, None)
        if not allow_empty and not nodes and not edges and not remove_node_ids and not remove_edge_ids:
            raise AgentOutputValidationError([{
                "code": "EMPTY_GRAPH_PATCH",
                "message": "当前片段必须至少新增、替换或删除一个元素",
            }])
        allowed_node_fields = {
            "task": {"id", "kind", "name", "sourceSentences"},
            "event": {"id", "kind", "name", "eventType", "trigger", "eventRole", "sourceSentences"},
            "gateway": {"id", "kind", "name", "gatewayType", "role", "sourceSentences"},
        }
        allowed_edge_fields = {
            "id", "source", "target", "condition", "isDefault", "sourceSentences",
        }
        for collection_name, items in (("nodes", nodes), ("edges", edges)):
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    issues.append({
                        "code": "SUBGRAPH_ITEM_NOT_OBJECT",
                        "message": f"{collection_name}[{index}] 必须是对象",
                    })
                elif collection_name == "nodes" and item.get("kind") in allowed_node_fields:
                    extras = sorted(set(item) - allowed_node_fields[item["kind"]])
                    if extras:
                        issues.append({
                            "code": "SUBGRAPH_NODE_EXTRA_FIELDS",
                            "message": f"nodes[{index}] 包含不属于 {item['kind']} 的字段: {', '.join(extras)}",
                        })
                elif collection_name == "edges":
                    extras = sorted(set(item) - allowed_edge_fields)
                    if extras:
                        issues.append({
                            "code": "SUBGRAPH_EDGE_EXTRA_FIELDS",
                            "message": f"edges[{index}] 包含未定义字段: {', '.join(extras)}",
                        })
        if issues:
            raise AgentOutputValidationError(issues)
        try:
            apply_graph_patch(
                graph, nodes, edges, "VALIDATION", remove_node_ids, remove_edge_ids
            )
        except (EditError, ValidationError, KeyError, TypeError, ValueError) as exc:
            raise AgentOutputValidationError([{
                "code": "INVALID_GRAPH_PATCH",
                "message": f"Graph Patch 节点、边、删除项或引用无效：{exc}",
            }]) from None
        return {
            "nodes": nodes,
            "edges": edges,
            "removeNodeIds": remove_node_ids,
            "removeEdgeIds": remove_edge_ids,
        }

    @staticmethod
    def _validate_review(value: dict) -> dict:
        extra_fields = sorted(set(value) - {"approved", "findings"})
        raw_findings = value.get("findings")
        issues: list[dict] = []
        if extra_fields:
            issues.append({
                "code": "REVIEW_EXTRA_FIELDS",
                "message": f"Reviewer 输出包含未定义字段: {', '.join(extra_fields)}",
            })
        if not isinstance(raw_findings, list):
            issues.append({"code": "FINDINGS_NOT_ARRAY", "message": "findings 必须是数组"})
            raw_findings = []
        findings: list[dict] = []
        seen_ids: set[str] = set()
        required = {"id", "code", "message", "elementIds", "sourceSentences"}
        for index, raw in enumerate(raw_findings):
            if not isinstance(raw, dict):
                issues.append({
                    "code": "FINDING_NOT_OBJECT",
                    "message": f"findings[{index}] 必须是对象",
                })
                continue
            missing = sorted(required - set(raw))
            extras = sorted(set(raw) - required)
            if missing or extras:
                issues.append({
                    "code": "INVALID_FINDING_FIELDS",
                    "message": f"findings[{index}] 缺少 {missing} 或包含额外字段 {extras}",
                })
                continue
            finding_id = raw["id"]
            if not isinstance(finding_id, str) or not finding_id.strip() or finding_id in seen_ids:
                issues.append({
                    "code": "INVALID_FINDING_ID",
                    "message": f"findings[{index}].id 必须是唯一非空字符串",
                })
                continue
            if not all(isinstance(raw[field], str) and raw[field].strip()
                       for field in ("code", "message")):
                issues.append({
                    "code": "INVALID_FINDING_TEXT",
                    "message": f"findings[{index}] 的 code 和 message 必须是非空字符串",
                })
                continue
            if not all(isinstance(raw[field], list)
                       and all(isinstance(item, str) for item in raw[field])
                       for field in ("elementIds", "sourceSentences")):
                issues.append({
                    "code": "INVALID_FINDING_REFERENCES",
                    "message": f"findings[{index}] 的 elementIds 和 sourceSentences 必须是字符串数组",
                })
                continue
            seen_ids.add(finding_id)
            findings.append(raw)
        if issues:
            raise AgentOutputValidationError(issues)
        approved = value.get("approved")
        if not isinstance(approved, bool) or approved != (len(findings) == 0):
            raise AgentOutputValidationError([{
                "code": "REVIEW_APPROVAL_MISMATCH",
                "message": "approved 必须是布尔值，且仅在 findings 为空时为 true",
            }])
        return {"approved": approved, "findings": findings}

    @staticmethod
    def _validate_generator_decision(value: dict, findings: list[dict]) -> dict:
        extra_fields = sorted(set(value) - {"decisions", "revisionPlan"})
        decisions = value.get("decisions")
        revision_plan = value.get("revisionPlan", "")
        issues: list[dict] = []
        if extra_fields:
            issues.append({
                "code": "GENERATOR_DECISION_EXTRA_FIELDS",
                "message": f"复议输出包含未定义字段: {', '.join(extra_fields)}",
            })
        if not isinstance(decisions, list):
            issues.append({"code": "DECISIONS_NOT_ARRAY", "message": "decisions 必须是数组"})
            decisions = []
        finding_ids = {item["id"] for item in findings}
        returned_ids: list[str] = []
        normalized: list[dict] = []
        for index, raw in enumerate(decisions):
            if not isinstance(raw, dict):
                issues.append({
                    "code": "DECISION_NOT_OBJECT",
                    "message": f"decisions[{index}] 必须是对象",
                })
                continue
            extras = sorted(set(raw) - {"findingId", "decision", "reason"})
            finding_id = raw.get("findingId")
            decision = raw.get("decision")
            reason = raw.get("reason", "")
            if extras or finding_id not in finding_ids or decision not in {"accept", "reject"}:
                issues.append({
                    "code": "INVALID_DECISION",
                    "message": f"decisions[{index}] 必须引用现有 finding 并选择 accept 或 reject",
                })
                continue
            if decision == "reject" and (not isinstance(reason, str) or not reason.strip()):
                issues.append({
                    "code": "REJECTION_REASON_REQUIRED",
                    "message": f"拒绝 {finding_id} 时必须给出非空 reason",
                })
                continue
            returned_ids.append(finding_id)
            normalized.append({
                "findingId": finding_id,
                "decision": decision,
                "reason": reason if isinstance(reason, str) else "",
            })
        if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != finding_ids:
            issues.append({
                "code": "INCOMPLETE_FINDING_DECISIONS",
                "message": "必须对每个 finding 恰好作出一次 accept 或 reject 决定",
            })
        if issues:
            raise AgentOutputValidationError(issues)
        accepted = any(item["decision"] == "accept" for item in normalized)
        if accepted and (not isinstance(revision_plan, str) or not revision_plan.strip()):
            raise AgentOutputValidationError([{
                "code": "REVISION_PLAN_REQUIRED",
                "message": "存在 accept 时必须先给出非空 revisionPlan，说明修改后的目标流程",
            }])
        if not isinstance(revision_plan, str):
            raise AgentOutputValidationError([{
                "code": "INVALID_REVISION_PLAN",
                "message": "revisionPlan 必须是字符串",
            }])
        return {"decisions": normalized, "revisionPlan": revision_plan}

    @staticmethod
    def _validate_repair_plan(value: dict) -> dict:
        if set(value) != {"repairPlan"} or not isinstance(value.get("repairPlan"), str) \
                or not value["repairPlan"].strip():
            raise AgentOutputValidationError([{
                "code": "INVALID_REPAIR_PLAN",
                "message": "repairPlan 必须是唯一字段且为非空字符串",
            }])
        return {"repairPlan": value["repairPlan"].strip()}

    @classmethod
    def _validate_generator_revision_patch(cls, value: dict, candidate: Graph) -> dict:
        patch = cls._validate_generated_patch(value, candidate)
        revised = apply_graph_patch(
            candidate,
            patch["nodes"], patch["edges"], "VALIDATION",
            patch["removeNodeIds"], patch["removeEdgeIds"],
        )
        if revised.nodes == candidate.nodes and revised.edges == candidate.edges:
            raise AgentOutputValidationError([{
                "code": "ACCEPTED_PATCH_HAS_NO_EFFECT",
                "message": "Graph Patch 必须实际改变 candidateGraph 并落实 revisionPlan",
            }])
        structural_issues = validate_graph(revised)
        if structural_issues:
            raise AgentOutputValidationError([
                {
                    "code": issue.code,
                    "message": issue.message,
                    "elementIds": issue.elementIds,
                    "sourceSentences": issue.sourceSentences,
                }
                for issue in structural_issues
            ])
        return patch

    def _complete_tools(self, state: dict, agent: str, messages: list[dict], repair: bool = False):
        attempts = self.settings.pipeline.max_agent_retries + 1
        last_error = "no tool calls"
        for attempt in range(1, attempts + 1):
            try:
                attempt_messages = messages if attempt == 1 else [
                    *messages,
                    {"role": "user", "content":
                     f"上一轮未产生可执行工具调用（{last_error}）。请至少调用一个允许的编辑工具，不要输出解释。"},
                ]
                result = self.llm.complete(attempt_messages, tools=True, repair=repair, agent=agent)
                if result.tool_calls:
                    self._record(state, agent, result)
                    return result
                last_error = "no tool calls"
                self._record(state, agent, result)
            except ValueError as exc:
                last_error = str(exc)
            state.setdefault("agentFailures", []).append(
                {"agent": agent, "attempt": attempt, "error": last_error}
            )
        raise ValueError(f"{agent} failed after {attempts} attempts: {last_error}")

    def _require_llm(self):
        if not self.llm: raise RuntimeError("请在配置文件中设置模型并通过环境变量提供 API key")

from __future__ import annotations

import json
from typing import Callable

from pydantic import ValidationError

from .bpmn import graph_to_bpmn
from .config import Settings
from .domain import EditCall, Graph, Issue, Segment, Sentence
from .editor import EditError, apply_edits
from .llm import LlmClient, parse_json_object, prompt
from .layout import BpmnLayoutClient
from .segmentation import FixedLengthStrategy, SingleSegmentStrategy, split_sentences
from .validation import open_nodes, validate_graph


STAGES = ["INPUT_READY", "SENTENCES_PREPARED", "SEMANTIC_RESOLVED", "SEGMENTS_CREATED",
          "CONTEXT_BUILT", "EDITS_GENERATED", "CANDIDATE_APPLIED", "STRUCTURE_VALIDATED",
          "SEMANTIC_REVIEWED", "REPAIRING", "SEGMENT_COMMITTED", "FINAL_VALIDATED", "BPMN_GENERATED", "COMPLETED"]


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
        graph.metadata.promptVersion = "v3"
        return {"strategy": strategy, "sentences": [], "normalized": None, "segments": [], "segmentIndex": 0,
                "graph": graph.model_dump(), "candidate": None, "edits": [], "issues": [], "snapshots": [],
                "llmCalls": [], "repairCount": 0, "repairHistory": [], "applyingRepair": False,
                "agentFailures": [], "reviewWarnings": [],
                "finalizing": False, "bpmnXml": None}

    def step(self, stage: str, state: dict, input_text: str) -> tuple[str, dict]:
        graph = Graph.model_validate(state["graph"])
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
            result = self._complete_tools(
                state, "generator",
                [{"role": "system", "content": prompt("generator")},
                 {"role": "user", "content": json.dumps(state["context"], ensure_ascii=False)}],
            )
            state["edits"] = [c.model_dump() for c in result.tool_calls]
            return "EDITS_GENERATED", state
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
            issues = validate_graph(Graph.model_validate(state["candidate"]))
            state["issues"] = [i.model_dump() for i in issues]
            return "STRUCTURE_VALIDATED", state
        if stage == "STRUCTURE_VALIDATED":
            if state["issues"]: return "REPAIRING", state
            self._require_llm()
            payload = {"context": state["context"], "previousGraph": state["graph"], "edits": state["edits"], "candidate": state["candidate"]}
            review = self._complete_json(
                state, "reviewer",
                [{"role": "system", "content": prompt("reviewer")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            review_issues = self._normalize_review_issues(review.get("issues", []))
            review_issues = self._apply_incremental_review_policy(
                review_issues,
                Graph.model_validate(state["candidate"]),
                bool(state["context"].get("isFinalSegment")),
            )
            review["issues"] = [issue.model_dump() for issue in review_issues]
            state["review"] = review
            state["issues"] = [issue.model_dump() for issue in review_issues if issue.severity == "error"]
            state.setdefault("reviewWarnings", []).extend(
                {"segmentId": state["segments"][state["segmentIndex"]]["id"], **issue.model_dump()}
                for issue in review_issues if issue.severity == "warning"
            )
            return "SEMANTIC_REVIEWED", state
        if stage == "SEMANTIC_REVIEWED":
            if state["issues"]: return "REPAIRING", state
            candidate = Graph.model_validate(state["candidate"])
            segment = state["segments"][state["segmentIndex"]]
            candidate.metadata.processedSegments.append(segment["id"])
            state["graph"] = candidate.model_dump()
            state["snapshots"].append({"segment": segment, "graph": candidate.model_dump(), "bpmnXml": self._render_bpmn(candidate)})
            state["candidate"] = None; state["segmentIndex"] += 1; state["repairCount"] = 0
            return "SEGMENT_COMMITTED", state
        if stage == "REPAIRING":
            if state["repairCount"] >= self.settings.pipeline.max_semantic_repairs:
                raise ValueError(f"局部修订已达到上限 {self.settings.pipeline.max_semantic_repairs}")
            self._require_llm()
            payload = {"context": state["context"], "committedGraph": state["graph"],
                       "candidateGraph": state["candidate"], "previousEdits": state["edits"],
                       "issues": state["issues"]}
            result = self._complete_tools(
                state, "repair",
                [{"role": "system", "content": prompt("repair")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                repair=True,
            )
            state["repairHistory"].append({"attempt": state["repairCount"] + 1,
                                            "issues": state["issues"], "edits": state["edits"]})
            state["repairCount"] += 1
            state["edits"] = [call.model_dump() for call in result.tool_calls]
            state["issues"] = []
            state["applyingRepair"] = state.get("candidate") is not None
            return "EDITS_GENERATED", state
        if stage == "FINAL_VALIDATED":
            state["bpmnXml"] = self._render_bpmn(Graph.model_validate(state["graph"]))
            return "BPMN_GENERATED", state
        if stage == "BPMN_GENERATED": return "COMPLETED", state
        return stage, state

    def _final_validate(self, state: dict) -> dict:
        issues = validate_graph(Graph.model_validate(state["graph"]), final=True)
        state["issues"] = [i.model_dump() for i in issues]
        if issues: raise ValueError("终局校验失败")
        return state

    def _render_bpmn(self, graph: Graph) -> str:
        semantic_xml = graph_to_bpmn(graph)
        return self.layout.layout(semantic_xml) if self.layout else semantic_xml

    def _context(self, state: dict, graph: Graph) -> dict:
        current_index = state["segmentIndex"]
        segment = state["segments"][current_index]
        normalized = {s["id"]: s["normalized"] for s in state["normalized"]["sentences"]}
        frontier = open_nodes(graph)
        relevant_ids = {item["id"] for item in frontier}
        segment_index = {item["id"]: index for index, item in enumerate(state["segments"])}
        earliest = current_index
        for node in graph.nodes:
            if node.id in relevant_ids and node.introducedInSegment in segment_index:
                earliest = min(earliest, segment_index[node.introducedInSegment])
        sentence_ids = [sid for item in state["segments"][earliest:current_index + 1] for sid in item["sentenceIds"]]
        return {"currentSegment": segment, "contextSegments": state["segments"][earliest:current_index + 1],
                "segmentIndex": current_index, "totalSegments": len(state["segments"]),
                "isFirstSegment": current_index == 0, "isFinalSegment": current_index == len(state["segments"]) - 1,
                "sentences": [{"id": sid, "text": normalized[sid]} for sid in sentence_ids],
                "graph": graph.model_dump(), "openNodes": frontier}

    def _finalization_context(self, state: dict, graph: Graph) -> dict:
        return {
            "currentSegment": {"id": "FINALIZATION", "title": "终局闭合", "sentenceIds": []},
            "contextSegments": state["segments"],
            "sentences": state.get("normalized", {}).get("sentences", []),
            "graph": graph.model_dump(),
            "openNodes": open_nodes(graph),
            "isFinalization": True,
        }

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

    @staticmethod
    def _normalize_review_issues(raw_issues: object) -> list[Issue]:
        if not isinstance(raw_issues, list):
            raise ValueError("reviewer issues must be a list")
        normalized: list[Issue] = []
        for raw in raw_issues:
            if not isinstance(raw, dict):
                raise ValueError("each reviewer issue must be an object")
            item = dict(raw)
            severity = str(item.get("severity", "error")).lower()
            item["severity"] = "error" if severity in {"error", "critical", "high", "blocking"} else "warning"
            normalized.append(Issue.model_validate(item))
        return normalized

    @staticmethod
    def _apply_incremental_review_policy(issues: list[Issue], candidate: Graph,
                                         is_final_segment: bool) -> list[Issue]:
        """Do not let a reviewer close an intentionally open incremental split."""
        if is_final_segment:
            return issues
        outgoing = {node.id: 0 for node in candidate.nodes}
        for edge in candidate.edges:
            outgoing[edge.source] = outgoing.get(edge.source, 0) + 1
        open_splits = {
            node.id for node in candidate.nodes
            if node.kind == "gateway" and node.role == "split" and outgoing.get(node.id, 0) < 2
        }
        result: list[Issue] = []
        for issue in issues:
            code = issue.code.upper()
            only_requests_missing_branch = (
                "INCOMPLETE" in code and ("SPLIT" in code or "BRANCH" in code)
                and bool(open_splits.intersection(issue.elementIds))
            )
            result.append(issue.model_copy(update={"severity": "warning"})
                          if only_requests_missing_branch else issue)
        return result

    def _require_llm(self):
        if not self.llm: raise RuntimeError("请在配置文件中设置模型并通过环境变量提供 API key")

from __future__ import annotations

import json
from typing import Callable

from pydantic import ValidationError

from .bpmn import graph_to_bpmn
from .config import Settings
from .domain import Edge, Graph, Issue, Node, Segment, Sentence, node_kind
from .editor import EditError, apply_graph_patch
from .llm import LlmClient, parse_json_object, prompt
from .layout import BpmnLayoutClient
from .process_quality import assess_soundness
from .segmentation import FixedLengthStrategy, SingleSegmentStrategy, split_sentences
from .validation import validate_graph, validate_incremental_batch


STAGES = ["INPUT_READY", "SENTENCES_PREPARED", "SEGMENTS_CREATED",
          "CONTEXT_BUILT", "SUBGRAPH_GENERATED", "CANDIDATE_APPLIED", "STRUCTURE_VALIDATED",
          "SEMANTIC_REVIEWED", "SEMANTIC_REPAIRING", "SEMANTIC_REPAIR_DECIDED", "ARBITRATING",
          "STRUCTURE_REPAIRING",
          "SEGMENT_COMMITTED", "FINAL_VALIDATED", "BPMN_GENERATED", "COMPLETED"]

LEGACY_STAGE_ALIASES = {
    "GENERATOR_RECONSIDERING": "SEMANTIC_REPAIRING",
    "GENERATOR_DECIDED": "SEMANTIC_REPAIR_DECIDED",
    "REPAIRING": "STRUCTURE_REPAIRING",
    "EDITS_GENERATED": "STRUCTURE_REPAIRING",
}


class AgentOutputValidationError(ValueError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("; ".join(issue["message"] for issue in issues))


class AgentRetriesExhaustedError(ValueError):
    def __init__(self, agent: str, attempts: int, issues: list[dict],
                 last_invalid_output: dict | None):
        self.agent = agent
        self.attempts = attempts
        self.issues = issues
        self.last_invalid_output = last_invalid_output
        detail = "; ".join(item.get("message", str(item)) for item in issues)
        super().__init__(f"{agent} failed after {attempts} attempts: {detail}")


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = LlmClient(settings.llm) if settings.llm.model and settings.llm.resolved_key() else None
        arbitrator_config = settings.arbitrator_llm
        self.arbitrator_llm = (
            LlmClient(arbitrator_config)
            if arbitrator_config and arbitrator_config.model and arbitrator_config.resolved_key()
            else None
        )
        self.layout = BpmnLayoutClient(settings.layout) if settings.layout.enabled else None

    def _prompt(self, name: str) -> str:
        return prompt(name, self.settings.prompts.language)

    def initial_state(self, input_text: str, strategy: str) -> dict:
        graph = Graph()
        graph.metadata.segmentationStrategy = strategy
        graph.metadata.promptVersion = "v10-add-remove-graph-delta"
        return {"strategy": strategy, "sentences": [], "segments": [], "segmentIndex": 0,
                "graph": graph.model_dump(), "candidate": None, "subgraph": None,
                "issues": [], "snapshots": [],
                "llmCalls": [], "repairCount": 0, "repairHistory": [],
                "agentFailures": [], "agentAttemptDiagnostics": [],
                "terminalAgentFailures": [],
                "structureWarnings": [], "reviewWarnings": [], "reviewHistory": [],
                "reviewFindings": [], "reviewResolutions": [], "rejectedFindings": [],
                "semanticRepairCount": 0, "semanticRepairFailures": [],
                "reviewDiscussionRound": 0, "reviewDiscussionHistory": [],
                "arbitrationHistory": [], "pendingArbitrationFindings": [],
                "commitAfterArbitrationRepair": False,
                "reviewerEnabled": self.settings.pipeline.reviewer_enabled,
                "repairLimitEvents": [],
                "finalizing": False, "soundnessRepairing": False,
                "soundnessRepairCount": 0, "soundnessRepairExhausted": False,
                "soundnessRepairBlocked": False,
                "soundness": None, "soundnessHistory": [], "bpmnXml": None}

    def step(self, stage: str, state: dict, input_text: str) -> tuple[str, dict]:
        previous_stage = stage
        stage = LEGACY_STAGE_ALIASES.get(stage, stage)
        if previous_stage == "EDITS_GENERATED":
            history = state.get("repairHistory", [])
            if not state.get("issues") and history:
                state["issues"] = history[-1].get("issues", [])
            state["repairCount"] = max(0, state.get("repairCount", 0) - 1)
            if state.get("soundnessRepairing"):
                state["soundnessRepairCount"] = state["repairCount"]
        state.setdefault("semanticRepairCount", state.get("reviewRevisionCount", 0))
        state.setdefault("semanticRepairFailures", state.get("reviewRevisionFailures", []))
        if "semanticRepair" not in state and state.get("generatorRevision") is not None:
            state["semanticRepair"] = state["generatorRevision"]
        if state.pop("commitAfterArbitrationRevision", False):
            state["commitAfterArbitrationRepair"] = True
        graph = Graph.model_validate(state["graph"])
        reviewer_enabled = self.settings.pipeline.reviewer_enabled
        state["reviewerEnabled"] = reviewer_enabled
        if not reviewer_enabled and stage in {
            "SEMANTIC_REVIEWED", "SEMANTIC_REPAIRING", "SEMANTIC_REPAIR_DECIDED", "ARBITRATING",
        }:
            return self._commit_segment(state)
        if stage == "INPUT_READY":
            state["sentences"] = [s.model_dump() for s in split_sentences(input_text)]
            return "SENTENCES_PREPARED", state
        if stage == "SENTENCES_PREPARED":
            sentences = [Sentence.model_validate(item) for item in state["sentences"]]
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
                    [{"role": "system", "content": self._prompt("planner")}, {"role": "user", "content": body}],
                )
                by_id = {s.id: i for i, s in enumerate(sentences)}
                segments = []
                for item in raw["segments"]:
                    # Planner JSON occasionally includes sentence-list punctuation
                    # in a boundary value (for example ``"S3,"``). Treat that as
                    # formatting noise instead of crashing the whole experiment.
                    start = str(item["start"]).strip().rstrip(",;:，；：")
                    end = str(item["end"]).strip().rstrip(",;:，；：")
                    normalized = {**item, "start": start, "end": end}
                    ids = [s.id for s in sentences[by_id[start]:by_id[end] + 1]]
                    segments.append(Segment(**normalized, sentenceIds=ids))
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
                    return "STRUCTURE_REPAIRING", state
                return "FINAL_VALIDATED", state
            state["context"] = self._context(state, graph)
            return "CONTEXT_BUILT", state
        if stage == "CONTEXT_BUILT":
            self._require_llm()
            try:
                state["subgraph"] = self._complete_json(
                    state, "generator",
                    [{"role": "system", "content": self._prompt("generator")},
                     {"role": "user", "content": json.dumps(state["context"], ensure_ascii=False)}],
                    validator=lambda value: self._validate_generated_patch(value, graph),
                )
            except AgentRetriesExhaustedError as exc:
                # Preserve the invalid artifact so the structural Repair mode can
                # reconstruct one valid Graph Patch against the current graph.
                state["subgraph"] = exc.last_invalid_output
                state["candidate"] = graph.model_dump()
                state["issues"] = exc.issues
                state["generatorPatchFailure"] = {
                    "agent": exc.agent,
                    "attempts": exc.attempts,
                    "currentSegment": state["segments"][state["segmentIndex"]],
                    "lastInvalidPatch": exc.last_invalid_output,
                    "issues": exc.issues,
                }
                state["repairCount"] = 0
                return "STRUCTURE_REPAIRING", state
            return "SUBGRAPH_GENERATED", state
        if stage == "SUBGRAPH_GENERATED":
            segment = state["segments"][state["segmentIndex"]]
            try:
                candidate = apply_graph_patch(
                    graph,
                    state["subgraph"]["addNodes"],
                    state["subgraph"]["addEdges"],
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
                return "STRUCTURE_REPAIRING", state
            state["candidate"] = candidate.model_dump()
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
            if state["issues"]: return "STRUCTURE_REPAIRING", state
            if state.pop("commitAfterArbitrationRepair", False):
                return self._commit_segment(state)
            if not reviewer_enabled:
                state["review"] = None
                return self._commit_segment(state)
            if state.get("pendingArbitrationFindings"):
                return "ARBITRATING", state
            self._require_llm()
            segment_id = state["segments"][state["segmentIndex"]]["id"]
            payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": self._graph_view(state["candidate"]),
            }
            if state.get("reviewResolutions"):
                payload["previousFindings"] = state["reviewResolutions"]
            discussion_history = self._segment_discussion_history(state)
            if discussion_history:
                payload["discussionHistory"] = discussion_history
            review = self._complete_json(
                state, "reviewer",
                [{"role": "system", "content": self._prompt("reviewer")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                validator=self._validate_review,
            )
            state["review"] = review
            state["reviewFindings"] = review["findings"]
            state.setdefault("reviewHistory", []).append({
                "segmentId": segment_id,
                "findings": review["findings"],
                "previousFindings": payload.get("previousFindings", []),
                "discussionRound": state.get("reviewDiscussionRound", 0) + 1,
            })
            return "SEMANTIC_REVIEWED", state
        if stage == "SEMANTIC_REVIEWED":
            if state.get("reviewFindings"):
                if state.get("semanticRepairCount", 0) >= self.settings.pipeline.max_semantic_repairs:
                    return self._accept_unrepaired_candidate(
                        state,
                        f"语义修订已达到上限 {self.settings.pipeline.max_semantic_repairs}",
                    )
                return "SEMANTIC_REPAIRING", state
            return self._commit_segment(state)
        if stage == "SEMANTIC_REPAIRING":
            self._require_llm()
            findings = state.get("reviewFindings", [])
            payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": self._graph_view(state["candidate"]),
                "findings": findings,
            }
            discussion_history = self._segment_discussion_history(state)
            if discussion_history:
                payload["discussionHistory"] = discussion_history
            if state.get("arbitrationDecisions"):
                payload["arbitrationDecisions"] = state["arbitrationDecisions"]
            try:
                repair_result = self._complete_json(
                    state, "repair",
                    [{"role": "system", "content": self._prompt("repair_semantic")},
                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    validator=lambda value: self._validate_semantic_repair(
                        value, findings, Graph.model_validate(state["candidate"]),
                        required_decisions={
                            item["findingId"]: item["decision"]
                            for item in state.get("arbitrationDecisions", [])
                        } or None,
                    ),
                )
            except ValueError as exc:
                if state.get("arbitrationDecisions"):
                    raise
                return self._accept_unrepaired_candidate(state, str(exc))
            state["semanticRepair"] = repair_result
            return "SEMANTIC_REPAIR_DECIDED", state
        if stage == "SEMANTIC_REPAIR_DECIDED":
            repair_result = state["semanticRepair"]
            findings_by_id = {item["id"]: item for item in state.get("reviewFindings", [])}
            accepted_ids = {
                item["findingId"] for item in repair_result["decisions"]
                if item["decision"] == "accept"
            }
            resolutions = []
            for item in repair_result["decisions"]:
                finding = findings_by_id[item["findingId"]]
                resolution = {"finding": finding, **item}
                resolutions.append(resolution)
            rejected_resolutions = [item for item in resolutions if item["decision"] == "reject"]
            if rejected_resolutions and not state.get("arbitrationDecisions"):
                state["reviewDiscussionRound"] = state.get("reviewDiscussionRound", 0) + 1
                state.setdefault("reviewDiscussionHistory", []).append({
                    "segmentId": state["segments"][state["segmentIndex"]]["id"],
                    "round": state["reviewDiscussionRound"],
                    "reviewFindings": state.get("reviewFindings", []),
                    "repairDecisions": repair_result["decisions"],
                })
            reached_discussion_limit = (
                bool(rejected_resolutions)
                and state.get("reviewDiscussionRound", 0)
                >= self.settings.pipeline.max_review_discussion_rounds
                and not state.get("arbitrationDecisions")
            )
            if not accepted_ids:
                state["reviewResolutions"] = resolutions
                if reached_discussion_limit:
                    state["pendingArbitrationFindings"] = [
                        item["finding"] for item in rejected_resolutions
                    ]
                    return "ARBITRATING", state
                state["reviewFindings"] = []
                return "STRUCTURE_VALIDATED", state
            segment = state["segments"][state["segmentIndex"]]
            patch = repair_result["patch"]
            candidate_before_repair = Graph.model_validate(state["candidate"])
            candidate = apply_graph_patch(
                candidate_before_repair,
                patch["addNodes"], patch["addEdges"], segment["id"],
                patch["removeNodeIds"], patch["removeEdgeIds"],
            )
            state["reviewFindings"] = []
            state["candidate"] = candidate.model_dump()
            state["reviewResolutions"] = resolutions
            state["semanticRepairCount"] = state.get("semanticRepairCount", 0) + 1
            state.pop("arbitrationDecisions", None)
            if reached_discussion_limit:
                state["pendingArbitrationFindings"] = [
                    item["finding"] for item in rejected_resolutions
                ]
            return "CANDIDATE_APPLIED", state
        if stage == "ARBITRATING":
            findings = state.get("pendingArbitrationFindings") or state.get("reviewFindings", [])
            payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": self._graph_view(state["candidate"]),
                "findings": findings,
                "discussionHistory": self._segment_discussion_history(state),
            }
            decisions = self._complete_json(
                state, "arbitrator",
                [{"role": "system", "content": self._prompt("arbitrator")},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                validator=lambda value: self._validate_arbitration(value, findings),
                client=self.arbitrator_llm or self.llm,
            )["decisions"]
            segment_id = state["segments"][state["segmentIndex"]]["id"]
            record = {
                "segmentId": segment_id,
                "findings": findings,
                "discussionHistory": payload["discussionHistory"],
                "decisions": decisions,
            }
            state.setdefault("arbitrationHistory", []).append(record)
            findings_by_id = {item["id"]: item for item in findings}
            accepted_findings = [
                findings_by_id[item["findingId"]]
                for item in decisions if item["decision"] == "accept"
            ]
            for item in decisions:
                if item["decision"] == "reject":
                    state.setdefault("rejectedFindings", []).append({
                        "segmentId": segment_id,
                        "finding": findings_by_id[item["findingId"]],
                        **item,
                        "resolvedBy": "arbitrator",
                    })
            state["pendingArbitrationFindings"] = []
            state["reviewResolutions"] = [
                {"finding": findings_by_id[item["findingId"]], **item, "resolvedBy": "arbitrator"}
                for item in decisions
            ]
            if not accepted_findings:
                state["reviewFindings"] = []
                return self._commit_segment(state)
            state["reviewFindings"] = accepted_findings
            state["arbitrationDecisions"] = [
                item for item in decisions if item["decision"] == "accept"
            ]
            state["commitAfterArbitrationRepair"] = True
            return "SEMANTIC_REPAIRING", state
        if stage == "STRUCTURE_REPAIRING":
            repair_limit = (
                self.settings.pipeline.max_soundness_repairs
                if state.get("soundnessRepairing")
                else self.settings.pipeline.max_structure_repairs
            )
            if state["repairCount"] >= repair_limit:
                return self._accept_repair_limited_candidate(state)
            try:
                self._require_llm()
            except Exception as exc:
                if state.get("soundnessRepairing"):
                    return self._abort_soundness_repair(state, exc, "repair")
                raise
            repair_payload = {
                "descriptionPrefix": self._description_prefix(state),
                "candidateGraph": self._graph_view(state["candidate"]),
                "issues": state["issues"],
            }
            if state.get("generatorPatchFailure"):
                repair_payload["currentSegment"] = state["generatorPatchFailure"]["currentSegment"]
                repair_payload["failedGeneratedPatch"] = state["generatorPatchFailure"]["lastInvalidPatch"]
            if state.get("soundnessRepairing"):
                repair_payload["soundnessDiagnostics"] = state.get("soundnessPromptDiagnostics", {})
            try:
                patch = self._complete_json(
                    state, "repair",
                    [{"role": "system", "content": self._prompt("repair_structure")},
                     {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)}],
                    validator=lambda value: self._validate_repair_patch(
                        value, Graph.model_validate(state["candidate"])
                    ),
                )
            except Exception as exc:
                if state.get("soundnessRepairing"):
                    return self._abort_soundness_repair(state, exc, "repair")
                raise
            segment_id = (
                "FINALIZATION" if state.get("finalizing")
                else state["segments"][state["segmentIndex"]]["id"]
            )
            try:
                candidate = apply_graph_patch(
                    Graph.model_validate(state["candidate"]),
                    patch["addNodes"], patch["addEdges"], segment_id,
                    patch["removeNodeIds"], patch["removeEdgeIds"],
                )
            except (EditError, ValidationError, KeyError, TypeError, ValueError) as exc:
                if state.get("soundnessRepairing"):
                    return self._abort_soundness_repair(state, exc, "repair")
                state["issues"] = [Issue(
                    code="REPAIR_GRAPH_PATCH_APPLICATION_FAILED",
                    message=f"Repair Graph Patch 无法应用：{exc}",
                ).model_dump()]
                return "STRUCTURE_REPAIRING", state
            state["repairHistory"].append({
                "mode": "soundness" if state.get("soundnessRepairing") else "structure",
                "attempt": state["repairCount"] + 1,
                "issues": state["issues"],
                "generatedSubgraph": state.get("subgraph"),
                "patch": patch,
            })
            state["repairCount"] += 1
            if state.get("soundnessRepairing"):
                state["soundnessRepairCount"] = state["repairCount"]
            state["structureRepairPatch"] = patch
            state["candidate"] = candidate.model_dump()
            state["issues"] = []
            if state.get("finalizing"):
                issues = validate_graph(candidate, final=True)
                state["issues"] = [issue.model_dump() for issue in issues]
                if issues:
                    return "STRUCTURE_REPAIRING", state
                state["graph"] = candidate.model_dump()
                state["candidate"] = None
                state["finalizing"] = False
                return "FINAL_VALIDATED", state
            return "CANDIDATE_APPLIED", state
        if stage == "FINAL_VALIDATED":
            final_graph = Graph.model_validate(state["graph"])
            semantic_xml = graph_to_bpmn(final_graph)
            soundness = assess_soundness(semantic_xml)
            state["soundness"] = soundness
            state.setdefault("soundnessHistory", []).append({
                "repairCount": state.get("soundnessRepairCount", 0),
                "result": soundness,
            })
            if soundness.get("calculationSucceeded") and not soundness.get("sound"):
                if state.get("soundnessRepairBlocked"):
                    state["soundnessRepairing"] = False
                elif not state.get("soundnessRepairing"):
                    state["repairCount"] = 0
                    state["soundnessRepairCount"] = 0
                    state["soundnessRepairing"] = True
                if state.get("soundnessRepairing") and (
                    state["repairCount"] < self.settings.pipeline.max_soundness_repairs
                ):
                    prompt_diagnostics = self._soundness_prompt_diagnostics(soundness)
                    state["soundnessPromptDiagnostics"] = prompt_diagnostics
                    state["candidate"] = final_graph.model_dump()
                    state["context"] = self._finalization_context(state, final_graph)
                    state["issues"] = [{
                        "code": "WORKFLOW_NET_UNSOUND",
                        "message": "Woflan 判定最终流程不满足工作流网 soundness，请依据完整流程描述、最终图和结构化诊断修复控制流。",
                        "elementIds": [],
                        "sourceSentences": [item["id"] for item in state.get("sentences", [])],
                        "severity": "error",
                        "diagnostics": prompt_diagnostics,
                    }]
                    state["finalizing"] = True
                    return "STRUCTURE_REPAIRING", state
                if not state.get("soundnessRepairBlocked"):
                    state["soundnessRepairExhausted"] = True
                    state["soundnessRepairing"] = False
                    state.setdefault("structureWarnings", []).append({
                        "segmentId": "FINALIZATION",
                        "code": "SOUNDNESS_REPAIR_LIMIT_REACHED",
                        "message": (
                            f"Soundness 修复已达到上限 {self.settings.pipeline.max_soundness_repairs}，"
                            "仍交付当前 BPMN 产物并保留 sound=false。"
                        ),
                        "severity": "warning",
                    })
            elif not soundness.get("calculationSucceeded"):
                state["soundnessRepairing"] = False
                state.setdefault("structureWarnings", []).append({
                    "segmentId": "FINALIZATION",
                    "code": "SOUNDNESS_CALCULATION_FAILED",
                    "message": f"Soundness 计算失败，仍交付当前 BPMN：{soundness.get('reason', 'unknown error')}",
                    "severity": "warning",
                })
            else:
                state["soundnessRepairing"] = False
                state["soundnessRepairExhausted"] = False
                state["soundnessRepairBlocked"] = False
            state["bpmnXml"] = self.layout.layout(semantic_xml) if self.layout else semantic_xml
            final_snapshot = {
                "segment": {"id": "FINALIZATION", "title": "最终流程", "sentenceIds": []},
                "graph": final_graph.model_dump(),
                "bpmnXml": state["bpmnXml"],
                "soundness": soundness,
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
        state["semanticRepairCount"] = 0
        state["reviewDiscussionRound"] = 0
        state["pendingArbitrationFindings"] = []
        state.pop("semanticRepair", None)
        state.pop("structureRepairPatch", None)
        state.pop("arbitrationDecisions", None)
        state.pop("commitAfterArbitrationRepair", None)
        state.pop("generatorPatchFailure", None)
        state["segmentIndex"] += 1
        state["repairCount"] = 0
        return "SEGMENT_COMMITTED", state

    def _accept_unrepaired_candidate(self, state: dict, error: str) -> tuple[str, dict]:
        segment = state["segments"][state["segmentIndex"]]
        failure = {
            "segmentId": segment["id"],
            "message": error,
            "findings": state.get("reviewFindings", []),
            "decisions": state.get("semanticRepair", {}).get("decisions", []),
            "candidateRetained": True,
        }
        state.setdefault("semanticRepairFailures", []).append(failure)
        state.setdefault("reviewWarnings", []).append({
            "segmentId": segment["id"],
            "code": "SEMANTIC_REPAIR_FAILED",
            "message": error,
            "severity": "warning",
        })
        return self._commit_segment(state)

    def _accept_repair_limited_candidate(self, state: dict) -> tuple[str, dict]:
        """Retain the latest repair artifact and continue after exhausting repair rounds."""
        candidate = Graph.model_validate(state.get("candidate") or state["graph"])
        finalizing = bool(state.get("finalizing"))
        if finalizing:
            segment_id = "FINALIZATION"
        else:
            segment_id = state["segments"][state["segmentIndex"]]["id"]
        repair_limit = (
            self.settings.pipeline.max_soundness_repairs
            if state.get("soundnessRepairing")
            else self.settings.pipeline.max_structure_repairs
        )
        message = f"局部修订已达到上限 {repair_limit}，保留最后一次修复产物"
        remaining_issues = list(state.get("issues", []))
        event = {
            "segmentId": segment_id,
            "repairCount": state["repairCount"],
            "message": message,
            "remainingIssues": remaining_issues,
            "candidateRetained": True,
            "finalization": finalizing,
        }
        state.setdefault("repairLimitEvents", []).append(event)
        state.setdefault("structureWarnings", []).append({
            "segmentId": segment_id,
            "code": "REPAIR_LIMIT_REACHED",
            "message": message,
            "severity": "warning",
            "remainingIssueCount": len(remaining_issues),
        })
        if finalizing:
            state["graph"] = candidate.model_dump()
            state["candidate"] = None
            state["finalizing"] = False
            return "FINAL_VALIDATED", state
        return self._commit_segment(state)

    def _abort_soundness_repair(
        self, state: dict, error: Exception, agent: str,
    ) -> tuple[str, dict]:
        """Preserve Completion when the optional final soundness repair cannot run."""
        candidate = Graph.model_validate(state.get("candidate") or state["graph"])
        state["graph"] = candidate.model_dump()
        state["candidate"] = None
        state["issues"] = []
        state["finalizing"] = False
        state["soundnessRepairing"] = False
        state["soundnessRepairExhausted"] = True
        state["soundnessRepairBlocked"] = True
        state.setdefault("structureWarnings", []).append({
            "segmentId": "FINALIZATION",
            "code": "SOUNDNESS_REPAIR_AGENT_FAILED",
            "message": (
                f"最终 Soundness 修复的 {agent} 调用失败，仍交付当前 BPMN 并保留 sound=false："
                f"{type(error).__name__}: {error}"
            ),
            "severity": "warning",
        })
        return "FINAL_VALIDATED", state

    def _render_bpmn(self, graph: Graph) -> str:
        semantic_xml = graph_to_bpmn(graph)
        return self.layout.layout(semantic_xml) if self.layout else semantic_xml

    @staticmethod
    def _soundness_prompt_diagnostics(result: dict) -> dict:
        """Keep Woflan evidence useful to repair without duplicating the full Petri net."""
        diagnostics = result.get("diagnostics") or {}
        return {
            "protocol": result.get("protocol"),
            "calculationSucceeded": result.get("calculationSucceeded"),
            "sound": result.get("sound"),
            "diagnosticMessages": diagnostics.get("Outputs.DIAGNOSTIC_MESSAGES", []),
            "uncoveredPlacesSComponent": diagnostics.get("uncovered_places_s_component", []),
            "deadTasks": diagnostics.get("dead_tasks", []),
            "conversion": result.get("conversion", {}),
        }

    def _context(self, state: dict, graph: Graph) -> dict:
        current_index = state["segmentIndex"]
        segment = state["segments"][current_index]
        return {
            "currentSegment": segment,
            "descriptionPrefix": self._description_prefix(state),
            "graph": self._graph_view(graph),
            "isFinalSegment": current_index == len(state["segments"]) - 1,
        }

    def _finalization_context(self, state: dict, graph: Graph) -> dict:
        return {
            "currentSegment": {"id": "FINALIZATION", "title": "终局闭合", "sentenceIds": []},
            "descriptionPrefix": [
                {"id": item["id"], "text": item["text"]}
                for item in state.get("sentences", [])
            ],
            "graph": self._graph_view(graph),
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
            {"id": item["id"], "text": item["text"]}
            for item in state["sentences"]
            if item["id"] in prefix_ids
        ]

    @staticmethod
    def _segment_discussion_history(state: dict) -> list[dict]:
        segment_id = state["segments"][state["segmentIndex"]]["id"]
        return [
            item for item in state.get("reviewDiscussionHistory", [])
            if item.get("segmentId") == segment_id
        ]

    @staticmethod
    def _graph_view(graph: Graph | dict) -> dict:
        """Expose only the process structure needed by model-facing prompts."""
        value = graph.model_dump() if isinstance(graph, Graph) else graph
        return {"nodes": value.get("nodes", []), "edges": value.get("edges", [])}

    def _record(self, state: dict, agent: str, result):
        state["llmCalls"].append({"agent": agent, "usage": result.usage, "latencyMs": result.latency_ms,
                                  "toolCallCount": len(result.tool_calls),
                                  "finishReason": result.finish_reason,
                                  "contentChars": len(result.content or ""),
                                  "thinkingMode": result.thinking_mode,
                                  "model": getattr(result, "model", None)})

    def _complete_json(self, state: dict, agent: str, messages: list[dict],
                       validator: Callable[[dict], dict] | None = None,
                       client: LlmClient | None = None) -> dict:
        attempts = self.settings.pipeline.max_agent_retries + 1
        last_error: Exception | None = None
        last_invalid_output: dict | None = None
        last_invalid_content: str | None = None
        for attempt in range(1, attempts + 1):
            current_output: dict | None = None
            current_content: str | None = None
            try:
                active_client = client or self.llm
                if not active_client:
                    raise RuntimeError("请在配置文件中设置模型并通过环境变量提供 API key")
                # Output retries intentionally repeat the original request verbatim.
                # Validation diagnostics remain internal and must not enlarge or anchor
                # the next model context to a previously invalid response.
                result = active_client.complete(messages, agent=agent)
                self._record(state, agent, result)
                current_content = result.content
                value = parse_json_object(result.content, agent)
                current_output = value
                return validator(value) if validator else value
            except ValueError as exc:
                last_error = exc
                last_invalid_content = current_content
                if current_output is not None:
                    last_invalid_output = current_output
                attempt_issues = (
                    exc.issues if isinstance(exc, AgentOutputValidationError)
                    else [{"code": "INVALID_JSON", "message": str(exc)}]
                )
                state.setdefault("agentFailures", []).append(
                    {"agent": agent, "attempt": attempt, "error": str(exc)}
                )
                state.setdefault("agentAttemptDiagnostics", []).append({
                    "agent": agent,
                    "attempt": attempt,
                    "error": str(exc),
                    "issues": attempt_issues,
                    "invalidOutput": current_output,
                    "invalidContent": current_content,
                })
        final_issues = (
            last_error.issues if isinstance(last_error, AgentOutputValidationError)
            else [{"code": "INVALID_JSON", "message": str(last_error)}]
        )
        state.setdefault("terminalAgentFailures", []).append({
            "agent": agent,
            "attempts": attempts,
            "issues": final_issues,
            "lastInvalidOutput": last_invalid_output,
        })
        raise AgentRetriesExhaustedError(
            agent, attempts, final_issues, last_invalid_output
        )

    @staticmethod
    def _validate_generated_patch(value: dict, graph: Graph,
                                  allow_empty: bool = False) -> dict:
        issues: list[dict] = []

        def element_ref(item: object, location: str) -> list[dict]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                return [{"id": item["id"]}]
            return [{"location": location, "content": item}]

        extra_fields = sorted(set(value) - {
            "addNodes", "addEdges", "removeNodeIds", "removeEdgeIds",
        })
        if extra_fields:
            issues.append({
                "code": "GRAPH_PATCH_EXTRA_FIELDS",
                "message": f"Graph Patch 包含未定义字段: {', '.join(extra_fields)}",
                "elements": [
                    {"location": field, "content": value.get(field)} for field in extra_fields
                ],
            })
        add_nodes = value.get("addNodes")
        add_edges = value.get("addEdges")
        remove_node_ids = value.get("removeNodeIds", [])
        remove_edge_ids = value.get("removeEdgeIds", [])
        if not isinstance(add_nodes, list):
            issues.append({
                "code": "GRAPH_PATCH_ADD_NODES_NOT_ARRAY", "message": "addNodes 必须是数组",
                "elements": [{"location": "addNodes", "content": add_nodes}],
            })
        if not isinstance(add_edges, list):
            issues.append({
                "code": "GRAPH_PATCH_ADD_EDGES_NOT_ARRAY", "message": "addEdges 必须是数组",
                "elements": [{"location": "addEdges", "content": add_edges}],
            })
        if not isinstance(remove_node_ids, list) or not all(
                isinstance(item, str) for item in remove_node_ids):
            issues.append({
                "code": "GRAPH_PATCH_REMOVE_NODES_NOT_STRING_ARRAY",
                "message": "removeNodeIds 必须是字符串数组",
                "elements": [{"location": "removeNodeIds", "content": remove_node_ids}],
            })
        if not isinstance(remove_edge_ids, list) or not all(
                isinstance(item, str) for item in remove_edge_ids):
            issues.append({
                "code": "GRAPH_PATCH_REMOVE_EDGES_NOT_STRING_ARRAY",
                "message": "removeEdgeIds 必须是字符串数组",
                "elements": [{"location": "removeEdgeIds", "content": remove_edge_ids}],
            })
        if issues:
            raise AgentOutputValidationError(issues)
        add_nodes = [dict(item) if isinstance(item, dict) else item for item in add_nodes]
        add_edges = [dict(item) if isinstance(item, dict) else item for item in add_edges]
        for item in [*add_nodes, *add_edges]:
            if isinstance(item, dict):
                item.pop("introducedInSegment", None)
        for item in add_nodes:
            if isinstance(item, dict):
                for field in ("eventDefinition", "role"):
                    if item.get(field) is None:
                        item.pop(field, None)
        if (not allow_empty and not add_nodes and not add_edges
                and not remove_node_ids and not remove_edge_ids):
            raise AgentOutputValidationError([{
                "code": "EMPTY_GRAPH_PATCH",
                "message": "当前片段必须至少新增、替换或删除一个元素",
                "elements": [],
            }])
        allowed_node_fields = {
            "task": {"id", "type", "name", "sourceSentences"},
            "event": {"id", "type", "name", "eventDefinition", "sourceSentences"},
            "gateway": {"id", "type", "name", "role", "sourceSentences"},
        }
        allowed_edge_fields = {
            "id", "source", "target", "condition", "isDefault", "sourceSentences",
        }
        for collection_name, items in (("addNodes", add_nodes), ("addEdges", add_edges)):
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    issues.append({
                        "code": "SUBGRAPH_ITEM_NOT_OBJECT",
                        "message": f"{collection_name}[{index}] 必须是对象",
                        "elements": element_ref(item, f"{collection_name}[{index}]"),
                    })
                elif collection_name == "addNodes":
                    try:
                        kind = node_kind(item.get("type", ""))
                    except ValueError:
                        issues.append({
                            "code": "SUBGRAPH_NODE_TYPE_INVALID",
                            "message": f"addNodes[{index}].type 不是受支持的 BPMN 节点类型",
                            "elements": element_ref(item, f"addNodes[{index}]"),
                        })
                    else:
                        extras = sorted(set(item) - allowed_node_fields[kind])
                        if extras:
                            issues.append({
                                "code": "SUBGRAPH_NODE_EXTRA_FIELDS",
                                "message": f"addNodes[{index}] 包含不属于 {kind} 的字段: {', '.join(extras)}",
                                "elements": element_ref(item, f"addNodes[{index}]"),
                            })
                        else:
                            try:
                                Node.model_validate(item)
                            except ValidationError as exc:
                                details = "; ".join(
                                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                                    for error in exc.errors()
                                )
                                issues.append({
                                    "code": "SUBGRAPH_NODE_INVALID",
                                    "message": f"addNodes[{index}] 无效：{details}",
                                    "elements": element_ref(item, f"addNodes[{index}]"),
                                })
                elif collection_name == "addEdges":
                    extras = sorted(set(item) - allowed_edge_fields)
                    if extras:
                        issues.append({
                            "code": "SUBGRAPH_EDGE_EXTRA_FIELDS",
                            "message": f"addEdges[{index}] 包含未定义字段: {', '.join(extras)}",
                            "elements": element_ref(item, f"addEdges[{index}]"),
                        })
                    else:
                        try:
                            Edge.model_validate(item)
                        except ValidationError as exc:
                            details = "; ".join(
                                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                                for error in exc.errors()
                            )
                            issues.append({
                                "code": "SUBGRAPH_EDGE_INVALID",
                                "message": f"addEdges[{index}] 无效：{details}",
                                "elements": element_ref(item, f"addEdges[{index}]"),
                            })
        if issues:
            raise AgentOutputValidationError(issues)
        node_ids = [item["id"] for item in add_nodes]
        edge_ids = [item["id"] for item in add_edges]
        duplicate_node_ids = sorted({item for item in node_ids if node_ids.count(item) > 1})
        duplicate_edge_ids = sorted({item for item in edge_ids if edge_ids.count(item) > 1})
        duplicate_remove_node_ids = sorted({
            item for item in remove_node_ids if remove_node_ids.count(item) > 1
        })
        duplicate_remove_edge_ids = sorted({
            item for item in remove_edge_ids if remove_edge_ids.count(item) > 1
        })
        for code, label, duplicate_ids in (
            ("GRAPH_PATCH_DUPLICATE_ADD_NODE_ID", "addNodes 中存在重复 ID", duplicate_node_ids),
            ("GRAPH_PATCH_DUPLICATE_ADD_EDGE_ID", "addEdges 中存在重复 ID", duplicate_edge_ids),
            ("GRAPH_PATCH_DUPLICATE_REMOVE_NODE_ID", "removeNodeIds 中存在重复 ID", duplicate_remove_node_ids),
            ("GRAPH_PATCH_DUPLICATE_REMOVE_EDGE_ID", "removeEdgeIds 中存在重复 ID", duplicate_remove_edge_ids),
        ):
            if duplicate_ids:
                issues.append({
                    "code": code,
                    "message": f"{label}: {', '.join(duplicate_ids)}",
                    "elementIds": duplicate_ids,
                })
        original_nodes = graph.node_map()
        original_edges = {edge.id: edge for edge in graph.edges}
        unknown_removed_nodes = sorted(set(remove_node_ids) - set(original_nodes))
        unknown_removed_edges = sorted(set(remove_edge_ids) - set(original_edges))
        if unknown_removed_nodes:
            issues.append({
                "code": "GRAPH_PATCH_REMOVE_MISSING_NODES",
                "message": f"removeNodeIds 引用了当前 graph 中不存在的节点: {', '.join(unknown_removed_nodes)}",
                "elementIds": unknown_removed_nodes,
            })
        if unknown_removed_edges:
            issues.append({
                "code": "GRAPH_PATCH_REMOVE_MISSING_EDGES",
                "message": f"removeEdgeIds 引用了当前 graph 中不存在的边: {', '.join(unknown_removed_edges)}",
                "elementIds": unknown_removed_edges,
            })
        implicit_node_replacements = sorted(
            (set(node_ids) & set(original_nodes)) - set(remove_node_ids)
        )
        implicit_edge_replacements = sorted(
            (set(edge_ids) & set(original_edges)) - set(remove_edge_ids)
        )
        if implicit_node_replacements:
            issues.append({
                "code": "GRAPH_PATCH_ADD_EXISTING_NODES_WITHOUT_REMOVE",
                "message": (
                    "addNodes 复用了已提交节点 ID，但未在 removeNodeIds 中显式删除: "
                    + ", ".join(implicit_node_replacements)
                ),
                "elementIds": implicit_node_replacements,
            })
        if implicit_edge_replacements:
            issues.append({
                "code": "GRAPH_PATCH_ADD_EXISTING_EDGES_WITHOUT_REMOVE",
                "message": (
                    "addEdges 复用了已提交边 ID，但未在 removeEdgeIds 中显式删除: "
                    + ", ".join(implicit_edge_replacements)
                ),
                "elementIds": implicit_edge_replacements,
            })
        for item in add_nodes:
            existing = original_nodes.get(item["id"])
            if existing and existing.kind != node_kind(item["type"]):
                issues.append({
                    "code": "GRAPH_PATCH_NODE_CATEGORY_IMMUTABLE",
                    "message": f"节点 {item['id']} 不能通过同 ID 替换跨 Task、Event、Gateway 类别改变 type",
                    "elementIds": [item["id"]],
                })
        if issues:
            raise AgentOutputValidationError(issues)

        committed_node_ids = set(original_nodes)
        retained_committed_node_ids = committed_node_ids - set(remove_node_ids)
        patch_node_ids = {
            item.get("id") for item in add_nodes
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        available_node_ids = retained_committed_node_ids | patch_node_ids
        reference_issues: list[dict] = []
        for edge in add_edges:
            if not isinstance(edge, dict):
                continue
            missing_endpoints = [
                endpoint for endpoint in (edge.get("source"), edge.get("target"))
                if isinstance(endpoint, str) and endpoint not in available_node_ids
            ]
            if not missing_endpoints:
                continue
            missing_parts = []
            if edge.get("source") in missing_endpoints:
                missing_parts.append(f"source={edge.get('source')}")
            if edge.get("target") in missing_endpoints:
                missing_parts.append(f"target={edge.get('target')}")
            reference_issues.append({
                "code": "GRAPH_PATCH_MISSING_NODE_REFERENCE",
                "message": f"边 {edge.get('id')} 的 {', '.join(missing_parts)} 引用了不存在的节点",
                "elementIds": [edge.get("id"), *missing_endpoints],
                "edgeId": edge.get("id"),
                "source": edge.get("source"),
                "target": edge.get("target"),
                "missingEndpoints": missing_endpoints,
                "availableCommittedNodeIds": sorted(retained_committed_node_ids),
                "nodeIdsAddedOrReplacedByThisPatch": sorted(patch_node_ids),
            })
        if reference_issues:
            raise AgentOutputValidationError(reference_issues)
        candidate = None
        try:
            candidate = apply_graph_patch(
                graph, add_nodes, add_edges, "VALIDATION", remove_node_ids, remove_edge_ids
            )
        except (EditError, ValidationError, KeyError, TypeError, ValueError) as exc:
            raise AgentOutputValidationError([{
                "code": "INVALID_GRAPH_PATCH",
                "message": f"Graph Patch 节点、边、删除项或引用无效：{exc}",
                "elements": [],
            }]) from None
        return {
            "addNodes": add_nodes,
            "addEdges": add_edges,
            "removeNodeIds": remove_node_ids,
            "removeEdgeIds": remove_edge_ids,
        }

    @staticmethod
    def _validate_review(value: dict) -> dict:
        extra_fields = sorted(set(value) - {"findings"})
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
        required = {"id", "message", "elementIds", "sourceSentences"}
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
            if not isinstance(raw["message"], str) or not raw["message"].strip():
                issues.append({
                    "code": "INVALID_FINDING_TEXT",
                    "message": f"findings[{index}].message 必须是非空字符串",
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
        return {"findings": findings}

    @classmethod
    def _validate_semantic_repair(
        cls, value: dict, findings: list[dict], candidate: Graph,
        required_decisions: dict[str, str] | None = None,
    ) -> dict:
        extra_fields = sorted(set(value) - {"decisions", "patch"})
        decisions = value.get("decisions")
        patch = value.get("patch")
        issues: list[dict] = []
        if extra_fields:
            issues.append({
                "code": "SEMANTIC_REPAIR_EXTRA_FIELDS",
                "message": f"Repair 语义修复输出包含未定义字段: {', '.join(extra_fields)}",
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
            if not isinstance(reason, str) or not reason.strip():
                issues.append({
                    "code": "DECISION_REASON_REQUIRED",
                    "message": f"决定 {finding_id} 时必须给出非空 reason",
                })
                continue
            returned_ids.append(finding_id)
            normalized.append({
                "findingId": finding_id,
                "decision": decision,
                "reason": reason.strip(),
            })
        if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != finding_ids:
            issues.append({
                "code": "INCOMPLETE_FINDING_DECISIONS",
                "message": "必须对每个 finding 恰好作出一次 accept 或 reject 决定",
            })
        if required_decisions:
            actual_decisions = {item["findingId"]: item["decision"] for item in normalized}
            for finding_id, required in required_decisions.items():
                if actual_decisions.get(finding_id) != required:
                    issues.append({
                        "code": "ARBITRATION_DECISION_NOT_FOLLOWED",
                        "message": f"{finding_id} 必须遵循仲裁结果 {required}",
                    })
        if issues:
            raise AgentOutputValidationError(issues)
        accepted = any(item["decision"] == "accept" for item in normalized)
        if not accepted and patch is not None:
            raise AgentOutputValidationError([{
                "code": "PATCH_FOR_REJECTED_FINDINGS",
                "message": "全部 finding 均被 reject 时 patch 必须为 null",
            }])
        if accepted and not isinstance(patch, dict):
            raise AgentOutputValidationError([{
                "code": "ACCEPTED_FINDINGS_REQUIRE_PATCH",
                "message": "存在 accept 时必须直接返回非空 Graph Patch",
            }])
        validated_patch = cls._validate_repair_patch(patch, candidate) if accepted else None
        return {"decisions": normalized, "patch": validated_patch}

    @staticmethod
    def _validate_arbitration(value: dict, findings: list[dict]) -> dict:
        extra_fields = sorted(set(value) - {"decisions"})
        decisions = value.get("decisions")
        issues: list[dict] = []
        if extra_fields:
            issues.append({
                "code": "ARBITRATION_EXTRA_FIELDS",
                "message": f"仲裁输出包含未定义字段: {', '.join(extra_fields)}",
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
                    "code": "INVALID_ARBITRATION_DECISION",
                    "message": f"decisions[{index}] 必须引用争议 finding 并选择 accept 或 reject",
                })
                continue
            if not isinstance(reason, str) or not reason.strip():
                issues.append({
                    "code": "ARBITRATION_REASON_REQUIRED",
                    "message": f"仲裁 {finding_id} 时必须给出非空 reason",
                })
                continue
            returned_ids.append(finding_id)
            normalized.append({
                "findingId": finding_id,
                "decision": decision,
                "reason": reason.strip(),
            })
        if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != finding_ids:
            issues.append({
                "code": "INCOMPLETE_ARBITRATION_DECISIONS",
                "message": "仲裁必须对每个争议 finding 恰好作出一次最终决定",
            })
        if issues:
            raise AgentOutputValidationError(issues)
        return {"decisions": normalized}

    @classmethod
    def _validate_repair_patch(cls, value: dict, candidate: Graph) -> dict:
        patch = cls._validate_generated_patch(value, candidate)
        revised = apply_graph_patch(
            candidate,
            patch["addNodes"], patch["addEdges"], "VALIDATION",
            patch["removeNodeIds"], patch["removeEdgeIds"],
        )
        if revised.nodes == candidate.nodes and revised.edges == candidate.edges:
            raise AgentOutputValidationError([{
                "code": "ACCEPTED_PATCH_HAS_NO_EFFECT",
                "message": "Repair Graph Patch 必须实际改变 candidateGraph",
            }])
        return patch

    def _require_llm(self):
        if not self.llm: raise RuntimeError("请在配置文件中设置模型并通过环境变量提供 API key")

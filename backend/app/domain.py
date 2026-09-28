from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


NodeKind = Literal["task", "event", "gateway"]


class Node(BaseModel):
    id: str
    kind: NodeKind
    name: str
    sourceSentences: list[str] = Field(default_factory=list)
    introducedInSegment: str | None = None
    taskType: str | None = None
    eventType: Literal["start", "intermediate", "end"] | None = None
    trigger: Literal["none", "message", "timer"] | None = None
    eventRole: Literal["catch", "throw"] | None = None
    gatewayType: Literal["exclusive", "parallel", "inclusive"] | None = None
    role: Literal["split", "join"] | None = None

    @model_validator(mode="after")
    def validate_variant(self):
        if self.kind == "task":
            if any(value is not None for value in (
                self.eventType, self.trigger, self.eventRole, self.gatewayType, self.role,
            )):
                raise ValueError("task cannot contain event or gateway fields")
            self.taskType = "task"
        elif self.kind == "event":
            if self.taskType is not None or self.gatewayType is not None or self.role is not None:
                raise ValueError("event cannot contain task or gateway fields")
            if not self.eventType or not self.trigger:
                raise ValueError("event requires eventType and trigger")
            if self.eventType != "intermediate" and self.eventRole is not None:
                raise ValueError("only intermediate events may define eventRole")
            if self.trigger == "timer" and self.eventRole == "throw":
                raise ValueError("timer intermediate event cannot throw")
        elif self.kind == "gateway":
            if any(value is not None for value in (
                self.taskType, self.eventType, self.trigger, self.eventRole,
            )):
                raise ValueError("gateway cannot contain task or event fields")
            if not self.gatewayType or not self.role:
                raise ValueError("gateway requires gatewayType and role")
        return self


class Condition(BaseModel):
    label: str
    expression: str | None = None


class Edge(BaseModel):
    id: str
    source: str
    target: str
    condition: Condition | None = None
    isDefault: bool = False
    sourceSentences: list[str] = Field(default_factory=list)
    introducedInSegment: str | None = None


class ProcessInfo(BaseModel):
    id: str = "process_main"
    name: str = "未命名流程"


class GraphMetadata(BaseModel):
    revision: int = 0
    processedSegments: list[str] = Field(default_factory=list)
    segmentationStrategy: str = "semantic"
    promptVersion: str = "v1"


class Graph(BaseModel):
    schemaVersion: str = "1.0"
    process: ProcessInfo = Field(default_factory=ProcessInfo)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    metadata: GraphMetadata = Field(default_factory=GraphMetadata)

    def node_map(self) -> dict[str, Node]:
        return {node.id: node for node in self.nodes}


class Sentence(BaseModel):
    id: str
    text: str


class Segment(BaseModel):
    id: str
    start: str
    end: str
    title: str
    sentenceIds: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    code: str
    message: str
    elementIds: list[str] = Field(default_factory=list)
    sourceSentences: list[str] = Field(default_factory=list)
    severity: Literal["error", "warning"] = "error"


class EditCall(BaseModel):
    name: Literal[
        "add_task", "add_event", "add_gateway", "add_node",
        "add_edge", "update_node", "update_edge",
        "delete_node", "delete_edge", "add_linear_sequence"
    ]
    arguments: dict[str, Any]

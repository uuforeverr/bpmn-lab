from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


NodeKind = Literal["task", "event", "gateway"]
TaskType = Literal[
    "task", "userTask", "serviceTask", "sendTask", "receiveTask",
    "businessRuleTask", "manualTask", "scriptTask",
]
EventType = Literal[
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
]
GatewayType = Literal["exclusiveGateway", "parallelGateway", "inclusiveGateway"]
NodeType = TaskType | EventType | GatewayType
EventDefinition = Literal[
    "message", "timer", "signal", "conditional", "link", "error",
    "escalation", "terminate", "compensation",
]

NODE_TYPES_BY_KIND: dict[str, tuple[str, ...]] = {
    "task": (
        "task", "userTask", "serviceTask", "sendTask", "receiveTask",
        "businessRuleTask", "manualTask", "scriptTask",
    ),
    "event": (
        "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
    ),
    "gateway": ("exclusiveGateway", "parallelGateway", "inclusiveGateway"),
}
TASK_TYPES = set(NODE_TYPES_BY_KIND["task"])
EVENT_TYPES = set(NODE_TYPES_BY_KIND["event"])
GATEWAY_TYPES = set(NODE_TYPES_BY_KIND["gateway"])
NODE_TYPE_DEFINITIONS: dict[str, str] = {
    "task": "未能从原文确定更具体执行方式的一般原子业务动作",
    "userTask": "由人员通过软件或工作流界面完成的任务",
    "serviceTask": "由系统自动调用服务或应用执行的任务",
    "sendTask": "向外部参与者发送消息的任务",
    "receiveTask": "等待并接收外部消息的任务",
    "businessRuleTask": "调用或评估业务规则、决策的任务",
    "manualTask": "由人员在工作流系统之外手工完成的任务",
    "scriptTask": "由流程引擎执行脚本的任务",
    "startEvent": "流程触发点，无入边",
    "endEvent": "一条流程路径的终止结果，无出边",
    "intermediateCatchEvent": "流程中等待事件发生的中间捕获事件，恰好一入一出",
    "intermediateThrowEvent": "流程中发出事件的中间抛出事件，恰好一入一出",
    "exclusiveGateway": "互斥选择一条分支，或汇合不会同时激活的互斥路径",
    "parallelGateway": "无条件同时启动全部分支，或等待同一并行块的全部分支",
    "inclusiveGateway": "启动一个或多个条件成立的分支，或等待实际被激活且仍可能到达的分支",
}
EVENT_DEFINITIONS: dict[str, set[str]] = {
    "startEvent": {"message", "timer", "signal", "conditional"},
    "endEvent": {"message", "signal", "error", "escalation", "terminate", "compensation"},
    "intermediateCatchEvent": {"message", "timer", "signal", "link", "conditional"},
    "intermediateThrowEvent": {"message", "signal", "link", "escalation", "compensation"},
}


def node_kind(node_type: str) -> NodeKind:
    if node_type in TASK_TYPES:
        return "task"
    if node_type in EVENT_TYPES:
        return "event"
    if node_type in GATEWAY_TYPES:
        return "gateway"
    raise ValueError(f"unsupported node type: {node_type}")


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: NodeType
    name: str
    sourceSentences: list[str] = Field(default_factory=list)
    eventDefinition: EventDefinition | None = None
    role: Literal["split", "join"] | None = None

    @model_validator(mode="after")
    def validate_variant(self):
        kind = node_kind(self.type)
        if kind == "task":
            if self.eventDefinition is not None or self.role is not None:
                raise ValueError("task cannot contain eventDefinition or role")
        elif kind == "event":
            if self.role is not None:
                raise ValueError("event cannot contain role")
            if self.eventDefinition is not None \
                    and self.eventDefinition not in EVENT_DEFINITIONS[self.type]:
                raise ValueError(
                    f"{self.type} does not support eventDefinition={self.eventDefinition}"
                )
        elif kind == "gateway":
            if self.eventDefinition is not None:
                raise ValueError("gateway cannot contain eventDefinition")
            if not self.role:
                raise ValueError("gateway requires role")
        return self

    @property
    def kind(self) -> NodeKind:
        """Internal category; Graph-JSON v2 serializes only the concrete type."""
        return node_kind(self.type)

    @property
    def taskType(self) -> str | None:
        return self.type if self.kind == "task" else None

    @property
    def eventType(self) -> Literal["start", "intermediate", "end"] | None:
        return {
            "startEvent": "start",
            "endEvent": "end",
            "intermediateCatchEvent": "intermediate",
            "intermediateThrowEvent": "intermediate",
        }.get(self.type)

    @property
    def eventRole(self) -> Literal["catch", "throw"] | None:
        return {
            "intermediateCatchEvent": "catch",
            "intermediateThrowEvent": "throw",
        }.get(self.type)

    @property
    def trigger(self) -> str | None:
        return self.eventDefinition or ("none" if self.kind == "event" else None)

    @property
    def gatewayType(self) -> Literal["exclusive", "parallel", "inclusive"] | None:
        return {
            "exclusiveGateway": "exclusive",
            "parallelGateway": "parallel",
            "inclusiveGateway": "inclusive",
        }.get(self.type)


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


class ProcessInfo(BaseModel):
    id: str = "process_main"
    name: str = "未命名流程"


class GraphMetadata(BaseModel):
    revision: int = 0
    processedSegments: list[str] = Field(default_factory=list)
    segmentationStrategy: str = "semantic"
    promptVersion: str = "v1"


class Graph(BaseModel):
    schemaVersion: str = "2.0"
    process: ProcessInfo = Field(default_factory=ProcessInfo)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    metadata: GraphMetadata = Field(default_factory=GraphMetadata)

    def node_map(self) -> dict[str, Node]:
        return {node.id: node for node in self.nodes}

    def model_dump(self, *args, **kwargs) -> dict[str, Any]:
        """Keep persisted and model-facing Graph-JSON v2 free of inapplicable null fields."""
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(*args, **kwargs)


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

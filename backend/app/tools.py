SOURCE_SENTENCES = {"type": "array", "items": {"type": "string"}}
TASK_TYPES = [
    "task", "userTask", "serviceTask", "sendTask", "receiveTask",
    "businessRuleTask", "manualTask", "scriptTask",
]
EVENT_TYPES = [
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
]
GATEWAY_TYPES = ["exclusiveGateway", "parallelGateway", "inclusiveGateway"]
EVENT_DEFINITIONS = [
    "message", "timer", "signal", "conditional", "link", "error",
    "escalation", "terminate", "compensation",
]

CONDITION = {"anyOf": [
    {"type": "object", "properties": {
        "label": {"type": "string"},
        "expression": {"type": ["string", "null"]},
    }, "required": ["label"], "additionalProperties": False},
    {"type": "null"},
]}

TASK_NODE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
        "type": {"enum": TASK_TYPES},
        "name": {"type": "string", "description": "动词加业务对象形式的任务名称。"},
        "sourceSentences": SOURCE_SENTENCES,
    },
    "required": ["id", "type", "name", "sourceSentences"],
    "additionalProperties": False,
}

EVENT_NODE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
        "type": {"enum": EVENT_TYPES},
        "name": {"type": "string"},
        "eventDefinition": {"enum": EVENT_DEFINITIONS},
        "sourceSentences": SOURCE_SENTENCES,
    },
    "required": ["id", "type", "name", "sourceSentences"],
    "additionalProperties": False,
}

EDIT_TOOLS = [
    {"type": "function", "function": {
        "name": "add_task",
        "description": "新增一个表示单一明确业务动作的 Task。type 省略时为普通 task。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
                "type": {"enum": TASK_TYPES},
                "name": {"type": "string", "description": "简洁的动词加业务对象名称。"},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "name", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "add_event",
        "description": "新增 Event。普通事件省略 eventDefinition；Event 只表达触发、等待、发生或结束结果，不得替代明确业务动作。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
                "name": {"type": "string"},
                "type": {"enum": EVENT_TYPES},
                "eventDefinition": {"enum": EVENT_DEFINITIONS},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "name", "type", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "add_gateway",
        "description": "新增控制流 Gateway。exclusiveGateway 恰好选择一路或汇聚互斥路径；parallelGateway 无条件启动或汇聚全部分支；inclusiveGateway 执行所有条件成立分支或等待实际启动分支。role=split 为一入多出分叉，role=join 为多入一出汇聚；同一位置既汇聚又分叉时使用同类型的 join Gateway 后接 split Gateway。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
                "name": {"type": "string"},
                "type": {"enum": GATEWAY_TYPES},
                "role": {"enum": ["split", "join"]},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "name", "type", "role", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "add_edge",
        "description": "新增有方向的 BPMN Sequence Flow，表示 target 在 source 之后执行。condition 只填写决定该流是否执行的业务判定，不得填写活动或分支名称；省略 condition 表示无条件流。isDefault=true 仅表示其他条件均不成立时的后备流，且自身不得有 condition。Parallel Split 的出边不得有 condition 或 default。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "source": {"type": "string"},
                "target": {"type": "string"},
                "condition": CONDITION,
                "isDefault": {"type": "boolean"},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "source", "target", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "update_node",
        "description": "仅在 Repair 阶段最小修改已有节点。不得修改 id；type 只能在同一节点类别内改变。",
        "parameters": {"type": "object", "properties": {
            "targetId": {"type": "string"},
            "changes": {"type": "object", "properties": {
                "name": {"type": "string"},
                "sourceSentences": SOURCE_SENTENCES,
                "type": {"enum": TASK_TYPES + EVENT_TYPES + GATEWAY_TYPES},
                "eventDefinition": {"anyOf": [{"enum": EVENT_DEFINITIONS}, {"type": "null"}]},
                "role": {"enum": ["split", "join"]},
            }, "minProperties": 1, "additionalProperties": False},
            "issueId": {"type": "string"},
        }, "required": ["targetId", "changes"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "update_edge",
        "description": "仅在 Repair 阶段最小修改已有 Sequence Flow，不得修改 edge id。使用 changes.condition=null 可移除条件并使该流无条件执行。",
        "parameters": {"type": "object", "properties": {
            "targetId": {"type": "string"},
            "changes": {"type": "object", "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "condition": CONDITION,
                "isDefault": {"type": "boolean"},
                "sourceSentences": SOURCE_SENTENCES,
            }, "minProperties": 1, "additionalProperties": False},
            "issueId": {"type": "string"},
        }, "required": ["targetId", "changes"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "delete_node",
        "description": "仅在 Repair 阶段删除错误节点；调用前必须先显式删除所有相连边。",
        "parameters": {"type": "object", "properties": {
            "targetId": {"type": "string"}, "issueId": {"type": "string"},
        }, "required": ["targetId"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "delete_edge",
        "description": "仅在 Repair 阶段删除错误的 Sequence Flow。",
        "parameters": {"type": "object", "properties": {
            "targetId": {"type": "string"}, "issueId": {"type": "string"},
        }, "required": ["targetId"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "add_linear_sequence",
        "description": "批量新增纯串行的 Task/Event 链，系统自动生成相邻 Sequence Flow。严禁在 nodes 中放入任何 Gateway；出现分叉、汇聚或循环时必须改用 add_gateway/add_edge 显式建模。",
        "parameters": {
            "type": "object",
            "properties": {
                "predecessorId": {"type": ["string", "null"], "description": "可选的已有前驱节点 ID。"},
                "nodes": {"type": "array", "minItems": 1, "items": {"oneOf": [TASK_NODE, EVENT_NODE]}},
            },
            "required": ["nodes"],
            "additionalProperties": False,
        },
    }},
]

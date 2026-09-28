SOURCE_SENTENCES = {"type": "array", "items": {"type": "string"}}

TASK_NODE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
        "kind": {"const": "task"},
        "name": {"type": "string", "description": "动词加业务对象形式的任务名称。"},
        "sourceSentences": SOURCE_SENTENCES,
    },
    "required": ["id", "kind", "name", "sourceSentences"],
    "additionalProperties": False,
}

EVENT_NODE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
        "kind": {"const": "event"},
        "name": {"type": "string"},
        "eventType": {"enum": ["start", "intermediate", "end"]},
        "trigger": {"enum": ["none", "message", "timer"]},
        "eventRole": {"enum": ["catch", "throw"]},
        "sourceSentences": SOURCE_SENTENCES,
    },
    "required": ["id", "kind", "name", "eventType", "trigger", "sourceSentences"],
    "additionalProperties": False,
}

EDIT_TOOLS = [
    {"type": "function", "function": {
        "name": "add_task",
        "description": "新增一个表示明确业务动作的 Task。只能填写任务字段，不能携带 eventType、trigger、gatewayType 或 role。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
                "name": {"type": "string", "description": "简洁的动词加业务对象名称。"},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "name", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "add_event",
        "description": "新增 Start、Intermediate 或 End Event。Event 只表达流程状态或触发，不得用来替代文本明确描述的业务动作。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
                "name": {"type": "string"},
                "eventType": {"enum": ["start", "intermediate", "end"]},
                "trigger": {"enum": ["none", "message", "timer"]},
                "eventRole": {"enum": ["catch", "throw"], "description": "仅 intermediate event 可用；省略时按 catch 处理。"},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "name", "eventType", "trigger", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "add_gateway",
        "description": "新增控制流 Gateway。exclusive 表示互斥选择，parallel 表示全部并行，inclusive 表示一个或多个分支；role=split 为分叉，role=join 为汇聚。Gateway 不表示业务动作。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "稳定且唯一的英文 XML ID。"},
                "name": {"type": "string"},
                "gatewayType": {"enum": ["exclusive", "parallel", "inclusive"]},
                "role": {"enum": ["split", "join"]},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "name", "gatewayType", "role", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "add_edge",
        "description": "新增一条有方向的 BPMN Sequence Flow。循环通过 target 指向已存在的较早节点形成回边；不要复制被重复执行的节点。条件只放在 condition 中，并行分叉的出边不得带条件。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "source": {"type": "string"},
                "target": {"type": "string"},
                "condition": {"anyOf": [
                    {"type": "object", "properties": {
                        "label": {"type": "string"},
                        "expression": {"type": ["string", "null"]},
                    }, "required": ["label"], "additionalProperties": False},
                    {"type": "null"},
                ]},
                "isDefault": {"type": "boolean"},
                "sourceSentences": SOURCE_SENTENCES,
            },
            "required": ["id", "source", "target", "sourceSentences"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "update_node",
        "description": "仅在 Repair 阶段按 issue 修改已有节点的可变字段。不得修改 id、kind，也不得把 Task/Event/Gateway 的专属字段混用。",
        "parameters": {"type": "object", "properties": {
            "targetId": {"type": "string"}, "changes": {"type": "object"}, "issueId": {"type": "string"},
        }, "required": ["targetId", "changes"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "update_edge",
        "description": "仅在 Repair 阶段按 issue 修改已有 Sequence Flow，不得修改 edge id。",
        "parameters": {"type": "object", "properties": {
            "targetId": {"type": "string"}, "changes": {"type": "object"}, "issueId": {"type": "string"},
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

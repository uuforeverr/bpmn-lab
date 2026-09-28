from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring, register_namespace

from .domain import Graph, Node

BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DI = "http://www.omg.org/spec/DD/20100524/DI"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
for prefix, uri in (("bpmn", BPMN), ("bpmndi", BPMNDI), ("dc", DC), ("di", DI), ("xsi", XSI)):
    register_namespace(prefix, uri)


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _tag(node: Node) -> str:
    if node.kind == "task": return "task"
    if node.kind == "gateway":
        return {"exclusive": "exclusiveGateway", "parallel": "parallelGateway", "inclusive": "inclusiveGateway"}[node.gatewayType]
    return {"start": "startEvent", "intermediate": "intermediateCatchEvent" if node.eventRole != "throw" else "intermediateThrowEvent", "end": "endEvent"}[node.eventType]


def graph_to_bpmn(graph: Graph) -> str:
    definitions = Element(q(BPMN, "definitions"), {"id": "Definitions_1", "targetNamespace": "https://bpmn-lab.local"})
    process = SubElement(definitions, q(BPMN, "process"), {"id": graph.process.id, "name": graph.process.name, "isExecutable": "false"})
    for node in graph.nodes:
        attrs = {"id": node.id, "name": node.name}
        if node.kind == "gateway" and node.role == "split":
            default_flow = next((edge.id for edge in graph.edges if edge.source == node.id and edge.isDefault), None)
            if default_flow:
                attrs["default"] = default_flow
        element = SubElement(process, q(BPMN, _tag(node)), attrs)
        if node.kind == "event" and node.eventType == "intermediate":
            definition_tag = {"message": "messageEventDefinition", "timer": "timerEventDefinition"}.get(node.trigger)
            if definition_tag:
                SubElement(element, q(BPMN, definition_tag), {"id": f"{node.id}_definition"})
    for edge in graph.edges:
        attrs = {"id": edge.id, "sourceRef": edge.source, "targetRef": edge.target}
        if edge.condition: attrs["name"] = edge.condition.label
        flow = SubElement(process, q(BPMN, "sequenceFlow"), attrs)
        if edge.condition and edge.condition.expression:
            expression = SubElement(flow, q(BPMN, "conditionExpression"), {q(XSI, "type"): "bpmn:tFormalExpression"})
            expression.text = edge.condition.expression

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(definitions, encoding="unicode")

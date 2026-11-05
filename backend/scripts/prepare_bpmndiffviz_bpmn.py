from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree as ET

BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_for_legacy_camunda(source: Path, destination: Path) -> list[str]:
    """Make standards-equivalent ordering fixes required by Camunda 7.3.

    PMo's normalized BPMN places event definitions before incoming/outgoing
    references, which the old parser bundled with BPMNDiffViz rejects. Empty
    conditional event definitions also need a formal condition to satisfy that
    parser. The latter is recorded as a compatibility assumption.
    """
    namespaces: dict[str, str] = {}
    for _, value in ET.iterparse(source, events=("start-ns",)):
        prefix, uri = value
        namespaces.setdefault(prefix, uri)
    for prefix, uri in namespaces.items():
        ET.register_namespace(prefix, uri)

    tree = ET.parse(source)
    root = tree.getroot()
    notes: list[str] = []
    event_names = {
        "startEvent",
        "endEvent",
        "intermediateCatchEvent",
        "intermediateThrowEvent",
        "boundaryEvent",
    }
    for element in root.iter():
        if _local_name(element.tag) not in event_names:
            continue
        children = list(element)
        order = {
            "documentation": 0,
            "extensionElements": 1,
            "incoming": 2,
            "outgoing": 3,
            "property": 4,
            "dataInput": 5,
            "dataOutput": 6,
            "dataInputAssociation": 7,
            "dataOutputAssociation": 8,
            "inputSet": 9,
            "outputSet": 10,
        }
        reordered = sorted(
            enumerate(children),
            key=lambda item: (order.get(_local_name(item[1].tag), 20), item[0]),
        )
        if [child for _, child in reordered] != children:
            element[:] = [child for _, child in reordered]
            notes.append(f"reordered_event_children:{element.get('id', '')}")

        for child in element:
            if _local_name(child.tag) != "conditionalEventDefinition":
                continue
            if not any(_local_name(grandchild.tag) == "condition" for grandchild in child):
                condition = ET.SubElement(child, f"{{{BPMN}}}condition")
                condition.set(f"{{{XSI}}}type", "tFormalExpression")
                condition.text = "true"
                notes.append(f"inserted_true_condition:{element.get('id', '')}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="utf-8", xml_declaration=True)
    return notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    notes = normalize_for_legacy_camunda(args.source, args.destination)
    for note in notes:
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

const test = require('node:test');
const assert = require('node:assert/strict');
const { layoutBpmnXml } = require('./layout');

const semanticXml = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
  id="Definitions_1" targetNamespace="https://bpmn-lab.local">
  <bpmn:process id="process_main" isExecutable="false">
    <bpmn:startEvent id="start" />
    <bpmn:task id="work" name="Work" />
    <bpmn:endEvent id="end" />
    <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="work" />
    <bpmn:sequenceFlow id="f2" sourceRef="work" targetRef="end" />
  </bpmn:process>
</bpmn:definitions>`;

test('adds complete BPMN DI to semantic XML', async () => {
  const { xml, warnings } = await layoutBpmnXml(semanticXml);
  assert.match(xml, /BPMNDiagram/);
  assert.match(xml, /BPMNShape/);
  assert.match(xml, /BPMNEdge/);
  assert.match(xml, /waypoint/);
  assert.ok(Array.isArray(warnings));
});

test('rejects an empty payload', async () => {
  await assert.rejects(() => layoutBpmnXml(''), /non-empty string/);
});

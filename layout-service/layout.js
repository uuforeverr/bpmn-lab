const { layoutProcess } = require('bpmn-auto-layout');

async function layoutBpmnXml(bpmnXml) {
  if (typeof bpmnXml !== 'string' || !bpmnXml.trim()) {
    throw new TypeError('bpmnXml must be a non-empty string');
  }

  const result = await layoutProcess(bpmnXml);
  if (typeof result === 'string') {
    return { xml: result, warnings: [] };
  }
  return { xml: result.xml, warnings: result.warnings || [] };
}

module.exports = { layoutBpmnXml };

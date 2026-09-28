const express = require('express');
const { layoutBpmnXml } = require('./layout');

const app = express();
const port = Number(process.env.PORT || 3001);

app.use(express.json({ limit: '5mb' }));

app.get('/health', (_request, response) => {
  response.json({ status: 'ok', service: 'bpmn-layout' });
});

app.post('/layout', async (request, response) => {
  try {
    const { xml, warnings } = await layoutBpmnXml(request.body?.bpmnXml);
    response.json({
      layoutedXml: xml,
      warnings: warnings.map(({ message, code }) => ({ message, code })),
    });
  } catch (error) {
    console.error('BPMN layout failed:', error);
    response.status(422).json({ detail: error instanceof Error ? error.message : String(error) });
  }
});

app.listen(port, '127.0.0.1', () => {
  console.log(`BPMN layout service listening on http://127.0.0.1:${port}`);
});

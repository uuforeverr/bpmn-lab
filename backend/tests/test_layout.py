import httpx

from app.config import LayoutConfig
from app.layout import BpmnLayoutClient


def test_layout_client_returns_layouted_xml():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/layout"
        assert "bpmnXml" in request.read().decode()
        return httpx.Response(200, json={"layoutedXml": "<definitions><BPMNDiagram /></definitions>"})

    client = httpx.Client(
        base_url="http://layout.test",
        transport=httpx.MockTransport(handler),
    )
    layout = BpmnLayoutClient(LayoutConfig(enabled=True, base_url="http://layout.test"), client)

    assert "BPMNDiagram" in layout.layout("<definitions />")


def test_layout_client_rejects_empty_xml_response():
    client = httpx.Client(
        base_url="http://layout.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"layoutedXml": ""})),
    )
    layout = BpmnLayoutClient(LayoutConfig(enabled=True, base_url="http://layout.test"), client)

    try:
        layout.layout("<definitions />")
    except RuntimeError as exc:
        assert "空 XML" in str(exc)
    else:
        raise AssertionError("empty layout response must fail")


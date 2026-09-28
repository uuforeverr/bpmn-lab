from __future__ import annotations

import httpx

from .config import LayoutConfig


class BpmnLayoutClient:
    def __init__(self, config: LayoutConfig, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            trust_env=False,
        )

    def health(self) -> bool:
        try:
            return self.client.get("/health").json().get("status") == "ok"
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def layout(self, bpmn_xml: str) -> str:
        try:
            response = self.client.post("/layout", json={"bpmnXml": bpmn_xml})
            response.raise_for_status()
            layouted_xml = response.json().get("layoutedXml")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise RuntimeError(f"BPMN 自动布局服务调用失败: {exc}") from exc
        if not isinstance(layouted_xml, str) or not layouted_xml.strip():
            raise RuntimeError("BPMN 自动布局服务返回了空 XML")
        return layouted_xml

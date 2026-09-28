from fastapi.testclient import TestClient

from app.main import app


def test_health_and_first_step():
    client = TestClient(app)
    health = client.get("/api/health")
    assert health.status_code == 200
    created = client.post("/api/runs", json={"inputText": "提交申请。系统审核。", "strategy": "fixed_length"})
    assert created.status_code == 200
    run = created.json()
    stepped = client.post(f"/api/runs/{run['id']}/step")
    assert stepped.status_code == 200
    assert stepped.json()["stage"] == "SENTENCES_PREPARED"

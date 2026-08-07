"""冒烟测试: 校验新增的调参闭环端点(不依赖真实交易数据)."""
from fastapi.testclient import TestClient
from app.main import app


def test_tuning_policy_and_changes():
    c = TestClient(app)
    # 护栏策略
    r = c.get("/api/ai-review/tuning-policy")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["max_step_pct"] == 20
    assert d["max_drift_pct"] == 50
    assert d["cooldown_days"] == 7
    assert d["max_accept_per_review"] == 3
    assert d["field_count"] == 21
    assert "风控" in d["forbidden_groups"]
    assert "趋势" in d["allowed_groups"]

    # 变更记录(初始应为空列表)
    r = c.get("/api/ai-review/changes")
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["data"], list)

    # 撤销一个不存在的变更应 400
    r = c.post("/api/ai-review/changes/999999/revert")
    assert r.status_code == 400
    print("SMOKE OK: tuning-policy + changes endpoints healthy")

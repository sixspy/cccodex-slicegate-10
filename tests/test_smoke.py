import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield

def _login(c):
    r = c.post("/api/login", json={"username":"admin","password":"123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}

def _task_id(c, h, slice_no):
    q = c.get("/api/sampling/queue", headers=h).json()["items"]
    return next(t["task_id"] for t in q if t["slice_no"] == slice_no)

def test_pipeline_gated():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    h = _login(c)
    assert len(c.get("/api/slices", headers=h).json()["items"]) >= 1
    # 存量已放行切片被补登为抽检通过，原放行与水位不动
    seeded = c.get("/api/slices", headers=h).json()["items"]
    west = next(s for s in seeded if s["slice_no"] == "SL-2026-11-01-west")
    assert west["release_status"] == "已放行" and west["sampling_status"] == "抽检通过"
    # 部分完整仍不可放行
    assert c.post("/api/slices/1/release", headers=h, json={"consumer":"x"}).status_code == 409
    # 登记完整切片：未过闸前只能标待抽检，不能直接完整/放行
    ok = c.post("/api/slices", headers=h, json={
        "slice_no":"SL-T-1","biz_date":"2026-11-04","partition_key":"mid",
        "row_count":10,"expected_count":10})
    assert ok.status_code == 200
    sid = ok.json()["id"]
    assert ok.json()["completeness"] == "待抽检"
    assert c.post(f"/api/slices/{sid}/evaluate", headers=h).status_code == 409
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer":"ads_x"}).status_code == 409
    # 过抽检复核闸（钉死乙路径）
    tid = _task_id(c, h, "SL-T-1")
    d = c.post(f"/api/sampling/tasks/{tid}/decision", headers=h,
               json={"passed": True, "note": "抽检通过", "policy_path": "乙"})
    assert d.status_code == 200 and d.json()["status"] == "抽检通过"
    # 通过后才允许完整度判定与放行
    ev = c.post(f"/api/slices/{sid}/evaluate", headers=h)
    assert ev.status_code == 200 and ev.json()["completeness"] == "完整"
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer":"ads_x","policy_path":"乙"})
    assert rel.status_code == 200
    assert any(w["source_slice"]=="SL-T-1" for w in c.get("/api/watermarks", headers=h).json()["items"])
    # 说明册钉乙、同库巡检无偏离
    assert c.get("/api/sampling/manual", headers=h).json()["policy_path"] == "乙"
    assert c.get("/api/sampling/consistency", headers=h).json()["ok"] is True

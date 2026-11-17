import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db, connect


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _register_complete(c, h, no="SL-G-1"):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": no, "biz_date": "2026-11-05", "partition_key": "east",
        "row_count": 100, "expected_count": 100})
    assert r.status_code == 200
    return r.json()["id"]


def _task_of(c, h, no):
    items = c.get("/api/sampling/queue", headers=h).json()["items"]
    return next(t["task_id"] for t in items if t["slice_no"] == no)


def _events(c, h):
    return c.get("/api/sampling/events", headers=h).json()["items"]


def test_no_skip_gate_and_manual_pinned():
    c = TestClient(app)
    h = _login(c)
    sid = _register_complete(c, h)
    # 禁止跳过抽检直接标完整
    r1 = c.post(f"/api/slices/{sid}/evaluate", headers=h)
    assert r1.status_code == 409 and "抽检" in r1.json()["detail"]
    # 禁止跳过抽检直接放行
    r2 = c.post(f"/api/slices/{sid}/release", headers=h,
                json={"consumer": "ads_a", "policy_path": "乙"})
    assert r2.status_code == 409
    # 没有成功放行流水、没有水位推进
    rels = [r for r in c.get("/api/releases", headers=h).json()["items"]
            if r["slice_id"] == sid and r["status"] == "成功"]
    assert rels == []
    assert all(w["source_slice"] != "SL-G-1"
               for w in c.get("/api/watermarks", headers=h).json()["items"])
    m = c.get("/api/sampling/manual", headers=h).json()
    assert m["policy_path"] == "乙" and m["locked"] is True
    # 说明册单行钉死，不提供任何改写接口
    assert c.put("/api/sampling/manual", headers=h, json={"policy_path": "甲"}).status_code == 405


def test_path_b_fail_freeze_hard_refuse_until_unfreeze():
    c = TestClient(app)
    h = _login(c)
    sid = _register_complete(c, h, "SL-G-2")
    tid = _task_of(c, h, "SL-G-2")

    # 抽检不通过 → 复核冻结
    d = c.post(f"/api/sampling/tasks/{tid}/decision", headers=h,
               json={"passed": False, "note": "缺数", "policy_path": "乙"})
    assert d.status_code == 200 and d.json()["status"] == "复核冻结"

    # 复核详情可看
    det = c.get(f"/api/sampling/tasks/{tid}", headers=h).json()
    assert det["task"]["status"] == "复核冻结"
    assert any(e["event_type"] == "抽检失败" for e in det["events"])

    # 任何成功放行硬拒绝
    rel = c.post(f"/api/slices/{sid}/release", headers=h,
                 json={"consumer": "ads_b", "policy_path": "乙"})
    assert rel.status_code == 409 and "硬拒绝" in rel.json()["detail"]
    # 完整度判定同样硬拒绝
    assert c.post(f"/api/slices/{sid}/evaluate", headers=h).status_code == 409
    # 无成功流水、无水位
    assert not [r for r in c.get("/api/releases", headers=h).json()["items"]
                if r["slice_id"] == sid and r["status"] == "成功"]
    assert all(w["source_slice"] != "SL-G-2"
               for w in c.get("/api/watermarks", headers=h).json()["items"])

    # 解冻重裁 → 回到待抽检（轮次+1）
    u = c.post(f"/api/sampling/tasks/{tid}/unfreeze", headers=h,
               json={"note": "补齐", "policy_path": "乙"})
    assert u.status_code == 200
    assert u.json()["status"] == "待抽检" and u.json()["round_no"] == 2

    # 解冻后仍需重新抽检，不能直接放行
    assert c.post(f"/api/slices/{sid}/release", headers=h,
                  json={"consumer": "ads_b"}).status_code == 409
    # 重裁通过 → 完整 → 放行 → 水位推进
    d2 = c.post(f"/api/sampling/tasks/{tid}/decision", headers=h,
                json={"passed": True, "policy_path": "乙"})
    assert d2.status_code == 200 and d2.json()["status"] == "抽检通过"
    assert c.post(f"/api/slices/{sid}/evaluate", headers=h).json()["completeness"] == "完整"
    rel2 = c.post(f"/api/slices/{sid}/release", headers=h,
                  json={"consumer": "ads_b", "policy_path": "乙"})
    assert rel2.status_code == 200
    assert any(w["source_slice"] == "SL-G-2"
               for w in c.get("/api/watermarks", headers=h).json()["items"])
    # 红线：失败与已放行永不同时成立
    q = c.get("/api/sampling/queue", headers=h).json()["items"]
    row = next(t for t in q if t["slice_no"] == "SL-G-2")
    assert not (row["status"] == "复核冻结" and row["release_status"] == "已放行")
    assert c.get("/api/sampling/consistency", headers=h).json()["ok"] is True


def test_policy_deviation_blocked_and_logged():
    c = TestClient(app)
    h = _login(c)
    sid = _register_complete(c, h, "SL-G-3")
    tid = _task_of(c, h, "SL-G-3")
    # 混抽甲路径 → 拦下，记「抽检口径偏了」，任务保持待抽检
    r = c.post(f"/api/sampling/tasks/{tid}/decision", headers=h,
               json={"passed": True, "policy_path": "甲"})
    assert r.status_code == 409 and "抽检口径偏了" in r.json()["detail"]
    q = c.get("/api/sampling/queue", headers=h).json()["items"]
    assert next(t for t in q if t["slice_no"] == "SL-G-3")["status"] == "待抽检"
    assert any(e["event_type"] == "抽检口径偏了" for e in _events(c, h))
    # 放行闸上混口径同样拦截并落台账
    # 先正常通过抽检
    assert c.post(f"/api/sampling/tasks/{tid}/decision", headers=h,
                  json={"passed": True, "policy_path": "乙"}).status_code == 200
    c.post(f"/api/slices/{sid}/evaluate", headers=h)
    r2 = c.post(f"/api/slices/{sid}/release", headers=h,
                json={"consumer": "ads_c", "policy_path": "甲"})
    assert r2.status_code == 409 and "抽检口径偏了" in r2.json()["detail"]
    # 口径偏离时没有成功放行
    assert not [x for x in c.get("/api/releases", headers=h).json()["items"]
                if x["slice_id"] == sid and x["status"] == "成功"]
    assert len([e for e in _events(c, h) if e["event_type"] == "抽检口径偏了"]) >= 2


def test_concurrent_decisions_serialized_no_silent_success():
    c = TestClient(app)
    h = _login(c)
    sid = _register_complete(c, h, "SL-G-4")
    tid = _task_of(c, h, "SL-G-4")
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    # 直接调用端点函数，避免线程内 TestClient 触发 startup 重建库；
    # 真实经过 BEGIN IMMEDIATE 串行锁与 409 争用处理
    def worker(passed):
        from app import sampling_decision, DecisionIn
        barrier.wait()
        try:
            out = sampling_decision(tid, DecisionIn(passed=passed, policy_path="乙"),
                                    authorization=h["Authorization"])
            code, body = 200, dict(out)
        except Exception as exc:  # HTTPException
            code, body = getattr(exc, "status_code", 500), {"detail": getattr(exc, "detail", str(exc))}
        with lock:
            results.append((code, body))

    t1 = threading.Thread(target=worker, args=(True,))
    t2 = threading.Thread(target=worker, args=(False,))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(code for code, _ in results)
    assert codes == [200, 409]  # 一成一争用，谁先拿到锁不影响判定
    # 最终任务只有一个终态，且与成功写入者一致，绝无两份裁决
    q = c.get("/api/sampling/queue", headers=h).json()["items"]
    final = next(t for t in q if t["slice_no"] == "SL-G-4")
    assert final["status"] in ("抽检通过", "复核冻结")
    ok_resp = next(j for code, j in results if code == 200)
    assert ok_resp["status"] == final["status"]
    # 负方被明确记为抽检争用（含锁失败独立短连接台账）
    assert any(e["event_type"] == "抽检争用" for e in _events(c, h))


def test_db_write_lock_unit():
    """白盒：持有写锁时第二个写者必须 OperationalError（串行锁存在）。"""
    holder = connect()
    holder.execute("BEGIN IMMEDIATE")
    other = connect()
    other.execute("PRAGMA busy_timeout=200")
    with pytest.raises(sqlite3.OperationalError):
        other.execute("BEGIN IMMEDIATE")
    other.close()
    holder.rollback()
    holder.close()


def test_consistency_flags_illegal_states_whitebox():
    c = TestClient(app)
    h = _login(c)
    sid = _register_complete(c, h, "SL-G-5")
    # 白盒制造非法态：未过抽检却标完整（正常接口做不到）
    conn = connect()
    conn.execute("UPDATE slices SET completeness='完整' WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    rep = c.get("/api/sampling/consistency", headers=h).json()
    assert rep["ok"] is False
    assert any(v["slice_id"] == sid and "跳过抽检" in v["problem"] for v in rep["violations"])
    # 巡检同时把偏离写进台账
    assert any(e["event_type"] == "抽检口径偏了" for e in _events(c, h))


def test_viewer_login_retained_and_needs_auth():
    c = TestClient(app)
    # 登录保留：viewer 账号仍可登录
    r = c.post("/api/login", json={"username": "viewer", "password": "view123"})
    assert r.status_code == 200 and r.json()["role"] == "只读"
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert c.get("/api/sampling/queue", headers=h).status_code == 200
    # 未登录一律 401
    assert c.get("/api/sampling/queue").status_code == 401
    assert c.get("/api/sampling/manual").status_code == 401

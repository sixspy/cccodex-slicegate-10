
import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# ---- 抽检说明册：甲乙互斥，钉死乙路径，当天不许改口 ----
MANUAL_VERSION = "2026-11-16-钉版"
MANUAL_LOCKED_DATE = "2026-11-16"
POLICY_PATH = "乙"  # 仅允许 "乙"；"甲" 为备选存档写法，禁止启用/混用
BUSY_TIMEOUT_MS = 5000

MANUAL_BODY = """抽检说明册（钉版 2026-11-16）

一、钉死口径
本工程在甲、乙两条互斥写法中钉死【乙路径】，当天不许改口，禁止甲、乙两边混抽。
任何偏离本说明册的请求一律拦下，并记「抽检口径偏了」流水。

二、乙路径写法（唯一生效）
1. 切片登记即入抽检队列，抽检状态「待抽检」，未过闸不得标「完整」；
2. 抽检不通过 → 切片进入「复核冻结」，可查看复核详情，但任何成功放行一律硬拒绝，
   直至「解冻重裁」后重新裁决；
3. 解冻后回到抽检队列重新抽检，抽检通过后才允许完整度判定与放行；
4. 抽检未通过/冻结期间，成功放行落库与消费水位推进一并挡住，只写抽检失败/拦截流水，
   绝不写入成功放行流水、绝不推进水位。

三、互斥红线
- 禁止跳过抽检直接标「完整 / 已放行」；
- 禁止「抽检失败（复核冻结）」与「已放行」同时成立；
- 两名抽检值班同时点下必须串行加锁：后手写明「抽检争用」，禁止默许写成成功；
- 抽检队列、切片列表旁注、复核详情、放行闸必须同库核对，不得各读各的。

四、甲路径（备选存档，不得启用）
抽检未通过则成功放行落库以及水位推进一并挡住，只写抽检失败流水。
"""

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

def log_event(conn, slice_id: int | None, task_id: int | None,
              event_type: str, detail: str, operator: str):
    conn.execute(
        "INSERT INTO sampling_events(slice_id,task_id,event_type,detail,operator,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (slice_id, task_id, event_type, detail, operator, now()),
    )

def log_event_standalone(slice_id, task_id, event_type, detail, operator):
    """主事务拿不到锁时，用独立短连接记流水（如并发争用负方）。"""
    c = connect()
    try:
        log_event(c, slice_id, task_id, event_type, detail, operator)
        c.commit()
    finally:
        c.close()

class PolicyDeviation(Exception):
    """抽检口径偏离说明册：调用方须在同连接落「抽检口径偏了」台账、提交后再转 409。"""
    def __init__(self, path: str):
        self.path = path

def check_policy(body_path: str | None, conn, slice_id, task_id, operator) -> None:
    """混用甲路径或任何非钉死口径 → 记台账并抛 PolicyDeviation（同连接写入，禁止另开连接自锁）。"""
    if body_path is not None and body_path != POLICY_PATH:
        log_event(conn, slice_id, task_id, "抽检口径偏了",
                  f"请求口径={body_path}，说明册钉死={POLICY_PATH}，禁止混用", operator)
        raise PolicyDeviation(body_path)

def policy_409(path: str) -> HTTPException:
    return HTTPException(409, f"抽检口径偏了：说明册钉死乙路径，禁止混用甲/其它写法（{path}）")

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sampling_manual(
          id INTEGER PRIMARY KEY CHECK (id=1),
          policy_path TEXT NOT NULL,
          version TEXT NOT NULL,
          locked_for_date TEXT NOT NULL,
          pinned_at TEXT NOT NULL,
          body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sampling_tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL UNIQUE,
          status TEXT NOT NULL,
          round_no INTEGER NOT NULL DEFAULT 1,
          inspector TEXT,
          decided_at TEXT,
          detail TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sampling_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER,
          task_id INTEGER,
          event_type TEXT NOT NULL,
          detail TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute(
                    "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status)"
                    " VALUES (?,?,?,?,?,?,?)", r)
            conn.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at)"
                " VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute(
                "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice)"
                " VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")

        # 说明册单行钉死：INSERT OR IGNORE，重启不变，且不提供任何改写接口
        conn.execute(
            "INSERT OR IGNORE INTO sampling_manual(id,policy_path,version,locked_for_date,pinned_at,body)"
            " VALUES (1,?,?,?,?,?)",
            (POLICY_PATH, MANUAL_VERSION, MANUAL_LOCKED_DATE, now(), MANUAL_BODY))

        # 存量切片补登抽检任务（幂等）：已放行→视同抽检通过补登；其余→待抽检入队
        existed = conn.execute("SELECT s.id, s.completeness, s.release_status FROM slices s"
                               " LEFT JOIN sampling_tasks t ON t.slice_id=s.id WHERE t.id IS NULL").fetchall()
        for r in existed:
            if r["release_status"] == "已放行":
                conn.execute(
                    "INSERT INTO sampling_tasks(slice_id,status,round_no,inspector,decided_at,detail,updated_at)"
                    " VALUES (?, '抽检通过', 1, 'system', ?, '存量已放行切片补登抽检通过', ?)",
                    (r["id"], now(), now()))
                log_event(conn, r["id"], None, "存量补登", "存量已放行切片补登抽检通过，原数据不变", "system")
            else:
                conn.execute(
                    "INSERT INTO sampling_tasks(slice_id,status,round_no,detail,updated_at)"
                    " VALUES (?, '待抽检', 1, '存量切片补录入队', ?)", (r["id"], now()))
                log_event(conn, r["id"], None, "抽检入队", "存量切片补录入抽检队列", "system")
                # 存量若已标「完整」却未放行，违反新闸不变量，回退为待抽检并记偏离
                if r["completeness"] == "完整":
                    conn.execute("UPDATE slices SET completeness='待抽检' WHERE id=?", (r["id"],))
                    log_event(conn, r["id"], None, "抽检口径偏了",
                              "存量切片未过抽检却标完整，回退为待抽检", "system")

@app.on_event("startup")
def _start():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute(
            "SELECT username, role FROM users WHERE username=? AND password_sha=?",
            (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        # 与抽检队列同库 JOIN 核对，列表旁注直接读抽检任务表
        rows = c.execute("""
            SELECT s.*, t.id AS sampling_task_id, t.status AS sampling_status,
                   t.round_no AS sampling_round, t.inspector AS sampling_inspector
            FROM slices s
            LEFT JOIN sampling_tasks t ON t.slice_id = s.id
            ORDER BY s.id""").fetchall()
    return {"items": [dict(r) for r in rows]}

class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int

@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    would_complete = body.row_count >= body.expected_count and body.expected_count > 0
    # 未过抽检闸前，即使行数达标也只能标「待抽检」，禁止直接标完整
    comp = "待抽检" if would_complete else (
        "部分完整" if body.row_count > 0 else "待检")
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status)"
                " VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key,
                 body.row_count, body.expected_count, comp, "未放行"))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")
        sid = cur.lastrowid
        c.execute(
            "INSERT INTO sampling_tasks(slice_id,status,round_no,detail,updated_at)"
            " VALUES (?, '待抽检', 1, '登记即入抽检队列', ?)", (sid, now()))
        task = c.execute("SELECT id FROM sampling_tasks WHERE slice_id=?", (sid,)).fetchone()
        log_event(c, sid, task["id"], "抽检入队",
                  "切片登记即入抽检队列，完整判定与放行须先过抽检复核闸", operator)
    return {"id": sid, "completeness": comp, "sampling_status": "待抽检", "sampling_task_id": task["id"]}

def _recompute_completeness(row) -> str:
    return "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else (
        "部分完整" if row["row_count"] > 0 else "待检")

@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        target = _recompute_completeness(row)
        if target == "完整":
            task = c.execute("SELECT * FROM sampling_tasks WHERE slice_id=?", (sid,)).fetchone()
            if task is None:
                log_event(c, sid, None, "抽检口径偏了", "抽检任务缺失仍试图标完整，闸口拦截", operator)
                c.commit()
                raise HTTPException(409, "抽检复核闸缺失，禁止跳过抽检标完整")
            if task["status"] == "复核冻结":
                log_event(c, sid, task["id"], "闸口拦截",
                          "复核冻结期间申请完整度判定，硬拒绝", operator)
                c.commit()
                raise HTTPException(409, "抽检失败已进入复核冻结：完整度判定硬拒绝，直至解冻重裁")
            if task["status"] != "抽检通过":
                log_event(c, sid, task["id"], "闸口拦截",
                          f"抽检状态={task['status']}，申请标完整被拦截", operator)
                c.commit()
                raise HTTPException(409, "抽检复核未通过，禁止标完整（放行落库与水位推进一并挡住）")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (target, sid))
        log_event(c, sid, None, "完整度裁决", f"重裁结论={target}", operator)
    return {"id": sid, "completeness": target}

class ReleaseIn(BaseModel):
    consumer: str
    policy_path: str | None = None

@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        if row["release_status"] == "已放行":
            raise HTTPException(409, "切片已放行，禁止重复放行")

        # ===== 抽检复核闸：成功放行落库之前硬校验，优先级高于完整度检查 =====
        try:
            check_policy(body.policy_path, c, sid, None, operator)
        except PolicyDeviation as e:
            c.commit()
            raise policy_409(e.path)
        task = c.execute("SELECT * FROM sampling_tasks WHERE slice_id=?", (sid,)).fetchone()
        if task is None:
            log_event(c, sid, None, "抽检口径偏了", "无抽检任务却申请放行，闸口拦截", operator)
            c.commit()  # 偏离台账先落库，再拒，避免随异常回滚丢失
            raise HTTPException(409, "抽检复核闸缺失，禁止跳过抽检直接放行")
        if task["status"] == "复核冻结":
            log_event(c, sid, task["id"], "闸口拦截",
                      f"复核冻结期间申请放行至{body.consumer}，硬拒绝：不写成功流水、不推进水位", operator)
            c.commit()
            raise HTTPException(409, "抽检失败复核冻结中：任何成功放行硬拒绝，直至解冻重裁（本次未写成功流水、未推进水位）")
        if task["status"] != "抽检通过":
            log_event(c, sid, task["id"], "闸口拦截",
                      f"抽检状态={task['status']} 申请放行至{body.consumer}，成功落库与水位推进一并挡住", operator)
            c.commit()
            raise HTTPException(409, "抽检复核未通过，禁止放行：成功放行落库与水位推进一并挡住")

        if row["completeness"] != "完整":
            c.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                (sid, body.consumer, "拒绝", "完整度未达标", now()))
            c.commit()
            raise HTTPException(409, "未完整禁止放行")

        c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        c.execute(
            "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
            (sid, body.consumer, "成功", "抽检通过且完整切片放行", now()))
        c.execute(
            "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
            (body.consumer, row["biz_date"], now(), row["slice_no"]))
        log_event(c, sid, task["id"], "放行成功",
                  f"抽检通过后放行至{body.consumer}，水位同步推进", operator)
    return {"ok": True}

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute(
            "SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id"
            " ORDER BY r.id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

# ---------------- 抽检复核闸 ----------------

class DecisionIn(BaseModel):
    passed: bool
    note: str = ""
    policy_path: str | None = None

class UnfreezeIn(BaseModel):
    note: str = ""
    policy_path: str | None = None

@app.get("/api/sampling/manual")
def sampling_manual(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM sampling_manual WHERE id=1").fetchone()
    return {
        "policy_path": row["policy_path"], "version": row["version"],
        "locked_for_date": row["locked_for_date"], "pinned_at": row["pinned_at"],
        "body": row["body"], "locked": True,
    }

@app.get("/api/sampling/queue")
def sampling_queue(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""
            SELECT t.id AS task_id, t.status, t.round_no, t.inspector, t.decided_at,
                   t.detail, t.updated_at,
                   s.id AS slice_id, s.slice_no, s.biz_date, s.partition_key,
                   s.row_count, s.expected_count, s.completeness, s.release_status
            FROM sampling_tasks t JOIN slices s ON s.id = t.slice_id
            ORDER BY t.id""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/sampling/tasks/{tid}")
def sampling_detail(tid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        task = c.execute("""
            SELECT t.*, s.slice_no, s.biz_date, s.partition_key,
                   s.row_count, s.expected_count, s.completeness, s.release_status
            FROM sampling_tasks t JOIN slices s ON s.id=t.slice_id
            WHERE t.id=?""", (tid,)).fetchone()
        if not task:
            raise HTTPException(404, "抽检任务不存在")
        events = c.execute(
            "SELECT * FROM sampling_events WHERE task_id=? OR slice_id=? ORDER BY id",
            (tid, task["slice_id"])).fetchall()
    return {"task": dict(task), "events": [dict(e) for e in events]}

@app.get("/api/sampling/events")
def sampling_events(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""
            SELECT e.*, s.slice_no FROM sampling_events e
            LEFT JOIN slices s ON s.id=e.slice_id
            ORDER BY e.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.post("/api/sampling/tasks/{tid}/decision")
def sampling_decision(tid: int, body: DecisionIn,
                      authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    conn = connect()
    try:
        try:
            # 串行加锁：两名值班同时点下，只有一个能拿到写锁
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            conn.close()
            log_event_standalone(None, tid, "抽检争用",
                                 f"值班{operator}抢锁失败，本次裁决未写入（禁止默许成功）", operator)
            raise HTTPException(409, "抽检争用：已有值班正在裁决，本次写入被串行锁拒绝，禁止默许写成成功")
        task = conn.execute("SELECT * FROM sampling_tasks WHERE id=?", (tid,)).fetchone()
        if not task:
            conn.rollback()
            raise HTTPException(404, "抽检任务不存在")
        try:
            check_policy(body.policy_path, conn, task["slice_id"], tid, operator)
        except PolicyDeviation as e:
            conn.commit()  # 偏离台账在同一连接落库
            raise policy_409(e.path)
        if task["status"] != "待抽检":
            # 后到的写：显式争用/重复裁决，绝不默许写成成功
            log_event(conn, task["slice_id"], tid, "抽检争用",
                      f"任务状态={task['status']}，值班{operator}重复裁决被拒绝", operator)
            conn.commit()
            raise HTTPException(409, f"抽检争用：该切片已裁决（{task['status']}），禁止重复写入，须先解冻重裁")
        if body.passed:
            conn.execute(
                "UPDATE sampling_tasks SET status='抽检通过', inspector=?, decided_at=?,"
                " detail=?, updated_at=? WHERE id=?",
                (operator, now(), body.note or "抽检通过", now(), tid))
            log_event(conn, task["slice_id"], tid, "抽检通过",
                      body.note or "值班抽检通过，闸口放行", operator)
        else:
            # 乙路径：失败 → 复核冻结；切片绝不可能同时为已放行
            conn.execute(
                "UPDATE sampling_tasks SET status='复核冻结', inspector=?, decided_at=?,"
                " detail=?, updated_at=? WHERE id=?",
                (operator, now(), body.note or "抽检不通过，进入复核冻结", now(), tid))
            log_event(conn, task["slice_id"], tid, "抽检失败",
                      body.note or "抽检不通过，进入复核冻结：成功放行硬拒绝直至解冻重裁", operator)
        conn.commit()
        out = conn.execute("SELECT * FROM sampling_tasks WHERE id=?", (tid,)).fetchone()
        return dict(out)
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.post("/api/sampling/tasks/{tid}/unfreeze")
def sampling_unfreeze(tid: int, body: UnfreezeIn,
                      authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    conn = connect()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            conn.close()
            log_event_standalone(None, tid, "抽检争用",
                                 f"值班{operator}解冻抢锁失败，本次未写入", operator)
            raise HTTPException(409, "抽检争用：解冻操作被串行锁拒绝，请稍后重试")
        task = conn.execute("SELECT * FROM sampling_tasks WHERE id=?", (tid,)).fetchone()
        if not task:
            conn.rollback()
            raise HTTPException(404, "抽检任务不存在")
        try:
            check_policy(body.policy_path, conn, task["slice_id"], tid, operator)
        except PolicyDeviation as e:
            conn.commit()
            raise policy_409(e.path)
        if task["status"] != "复核冻结":
            log_event(conn, task["slice_id"], tid, "抽检争用",
                      f"非冻结状态（{task['status']}）申请解冻，拒绝", operator)
            conn.commit()
            raise HTTPException(409, f"仅复核冻结的任务可解冻，当前状态={task['status']}")
        conn.execute(
            "UPDATE sampling_tasks SET status='待抽检', inspector=NULL, decided_at=NULL,"
            " detail=?, round_no=round_no+1, updated_at=? WHERE id=?",
            (body.note or "解冻重裁，回到抽检队列", now(), tid))
        log_event(conn, task["slice_id"], tid, "解冻重裁",
                  body.note or "解冻重裁：冻结解除前所有成功放行已硬拒绝", operator)
        conn.commit()
        out = conn.execute("SELECT * FROM sampling_tasks WHERE id=?", (tid,)).fetchone()
        return dict(out)
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.get("/api/sampling/consistency")
def sampling_consistency(authorization: str | None = Header(default=None)):
    """同库核对：队列、列表旁注、复核详情、放行闸的不变量巡检。"""
    require_user(authorization)
    violations: list[dict[str, Any]] = []
    with connect() as c:
        rows = c.execute("""
            SELECT s.id, s.slice_no, s.completeness, s.release_status,
                   t.id AS task_id, t.status AS task_status
            FROM slices s LEFT JOIN sampling_tasks t ON t.slice_id=s.id""").fetchall()
        for r in rows:
            problems = []
            if r["task_id"] is None:
                problems.append("抽检任务缺失")
            else:
                if r["release_status"] == "已放行" and r["task_status"] != "抽检通过":
                    problems.append(f"已放行但抽检状态={r['task_status']}（失败与放行同时成立）")
                if r["completeness"] == "完整" and r["task_status"] != "抽检通过":
                    problems.append(f"标完整但抽检状态={r['task_status']}（跳过抽检）")
                if r["task_status"] == "复核冻结" and r["release_status"] == "已放行":
                    problems.append("复核冻结与已放行同时成立")
            for p in problems:
                violations.append({"slice_id": r["id"], "slice_no": r["slice_no"], "problem": p})
                dup = c.execute(
                    "SELECT 1 FROM sampling_events WHERE event_type='抽检口径偏了' AND detail=? LIMIT 1",
                    (p,)).fetchone()
                if not dup:
                    log_event(c, r["id"], r["task_id"], "抽检口径偏了", p, "system")
    return {"ok": not violations, "violations": violations,
            "policy_path": POLICY_PATH, "manual_version": MANUAL_VERSION}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

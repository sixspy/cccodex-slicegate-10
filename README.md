# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 抽检复核闸（钉死乙路径）

完整判定前必须过抽检复核闸，甲/乙两条互斥写法只钉乙路径，当天不许改口，原文写进库内「抽检说明册」（`sampling_manual` 单行钉版，无改写接口）：

- 登记即入抽检队列（`待抽检`）；未过闸禁止标「完整」、禁止放行
- 抽检不通过 → `复核冻结`：复核详情可看，任何成功放行硬拒绝直至「解冻重裁」；冻结期不写成功放行流水、不推进水位，只写抽检失败/拦截流水
- 禁止跳过抽检直接标完整已放行；禁止抽检失败与已放行同时成立
- 两名值班同时裁决按 `BEGIN IMMEDIATE` 串行加锁，负方显式 409「抽检争用」并记台账，禁止默许写成成功
- 偏离说明册（如混甲路径）一律 409 拦下并记「抽检口径偏了」
- 抽检队列、切片列表旁注、复核详情、放行闸同库核对；`GET /api/sampling/consistency` 巡检不变量

新增表（同库 `data/app.db`，不动原表与库文件）：`sampling_tasks` / `sampling_events` / `sampling_manual`；存量切片启动时幂等补登。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10620 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-10
docker compose up -d --build
`

浏览器：http://127.0.0.1:10620/  
健康检查：GET http://127.0.0.1:10620/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10620
python -m pytest -q
`

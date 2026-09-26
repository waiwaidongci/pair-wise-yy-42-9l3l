# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/segments`，登记火线片段（现场编号、起止界桩、火势状态、阵风等级、观测时刻）
- `GET /api/items/{id}/segments`，列出归并后的片段与待核冲突
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 火线片段归并

- 同一`field_ref`（现场编号）重放登记时返回第一次的结果，不重复建段。
- 同一事件内首尾相接（界桩号相邻或重叠）的片段自动合成一段，火势与阵风取观测时刻最新的一次。
- 片段与别的未关闭事件的火线重叠时退回（409），说明冲突事件并登记为待核冲突。
- 存在燃烧中（burning/smoldering）片段的事件不能关闭；用新的现场编号补报`contained`等状态后放行。
- 事件有片段后，响应时限按未控制长度与最大阵风等级重算，列表中可见`merged`合并段与`pending`待核冲突。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

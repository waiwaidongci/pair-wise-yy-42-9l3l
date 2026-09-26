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
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/fire-segments`：登记火线片段
- `GET /api/items/{id}/fire-segments`：查看该事件的归并结果
- `PATCH /api/items/{id}/fire-segments/{seg_id}`：更新火势/阵风/观测时刻
- `GET /api/firelines`：所有事件的火线归并概览
- `GET /api/segment-conflicts`：待核冲突（可用`item_id`、`status`过滤）
- `PATCH /api/segment-conflicts/{id}`：核销待核冲突（incident_commander）
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

### 火线片段归并规则

- 登记字段：`site_code`（现场编号，幂等键）、`start_marker`/`end_marker`（起止界桩，整数）、`fire_status`（burning/controlled）、`gust_level`（阵风0-12）、`observed_at`（ISO-8601观测时刻）。
- **幂等重放**：同一事件、同一现场编号重复上报，无论内容是否变化，都返回第一次登记结果（`replayed=true`），不重复落库。
- **首尾相接合并**：同事件内界桩相邻或重叠的片段在视图中归并成一段，`merged=true`并列出组成编号；阵风取最大、观测时刻取最新；任一段在燃烧则合并段视为燃烧。
- **跨事件冲突**：现场编号已被别的未关闭火线占用，或界桩范围与别的未关闭火线重叠，登记被退回（409），响应体`details`说明冲突事件、原因和双方界桩范围，并自动登记一条"待核冲突"（双方事件的列表都可见，重复上报不会重复建单）。归属事件关闭后编号与范围可被新事件使用。
- **关闭闸门**：事件下存在`burning`片段时，`closed`转换被拒绝；将片段更新为controlled后才能关闭。
- **时限重算**：有片段时按未控制长度（燃烧中合并段长度之和）和最大阵风重算`fireline_deadline_hours`（1-72小时，越短越紧），并覆盖事件的`deadline_hours`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

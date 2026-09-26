# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。

## 溯源链与快照

- 校准执行（`perform`）时必须登记所用标准器编号`standard_id`；在`instrument`上标记`is_reference: true`的基准仪器除外，它是溯源链顶端。
- 审批（`approve`）时沿标准器的校验链逐级向上检查：日期先后（标准器校准日期不得晚于使用日期）、有效期（使用日期不得超过标准器校准到期日）、断链（标准器不存在或无已批准校准）和成环（链上重复出现同一标准器）。
- 检查失败时审批被退回：记录保持`passed`状态，错误响应和审计（`approve_returned`）中列出问题节点，修正后可重新`perform`再审批。
- 审批通过后，完整链路作为`traceability_snapshot`存入校准记录，仪器同步置为`active`并更新`due_at`（隔离中的仪器只更新`due_at`）。快照是批准时刻的副本，标准器日后再校准不会改写旧快照。
- 结果放行（`release`）时，仪器最近已批准校准的快照作为`traceability_chain`写入结果，返回采用的完整链路。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。

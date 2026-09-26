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

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果；`standard`：计量标准器。

## 计量溯源

- 标准器登记：`standard` 记录 `code`（编号唯一）、`calibrated_at`、`due_at`，可选 `higher_standard_code` 指向上一级标准器，逐级构成溯源链，无上级者视为链顶（参考标准）。`recalibrate` 动作登记再校准，新校准日期不得早于上次。
- 校准登记：`calibration` 创建时必须填写 `standard_code`（所用标准器编号）。
- 审批核验：`approve` 时沿标准器校验链逐级检查——日期先后（标准器校准日期不得晚于其使用日期，上级标准以下级的校准日期为使用日期）、有效期（使用当日不得过期）、断链（引用的标准器未登记）、成环（链回环）。任一不通过即退回，错误响应的 `details` 列出全部问题节点（`expired` / `date_order` / `broken_chain` / `cycle`），记录保持 `passed` 可整改后重新 `perform` 再审。
- 溯源快照：审批通过在同一事务内保存本次溯源快照（`traceability_snapshots` 表只插不改）、更新校准记录并让仪器生效（写回 `due_at` 与 `current_calibration_id`，`calibrating` 恢复 `active`）。标准器日后再校准不会改写历史快照。
- 结果放行：`release` 要求仪器存在已批准的溯源校准，放行结果携带该次校准快照中的完整链路（`traceability_chain`）与 `calibration_id`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/snapshots/<calibration_id>`：读取某次校准的溯源快照。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。

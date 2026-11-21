# 数据质量监控与元数据治理平台

面向数据质量标准定义、自动巡检、异常告警、元数据采集与数据目录构建的数据质量治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 质量规则定义 | `dq_rule_def` | 质量规则 | 规则编号、规则名称、目标表名 |
| 巡检任务管理 | `dq_check_run` | 巡检任务 | 任务编号、关联规则集、数据范围 |
| 质量事件管理 | `dq_incident` | 质量事件 | 事件编号、关联规则、触发时间 |
| 元数据采集器 | `metadata_crawler` | 采集器配置 | 采集器编号、数据源类型、连接地址 |
| 数据表目录 | `table_catalog` | 表目录 | 表编号、表名称、所属库名 |
| 字段画像分析 | `column_profile` | 字段画像 | 画像编号、关联表名、字段名称 |
| 血缘图谱 | `lineage_graph` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 指标波动监控 | `metric_monitor` | 监控指标 | 指标编号、目标表名、监控字段 |
| 质量评分卡 | `dq_scorecard` | 评分记录 | 记录编号、目标表名、完整性得分 |
| 数据分类标签 | `tag_classify` | 分类标签 | 标签编号、标签名称、分类维度 |
| 规则模板库 | `dq_template` | 规则模板 | 模板编号、模板名称、规则类型 |
| 异常模式识别 | `anomaly_pattern` | 异常模式 | 模式编号、模式名称、检测方法 |
| 质量报告生成 | `dq_report` | 质量报告 | 报告编号、报告周期、覆盖表数 |
| 元数据变更对比 | `metadata_diff` | 变更记录 | 变更编号、变更对象、变更类型 |
| 告警通知通道 | `dq_alert_channel` | 告警配置 | 配置编号、关联规则集、通知方式 |
| 质量基线管理 | `dq_baseline` | 基线定义 | 基线编号、目标表名、基线指标 |
| 主数据实体注册 | `mdm_entity` | 主数据实体 | 实体编号、实体名称、业务域 |
| 质量趋势看板 | `dq_trend` | 趋势记录 | 记录编号、目标表名、日期 |

## 评分降分依据与日报复查

评分卡记录本身只保存得分，无法说明“这张表为什么降分”。`dq_scorecard` 额外维护
规则贡献账本（每个评分版本逐条记录权重、结果、规则得分、加权得分、降分贡献），
并据此生成降分依据视图；日报从该视图读取规则权重与得分生成复查项，不再二次猜测。

页面入口：`/scorecard-deduction`（侧栏“评分降分依据”），演示场景为
`dws_daily_summary` 的 `DQ_S-1001 / v1`：两条降分规则已确认，第三条（`DQ_R-1005`）
更正结果尚未到达。

关键约束与对应实现：

| 约束 | 实现 |
| --- | --- |
| 同一版本总分按规则权重和结果复算 | `composite_of` = Σ(权重 × 规则得分)，`recompute_version` 校验差异 < 0.05 |
| 并发修订只接纳同版本贡献 | `accept_correction` 校验 `version` 与 `expected_revision`，版本不符直接拒绝 |
| 计算中断可恢复 | `start_calculation(stop_after=…)` 写检查点，`resume_calculation` 从检查点续算 |
| 旧报告引用的版本可重放 | 修订前归档快照，`replay_version(..., revision=)` 按快照复算并核对摘要 |
| 未齐项单独标明 | 视图返回 `未齐项`，复核项 `未齐=true`，派工与版本确认都会被拦截 |
| 版本确认回写复核清单 | `confirm_version` 回写 `dq_deduction_review` 的确认结果与状态 |
| 日报按降分依据生成复查项 | `generate_daily_report` 冻结版本/修订/分项/综合分，逐规则建复查项 |
| 更正先于派工到达 | `dispatch_review_items` 拒绝过期修订，需先 `rebase_report` 重挂再派工 |
| 补证据后重开并核对 | `add_evidence` + `reopen_review_items` + `verify_dispatch_basis` 核对分项与综合分 |

相关接口：

```text
GET  /api/scorecard-scenario
GET  /api/scorecard/{id}/contribution?version=
GET  /api/scorecard/{id}/replay?version=&revision=
GET  /api/scorecard/{id}/verify?version=&expected=
POST /api/scorecard/{id}/calculation/start | /resume
POST /api/scorecard/{id}/corrections | /confirm
POST /api/evidence            GET /api/evidence
POST /api/daily-report        POST /api/daily-report/{no}/dispatch | /rebase | /reopen
GET  /api/daily-report/{no}/verify
```

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖

# sre-cross-region-disaster-exercise-planner

跨地域灾备演练**阶段规划工具**（Python 3.8.2 兼容，仅使用标准库）。
输入服务依赖、RPO/RTO、观测证据、切换动作、回滚窗口、错误预算和
witness 状态，生成带影响面、验证条件和执行顺序的阶段计划，并可在
离线环境中模拟演练执行。

## 安全边界

- 本工具**只生成计划与模拟报告，不连接、不调用、不执行任何生产切换**，
  没有任何网络、DNS、数据库或消息系统副作用。
- 模拟执行所需的观测证据（`observations`）和动作结果
  （`action_results`）全部由调用方在输入中预置，缺省动作视为成功。
- 真正的生产演练必须由人工审批后，交给具备审计与权限控制的变更平台执行。

## 核心规则

- 没有停止条件或没有证据采集要求的切换动作一律不可执行，
  在计划生成阶段直接抛出 `InvalidActionError`。
- 服务依赖必须构成 DAG；存在环或未知依赖时拒绝出计划。
- witness 未就绪（非 `ready` / `failover_granted`）时禁止生成可执行计划，
  模拟执行前再次校验。
- 错误预算按阶段累计校验，总预算或单阶段预算不足时拒绝出计划。
- 每个阶段校验其依赖链回滚总耗时，超过服务的回滚窗口即拒绝出计划。
- 某阶段失败时**只回滚其依赖链**：当前阶段已完成的动作按逆序回滚，
  再回滚祖先依赖链；无依赖关系的独立服务保持已完成，影响不被扩大。
- 证据门禁覆盖证据缺失、证据过期和健康区间越界（复制延迟越界按
  RPO 违规处理）；阶段累计耗时超过 RTO 立即中止。

## 输入与输出

输入通过 `DrillDefinition` 提供：

- `services`：服务列表，含依赖、`RPOTarget`、`SwitchAction` 与回滚窗口；
- `error_budget`：本次演练可用错误预算；
- `witness`：`WitnessState` 仲裁状态；
- `observations`：离线模拟使用的观测证据；
- `action_results`：离线模拟使用的动作结果（`True` 或失败原因）。

`build_drill_plan()` 返回 `DrillPlan`，每个 `Phase` 包含：

- 执行顺序（拓扑序，被依赖方先切换）；
- `PhaseImpact`：影响用户数、爆炸半径服务列表、预计停机时间、预算消耗；
- `validation_conditions`：RPO/RTO、证据门禁、停止条件与回滚窗口要求；
- `rollback_chain`：该阶段失败时的完整依赖链回滚动作。

`simulate()` 返回 `ExecutionReport`，给出每阶段状态
（`completed` / `failed` / `rolled_back` / `skipped`）、失败原因、
实际回滚链以及被跳过和保持完成的服务。

## 快速示例

```python
from drill_planner import (
    DrillDefinition, Service, SwitchAction, EvidenceRequirement,
    StopCondition, RPOTarget, WitnessState, Observation,
    build_drill_plan, simulate,
)

action = SwitchAction(
    action_id="db_failover",
    name="数据库主备切换",
    action_type="db_switch",
    estimated_seconds=15,
    budget_cost=1.0,
    evidence_requirements=[
        EvidenceRequirement("replication_lag_seconds", 30, 0, 30),
    ],
    stop_conditions=[StopCondition("error_rate", max_value=0.01)],
    rollback_seconds=8,
)
service = Service(
    service_id="db", name="主数据库",
    rpo=RPOTarget(max_replication_lag_seconds=30, rto_seconds=120),
    actions=[action], rollback_window_seconds=300,
)
drill = DrillDefinition(
    name="region-b 演练",
    services=[service],
    error_budget=10,
    witness=WitnessState.READY,
    observations={
        "replication_lag_seconds": Observation(
            "replication_lag_seconds", 4, 12
        ),
        "error_rate": Observation("error_rate", 0.002, 4),
    },
)

plan = build_drill_plan(drill)      # 静态阶段计划，无任何生产副作用
report = simulate(drill, plan)      # 离线模拟执行
print(plan.to_dict())
print(report.to_dict())
```

## 测试

测试覆盖依赖失败、预算不足、部分切换、证据缺失/过期/RPO 违规、
停止条件触发、RTO 超时、witness 未就绪、依赖环和回滚窗口超限：

```bash
python -m unittest
```

当前共 22 个用例，全部通过。代码仅依赖 Python 标准库，
语法与类型用法兼容 Python 3.8.2（`dataclasses`、`typing`、`enum`）。

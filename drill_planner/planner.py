"""阶段计划生成器。

职责：
1. 校验服务依赖 DAG、动作可执行前提、witness、错误预算与回滚窗口；
2. 按依赖拓扑为每个服务生成一个切换阶段并给出执行顺序；
3. 输出每阶段的影响面、验证条件与完整回滚链。

本模块只做静态规划，不执行任何真实切换动作。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .errors import (
    BudgetExhaustedError,
    DependencyCycleError,
    DependencyNotFoundError,
    InvalidActionError,
    RollbackWindowExceededError,
    WitnessNotReadyError,
)
from .models import DrillDefinition, Service, WitnessState


@dataclass
class PhaseImpact:
    """单个阶段的预期影响面。"""

    service_id: str
    service_name: str
    affected_users: int
    blast_radius_services: List[str]
    estimated_downtime_seconds: float
    budget_cost: float

    def to_dict(self) -> dict:
        return {
            "service_id": self.service_id,
            "service_name": self.service_name,
            "affected_users": self.affected_users,
            "blast_radius_services": list(self.blast_radius_services),
            "estimated_downtime_seconds": self.estimated_downtime_seconds,
            "budget_cost": self.budget_cost,
        }


@dataclass
class RollbackStep:
    """回滚链中的单个回滚动作。"""

    rollback_id: str
    service_id: str
    action_id: str
    estimated_seconds: float

    def to_dict(self) -> dict:
        return {
            "rollback_id": self.rollback_id,
            "service_id": self.service_id,
            "action_id": self.action_id,
            "estimated_seconds": self.estimated_seconds,
        }


@dataclass
class Phase:
    """一个服务对应的切换阶段。"""

    order: int
    service_id: str
    service_name: str
    action_ids: List[str]
    impact: PhaseImpact
    validation_conditions: List[str]
    rollback_chain: List[RollbackStep]
    rollback_window_seconds: float
    rpo_max_lag_seconds: Optional[float]
    rto_seconds: Optional[float]

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "service_id": self.service_id,
            "service_name": self.service_name,
            "action_ids": list(self.action_ids),
            "impact": self.impact.to_dict(),
            "validation_conditions": list(self.validation_conditions),
            "rollback_chain": [step.to_dict() for step in self.rollback_chain],
            "rollback_window_seconds": self.rollback_window_seconds,
            "rpo_max_lag_seconds": self.rpo_max_lag_seconds,
            "rto_seconds": self.rto_seconds,
        }


@dataclass
class DrillPlan:
    """完整演练阶段计划。"""

    name: str
    phases: List[Phase]
    error_budget: float
    total_budget_cost: float
    witness: WitnessState
    service_index: Dict[str, Service] = field(default_factory=dict)
    phase_index: Dict[str, Phase] = field(default_factory=dict)

    @property
    def execution_order(self) -> List[str]:
        return [phase.service_id for phase in self.phases]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "error_budget": self.error_budget,
            "total_budget_cost": self.total_budget_cost,
            "witness": self.witness.value,
            "execution_order": self.execution_order,
            "phases": [phase.to_dict() for phase in self.phases],
        }


def _validate_dependencies(services: List[Service]) -> None:
    known = {service.service_id for service in services}
    for service in services:
        unknown = [dep for dep in service.dependencies if dep not in known]
        if unknown:
            raise DependencyNotFoundError(
                "服务 %s 依赖了不存在的服务: %s"
                % (service.service_id, ", ".join(unknown))
            )


def topo_order(services: List[Service]) -> List[Service]:
    """Kahn 拓扑排序；同层按输入顺序，保证结果确定。

    被依赖方排在依赖方前面，因此上游服务先切换。
    """
    by_id = {service.service_id: service for service in services}
    indegree = {
        service.service_id: len(set(service.dependencies)) for service in services
    }
    dependents: Dict[str, List[str]] = {service.service_id: [] for service in services}
    for service in services:
        for dep in set(service.dependencies):
            dependents[dep].append(service.service_id)

    ready = sorted(
        service.service_id
        for service in services
        if indegree[service.service_id] == 0
    )
    ordered: List[Service] = []
    while ready:
        current_id = ready.pop(0)
        ordered.append(by_id[current_id])
        for dependent_id in dependents[current_id]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
        ready.sort()

    if len(ordered) != len(services):
        remaining = [service_id for service_id, degree in indegree.items() if degree > 0]
        raise DependencyCycleError(
            "服务依赖存在环，无法排序，涉及: %s" % ", ".join(sorted(remaining))
        )
    return ordered


def _ancestors(service_id: str, by_id: Dict[str, Service]) -> List[str]:
    """返回所有直接或间接依赖的祖先服务（含自身），保持拓扑顺序。"""
    result: List[str] = []
    visited: Set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        for parent_id in by_id[node_id].dependencies:
            walk(parent_id)
        result.append(node_id)

    walk(service_id)
    return result


def _descendants(service_id: str, by_id: Dict[str, Service]) -> List[str]:
    """返回所有直接或间接依赖当前服务的后代服务（含自身）。"""
    result: List[str] = []
    visited: Set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        result.append(node_id)
        for service in by_id.values():
            if node_id in service.dependencies:
                walk(service.service_id)

    walk(service_id)
    return result


def _service_budget_cost(service: Service) -> float:
    return sum(action.budget_cost for action in service.actions)


def _build_rollback_chain(
    service: Service, by_id: Dict[str, Service]
) -> List[RollbackStep]:
    """生成依赖链回滚步骤。

    回滚顺序与切换相反：先回滚当前服务（动作逆序），
    再沿祖先依赖链自底向上回滚，每一步只回滚其依赖链上的服务。
    """
    chain_ids = _ancestors(service.service_id, by_id)
    steps: List[RollbackStep] = []
    for node_id in reversed(chain_ids):
        node = by_id[node_id]
        for action in reversed(node.actions):
            steps.append(
                RollbackStep(
                    rollback_id=action.rollback_id,
                    service_id=node_id,
                    action_id=action.action_id,
                    estimated_seconds=action.rollback_seconds,
                )
            )
    return steps


def _build_validation_conditions(
    service: Service,
    rollback_chain: List[RollbackStep],
) -> List[str]:
    conditions: List[str] = []
    if service.rpo is not None:
        conditions.append(
            "复制延迟 <= %ss（RPO 上限）"
            % service.rpo.max_replication_lag_seconds
        )
        conditions.append(
            "阶段累计耗时 <= %ss（RTO 上限）" % service.rpo.rto_seconds
        )
    else:
        conditions.append("未配置 RPO/RTO，按动作级证据门禁验证")

    for action in service.actions:
        for requirement in action.evidence_requirements:
            label = requirement.description or requirement.metric
            conditions.append(
                "动作 %s 前采集 %s，年龄 <= %ss 且值落在 [%s, %s]"
                % (
                    action.action_id,
                    label,
                    requirement.max_age_seconds,
                    requirement.healthy_min,
                    requirement.healthy_max,
                )
            )
        for stop in action.stop_conditions:
            label = stop.description or stop.metric
            bounds = []
            if stop.max_value is not None:
                bounds.append("<= %s" % stop.max_value)
            if stop.min_value is not None:
                bounds.append(">= %s" % stop.min_value)
            conditions.append(
                "动作 %s 期间持续监控 %s（%s），越界立即停止"
                % (action.action_id, label, " 且 ".join(bounds))
            )

    total_rollback = sum(step.estimated_seconds for step in rollback_chain)
    conditions.append(
        "失败时仅回滚 %s 的依赖链，共 %d 个回滚动作，预计 %ss（回滚窗口 %ss）"
        % (
            service.service_id,
            len(rollback_chain),
            total_rollback,
            service.rollback_window_seconds,
        )
    )
    return conditions


def build_drill_plan(drill: DrillDefinition) -> DrillPlan:
    """根据演练输入生成静态阶段计划。"""
    if not drill.services:
        raise DependencyNotFoundError("演练至少需要一个服务")
    if not drill.witness.allows_switch:
        raise WitnessNotReadyError(
            "witness 状态为 %s，未授予切换许可，禁止生成可执行计划"
            % drill.witness.value
        )

    _validate_dependencies(drill.services)
    ordered = topo_order(drill.services)
    by_id = {service.service_id: service for service in drill.services}

    for service in drill.services:
        if not service.actions:
            raise InvalidActionError(
                "服务 %s 没有任何切换动作，无法构成演练阶段" % service.service_id
            )
        for action in service.actions:
            action.validate()

    total_cost = sum(_service_budget_cost(service) for service in drill.services)
    if total_cost > drill.error_budget:
        raise BudgetExhaustedError(
            "演练总预算消耗 %s 超过错误预算 %s"
            % (total_cost, drill.error_budget)
        )

    phases: List[Phase] = []
    spent = 0.0
    for order, service in enumerate(ordered, start=1):
        phase_cost = _service_budget_cost(service)
        if spent + phase_cost > drill.error_budget:
            raise BudgetExhaustedError(
                "阶段 %s（%s）预算不足：已消耗 %s + 本阶段 %s > 预算 %s"
                % (
                    order,
                    service.service_id,
                    spent,
                    phase_cost,
                    drill.error_budget,
                )
            )
        spent += phase_cost

        rollback_chain = _build_rollback_chain(service, by_id)
        rollback_total = sum(step.estimated_seconds for step in rollback_chain)
        if (
            service.rollback_window_seconds > 0
            and rollback_total > service.rollback_window_seconds
        ):
            raise RollbackWindowExceededError(
                "服务 %s 依赖链回滚需 %ss，超过回滚窗口 %ss"
                % (
                    service.service_id,
                    rollback_total,
                    service.rollback_window_seconds,
                )
            )

        blast_radius = _descendants(service.service_id, by_id)
        downtime = sum(action.estimated_seconds for action in service.actions)
        impact = PhaseImpact(
            service_id=service.service_id,
            service_name=service.name,
            affected_users=service.affected_users,
            blast_radius_services=blast_radius,
            estimated_downtime_seconds=downtime,
            budget_cost=phase_cost,
        )
        phases.append(
            Phase(
                order=order,
                service_id=service.service_id,
                service_name=service.name,
                action_ids=[action.action_id for action in service.actions],
                impact=impact,
                validation_conditions=_build_validation_conditions(
                    service, rollback_chain
                ),
                rollback_chain=rollback_chain,
                rollback_window_seconds=service.rollback_window_seconds,
                rpo_max_lag_seconds=(
                    service.rpo.max_replication_lag_seconds if service.rpo else None
                ),
                rto_seconds=service.rpo.rto_seconds if service.rpo else None,
            )
        )

    plan = DrillPlan(
        name=drill.name,
        phases=phases,
        error_budget=drill.error_budget,
        total_budget_cost=total_cost,
        witness=drill.witness,
        service_index=by_id,
        phase_index={phase.service_id: phase for phase in phases},
    )
    return plan


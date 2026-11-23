"""离线模拟执行器。

该模块不会对生产环境发起任何调用：观测证据来自 ``DrillDefinition``
中预置的 observations，动作结果来自 action_results，缺省视为成功。
它用于在演练前验证计划行为，包括依赖失败、预算不足与部分切换场景。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import (
    ActionExecutionError,
    EvidenceMissingError,
    EvidenceStaleError,
    EvidenceUnhealthyError,
    RPOViolationError,
    RTOExceededError,
    StopConditionBreachError,
    WitnessNotReadyError,
)
from .models import (
    DrillDefinition,
    EvidenceRequirement,
    Observation,
    Service,
    SwitchAction,
)
from .planner import DrillPlan, RollbackStep

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_SKIPPED = "skipped"


@dataclass
class PhaseReport:
    """单个阶段的模拟执行报告。"""

    order: int
    service_id: str
    status: str
    executed_action_ids: List[str] = field(default_factory=list)
    failure_reason: Optional[str] = None
    rollback_chain: List[RollbackStep] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "service_id": self.service_id,
            "status": self.status,
            "executed_action_ids": list(self.executed_action_ids),
            "failure_reason": self.failure_reason,
            "rollback_chain": [step.to_dict() for step in self.rollback_chain],
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass
class ExecutionReport:
    """整次演练的模拟执行报告。"""

    name: str
    success: bool
    phase_reports: List[PhaseReport]
    completed_services: List[str]
    rolled_back_services: List[str]
    skipped_services: List[str]
    failure_service: Optional[str]
    failure_reason: Optional[str]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "success": self.success,
            "failure_service": self.failure_service,
            "failure_reason": self.failure_reason,
            "completed_services": list(self.completed_services),
            "rolled_back_services": list(self.rolled_back_services),
            "skipped_services": list(self.skipped_services),
            "phase_reports": [report.to_dict() for report in self.phase_reports],
        }


def _check_evidence(
    action: SwitchAction,
    observations: Dict[str, Observation],
) -> None:
    """证据门禁：每个证据要求都必须存在、新鲜且健康。"""
    for requirement in action.evidence_requirements:
        observation = observations.get(requirement.metric)
        if observation is None:
            raise EvidenceMissingError(
                "动作 %s 缺少观测证据 %s"
                % (action.action_id, requirement.metric)
            )
        if observation.age_seconds > requirement.max_age_seconds:
            raise EvidenceStaleError(
                "动作 %s 的证据 %s 年龄 %ss 超过上限 %ss"
                % (
                    action.action_id,
                    requirement.metric,
                    observation.age_seconds,
                    requirement.max_age_seconds,
                )
            )
        if not (
            requirement.healthy_min <= observation.value <= requirement.healthy_max
        ):
            raise _unhealthy_error(action, requirement, observation.value)


def _unhealthy_error(
    action: SwitchAction,
    requirement: EvidenceRequirement,
    value: float,
) -> EvidenceUnhealthyError:
    if requirement.metric == "replication_lag_seconds":
        return RPOViolationError(
            "动作 %s 复制延迟 %s 超出 RPO 健康区间 [%s, %s]"
            % (
                action.action_id,
                value,
                requirement.healthy_min,
                requirement.healthy_max,
            )
        )
    return EvidenceUnhealthyError(
        "动作 %s 的证据 %s 值 %s 不在健康区间 [%s, %s]"
        % (
            action.action_id,
            requirement.metric,
            value,
            requirement.healthy_min,
            requirement.healthy_max,
        )
    )


def _check_stop_conditions(
    action: SwitchAction,
    observations: Dict[str, Observation],
) -> None:
    for stop in action.stop_conditions:
        observation = observations.get(stop.metric)
        if observation is None:
            raise StopConditionBreachError(
                "动作 %s 执行期间缺少停止条件监控指标 %s，按触发处理"
                % (action.action_id, stop.metric)
            )
        if stop.breached_by(observation.value):
            raise StopConditionBreachError(
                "动作 %s 停止条件 %s 被突破，当前值 %s"
                % (action.action_id, stop.metric, observation.value)
            )


def _runtime_rollback_chain(
    plan: DrillPlan,
    failed_service_id: str,
    executed_action_ids: List[str],
) -> List[RollbackStep]:
    """失败时只回滚依赖链，且只包含本次已经完成的动作。

    当前阶段已完成的动作按逆序回滚，再回滚其祖先依赖链；
    与失败服务无依赖关系的独立服务保持已完成状态，不在回滚范围内。
    """
    executed = set(executed_action_ids)
    for completed_phase in plan.phases:
        if completed_phase.service_id == failed_service_id:
            break
        executed.update(completed_phase.action_ids)
    full_chain = plan.phase_index[failed_service_id].rollback_chain
    return [step for step in full_chain if step.action_id in executed]


def simulate(drill: DrillDefinition, plan: DrillPlan) -> ExecutionReport:
    """按计划离线模拟执行并产出报告。"""
    if not drill.witness.allows_switch:
        raise WitnessNotReadyError(
            "witness 状态为 %s，拒绝开始模拟切换" % drill.witness.value
        )

    action_map: Dict[str, SwitchAction] = {}
    for service in drill.services:
        for action in service.actions:
            action_map[action.action_id] = action

    observations = drill.observations
    reports: List[PhaseReport] = []
    completed_services: List[str] = []
    aborted = False
    failure_service: Optional[str] = None
    failure_reason: Optional[str] = None

    for phase in plan.phases:
        if aborted:
            reports.append(
                PhaseReport(
                    order=phase.order,
                    service_id=phase.service_id,
                    status=STATUS_SKIPPED,
                )
            )
            continue

        service = plan.service_index[phase.service_id]
        executed: List[str] = []
        elapsed = 0.0
        try:
            for action in service.actions:
                _check_evidence(action, observations)
                _check_stop_conditions(action, observations)

                result = drill.action_results.get(action.action_id, True)
                if result is not True:
                    raise ActionExecutionError(
                        "动作 %s 执行失败: %s"
                        % (action.action_id, result)
                    )

                elapsed += action.estimated_seconds
                executed.append(action.action_id)

                if service.rpo is not None and elapsed > service.rpo.rto_seconds:
                    raise RTOExceededError(
                        "服务 %s 阶段耗时 %ss 超过 RTO %ss"
                        % (
                            service.service_id,
                            elapsed,
                            service.rpo.rto_seconds,
                        )
                    )
        except Exception as error:  # noqa: BLE001 - 模拟器需统一收口失败
            failure_service = service.service_id
            failure_reason = str(error)
            chain = _runtime_rollback_chain(plan, service.service_id, executed)
            rolled_services = list(
                dict.fromkeys(step.service_id for step in chain)
            )
            for rolled_id in rolled_services:
                if rolled_id in completed_services:
                    completed_services.remove(rolled_id)
            reports.append(
                PhaseReport(
                    order=phase.order,
                    service_id=service.service_id,
                    status=STATUS_FAILED,
                    executed_action_ids=executed,
                    failure_reason=failure_reason,
                    rollback_chain=chain,
                    elapsed_seconds=elapsed,
                )
            )
            ancestor_set = set(rolled_services) - {service.service_id}
            for prior in reports[:-1]:
                if prior.service_id in ancestor_set:
                    prior.status = STATUS_ROLLED_BACK
            aborted = True
            continue

        completed_services.append(service.service_id)
        reports.append(
            PhaseReport(
                order=phase.order,
                service_id=service.service_id,
                status=STATUS_COMPLETED,
                executed_action_ids=executed,
                elapsed_seconds=elapsed,
            )
        )

    rolled_back_services = [
        report.service_id
        for report in reports
        if report.status == STATUS_ROLLED_BACK
    ]
    skipped_services = [
        report.service_id
        for report in reports
        if report.status == STATUS_SKIPPED
    ]
    order_rank = {phase.service_id: phase.order for phase in plan.phases}
    completed_services.sort(key=lambda service_id: order_rank[service_id])
    return ExecutionReport(
        name=drill.name,
        success=not aborted,
        phase_reports=reports,
        completed_services=completed_services,
        rolled_back_services=rolled_back_services,
        skipped_services=skipped_services,
        failure_service=failure_service,
        failure_reason=failure_reason,
    )

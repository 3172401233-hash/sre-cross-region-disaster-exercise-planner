"""跨地域灾备演练阶段规划工具。

本包只负责生成阶段计划、验证条件与离线模拟执行报告，
不会连接或操作任何生产环境。
"""
from .errors import (
    ActionExecutionError,
    BudgetExhaustedError,
    DependencyCycleError,
    DependencyNotFoundError,
    DrillPlannerError,
    EvidenceGateError,
    EvidenceMissingError,
    EvidenceStaleError,
    EvidenceUnhealthyError,
    InvalidActionError,
    RollbackWindowExceededError,
    RPOViolationError,
    RTOExceededError,
    StopConditionBreachError,
    WitnessNotReadyError,
)
from .executor import ExecutionReport, PhaseReport, simulate
from .models import (
    DrillDefinition,
    EvidenceRequirement,
    Observation,
    RPOTarget,
    Service,
    StopCondition,
    SwitchAction,
    WitnessState,
)
from .planner import DrillPlan, Phase, PhaseImpact, build_drill_plan

__all__ = [
    "ActionExecutionError",
    "BudgetExhaustedError",
    "DependencyCycleError",
    "DependencyNotFoundError",
    "DrillDefinition",
    "DrillPlannerError",
    "DrillPlan",
    "EvidenceGateError",
    "EvidenceMissingError",
    "EvidenceRequirement",
    "EvidenceStaleError",
    "EvidenceUnhealthyError",
    "ExecutionReport",
    "InvalidActionError",
    "Observation",
    "Phase",
    "PhaseImpact",
    "PhaseReport",
    "RPOTarget",
    "RollbackWindowExceededError",
    "RPOViolationError",
    "RTOExceededError",
    "Service",
    "StopCondition",
    "StopConditionBreachError",
    "SwitchAction",
    "WitnessNotReadyError",
    "WitnessState",
    "build_drill_plan",
    "simulate",
]

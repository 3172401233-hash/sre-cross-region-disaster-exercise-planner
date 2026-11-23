"""规划与模拟执行过程中使用的异常类型。"""


class DrillPlannerError(Exception):
    """所有规划器异常的基类。"""


class PlanValidationError(DrillPlannerError):
    """输入配置不满足可执行前提（计划生成阶段）。"""


class InvalidActionError(PlanValidationError):
    """切换动作缺少停止条件或证据采集要求。"""


class DependencyNotFoundError(PlanValidationError):
    """服务依赖指向了不存在的服务。"""


class DependencyCycleError(PlanValidationError):
    """服务依赖图中存在环，无法生成阶段顺序。"""


class BudgetExhaustedError(PlanValidationError):
    """错误预算不足以覆盖整个演练或某个阶段。"""


class RollbackWindowExceededError(PlanValidationError):
    """依赖链回滚耗时超过允许的回滚窗口。"""


class ExecutionError(DrillPlannerError):
    """模拟执行阶段出现的失败基类。"""


class WitnessNotReadyError(ExecutionError):
    """witness 未就绪，禁止开始切换。"""


class EvidenceGateError(ExecutionError):
    """观测证据未通过门禁。"""


class EvidenceMissingError(EvidenceGateError):
    """缺少所需观测证据。"""


class EvidenceStaleError(EvidenceGateError):
    """观测证据已超过新鲜度上限。"""


class EvidenceUnhealthyError(EvidenceGateError):
    """观测指标未达到健康阈值。"""


class RPOViolationError(EvidenceUnhealthyError):
    """复制延迟超过 RPO 上限。"""


class StopConditionBreachError(ExecutionError):
    """动作的停止条件被触发，必须立即中止。"""


class ActionExecutionError(ExecutionError):
    """切换动作本身返回失败结果。"""


class RTOExceededError(ExecutionError):
    """阶段累计耗时超过 RTO 上限。"""

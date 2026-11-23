"""灾备演练的输入数据模型。

所有模型均为纯数据结构，不包含任何对生产系统的调用，
便于在离线环境中构造演练输入并做单元测试。
兼容 Python 3.8（使用 dataclasses 与 typing）。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .errors import InvalidActionError


class WitnessState(str, Enum):
    """跨地域仲裁 witness 的状态。"""

    NOT_READY = "not_ready"
    CATCHING_UP = "catching_up"
    READY = "ready"
    FAILOVER_GRANTED = "failover_granted"

    @property
    def allows_switch(self) -> bool:
        return self in (WitnessState.READY, WitnessState.FAILOVER_GRANTED)


@dataclass
class EvidenceRequirement:
    """单个动作执行前必须满足的观测证据要求。

    - metric: 观测指标名，如 ``replication_lag_seconds``
    - max_age_seconds: 证据允许的最大采集年龄（新鲜度）
    - healthy_min / healthy_max: 指标健康区间，闭区间
    """

    metric: str
    max_age_seconds: float
    healthy_min: float
    healthy_max: float
    description: str = ""


@dataclass
class Observation:
    """一次实际采集到的观测证据。"""

    metric: str
    value: float
    age_seconds: float


@dataclass
class RPOTarget:
    """服务的 RPO/RTO 目标。"""

    max_replication_lag_seconds: float
    rto_seconds: float


@dataclass
class StopCondition:
    """动作执行期间必须持续满足的停止条件。

    任一上下界被突破即视为触发，演练必须立即停止并回滚。
    上下界为 None 表示该方向不做限制。
    """

    metric: str
    max_value: Optional[float] = None
    min_value: Optional[float] = None
    description: str = ""

    def breached_by(self, value: float) -> bool:
        if self.max_value is not None and value > self.max_value:
            return True
        if self.min_value is not None and value < self.min_value:
            return True
        return False


@dataclass
class SwitchAction:
    """一个可执行的切换动作（数据库切换 / DNS / 消息复制 / 客户端重连）。

    硬性规则：没有停止条件或没有证据采集要求的动作一律不可执行，
    在计划生成阶段就会被拒绝。
    """

    action_id: str
    name: str
    action_type: str
    estimated_seconds: float
    budget_cost: float
    evidence_requirements: List[EvidenceRequirement] = field(default_factory=list)
    stop_conditions: List[StopCondition] = field(default_factory=list)
    rollback_seconds: float = 0.0
    rollback_action_id: Optional[str] = None

    def validate(self) -> None:
        if not self.stop_conditions:
            raise InvalidActionError(
                "动作 %s 缺少停止条件，禁止进入演练计划" % self.action_id
            )
        if not self.evidence_requirements:
            raise InvalidActionError(
                "动作 %s 缺少证据采集要求，禁止进入演练计划" % self.action_id
            )

    @property
    def rollback_id(self) -> str:
        return self.rollback_action_id or ("rollback_%s" % self.action_id)


@dataclass
class Service:
    """参与演练的服务及其依赖链。"""

    service_id: str
    name: str
    dependencies: List[str] = field(default_factory=list)
    rpo: Optional[RPOTarget] = None
    actions: List[SwitchAction] = field(default_factory=list)
    rollback_window_seconds: float = 0.0
    affected_users: int = 0


@dataclass
class DrillDefinition:
    """一次灾备演练的完整输入。

    对应题目要求的七类输入：服务依赖、RPO/RTO、观测证据、
    切换动作、回滚窗口、错误预算和 witness 状态。
    observations 与 action_results 仅用于离线模拟执行，
    规划阶段不依赖它们。
    """

    name: str
    services: List[Service]
    error_budget: float
    witness: WitnessState
    observations: dict = field(default_factory=dict)
    action_results: dict = field(default_factory=dict)

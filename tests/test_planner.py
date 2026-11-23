import unittest

from drill_planner import (
    BudgetExhaustedError,
    DependencyCycleError,
    DependencyNotFoundError,
    DrillDefinition,
    EvidenceMissingError,
    EvidenceRequirement,
    EvidenceStaleError,
    InvalidActionError,
    Observation,
    RPOTarget,
    RollbackWindowExceededError,
    RPOViolationError,
    RTOExceededError,
    Service,
    StopCondition,
    StopConditionBreachError,
    SwitchAction,
    WitnessNotReadyError,
    WitnessState,
    build_drill_plan,
    simulate,
)


def make_action(
    action_id,
    action_type="db_switch",
    budget_cost=1.0,
    estimated=10.0,
    rollback=5.0,
    metrics=None,
    extra_requirements=None,
):
    metrics = metrics or ("replication_lag_seconds", 0.0, 30.0)
    if isinstance(metrics, tuple):
        metrics = [metrics]
    requirements = [
        EvidenceRequirement(
            metric=name,
            max_age_seconds=60.0,
            healthy_min=low,
            healthy_max=high,
        )
        for name, low, high in metrics
    ]
    if extra_requirements:
        requirements.extend(extra_requirements)
    return SwitchAction(
        action_id=action_id,
        name=action_id,
        action_type=action_type,
        estimated_seconds=estimated,
        budget_cost=budget_cost,
        evidence_requirements=requirements,
        stop_conditions=[
            StopCondition(
                metric="error_rate",
                max_value=0.01,
                description="错误率不超过 1%",
            )
        ],
        rollback_seconds=rollback,
    )


def make_service(
    service_id,
    dependencies=None,
    actions=None,
    rollback_window=300.0,
    rto=120.0,
    affected_users=100,
):
    return Service(
        service_id=service_id,
        name=service_id,
        dependencies=dependencies or [],
        rpo=RPOTarget(max_replication_lag_seconds=30.0, rto_seconds=rto),
        actions=actions or [make_action("%s_action" % service_id)],
        rollback_window_seconds=rollback_window,
        affected_users=affected_users,
    )


def base_observations(lag=5.0, lag_age=10.0):
    data = {
        "replication_lag_seconds": Observation(
            "replication_lag_seconds", lag, lag_age
        ),
        "error_rate": Observation("error_rate", 0.001, 5.0),
        "dns_propagation_seconds": Observation(
            "dns_propagation_seconds", 20.0, 5.0
        ),
        "client_reconnect_rate": Observation("client_reconnect_rate", 0.99, 5.0),
    }
    return data


def make_drill(services, budget=100.0, witness=WitnessState.READY, **kwargs):
    return DrillDefinition(
        name="cross-region-drill",
        services=services,
        error_budget=budget,
        witness=witness,
        observations=base_observations(),
        **kwargs
    )


class PlannerValidationTest(unittest.TestCase):
    def test_action_without_stop_condition_rejected(self):
        action = make_action("a1")
        action.stop_conditions = []
        service = make_service("db", actions=[action])
        with self.assertRaises(InvalidActionError):
            build_drill_plan(make_drill([service]))

    def test_action_without_evidence_rejected(self):
        action = make_action("a1")
        action.evidence_requirements = []
        service = make_service("db", actions=[action])
        with self.assertRaises(InvalidActionError):
            build_drill_plan(make_drill([service]))

    def test_unknown_dependency_rejected(self):
        service = make_service("api", dependencies=["ghost"])
        with self.assertRaises(DependencyNotFoundError):
            build_drill_plan(make_drill([service]))

    def test_dependency_cycle_rejected(self):
        db = make_service("db", dependencies=["api"])
        api = make_service("api", dependencies=["db"])
        with self.assertRaises(DependencyCycleError):
            build_drill_plan(make_drill([db, api]))

    def test_witness_not_ready_rejected(self):
        service = make_service("db")
        with self.assertRaises(WitnessNotReadyError):
            build_drill_plan(
                make_drill([service], witness=WitnessState.NOT_READY)
            )

    def test_total_error_budget_insufficient(self):
        service = make_service(
            "db", actions=[make_action("db_action", budget_cost=10.0)]
        )
        with self.assertRaises(BudgetExhaustedError):
            build_drill_plan(make_drill([service], budget=5.0))

    def test_phase_budget_insufficient_midway(self):
        db = make_service("db", actions=[make_action("db_action", budget_cost=4.0)])
        api = make_service(
            "api",
            dependencies=["db"],
            actions=[make_action("api_action", budget_cost=8.0)],
        )
        with self.assertRaises(BudgetExhaustedError):
            build_drill_plan(make_drill([db, api], budget=10.0))

    def test_rollback_window_exceeded(self):
        db = make_service(
            "db",
            actions=[make_action("db_action", rollback=120.0)],
            rollback_window=100.0,
        )
        with self.assertRaises(RollbackWindowExceededError):
            build_drill_plan(make_drill([db]))


class PlanGenerationTest(unittest.TestCase):
    def _chain_services(self):
        db = make_service("db", affected_users=10)
        mq = make_service("mq", dependencies=["db"], affected_users=20)
        api = make_service("api", dependencies=["db", "mq"], affected_users=30)
        web = make_service("web", dependencies=["api"], affected_users=40)
        billing = make_service("billing", affected_users=50)
        return [web, api, billing, mq, db]

    def test_execution_order_follows_topology(self):
        plan = build_drill_plan(make_drill(self._chain_services()))
        order = plan.execution_order
        self.assertLess(order.index("db"), order.index("mq"))
        self.assertLess(order.index("mq"), order.index("api"))
        self.assertLess(order.index("api"), order.index("web"))
        self.assertEqual([phase.order for phase in plan.phases], [1, 2, 3, 4, 5])

    def test_phase_impact_and_blast_radius(self):
        plan = build_drill_plan(make_drill(self._chain_services()))
        db_phase = plan.phase_index["db"]
        self.assertEqual(db_phase.impact.affected_users, 10)
        self.assertEqual(
            set(db_phase.impact.blast_radius_services),
            {"db", "mq", "api", "web"},
        )
        web_phase = plan.phase_index["web"]
        self.assertEqual(web_phase.impact.blast_radius_services, ["web"])

    def test_validation_conditions_include_rpo_rto_evidence_stop(self):
        plan = build_drill_plan(make_drill([make_service("db")]))
        conditions = plan.phases[0].validation_conditions
        joined = "\n".join(conditions)
        self.assertIn("RPO", joined)
        self.assertIn("RTO", joined)
        self.assertIn("replication_lag_seconds", joined)
        self.assertIn("错误率不超过 1%", joined)
        self.assertIn("回滚", joined)

    def test_rollback_chain_covers_only_dependency_chain(self):
        plan = build_drill_plan(make_drill(self._chain_services()))
        api_chain = [
            step.service_id for step in plan.phase_index["api"].rollback_chain
        ]
        self.assertEqual(api_chain, ["api", "mq", "db"])
        billing_chain = [
            step.service_id
            for step in plan.phase_index["billing"].rollback_chain
        ]
        self.assertEqual(billing_chain, ["billing"])

    def test_plan_to_dict_is_serializable(self):
        plan = build_drill_plan(make_drill(self._chain_services()))
        data = plan.to_dict()
        self.assertEqual(data["execution_order"], plan.execution_order)
        self.assertEqual(len(data["phases"]), 5)


class SimulationTest(unittest.TestCase):
    def _chain_services(self):
        db = make_service("db", affected_users=10)
        mq = make_service("mq", dependencies=["db"], affected_users=20)
        api = make_service("api", dependencies=["db", "mq"], affected_users=30)
        web = make_service("web", dependencies=["api"], affected_users=40)
        billing = make_service("billing", affected_users=50)
        return [web, api, billing, mq, db]

    def test_successful_drill_completes_all_phases(self):
        drill = make_drill(self._chain_services())
        report = simulate(drill, build_drill_plan(drill))
        self.assertTrue(report.success)
        self.assertEqual(
            report.completed_services,
            ["billing", "db", "mq", "api", "web"],
        )
        self.assertEqual(report.rolled_back_services, [])
        self.assertEqual(report.skipped_services, [])

    def test_dependency_failure_rolls_back_chain_only(self):
        services = self._chain_services()
        drill = make_drill(
            services,
            action_results={"api_action": "dns 切换超时"},
        )
        plan = build_drill_plan(drill)
        report = simulate(drill, plan)

        self.assertFalse(report.success)
        self.assertEqual(report.failure_service, "api")
        self.assertIn("dns 切换超时", report.failure_reason)

        statuses = {item.service_id: item.status for item in report.phase_reports}
        self.assertEqual(statuses["api"], "failed")
        self.assertEqual(statuses["db"], "rolled_back")
        self.assertEqual(statuses["mq"], "rolled_back")
        self.assertEqual(statuses["web"], "skipped")
        # billing 与 api 无依赖关系，保持完成，不允许被扩大影响
        self.assertEqual(statuses["billing"], "completed")
        self.assertEqual(report.completed_services, ["billing"])

        failed_phase = next(
            item for item in report.phase_reports if item.service_id == "api"
        )
        # api 自身动作未完成，只回滚已完成的祖先依赖链
        self.assertEqual(
            [step.service_id for step in failed_phase.rollback_chain],
            ["mq", "db"],
        )

    def test_partial_switch_rolls_back_completed_actions_only(self):
        first = make_action("api_db_action", action_type="db_switch")
        second = make_action("api_dns_action", action_type="dns")
        api = make_service("api", actions=[first, second])
        drill = make_drill(
            [api],
            action_results={"api_dns_action": "DNS 权威节点拒绝更新"},
        )
        plan = build_drill_plan(drill)
        report = simulate(drill, plan)

        self.assertFalse(report.success)
        phase = report.phase_reports[0]
        self.assertEqual(phase.executed_action_ids, ["api_db_action"])
        self.assertEqual(
            [step.action_id for step in phase.rollback_chain],
            ["api_db_action"],
        )

    def test_evidence_missing_blocks_execution(self):
        service = make_service("db")
        drill = make_drill([service])
        drill.observations = {}
        report = simulate(drill, build_drill_plan(drill))
        self.assertFalse(report.success)
        self.assertIsInstance(EvidenceMissingError(report.failure_reason), Exception)
        self.assertEqual(report.phase_reports[0].executed_action_ids, [])

    def test_stale_evidence_blocks_execution(self):
        service = make_service("db")
        drill = make_drill([service])
        drill.observations = base_observations(lag_age=120.0)
        report = simulate(drill, build_drill_plan(drill))
        self.assertFalse(report.success)
        self.assertIn("年龄", report.failure_reason)

    def test_rpo_violation_blocks_switch(self):
        service = make_service("db")
        drill = make_drill([service])
        drill.observations = base_observations(lag=999.0)
        report = simulate(drill, build_drill_plan(drill))
        self.assertFalse(report.success)
        self.assertIn("RPO", report.failure_reason)

    def test_stop_condition_breach_aborts_phase(self):
        service = make_service("db")
        drill = make_drill([service])
        drill.observations = base_observations()
        drill.observations["error_rate"] = Observation("error_rate", 0.5, 1.0)
        report = simulate(drill, build_drill_plan(drill))
        self.assertFalse(report.success)
        self.assertIsInstance(
            StopConditionBreachError(report.failure_reason), Exception
        )

    def test_rto_breach_after_first_action(self):
        slow = make_action(
            "db_slow",
            estimated=90.0,
            extra_requirements=None,
        )
        fast = make_action("db_fast", estimated=90.0)
        service = make_service(
            "db",
            actions=[slow, fast],
            rto=120.0,
        )
        drill = make_drill([service])
        plan = build_drill_plan(drill)
        report = simulate(drill, plan)
        self.assertFalse(report.success)
        self.assertIn("RTO", report.failure_reason)
        self.assertEqual(
            report.phase_reports[0].executed_action_ids, ["db_slow", "db_fast"]
        )

    def test_witness_failure_at_runtime_aborts_safely(self):
        service = make_service("db")
        drill = make_drill([service], witness=WitnessState.CATCHING_UP)
        plan = build_drill_plan(
            make_drill([service])
        )  # 计划生成时 witness 就绪，执行前变化
        with self.assertRaises(WitnessNotReadyError):
            simulate(drill, plan)


if __name__ == "__main__":
    unittest.main()

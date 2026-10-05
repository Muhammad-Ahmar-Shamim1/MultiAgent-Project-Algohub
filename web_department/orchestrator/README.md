# orchestrator

LangGraph project state machine: Web Agent, orchestrator graph, requirement analyzer, planner, task router, scheduler.

`agent_factory.py` is the Agent Factory (FR-050..FR-053): `create()`/`acquire()`/`release()`/
`retire()` manage worker `Agent` rows through their `db.states.AGENT_TRANSITIONS` lifecycle, and
`watchdog_tick()`/`run_watchdog()` BLOCK-and-diagnose any agent stuck WORKING past its blueprint's
`budget.timeout_min`. See the top-level README's "Agent Factory" section.

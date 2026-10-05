# qa

QA gate: lint, type checks, unit/integration/API/E2E/regression test runs and acceptance-criteria coverage.

`checks.py` is the evaluation checks registry a Blueprint's `evaluation` list names checks from
(FR-043): `CHECKS` maps each of `lint_pass`, `unit_tests_pass`, `e2e_pass`, `screenshot_attached`,
`openapi_valid`, `migration_reversible` and `no_secrets` to an async function that runs the real
check inside the project's workstation via `workstation/tools.py` and returns a `CheckResult`.

`openapi_valid` is the one check that runs no command: it reads `openapi.yaml` (the project's API
contract, written by the Architect) through the tool layer and validates it in-process as an
OpenAPI 3.x document, so roles without RUN_COMMAND can use it.

The QA gate (FR-100..FR-104):

- **`test_runner.py`** runs the stages inside the workstation as the QA role: lint, typecheck,
  unit, integration, e2e and regression. Integration and regression run only when
  `backend/tests/integration` / `backend/tests/regression` exist. It parses the JUnit XML under
  `artifacts/junit/` into a `QAReport` with passed, failed, skipped, duration, failures and
  `criteria_coverage`.
  - Criterion ids are `<feature id>.<n>`, e.g. `F-02.1`.
  - A test covers a criterion when its name carries the tag `F02_1`. The `_1` may be left out
    for a feature with a single criterion, e.g. `test_F02_cart_total_updates`.
  - `save_report()` writes `artifacts/qa-report.json` and `artifacts/qa-report.md`.
- **`quality_gate.py`** passes only with zero failures and every criterion `pass`.
- **`failure_triage.py`** picks the responsible task for a failure:
  1. `git blame` of the app line in the traceback (workers commit as `WEB-NNN: title`);
  2. else the test's criterion tag, then its feature, then that feature's task;
  3. else the last commit on the failing file.

  It then runs one fix cycle per task. A REVIEW task becomes FIX_REQUIRED with `fix_context`
  attached. A COMPLETED task gets a new READY "Fix ..." task. After `MAX_FIX_CYCLES` (3) cycles
  it escalates: replan (LLM), then the strongest model (`escalation_purpose: planning`), then
  BLOCKED plus a human `REVIEW_REQUIRED` event.

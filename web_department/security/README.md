# security

Security gate (FR-110..FR-113): static analysis, secret scanning, dependency and container
auditing, and the security checklist.

- **`scanners.py`** runs inside the workstation as the Security role and normalises every tool's
  JSON into `Finding {tool, rule, severity, file, line, message}`. The scanners are:
  - semgrep (`p/owasp-top-ten`, `p/python`, `p/typescript`)
  - `gitleaks detect --redact`
  - `npm audit --json`
  - `pip-audit -f json`
  - `trivy fs`
  - `trivy image`, against `artifacts/image.tar` when a build step has exported one. Otherwise
    it is reported as `skipped`, because workstations have no Docker socket.

  A scanner whose output does not parse is reported as `error`.
- **`security_gate.py`**:
  - Any High/Critical finding, or any scanner `error`, blocks the gate. Each blocking finding
    gets a READY fix task for the role that owns its file; an open one is reused on a re-run.
  - Medium findings are warnings.
  - Findings with an approved exception are listed as `excepted` and do not block.
  - Emits `SECURITY_BLOCKED` and writes `artifacts/security-report.json` + `.md`.
- **Exceptions:** `POST /api/v1/security/exceptions` (Approver only) accepts one finding,
  identified by tool/rule/file/line. It is recorded as an append-only `Approval`
  (`action_type="security_exception"`) plus an `APPROVED` event (the audit log).

The worker is `agents/security/agent.py` (`SecurityWorker`). It runs the scanners, then one LLM
review of the app code against a 9-item checklist (`confidential=True`, local models only), then
the gate. The scanners are baked into `algohub/workstation:golden-v1`
(`workstation/images/Dockerfile.golden`).

Approval gates (FR-130..FR-132):

- **`approvals.py`**: `request_approval()` is called inside a graph node. It records a pending
  `Approval` (idempotent per `request_key`, committed before pausing), emits
  `APPROVAL_REQUESTED`, notifies the webhook, and pauses with LangGraph's `interrupt()`.
  `decide()` records the decision as a second append-only row (`decides_id` points to the
  request) and emits `APPROVED`/`REJECTED`. The six action types are listed in `ActionType`.
- **`evidence.py`**: `build_evidence()` collects the diff, QA/security reports, screenshots and
  a draft deployment plan, each best-effort.
- **API:** `GET /api/v1/approvals`, `GET /api/v1/approvals/{id}`, and
  `POST /api/v1/approvals/{id}` (Approver only), which records the decision and re-queues the
  project. `orchestrator/runner.py` sees the paused run and resumes it with
  `Command(resume=decision)`.
- **Notifications:** `collaboration/notifications.py` posts to `APPROVAL_WEBHOOK_URL`, one body
  for both Discord and Slack, with bounded retries. It never blocks the pipeline.

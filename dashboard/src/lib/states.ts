/** State sets (SRS 7.2), for filter dropdowns. Kept in step with db/states.py by the typed client:
 * the filters are passed to typed API calls, so a state the API no longer has fails to compile. */
export const TASK_STATES = [
  "CREATED", "DEPENDENCY_WAITING", "READY", "ASSIGNED", "IN_PROGRESS", "REVIEW", "FIX_REQUIRED",
  "COMPLETED", "BLOCKED", "CANCELLED",
] as const;

export const AGENT_STATES = [
  "CREATED", "PROVISIONED", "AVAILABLE", "ASSIGNED", "WORKING", "REVIEW", "BLOCKED", "RETIRED",
] as const;

export const ROLES = [
  "architect", "frontend", "backend", "database", "api_integration", "qa", "security", "devops",
  "maintenance",
] as const;

export const ALERT_EVENTS = ["ALERT_RAISED", "SECURITY_BLOCKED", "TEST_FAILED", "ROLLED_BACK"] as const;

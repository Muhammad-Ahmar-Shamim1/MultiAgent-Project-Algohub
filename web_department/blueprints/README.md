# blueprints

Versioned YAML worker role definitions and their validation schema (FR-040..FR-043).

- `schema.py` - `BlueprintSpec` (the contract every `*.yaml` file must satisfy) and `Budget`.
  Validates semver, that `tools`/`evaluation` only name real tools/checks, and that `permissions`
  exactly matches `config.permissions.ROLE_PERMISSIONS` for the blueprint's `role` (SRS
  section 10) - not just valid `Permission` values.
- `*.yaml` - one file per role: `web_architect`, `frontend_developer`, `backend_developer`,
  `database_developer`, `api_integration`, `qa_engineer`, `security_engineer`,
  `devops_engineer`, `maintenance`.
- `registry.py` - `load_all()` parses and validates every file; `load_and_sync()` additionally
  upserts each into the DB (a checksummed cache - the YAML file in Git is the source of truth,
  FR-042) and returns a `BlueprintRegistry` (`get(role)` / `list()`). Both raise
  `BlueprintValidationError` naming the first bad file rather than partially loading - called
  once at API startup (`api/main.py`'s lifespan), so an invalid blueprint refuses to let the
  process start (FR-041).

See the top-level README's "Blueprints" section for the API endpoints and evaluation checks.

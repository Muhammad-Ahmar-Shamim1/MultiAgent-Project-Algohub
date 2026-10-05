# R1 findings: the todo app

This is the log `tests/e2e/test_r1_todo.py` (STEP 13) is meant to produce. Every failed run of
that test gets one entry below, in the format:

```
## <date> - <one-line symptom>

**Where:** which node / task / prompt failed, and how (paste the exact error).
**Root cause:** why it actually happened - not just what broke.
**Fix:** what changed in the platform (a prompt, a template, task sizing, a policy rule, ...) -
never a hand-edit to the generated app itself. Link the commit/file.
**Verified:** re-ran the e2e test; state the result.
```

Rule this file exists to enforce (AGENTS.md rule 10, and the R1 exit criteria's "no human code
edits"): when the pipeline produces broken code, the bug is in AlgoHub, not in the todo app. Fix
the thing that writes the code, not the code it wrote.

---

_No entries yet. `tests/e2e/test_r1_todo.py` has not completed a run against this codebase.
Local infra has fought this every step: Ollama's Windows installer is 1.5GB (impractical on this
connection - see the settled decision to fall back to a shared provider below), and Docker Hub
has been intermittently unreachable across multiple sessions, so `algohub/workstation:golden-v1`
is still mid-rebuild as of this writing (its Dockerfile now retries both the pip install and the
Playwright browser download, each of which has died mid-transfer on this connection at least
once - see the git history of `Dockerfile.golden` for the fixes).

Settled for this validation run: `providers.yaml` now also enables `groq-llama-3.3-70b` (shared),
and `Settings.coder_confidential` (new, defaults `True` per IR-024) is the one place to opt a
specific run's Coder into using it too - set `CODER_CONFIDENTIAL=false` in `.env` alongside
`GROQ_API_KEY`. Revert both once a private/local provider is available again.

See `docs/r1-metrics.json` for the latest run's numbers once one exists - the test writes it
automatically, pass or fail._

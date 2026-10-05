"""dashboard/openapi.json (the dashboard's typed-client source) matches the live API."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def test_dashboard_openapi_json_is_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    committed = module.OUT_PATH.read_text(encoding="utf-8")

    assert committed == module.render(), (
        "dashboard/openapi.json is stale: run `python scripts/export_openapi.py` and "
        "`npm run gen:api` in dashboard/"
    )

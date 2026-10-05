"""Export the platform API's OpenAPI document to dashboard/openapi.json.

python scripts/export_openapi.py
cd dashboard && npm run gen:api   # -> src/lib/api/schema.d.ts (the dashboard's typed client)

tests/unit/test_openapi_export.py fails when the committed file is stale.
"""

import json
from pathlib import Path
from typing import Any

from web_department.api.main import app

OUT_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "openapi.json"


def render() -> str:
    """The OpenAPI document as the exact text written to OUT_PATH."""
    document: dict[str, Any] = app.openapi()
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main() -> None:
    """Write the current OpenAPI document to OUT_PATH."""
    OUT_PATH.write_text(render(), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

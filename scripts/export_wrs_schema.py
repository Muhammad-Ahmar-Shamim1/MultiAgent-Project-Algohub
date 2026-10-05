"""Export the WebRequirementsSpec JSON Schema to docs/schemas/wrs.schema.json.

python scripts/export_wrs_schema.py
"""

import json
from pathlib import Path

from web_department.orchestrator.wrs_schema import WebRequirementsSpec

OUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "wrs.schema.json"


def main() -> None:
    """Write the current WebRequirementsSpec JSON Schema to OUT_PATH."""
    schema = WebRequirementsSpec.model_json_schema()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

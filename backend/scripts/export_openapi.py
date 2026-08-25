"""Write the OpenAPI document to disk.

Run in offline mode so schema export needs no API keys and produces byte-identical output
regardless of environment — the generated TypeScript is committed and diffed in CI.

Usage:
    uv run python scripts/export_openapi.py ../shared/openapi.json
"""

import json
import sys
from pathlib import Path

from motorooter.app import create_app
from motorooter.routing.factory import RoutingSettings

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "shared" / "openapi.json"


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)

    schema = create_app(RoutingSettings(offline=True)).openapi()
    # sort_keys so unrelated route reordering never shows up as a spurious diff.
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")

    print(f"wrote {output} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Write `openapi.json` from the live app.

The web app generates its fetch types from this document
(`pnpm sync:contracts` in Todd-Frontend). Regenerate it after any route or
model change, and diff it against the Node service's before cutting over —
§6: the spec must stay shape-compatible or the web app breaks.

    uv run python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from todd.api.main import create_app

OUT = pathlib.Path(__file__).resolve().parents[1] / "openapi.json"


def main() -> None:
    spec = create_app().openapi()
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    paths = spec.get("paths", {})
    print(f"wrote {OUT.relative_to(pathlib.Path.cwd())} — {len(paths)} paths")
    for path in sorted(paths):
        for method in sorted(paths[path]):
            print(f"  {method.upper():6} {path}")


if __name__ == "__main__":
    main()

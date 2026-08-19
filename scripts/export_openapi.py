"""Export the REGWATCH OpenAPI schema as canonical JSON (the contract snapshot).

Run from the repo root:

    uv run python scripts/export_openapi.py regwatch/frontend/openapi.json

With no argument the JSON goes to stdout. Given a path (which must resolve
inside the repository - the snapshot is a committed artifact), the snapshot is
written via a same-directory temp file + os.replace, so a failing export
(broken venv, import-time error in the app) can never truncate the committed
snapshot the way a shell `>` redirect would.

The output is the committed API-contract snapshot: `npm run gen:types` (see
regwatch/frontend/package.json) regenerates it plus lib/api-types.ts, and both
CI (the frontend-contract job) and tests/test_openapi_contract.py fail when
the committed copies drift from the live FastAPI schema in either direction.

Canonical form: sorted keys and a fixed indent so the diff of a contract
change reads as the change itself, and default ensure_ascii so the committed
snapshot stays ASCII even where route docstrings are not.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from pathlib import Path

from regwatch.api.main import app

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    # Serialize before opening any output (imports fail even earlier), so
    # every failure mode leaves an existing snapshot byte-for-byte intact.
    payload = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if len(sys.argv) < 2:
        sys.stdout.write(payload)
        return
    # The snapshot is a committed repo artifact; contain the write to the
    # repository so a caller-supplied path can never escape it.
    resolved = Path(sys.argv[1]).resolve()
    if not resolved.is_relative_to(_REPO_ROOT):
        sys.exit(f"refusing to write outside the repository: {resolved}")
    out = str(resolved)
    tmp = f"{out}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        # Same-directory rename is atomic: readers see the old snapshot or
        # the new one, never a half-written file.
        os.replace(tmp, out)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


if __name__ == "__main__":
    main()

"""The retired public drug API has no runtime endpoint or credential path."""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings

from regwatch.sources import _utils


def test_retired_api_configuration_and_helpers_are_absent() -> None:
    assert "openfda_api_key" not in Settings.model_fields
    assert not hasattr(_utils, "get_openfda_client")
    assert not hasattr(_utils, "openfda_params")
    assert not hasattr(_utils, "fetch_openfda_results")


def test_runtime_tree_contains_no_retired_api_endpoint_or_secret() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src",
        root / "config",
        root / ".github",
        root / "regwatch" / "frontend",
    ]
    forbidden = (
        "api.fda.gov",
        "download.open.fda.gov",
        "OPENFDA_API_KEY",
        "dailymed.nlm.nih.gov",
        "/scripts/cder/rems/",
    )
    findings: list[str] = []
    for base in paths:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".woff2"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in forbidden:
                if needle in text:
                    findings.append(f"{path.relative_to(root)}: {needle}")
    assert findings == []

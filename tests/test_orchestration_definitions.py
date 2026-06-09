from __future__ import annotations

import pytest

pytest.importorskip("dagster")


def test_dagster_seed_job_loads() -> None:
    from regwatch.orchestration import definitions

    assert definitions.seed_corpus_job.name == "seed_corpus_job"
    assert definitions.defs.resolve_job_def("seed_corpus_job").name == "seed_corpus_job"

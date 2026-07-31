"""Stable identities for prompt templates written to logs and eval artifacts.

The hash covers the public id, explicit version, and exact template text. Dynamic
user/source content is intentionally excluded: two runs with different questions
still share one auditable prompt contract, while any template edit changes the
fingerprint even if someone forgets to bump the human-readable version.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PromptIdentity:
    prompt_id: str
    version: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    def log_fields(self) -> dict[str, str]:
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.version,
            "prompt_sha256": self.sha256,
        }


def identify_prompt(prompt_id: str, version: str, *templates: str) -> PromptIdentity:
    digest = hashlib.sha256()
    for part in (prompt_id, version, *templates):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return PromptIdentity(prompt_id=prompt_id, version=version, sha256=digest.hexdigest())

"""One-shot G1 probe: stream a v6-style prose prompt and record wire facts.

Usage: python scripts/probe_stream_format.py --base-url URL --token TOK --endpoint NAME
Prints one JSON report; makes exactly 3 streamed calls. Never commits secrets.
"""

from __future__ import annotations

import argparse
import json

from openai import OpenAI

PROBE_SYSTEM = "You answer in 2-3 short cited sentences using [1] style markers."
PROBE_USER = (
    "Passages:\n[1] A fasting bioequivalence study with 36 subjects is "
    "recommended.\n\nQuestion: What study design is recommended?"
)


def probe(client: OpenAI, endpoint: str) -> dict[str, object]:
    events = client.chat.completions.create(
        model=endpoint,
        messages=[
            {"role": "system", "content": PROBE_SYSTEM},
            {"role": "user", "content": PROBE_USER},
        ],
        temperature=0.0,
        max_tokens=400,
        stream=True,
        stream_options={"include_usage": True},
    )
    deltas: list[str] = []
    first_model: str | None = None
    models: list[str | None] = []
    reasoning_fields: set[str] = set()
    n = 0
    for event in events:
        n += 1
        models.append(getattr(event, "model", None))
        if n == 1:
            first_model = getattr(event, "model", None)
        for choice in getattr(event, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                deltas.append(content)
            for field in ("reasoning_content", "reasoning", "thinking"):
                if getattr(delta, field, None):
                    reasoning_fields.add(field)
    text = "".join(deltas)
    return {
        "event_count": n,
        "content_delta_count": len(deltas),
        "incremental": len(deltas) > 3,
        "first_event_model": first_model,
        "served_models_seen": sorted({m for m in models if m}),
        "typed_reasoning_fields": sorted(reasoning_fields),
        "harmony_markup_in_content": "<|channel|>" in text or "<|message|>" in text,
        "think_tags_in_content": "<think>" in text.lower(),
        "content_preview": text[:400],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--endpoint", required=True)
    args = ap.parse_args()
    client = OpenAI(base_url=args.base_url, api_key=args.token)
    reports = [probe(client, args.endpoint) for _ in range(3)]
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()

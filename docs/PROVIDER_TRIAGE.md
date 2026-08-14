# Provider triage: is the model actually reachable?

Last updated: 2026-08-13.

How to decide, in under five minutes, whether the Databricks embedding or LLM
endpoint this app depends on is actually serving.

Read this before concluding that an endpoint is missing, deleted, or
misconfigured.
The 2026-08-13 outage lost roughly two hours to exactly that wrong conclusion,
and every command that produced it returned a confident, wrong answer.

## The trap

Databricks inventory commands do not describe the serving path.
`workspace.default.regwatch-embed` is invisible to all of them and serves fine
anyway.

Every one of these was run on 2026-08-13 while the endpoint was healthy:

```console
$ databricks serving-endpoints list --profile amneal
# 11 endpoints. workspace.default.regwatch-embed is NOT among them.

$ databricks serving-endpoints get workspace.default.regwatch-embed --profile amneal
Error: Endpoint with name 'workspace.default.regwatch-embed' does not exist.

$ databricks serving-endpoints get regwatch-embed --profile amneal
Error: Endpoint with name 'regwatch-embed' does not exist.

$ databricks registered-models get workspace.default.regwatch-embed --profile amneal
Error: Routine or Model 'workspace.default.regwatch-embed' does not exist.

$ databricks registered-models list --catalog-name workspace --schema-name default --profile amneal
# empty
```

Four "does not exist" results and an empty listing.
The obvious reading is that somebody deleted the endpoint.
That reading is wrong.

The listing was even checked against two profiles (`amneal` and `regwatch`) to
rule out a permissions artifact, and the `workspace` catalog and its `default`
schema were both enumerable.
Agreement across profiles increased confidence in a conclusion that was still
wrong, because both profiles were asking the wrong question.

## The one command that answers the question

Send the request the application sends.

```bash
# Embeddings. Substitute a real token; never paste one into a doc or a commit.
curl -s -o /tmp/probe.json -w 'HTTP %{http_code}\n' \
  -X POST "https://<workspace-host>/ai-gateway/mlflow/v1/embeddings" \
  -H "Authorization: Bearer $DATABRICKS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"workspace.default.regwatch-embed","input":["probe"],"dimensions":1024}'

jq '{model, dims: (.data[0].embedding | length)}' /tmp/probe.json
```

A healthy endpoint answers:

```json
{"model": "qwen3-embedding-0-6b-112025", "dims": 1024}
```

That single call settles in seconds what the inventory commands could not settle
at all.
`model` in the response is the served build id, which is the value worth
comparing against `docs/DECISIONS.md` when you suspect a model swap.

## Reading the result

| Result | Meaning | Next step |
| --- | --- | --- |
| `200` + expected `dims` | The endpoint serves. The fault is elsewhere. | Stop triaging the provider. |
| `401` / `403` | Token expired or lacks access. | Rotate `QWEN_EMBEDDING_TOKEN` / `DATABRICKS_LLM_TOKEN`. |
| `404` | The model name really does not route. | Now the inventory commands are worth reading. |
| `429 REQUEST_LIMIT_EXCEEDED` | Per-request size cap, not a dead endpoint. | See [Batch size](#batch-size) below. |
| `5xx` | Transient serving fault. | Retry; the provider already backs off six times. |

A `429` is the one most likely to be misread.
It proves the endpoint is alive and refusing this particular request.
It never means the endpoint is gone.

## Batch size

The embedding endpoint enforces a per-request input cap.
Measured live on 2026-08-13 with ~140-token inputs:

| Inputs per request | Result |
| --- | --- |
| 1, 8, 12, 16, 24 | `200` |
| 32, 64 | `429 REQUEST_LIMIT_EXCEEDED` |

The cap behaves as a token budget rather than a clean input count, so a batch of
16 long chunks can fail where 24 short ones pass.
**Size bulk embedding batches at 8.**

`Qwen3EmbeddingProvider.__init__` defaults to `batch_size=128`
(`src/regwatch/process/embedder.py`).
Every bulk path inherits that default and fails on its first request, while the
query path never notices because `embed_query` sends exactly one input.
That asymmetry is why an endpoint can look perfectly healthy to a user asking
questions and be completely unusable for ingest.

## Probing with production credentials

The probe above uses your token.
To prove the *application's* credentials work, run inside the deployed image on a
throwaway machine, which inherits the app's secrets:

```bash
fly machine run <deployed-image> -a amneal --restart no --region iad \
  --file-local /tmp/probe.py=<local-path> \
  -e REGWATCH_INIT_DB=false \
  /app/.venv/bin/python /tmp/probe.py
```

Two flyctl traps, both hit on 2026-08-13:

- **Never pass `python -c`.** `flyctl` claims `-c` as its own `--config`
  shorthand and tries to load your Python source as an app config file.
  Use `--file-local` and a bare positional command.
- **`--command` is ignored without `--shell`.** A machine launched with
  `--command "python foo.py"` and no `--shell` silently runs the image `CMD`
  instead, which for this image is `regwatch serve`.

## Where the values come from

Three places must agree, and a mismatch in any of them fails closed:

- `QWEN_EMBEDDING_MODEL` (Fly secret) is compared for exact string equality
  against the profile row's `model` by
  `get_embedding_provider_for_profile`.
- The `embedding_profile` row in Lakebase pins `model`, `dimension`, `revision`,
  `query_instruction_version`, and `preprocessing_version`.
- `QWEN_EMBEDDING_BASE_URL` is the gateway root the client posts `embeddings`
  under. In production it is the AI Gateway path
  (`https://<host>/ai-gateway/mlflow/v1`), which is **not** the per-endpoint URL
  shown in older revisions of `DEPLOY.md`.

If a probe with your own token succeeds and the app still fails, the divergence
is in one of those three, not in Databricks.

## Checklist

1. Probe the request path with `curl`. Do not start anywhere else.
2. If `200`, stop. The provider is not your bug.
3. If `429`, check batch size before anything else.
4. If `401`/`403`, check token freshness and rotation date.
5. Only on `404` should you reach for `serving-endpoints` and
   `registered-models`.

## Related

- [Deploy runbook](DEPLOY.md) - Fly and Lakebase operations
- [Secrets runbook](SECRETS_RUNBOOK.md) - where each secret lives, how to rotate
- [Decisions](DECISIONS.md) - the 2026-07-29 entry records the served build id

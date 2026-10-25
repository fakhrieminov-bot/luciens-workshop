# Horizon Research Lab v0.1

A small, dependency-free Python 3.11+ research collector for Lucien's Workshop.
It searches OpenAlex, normalizes paper metadata, deduplicates into SQLite, and
exports portable JSONL. It does not edit Google Docs, Notion, or the Horizon journal.

## Run locally

From the repository root:

```sh
python -m unittest discover -s tests -v
# Optional: export OPENALEX_API_KEY in your shell or secret manager.
python -m horizon --query 'machine learning reproducibility' --limit 25 --since 2026-01-01
# Optional install for the `horizon` command:
python -m pip install .
```

Repeat `--query` for up to 10 searches. Use your own research topics; no topic was
available in the prior plan. `--limit` is 1–100 per query; default 25. Each search
makes one page request, so this is a bounded discovery sample, not an exhaustive
literature review. `--since YYYY-MM-DD` filters publication date, not update date.
The search sorts newest publications first. No PDFs are fetched.

Outputs default to `data/horizon.sqlite3` and `data/papers.jsonl`. Reuse that folder
for incremental local collection. Back up the SQLite file when the collector is
stopped. JSONL is a portable export, not an input for restoring collector state.
There is no concurrent local writer support.

## Metadata and deduplication

Records include source identifiers, normalized DOI, title, year/date, authors,
venue, landing URL, reconstructed abstract when available, citation count,
open-access metadata, UTC retrieval/first-seen timestamps, and matched queries.
Missing upstream metadata remains null. IDs are matched first, then DOI, then an
unambiguous normalized title + year. Conflicting nonempty DOIs remain separate.
Title matching is conservative and can still conflate identically titled works;
review results before using them as evidence. No fuzzy matching is performed.
Metadata is refreshed on repeated results and query/identifier provenance retained.

All requests finish before the database transaction begins. A failed query leaves
the library unchanged; exports use an atomic file replacement. Export failure can
leave the database newer than JSONL: rerun after fixing filesystem permissions.

## Scheduled collection and credentials

1. Add a verified OpenAlex key as repository Actions secret `OPENALEX_API_KEY`.
2. Set repository Actions variable `HORIZON_QUERY` to your research topic.
3. Merge the pull request. Scheduled workflows run only on the default branch;
   use **Actions → Collect research → Run workflow** for a manual run.

The daily schedule is 05:17 UTC (09:17 Baku); GitHub can delay scheduled jobs.
Every run collects at most 25 results with at most 3 HTTP attempts, a 30-second
request timeout, and at least 1.1 seconds between attempts. Retryable network,
429, and selected 5xx errors use bounded backoff. Retry-After seconds or HTTP dates
are honored; a cooldown above 60 seconds stops the run instead of retrying early.
Authentication errors stop immediately. There are no paid fallback providers.

Each scheduled run starts a fresh library and uploads a 30-day artifact containing
SQLite and JSONL. **Actions runs do not accumulate state across days.** Download
artifacts for review; use a persistent local data folder for a cumulative library.
This avoids repository write permissions and unreliable cache-based persistence.
Workflow artifacts may be accessible to repository readers: do not put sensitive
research queries or private annotations in this public repository's workflow.

The key travels in the Authorization header and is never written into exports or
request URLs. No credentials are committed; `.env*` and local data are ignored.
`.env.example` is documentation only: environment files are not loaded by the app.
Actions have read-only repository access, pinned action revisions, a timeout, and
serialized collection runs. Tests never need credentials or make network calls.
Public-repository schedules may be disabled by GitHub after prolonged inactivity.

OpenAlex authentication policy has changed over time. Use a key for scheduled
operation; basic local single-page use may work without one. See the current
[OpenAlex authentication and limits](https://help.openalex.org/api/authentication/)
and [API reference](https://help.openalex.org/api/). The collector's limits are
intentionally much lower than provider throughput allowances. Respect OpenAlex
attribution/licensing and the rights of original abstract publishers when reusing data.

## Future integrations

Semantic Scholar is deferred in v0.1. Add a provider adapter that produces the same
metadata contract, with its own opt-in key, request budget, and fixture tests.
The storage layer already retains multiple source identifiers.

For Notion, add an explicit opt-in export/sync layer consuming JSONL. Store its
integration token only in a secret, require a configured target database ID, and
upsert by a stable Horizon paper ID with a sync ledger and dry-run preview. Do not
use title alone as a Notion identity. No Google Docs write adapter is included;
the existing Horizon journal remains independent and must never be overwritten.

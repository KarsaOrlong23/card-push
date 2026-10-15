# Card push

Lightweight knowledge auto-injection before each agent turn: **Push instead of pull**.

Instead of relying on the agent remembering to query a knowledge source, card push
extracts keywords from the latest user message, runs a BM25 search against a QMD
index, and injects the top matching documents directly into context — before the
model responds.

## Why

Without push, knowledge injection depends on the agent voluntarily calling
`mcp__wissen__query` (or equivalent) on every turn. In practice this happens rarely.
Card push makes knowledge delivery automatic and invisible.

## Insertion point

Hermes plugin, `pre_llm_call` hook. No modifications to Hermes core required.
Enable/disable with a single line in `plugins.enabled`.

## Tenant / collection isolation

The plugin supports a **tenant push** pattern for multi-tenant knowledge bases:

- **Base collections** (configured via `CARD_PUSH_COLLECTIONS` or `CARD_PUSH_COLLECTIONS_FILE`)
  are always searched.
- An **additional collection** can be temporarily released via
  `CARD_PUSH_RELEASE_FILE`. The file contains a single line with a collection slug
  (format: `^[a-z][a-z0-9-]+$`). When present, that one collection is searched
  alongside the base collections. When empty or absent, only base collections are
  searched.

This ensures that only the currently-active collection is accessible, and all
others remain locked. Example:

```bash
echo "tenant-prod" > /path/to/release.txt   # open tenant-prod
: > /path/to/release.txt                     # lock again (back to base only)
```

## Configuration (environment variables)

| Variable | Description | Default |
|---|---|---|
| `CARD_PUSH_QMD` | Path to qmd binary | `qmd` on PATH |
| `CARD_PUSH_COLLECTIONS` | Comma-separated base collections | *(none — must be configured)* |
| `CARD_PUSH_COLLECTIONS_FILE` | File with base collections (one per line) | *(none)* |
| `CARD_PUSH_INDEX` | XDG_CACHE_HOME for qmd | env `XDG_CACHE_HOME` |
| `CARD_PUSH_RELEASE_FILE` | Release file for an additional collection | — (locked) |
| `CARD_PUSH_STOPWORDS` | Comma-separated stop-words | built-in (EN+DE) |
| `CARD_PUSH_SENSITIVE` | Regex for doc IDs to never inject | `b[-_ ]?(71\|72\|100\|101\|102)` |
| `CARD_PUSH_MAX_HITS` | Max results to inject | `5` |
| `CARD_PUSH_TIMEOUT` | Search timeout (seconds) | `4` |
| `CARD_PUSH_INJECT_FORMAT` | Format template for injected context | built-in |

## Behavior

- **Fail-silent:** If qmd times out or errors, the agent proceeds with no
  injected context — no agent is ever blocked by the knowledge layer.
- **Sensitive documents are never injected:** Documents matching the
  `CARD_PUSH_SENSITIVE` regex are filtered out before injection.
- **Receipt log:** Per-profile log at `<HERMES_HOME>/logs/card-push.jsonl`
  records each search (query hash, collections, hit count, latency, errors).
  This makes it measurable whether the plugin fires and whether it produces
  useful results.

## License

MIT — see `LICENSE`.

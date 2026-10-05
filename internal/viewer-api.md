# Viewer API notes (for the front end)

Read endpoints under `/api/runs/{run_id}/`. All return JSON; unknown run is 404; a bad
`after`/`limit` is 400. The page must poll only while visible (spec 15 §4.4).

## Cursors: which kind of page you are reading

| Endpoint | `next_cursor` | Drains? |
|---|---|---|
| `transcript/{key}` (raw records), `transcript/{key}/events` | int, always | never: a live transcript keeps growing. Poll from `next_cursor`; caught up when it equals `total` |
| `diagnostics` | int line index, always | never (live file); caught up when `next_cursor == total` |
| `oracle` | int offset, or `null` | yes: a ledger page is finite, `null` means no more rows |

`transcript/{key}/events`: `after` is a raw record index; `limit` caps emitted events (a real
transcript is mostly `partial`/`stream_evt` records that emit nothing, so one page may cover
many raw records).

## Reader endpoints (spec 14 Phase 2)

- `retrospectives`: `{retrospectives[{ts, source_id, role, flagged, delegation_id, preamble, sections, text}], missing}`
- `critic_reviews`: `{reviews[{call, verdict, numbers, findings, text, mtime, source_id, delegation_id, delegation_id_source}]}`; `delegation_id_source` is `recorded | ordinal | null`
- `diagnostics?after&limit&kind`: `{rows, next_cursor, total, counts}`; `kind` = the row's `error_type`, else `tool`
- `notes`: `{notes[{name, mtime, text}]}` (the strategizer's `*.md`)
- `evidence`: `{index, repo, delegations[{id, to_node, status, workspace_sha, predecessor_sha, files}]}`; `evidence/{delegation_id}`: `{show_stat, diff_stat}`

- `literature`: `{papers[{paper_id, title, authors, year, doi, arxiv_id, venue, abstract, added_at, source, citation_count, full_text, in_run}], total, added_in_run, run_window{started, ended}, errors[diagnostics rows], degraded[rows]}`. `papers` is the STUDY corpus (all runs); `in_run` is true/false/null (null = no parseable `added_at`). Cooldowns and other failures are the same ERROR_RETURN row.
- `GET /api/study/history?limit=` (study-level, not per run): `{repo, path, commits[{sha, author, date, subject, files[{path, insertions, deletions}]}]}`; `repo: null` and `commits: []` when the study is under no repository; 502 if git fails. `insertions`/`deletions` are null for binaries.

## Earlier endpoints (shapes as served; only the first level shown)

Study level:
- `GET /api/session`: `{can_write}`. `GET /api/runs`: `[{run_id, path, has_debug, status}]`.
- `GET /api/study/preflight`: `{launcher, checks[], can_start, launched[]}`. POST `study/start`, `study/launch`, `study/launch/stop`, `study/kill` act on the study and need write access.

Per run (`/api/runs/{run_id}/...`):
- `graph`: `{nodes[], edges[], entry, tool_docs{tool: doc}, backend, node_w, node_h, canvas_*}` (a permission graph, not an execution graph)
- `delegations`: `[{id, from_node, to_node, task, deliverable, hypothesis_ids[], started_at, completed_at, status, tokens_in, tokens_out, cost_usd, is_falsification_attempt, evals}]`
- `ledger`: `{hypotheses[], milestones[]}`
- `vitals`: `{started_at, cost_usd, calls, unknown_cost_calls, max_awake_nodes, output_tokens, by_role{role: {...}}, elapsed_s, closed}`. `closed` is the signal to stop polling.
- `oracle?after&limit&namespace`: `{registered, evaluator_name, entrypoint, eval_budget, store_dir, store_found, stores[], total_evals, next_cursor}`; offset cursor, `null` once drained (see the cursor table). `limit` default 400, max 5000.
- `trajectory`: `{store_found, declared_outputs[], stores[]}`; `funnel?stages=`: `{store_found, stores[]}`; `figure_of_merit`: `{declared, ...}`
- `monitor`: `[{source, ts, text}]`; `operator`: `{questions[]}`; POST `answer` / `note` (JSON body) write to the operator channel and need write access
- `artifacts`: list; `artifact?path=`: one file by run-relative path
- `notebook`: `{cells[], path, live, error}`; POST `notebook/reexecute`
- `problem_statement`: `{text}`
- `log?name&after&limit`: `{name, exists, size, text, reset, next_cursor}`; byte-offset cursor, `reset` true when the file was truncated or replaced (restart from 0)
- `node/{name}/transcripts`: transcript keys for a node. `transcript/{key}`: raw records; `transcript/{key}/events`: normalised events; `transcript/{key}/fragment`: rendered fragment. All three take `after`, `limit` (default 200, max 1000)
- `stream`: live event stream; `POST stop` stops the run (write access)

Error shape: `{"error": "..."}` with 400 (bad query), 404 (unknown run / delegation), 502 (git could not answer).

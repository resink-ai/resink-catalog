---
description: Review the Resink catalog for tables that belong together, duplicates, unclear names and failed imports
---

Use the `resink-catalog` MCP server (tools: `list_tables`, `describe_table`, `sample_table`, `query_sql`,
`list_sources`, `add_tags`, `write_note`) to propose how to tidy the user's catalog. You cannot merge, rename, import
or delete: you find problems, write the plan down, and label tables. The user runs the plan.

1. Inventory: `list_tables` and `list_sources`; note sources with status `error`, `changed`, `missing` or `proposed`.
2. Tables that belong together: same or nearly the same columns, names that differ by a date, number or suffix.
   Confirm with `describe_table` and a `query_sql` check (non-overlapping periods or keys). Tables with a
   `_source_file` column were already grouped by the importer; leave them.
3. Duplicates: same columns and overlapping rows (`select count(*) from a.t1 join a.t2 using (key)`); say which is
   newer or fuller.
4. Unclear names (`export`, `final2`, `col1`): suggest a name from the data and flag it for confirmation.
5. Write the plan as one `write_note` per group, one line per item with its evidence.
6. `add_tags` per table (`sales-series`, `needs-review`, `duplicate`, `import-error`).
7. Report the plan in order of value and say what the user would run for each item.

Rules: propose, never imply anything was changed (only notes and tags were); a merge needs matching schemas and a
data check; do not suggest deleting data the user has not seen; `query_sql` takes a single read-only `SELECT`.

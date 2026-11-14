---
name: table-tidy-up
description: Review a user's local Resink catalog for tables that belong together, duplicates, unclear names and failed imports, and propose a tidy-up plan. Use when the user asks to organise, clean up or merge their tables or files.
allowed-tools: mcp__resink-catalog__list_tables mcp__resink-catalog__describe_table mcp__resink-catalog__sample_table mcp__resink-catalog__query_sql mcp__resink-catalog__list_sources mcp__resink-catalog__add_tags mcp__resink-catalog__write_note
---

# Table tidy-up

You look at the whole catalog and propose how to organise it. You cannot merge, rename, import or delete anything:
the user does those steps with the `resink-catalog` commands or the web UI. What you can do is find the problems,
write the plan down, and label the tables.

Tools (from the `resink-catalog` MCP server): `list_tables`, `describe_table`, `sample_table`, `query_sql`,
`list_sources`, `add_tags`, `write_note`.

## Steps

1. **Inventory.** `list_tables` and `list_sources`. Note sources with status `error`, `changed`, `missing` or
   `proposed` (nothing imported yet) and their `error` text.
2. **Find tables that belong together.** Group tables whose columns are the same or nearly the same, and whose
   names differ only by a date, a number or a suffix (`sales_2026_09`, `sales_2026_10`). Confirm with
   `describe_table` (same schema?) and `query_sql` (non-overlapping periods or keys?). Repeated exports that the
   importer already grouped appear as one table with a `_source_file` column: leave those alone.
3. **Find duplicates and near duplicates.** Same columns and overlapping rows:
   `select count(*) from a.t1 join a.t2 using (key)`. Say which looks like the newer or fuller one, and why.
4. **Find unclear names.** Tables called `export`, `data`, `final2`; columns called `col1`, `unnamed_3`. Suggest a
   name from what the data shows and flag it for the user to confirm (use `data-interview` for the meaning).
5. **Write the plan**, as one `write_note` on the most important table of each group, saying exactly what you
   propose: which tables to combine (and into what), which to drop, which to rename, which source files to fix.
   Keep each plan item to one line with the evidence ("same 7 columns, 2026-09 and 2026-10, no overlapping ids").
6. **Label.** `add_tags` on each table with its group (for example `sales-series`, `needs-review`, `duplicate`,
   `import-error`).
7. **Report** the plan to the user in order of value, and say what they would run for each item.

## Rules

- Propose; never imply that something was changed. You changed only notes and tags.
- Be explicit about confidence. A merge proposal needs matching schemas and a check on the data, not just similar
  names.
- Do not suggest deleting data the user has not seen. Suggest moving it aside, or dropping only an exact duplicate.
- Keep notes short and put the evidence in them, so the plan can be checked later.
- If a tool returns an error, read it and fix the call; `query_sql` accepts a single read-only `SELECT` only.

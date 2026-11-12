# keydelta

Plan a keyed delta between two snapshots of one table, instead of deleting every target row and
inserting every source row. Holds back every key that two rows share, names the rows, and prices
the plan against the delete-all it replaces. It plans only. It never opens a database.

A nightly job keeps a published copy of a road-segment table in step with its source. The job is
the simplest thing that works: delete every row in the copy, insert every row of the source. The
table is registered as versioned, so every row the job touches is written twice into the delta
tables, once as a delete and once as an add. A table of about fifty thousand rows writes about a
hundred thousand delta rows a night to carry perhaps seven rows of real change. The delta tables
grow, a compress has more to do, and nobody can see which seven rows moved. (The counts are
round numbers for the sake of the story.)

The fix everybody reaches for is a keyed diff: touch only the rows that differ. It works until the
first duplicate key. The key column has no unique constraint, and an editor renumbers a segment
and reuses the vacated number for a different feature in the same edit. A join on that key fans
out, the plan inserts one row twice or updates the wrong one, and a segment drops out of the
published copy. Nothing reports an error. The join did what it was told, and the old
delete-all job was immune only because it never compared anything.

The second fix people try is a tolerance of zero on the geometry. A row can differ in the last
bits of a coordinate after a write, with no edit at all. The diff flags it, the job rewrites it,
and tomorrow it is flagged again. The log stops being a signal, because the same rows are in it
every night.

The third trap is a length column that the published copy stores at two decimal places. The
source holds the full float, `101.5000000004`, and the copy holds `101.50`. As text they differ,
so the keyed diff flags every row, every night. Sixty rows read as sixty edits, the change rail
refuses the plan, and the three real edits in the batch are lost among the false ones. A
tolerance hides the noise, but it hides it everywhere: a tolerance of 0.01 also hides a real edit
of 0.006 that would store as a different number.

This tool plans the delta, treats a duplicate key as a fact to report, counts a difference
inside a tolerance as no change, and compares a column at the decimal places you say it stores
(`--scale LEN_FT=2`), and prints which scale it used. A plan that would delete most of the table,
or that shares no key between the two sides, is refused.

```
$ python keydelta.py --self-test
keydelta self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  a JSON null reads as blank and a boolean as true or false
...
PASS  nan and inf are text, never a number to subtract  <-- pinned defect
...
PASS  long integers above 2**53 are subtracted exactly, not as floats  <-- pinned defect
...
PASS  a key two source rows share is held back, with the rows named  <-- pinned defect
PASS  the held key is not inserted, updated or deleted, so the published row stays  <-- pinned defect
...
PASS  a 1e-9 difference is suppressed and listed, not rewritten every night  <-- pinned defect
...
PASS  two populated tables with no shared key are refused by name  <-- pinned defect
...
PASS  held keys beside a change still give a plan but exit 4, and a tripped rail still wins  <-- pinned defect
...
PASS  refused: a truncated first page  <-- pinned defect
PASS  refused: 1e400 read as inf  <-- pinned defect
...
PASS  --ap is refused, not read as a longer flag  <-- pinned defect
...
PASS  --apply never replaces a plan file that exists, and the verdict still prints  <-- pinned defect
...
PASS  a refused plan writes no file even with --apply  <-- pinned defect
...
PASS  a CSV that is not UTF-8 is refused with exit 3, not read as NO CHANGE  <-- pinned defect
PASS  a feature set that is not UTF-8 is refused as bad encoding, not as bad JSON  <-- pinned defect
PASS  a date as epoch milliseconds against text is a refusal that names the column  <-- pinned defect
...
PASS  a CSV that starts with a blank line is refused with exit 3 and the message says so  <-- pinned defect
PASS  a UTF-16 CSV is refused with exit 3  <-- pinned defect
PASS  a header-only target is refused by the change rail without --force-large  <-- pinned defect
...
PASS  an unquoted comma is refused with exit 3, not read as NO CHANGE  <-- pinned defect
PASS  a repeated CSV header is refused, not read as NO CHANGE  <-- pinned defect
...
PASS  --key @geometry on a CSV is exit 64, not a crash  <-- pinned defect
...
PASS  an edit of 0.006 that stores as 10.01 is a change at scale 2, though a tolerance of 0.01 would hide it  <-- pinned defect
...
PASS  without --scale every float reads as an edit and the change rail refuses  <-- pinned defect
...
PASS  --scale on a column the files do not hold is exit 64, not a silent no-op  <-- pinned defect
...
PASS  an unexpected exception exits 70, not the 1 that means a rail refused  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the exception it is looking for
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
268 assertions, 0 failed
```

The full run prints all 268 assertions. This block is a selection of its lines in run order, and each `...` line marks where lines of the run are left out.

## What already exists

Diffing two tables on a key is a solved problem, and these do it well.

- [csvdiff](https://github.com/aswinkarthik/csvdiff) compares two CSV files and reports
  additions, modifications and deletions. It takes a compound key, writes several formats
  including JSON, and is MIT licensed. It is the right tool when you want the diff of two CSVs
  and your keys are unique.
- [daff](https://github.com/paulfitz/daff) is a tabular diff with an `--id` flag that can be
  repeated for a multi-column key, and several output formats.
- ArcGIS Pro's [Append](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/append.html)
  tool can insert new records and update existing ones in one operation, matched on fields you
  name. Its Skip Options limit it to inserts or to updates. That is an upsert, and it writes.
- ArcGIS Pro's [Table Compare](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/table-compare.html)
  tool sorts both tables on a field you name and, by default, stops at the first difference. Its
  page says the compare tools do not try to find the next matching records after an added or
  deleted record. It has an Attribute Tolerance parameter that sets, per numeric field, "the
  range in which attribute values are considered equal". The page
  does not mention precision, scale or decimal places, so rounding to a stored scale is not
  what it offers.

I did not run csvdiff or daff for this README. Their pages say nothing about a key that two rows
share, so I make no claim about what they do with one. I also found no page that describes pricing
the diff against the delete-all, listing the rows a duplicate key held back, or comparing at a
declared decimal scale. This tool does those three things. Table Compare already has a tolerance,
so the tolerance is not new here. A search of GitHub
for the name `keydelta` on the day this was written found this repository and one unrelated
one, a set of buttons, so the name is not taken by another tool for this.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Each snapshot is a CSV, or a saved feature set (`.json`) from a layer query.

The self-test prints the same 268 assertions on Windows (Python 3.13.2 and 3.9.25) and on
Ubuntu (Python 3.12.3 and 3.9.25). The four outputs are identical line for line once the Windows
carriage returns are removed (the same MD5 of the output on all four).
`coverage run --branch keydelta.py --self-test` reports 100 percent of 1031 statements and 210
branches.

```
git clone https://github.com/uhsear/keydelta.git
```

## Usage

SOURCE is what the table should become. TARGET is what it is now. Name the key column, or
several for a composite key.

```
python keydelta.py --self-test
python keydelta.py source.csv target.csv --key SEG_ID
python keydelta.py source.csv target.csv --key CITY --key SEG_NO --ignore EDITED_ON
python keydelta.py source.csv target.csv --key SEG_ID --tolerance 0.01
python keydelta.py source.csv target.csv --key SEG_ID --scale LEN_FT=2
python keydelta.py source.csv target.csv --key SEG_ID --out plan.json --apply
python keydelta.py response.json target.csv --key SEG_ID
```

| Flag | Default | What it does |
|---|---|---|
| `SOURCE` | none | What the table should become. A CSV, or a `.json` feature set. |
| `TARGET` | none | What the table is now. A CSV, or a `.json` feature set. |
| `--key` | none | A key column, on both sides. Required. Repeat it for a composite key. |
| `--ignore` | none | A column to leave out of the comparison, such as an edit timestamp. Repeatable. |
| `--tolerance` | `0` | Two numbers within this are equal. At `0` the comparison is exact text. |
| `--scale` | none | `COLUMN=N`: the column stores N decimal places (0 to 38). Two numbers that round to the same value at N places are no change. It replaces `--tolerance` for that column. Repeatable, one per column. |
| `--min-rows` | `1` | Refuse a source with fewer rows than this. |
| `--max-change` | `0.10` | Refuse more inserts, updates and deletes than this fraction of the target. It also caps the rows under held-back keys, as a fraction of the rows read from both files. |
| `--max-delete` | `0.02` | Refuse more deletes than this fraction of the target. |
| `--max-duplicates` | `50` | Refuse more held-back duplicate keys than this. |
| `--force-large` | off | Lift the change, delete and no-shared-key rails. Not the two duplicate rails. |
| `--sample` | `10` | How many keys to list under each heading. `0` lists none. |
| `--out` | off | Path for the plan file, a new JSON file. Needs `--apply` to be written. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Other known flags are ignored. An unknown flag, or a known flag with a malformed value such as `--tolerance abc`, exits 64 before any assertion runs. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, and `--tol` is refused instead of being read as
`--tolerance`. The self-test pins twelve prefixes, and one run through `main` that confirms
`--ap` writes no file.

**Geometry.** A saved feature set that carries a `geometry` for any feature gets one extra
compared column, `@geometry`, holding the shape as JSON. A moved shape with the same attributes
is an `update` of `@geometry`. `--tolerance` applies to every coordinate. A CSV has no such
column, so comparing a feature set with geometry against a CSV exits 64 with `Only on one side:
@geometry` until you add `--ignore @geometry`. Two feature sets that differ only in a shape:

```
$ python keydelta.py g1.json g2.json --key ID --max-change 1
...
updates:
  1  columns @geometry

VERDICT: PLAN READY, 1 update(s), 0 insert(s), 0 delete(s), 0 held back.
```

With `--tolerance 0.6`, a move of 0.5 in x is noise and the verdict is `NO CHANGE`.

`--out` must name a new file. A path that is also an input is refused with exit 64, on a dry run
too. A path that already exists is refused at write time with exit 2, because the file is opened
for exclusive creation. A refused plan writes nothing even with `--apply`.

## What it plans

For each key, the tool looks at the rows each side holds under it:

| Case | Plan |
|---|---|
| One row on each side, all compared columns equal | no change |
| One row on each side, a column differs beyond the tolerance | `update`, naming the columns |
| One row on each side, every difference is inside the tolerance | no change, counted as noise |
| One row in the source, none in the target | `insert` |
| One row in the target, none in the source | `delete` |
| More than one row on either side, the same rows on both | no change |
| More than one row on either side, anything else | `held back`, left as published |

A held-back key is never inserted, updated or deleted. The plan says nothing about it, so the row
that is published stays exactly as it is. The report names the key and the rows each side holds
under it, by line number in a CSV or by feature number in a feature set, so a person can fix the
renumbering. Falling back to a full rebuild whenever a duplicate appears would bring the
delete-all back on those nights and hide the editors' habit that caused it.

The demo tables are synthetic. `source.csv` has 61 rows: 60 segments, plus a second row for
`S21`. `target.csv` has 60 rows. In the target, `S07` has a different name, `S13` has a length
that differs by a rounding error of about 1e-12, `S60` is missing, and `S99` is extra.

```
$ python keydelta.py source.csv target.csv --key SEG_ID --ignore EDITED_ON
keydelta: source.csv 61 row(s), target.csv 60 row(s)
key: SEG_ID. compared columns: 2
rows planned:
  unchanged    56
  update       2
  insert       1
  delete       1
  held back    1 (duplicate key, left as published)
  identical duplicate keys, no change    0
  noise inside tolerance, no change      0
delta-table rows, traditional versioning: keyed plan 6, delete-all plus insert-all 121

held back, a person decides:
  S21  source line 22, line 62, target line 22

updates:
  S07  columns NAME
  S13  columns LEN_FT

inserts:
  S60

deletes:
  S99

VERDICT: PLAN READY, 2 update(s), 1 insert(s), 1 delete(s), 1 held back.
```

`S13` is an update because the tolerance is 0. With `--tolerance 0.01` it is noise:

```
$ python keydelta.py source.csv target.csv --key SEG_ID --ignore EDITED_ON --tolerance 0.01
...
  unchanged    57
  update       1
...
  noise inside tolerance, no change      1
delta-table rows, traditional versioning: keyed plan 4, delete-all plus insert-all 121
...
VERDICT: PLAN READY, 1 update(s), 1 insert(s), 1 delete(s), 1 held back.
```

Without `--ignore EDITED_ON` every row differs, because the source was edited a day later, and
the change rail refuses the run. A column that changes on every write belongs in `--ignore`.

**The plan file.** `--out plan.json --apply` writes the plan as JSON: the key and compared
columns, both row counts, both delta costs, the inserts and deletes as key lists, each update
with its changed columns, each held-back key with its rows, the `scale` map that was used and
the `rounded` keys. Nothing else is written. Keys in
the file are lists, one entry per key column, so a composite key stays whole.

```
$ python keydelta.py source.csv target.csv --key SEG_ID --ignore EDITED_ON --tolerance 0.01 --out plan.json --apply
...
wrote plan.json

VERDICT: PLAN READY, 1 update(s), 1 insert(s), 1 delete(s), 1 held back.
```

**What the delta cost line means.** Esri's versioning vocabulary says the adds and deletes tables
are the delta tables. For traditional versioning the adds table stores the records inserted or
updated, the deletes table records the deletions, and "an update is the equivalent of deleting the
record as it previously existed and adding the altered record"
([Versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)).
So the keyed plan costs one row per insert, one per delete and two per update. A delete-all plus
insert-all costs one deletes row per target row and one adds row per source row. The line prices
both. It is an estimate from that rule, not a measurement of any table.

## Storage precision

A column that the published copy stores at fixed decimals cannot hold the full float of the
source. `--scale LEN_FT=2` tells the tool that LEN_FT stores two decimal places. For that column
two numbers are equal when they round to the same value at two places. The tool rounds the text as
written, half away from zero, using Python's `decimal` module, never a binary float.

The demo tables are synthetic. `source.csv` and `target.csv` hold 60 segments each. The source
holds LEN_FT as a full float, such as `101.5000000004`, and the target holds `101.50`. The
differences, all of them seeded: `S07` has a new name in the source, `S21` is `121.634` against a
stored `121.50`, `S30` is `130.504` against `130.50`, and `S31` is `131.506` against `131.50`.
Without `--scale`:

```
$ python keydelta.py source.csv target.csv --key SEG_ID --ignore EDITED_ON
keydelta: source.csv 60 row(s), target.csv 60 row(s)
key: SEG_ID. compared columns: 2
rows planned:
  unchanged    0
  update       60
...
delta-table rows, traditional versioning: keyed plan 120, delete-all plus insert-all 120

updates:
  S01  columns LEN_FT
...
REFUSED   60 changed rows exceeds the ceiling of 6 (10% of 60 target rows). That is more likely a bad read than real editing. Column(s) LEN_FT differ on every updated row. The same value written two ways reads this way, such as text against a number, or a full-precision number against its stored rounding. Export both sides the same way, add --scale COLUMN=PLACES for a number, or add --ignore. Rerun with --force-large if it is intended.

VERDICT: REFUSED, 1 rail(s) tripped. Do not apply this plan.
```

With the scale declared, the plan says which precision it used, and carries only the real edits:

```
$ python keydelta.py source.csv target.csv --key SEG_ID --ignore EDITED_ON --scale LEN_FT=2
keydelta: source.csv 60 row(s), target.csv 60 row(s)
key: SEG_ID. compared columns: 2
storage scale used, decimal places, half away from zero: LEN_FT=2
rows planned:
  unchanged    57
  update       3
  insert       0
  delete       0
  held back    0 (duplicate key, left as published)
  identical duplicate keys, no change    0
  noise inside tolerance, no change      0
  same at storage scale, no change       57
delta-table rows, traditional versioning: keyed plan 6, delete-all plus insert-all 120

updates:
  S07  columns NAME
  S21  columns LEN_FT
  S31  columns LEN_FT

same at storage scale, not rewritten:
  S01
...
VERDICT: PLAN READY, 3 update(s), 0 insert(s), 0 delete(s), 0 held back.
```

`S30` is not an update, because `130.504` stores as `130.50`. `S31` is one, because `131.506`
stores as `131.51`. A tolerance of 0.01 cannot tell these two apart. Run on the same files with
`--ignore EDITED_ON --tolerance 0.01 --max-change 0.5` it plans two updates, `S07` and `S21`, and hides `S31`:
a real edit that would have changed the stored value.

The rules, each pinned in the self-test:

- A scale replaces `--tolerance` for its column. Other columns keep the tolerance.
- A cell that is not a plain number on both sides is compared as text. A code with a leading
  zero, `nan`, `inf` and an empty cell are never rounded.
- `--scale` is a usage error, exit 64, for a column that is not compared (missing from a file, or
  the key, or ignored, or `@geometry`), for a repeated column, and for a value that is not a whole
  number from 0 to 38. A scale on a column that is not in the files is never a silent no-op.
- A JSON float from a feature set and the value stored in a CSV compare the same way.
- The single-row keys that matched only after rounding are listed in the report, counted, and written to the
  plan file with the scale map. The report prints the scale line only when `--scale` is used.

What the Esri and Microsoft pages say, and what the tool relies on:

- SQL Server: a `decimal(p,s)` column's scale is "the number of decimal digits that are stored to
  the right of the decimal point", from 0 through the precision, and the maximum precision is 38
  ([decimal and numeric](https://learn.microsoft.com/en-us/sql/t-sql/data-types/decimal-and-numeric-transact-sql)).
  That is where the 0 to 38 range comes from.
- The same page says "By default, SQL Server uses rounding when converting a number to a decimal
  or numeric value with a lower precision and scale". It rounds by default. The page does not describe other settings, so I make no claim about them. A
  truncating column would need a different rule, and the tool has none.
- That page does not say how a tie is broken on a conversion. The `ROUND` function's page does:
  "`ROUND` breaks ties by rounding half away from zero"
  ([ROUND](https://learn.microsoft.com/en-us/sql/t-sql/functions/round-transact-sql)).
  The tool uses that rule for ties, and I did not test it against a SQL Server instance. If your
  database breaks a tie another way, a value that is exactly halfway can read as an edit or as no
  change wrongly.
- Esri: the Add Field tool's Field Scale is "the number of decimal places stored in a field", and
  "only applicable to fields of type float or double". "If the input table is in a file
  geodatabase, the field scale value will be ignored"
  ([Add Field](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-field.html)).
  So a scale you read off a file geodatabase field is not a storage fact. It is a rule you choose.

## Refusals

The tool refuses before it plans, and it refuses a plan it has made.

**Input refusals, exit 3.** Each prints `REFUSED` and `VERDICT: REFUSED`.

- A blank key, or a key that is only spaces, on either side. For a composite key, one blank
  part is enough. A row with no key would be inserted and deleted on every run. The first five
  lines are named.
- A CSV that repeats a column name. Only the last column of that name would be read, so a change
  in the earlier one would show as `NO CHANGE`. The file is refused and the names are listed.
- A file with a byte that is not valid UTF-8. Replacing the byte would turn two different accented
  letters into the same character, so a real edit between them would show as `NO CHANGE`. The
  file is refused by name, exit 3.
- A CSV row whose field count differs from the header. A cell beyond the header would be dropped, so
  a real edit in it would show as `NO CHANGE`. The row is refused with its line number, exit 3.
- A source with fewer rows than `--min-rows`. An empty source plans to delete the target.
- A feature set whose `exceededTransferLimit` is `true`. Esri's query documentation shows a
  paged example response with `"exceededTransferLimit": true` next to a `resultPaginationToken`.
  It says paging continues until the response returns `exceededTransferLimit: false`, which signals
  that there are no more results to fetch
  ([Query](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)).
  So a `true` marks a response with more results still to fetch. The page does not discuss a saved
  file, so applying that to one is my reading. A plan from a partial file would delete every row it
  did not return, so the tool refuses it.
- A JSON number too large for a binary float, such as `1e400` or a float of 5000 digits. Python
  reads it as `inf`, and two different huge values would then print as the same text and show as
  `NO CHANGE`. The file is refused, exit 3.
- A JSON file that is not valid JSON, is not an object with a `features` list, has an `error`
  object, or has a feature with no `attributes`. The documentation shows failed requests as an
  `error` object with a code and a message. I did not establish the HTTP status that carries it,
  so the tool does not depend on it.

```
$ python keydelta.py page1.json target.csv --key SEG_ID --ignore EDITED_ON
REFUSED   page1.json has exceededTransferLimit true, so it holds only the first page. Planning from a partial source would delete every row it did not return.

VERDICT: REFUSED
```

**Rail refusals, exit 1.** The plan is made and printed, then refused. Nothing is written.

- More deletes than `--max-delete` of the target (default 2 percent).
- More inserts, updates and deletes together than `--max-change` of the target (default 10
  percent).
- Two populated tables that share no key. This is the shape of a delete-all in disguise, and it
  usually means the key is spelled differently on the two sides. Lower-casing the key in the
  target of the demo above gives:

```
REFUSED   no key is on both sides, so every target row would be deleted and every source row inserted. That is the delete-all this tool exists to avoid. The key columns probably spell the key differently on the two sides.

REFUSED   60 deletes exceeds the ceiling of 1 (2% of 60 target rows). ...

REFUSED   119 changed rows exceeds the ceiling of 6 (10% of 60 target rows). ...

VERDICT: REFUSED, 3 rail(s) tripped. Do not apply this plan.
```

- More held-back duplicate keys than `--max-duplicates` (default 50). A few is editors renumbering.
  Dozens means the key is not a key, and holding that many rows back would leave the published
  table stale. `--force-large` does not lift this one.
- More rows under held-back keys than `--max-change` of the rows read from both files. The key
  is probably not a key, and the plan would compare few of the rows. `--force-large` does not
  lift this one either. On a very small table one duplicate key can trip it: 10 source rows
  against 9 target rows with one duplicate key is refused, with `--force-large` too, as `3 rows
  sit under the held-back duplicate keys, above the ceiling of 1`. Raise `--max-change`.

`--force-large` lifts the delete, change and no-shared-key rails together. Use it for a first
load into an empty target, which has no row to divide by, and for a bulk load you meant to make.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A plan was made, no rail tripped and no key was held back. A plan with no change is also 0. |
| 1 | A rail refused the plan. |
| 2 | A file could not be read or written. The verdict still prints when the write failed. |
| 3 | An input was refused: blank key, repeated column name, a CSV row whose field count differs from the header, too few source rows (a source with no rows too), a JSON number too large for a binary float, truncated or error response. |
| 4 | A duplicate key was held back. With nothing else to write the verdict is `HELD BACK`. With other changes it is `PLAN READY`, and the plan file is still written under `--apply`. The published copy is not known to match. |
| 64 | Usage error, including a bad column name, a bad or unmatched `--scale`, a flag prefix, two files with different columns and a target file with no columns. |
| 70 | An unexpected crash in the tool. Never a finding about the data. |

Exit 4 is the held-only case. Two tables of which the only difference is one key that the source
holds twice:

```
$ python keydelta.py s2.csv t2.csv --key ID --max-change 1
...
held back, a person decides:
  2  source line 3, line 4, target line 3

VERDICT: HELD BACK, 1 duplicate key(s) left as published, a person decides. Nothing else to write.
```

A plan with changes and held-back keys, like the demo above, prints `PLAN READY` and exits 4, so a
scheduled job that reads only the exit code does not take the held rows for in step.

## Limits

- With `--apply` and a plan that has no change, the tool prints `wrote <file>` and then
  `VERDICT: NO CHANGE, nothing to write.`. The no-change plan file is still written. `nothing to
  write` means nothing for a job to apply.
- It plans and never applies. The plan file is for a person or a separate, reviewed job to carry
  out. There is no `--fix`, no SQL, no connection to a geodatabase and no edit session.
- Keys are compared verbatim. Case, leading zeros, padding and punctuation are not normalised, and
  the spaces inside a key count. Only a key with a blank part is refused. If the two sides
  spell the key differently, the no-shared-key rail catches the whole-table case, and a partial
  mismatch shows up as inserts and deletes.
- Cells are compared as text. `1.0` and `1` differ unless `--tolerance` is above 0. A NULL and an
  empty string both read as blank, so a change between them is not seen. A JSON number is
  written with Python's `str` or `repr`, so `5.0` or `1e16` in a feature set against `5` or
  `10000000000000000` in a CSV is an `update` unless `--tolerance` is above 0. That errs on the
  safe side.
- The tolerance applies to every column that holds a finite number on both sides, not to named
  columns. `nan` and `inf` are text. The limit is inclusive in exact arithmetic but not in binary
  floating point: `1` against `1.01` at tolerance `0.01` is not equal, because the difference is
  0.010000000000000009. Set the tolerance a little above the noise you mean to hide.
- A `--scale` is a fact you declare. The tool cannot read it from the data or the database. A
  scale lower than the column really stores hides real edits below that place, as an edit of 0.004
  disappears at scale 2. A scale higher than the column stores leaves the rounding noise in. Take
  it from the field definition, such as the `s` of a SQL Server `decimal(p,s)`.
- `--scale` rounds the number as the text writes it. A JSON float is written with Python's
  shortest `repr`, so `1.005` is read as the text `1.005` and rounds to `1.01`, though the binary
  float nearest to 1.005 is a hair below it. What a database does with that float on conversion is
  not described by the pages I read. A value that lands exactly on a tie can disagree with the
  database. That affects only values that end in a 5 at the place after the scale.
- `--scale` compares a column when both cells are plain numbers. It does not model a `decimal`
  column's precision limit, so a value too large for `p` digits is not flagged. It does not model
  truncation either.
- One `--tolerance` applies to every numeric column and every coordinate, in the units of each.
  In degrees, `0.01` hides a move of about 1 km as noise. The same value applies to numbers inside
  the geometry JSON, such as a `wkid`, and to code columns that look like numbers, so keep it well
  below 1. A shape exported as one text column, such as WKT, is compared as text. The tolerance
  does not test shape equality, and a buffer-containment test is not part of this.
- A duplicated key is judged by its set of rows, matched one to one in order of appearance with the
  tolerance. It is greedy, so with a tolerance larger than the spacing of your values, a pairing
  can come out differently from the best one. That can hold back (exit 4) a group whose rows
  do match, instead of counting it as identical. The error is conservative: it never reports a
  false clean. Keep the tolerance small.
- The report lists up to `--sample` keys per heading. The plan file lists all of them.
- The delta cost rule is the one for traditional versioning in the default version. It assumes
  every touched row is a base-table row, and it does not model several edits to one row in one
  state. Branch versioning is not what the line describes. The cost is an estimate from the rule
  and is not read from any table.
- The small-table effect: the ceilings are fractions of the target. On a target of 41 rows 2
  percent is 0 rows, so one delete is refused. Raise `--max-delete` for a small table.
- The source rows must be complete. The tool can see a truncated feature set only through
  `exceededTransferLimit`. A response cut at the maximum record count by a server that sets no
  such flag looks complete, so compare its row count with the layer's record count first. A CSV cut off by a failed export looks like a smaller table, and the
  delete rail is what stops it.
- Only UTF-8 input is supported. A CSV or feature set with a byte that is not valid UTF-8, such as
  a CSV that Excel saved in cp1252, is refused with exit 3 and the file name, so two different
  accented letters cannot both turn into one replacement character and hide an edit. Save it as
  UTF-8 first. A CSV row whose field count differs from the header, such as an unquoted comma, is
  refused with exit 3. A UTF-8 byte order mark is removed. A cp1252 file whose bytes happen to be
  valid UTF-8 cannot be told apart, which is rare.
- Excel often leaves trailing rows of only commas. Such a row has a blank key, so the whole file is
  refused with exit 3 instead of the row being skipped.
- Keys that differ only by Unicode normalisation form, composed against decomposed accents, are
  different keys. They show as an insert plus a delete.
- A date is epoch milliseconds in a saved feature set and formatted text in a CSV export, so a
  JSON against CSV run reads every date as an edit. The change rail refuses it and the message
  names the column that differs on every updated row. Export both sides the same way or use
  `--ignore` on that column. With `--force-large` the plan lists every such row as an update.
- `--scale` on a column whose cells are text on both sides is accepted and does nothing, with no
  warning. A non-number cell is compared as text.
- A zero-byte target file exits 64 as a columns-not-in-the-file error, not as an empty table to
  insert into. A zero-byte source exits 3, because the source has 0 rows.
- An empty feature set as the target (`features: []`) exits 64 with a columns-not-in-the-file
  error, because there is no header to check the key against. A header-only CSV target is a
  target of 0 rows, so the change rail refuses a first load, and `--force-large` plans it.
- A JSON integer of more than 4300 digits is reported as not valid JSON and exits 3, because
  Python limits the digits of an integer it converts from text. A JSON float of that size, or
  one above about 1.8e308, is refused as too large for a binary float, also exit 3.
- A CSV that starts with a blank line is refused with exit 3 and the message names the blank
  line. A UTF-16 CSV or feature set is refused with exit 3 as not UTF-8. A semicolon CSV exits 64,
  because the key is not a column of the one-column header it reads. All are refused cleanly.
- The parser takes two optional positional files. Whether `SOURCE --key K TARGET` (an option
  between the files) is accepted depends on the Python build. Python 3.9.25 and Linux 3.12.3 exit
  64 with `unrecognized arguments`, while Windows 3.12.10 and 3.13.2 accept it. Put both files
  first.
- A group of N rows under one key is compared in time that grows with N squared. With a
  low-cardinality column as `--key` and the two files in different orders, 4000 rows under one
  key took about 4 seconds here, and tens of thousands would run for minutes before any rail can
  fire. Use a real key.
- A JSON file nested hundreds of thousands of levels deep raises `RecursionError`, and a huge
  integer in a shape can raise `OverflowError`. Both exit 70, a fault in the tool, not a finding.
- A run without `--scale` prints no precision line. It compares cells as exact text, or within
  `--tolerance`.
- A closed stdout pipe can end the run with exit 70 on some hosts; read the exit code, not the
  report. A `| head -1` in Git Bash on Windows 3.13.2 gave exit 4 with no traceback, because the
  output was small.
- An unclosed CSV quote is reported by the `csv` module. It exits 2 with an error line and no
  `VERDICT` line, while other malformed CSV rows exit 3. A NUL byte depends on the interpreter.
  Python 3.9.25 raises `line contains NUL` and the run exits 2. Python 3.12 and 3.13 read the
  NUL as ordinary data, so the run compares it and ends with a normal `VERDICT`. I measured
  3.9.25, 3.12.3 and 3.13.2 only, and I did not find the version that changed this.
- A JSON `NaN` or `Infinity` literal is read as the text `nan` or `inf`, so it compares equal to
  the same text in a CSV. Esri does not emit these literals.
- A blank line between CSV data rows is skipped without a refusal.
- Exit 1 means a rail refused the plan in a normal run, and it means a failed assertion under
  `--self-test`. A job that runs both should read the mode, not only the code.
- A geometry that holds an integer above about 1e308 raises `OverflowError` and exits 70 only when
  `--tolerance` is above 0, because exact text comparison never reaches the numeric path.
- A key that holds a control character or a terminal escape sequence prints to the console
  unchanged. The exit code and the plan file are not affected, so read the exit code.
- A JSON number with more than 17 significant digits is parsed to a binary float before `--scale`
  rounds it or the comparison runs, so digits past the float's precision are lost. For example,
  `2.6749999999999999999` and the 17-digit `2.6749999999999998` are both read as 2.675 and round to
  2.68 at scale 2, where the decimal text rounds to 2.67. I found no Esri page that says how many
  digits a service writes, so do not assume this affects only hand-edited JSON.
- Only the JSON boolean `true` in `exceededTransferLimit` refuses a file. The string `"true"` is ignored.
- A repeated attribute name inside one JSON feature keeps the last value, with no warning.
- The `--apply` write path was verified by the self-test assertions and again by live command
  lines: a write-flag prefix such as `--ap` or `--apply=1` exits 64 and writes nothing, and a
  second `--apply` onto an existing plan exits 2 and leaves the first plan unchanged.
- The module docstring's exit-3 list is shorter than the one above. It leaves out a repeated CSV
  column name and a JSON number too large for a binary float.
- A JSON integer of more than 4300 digits exits 3 with Python's own message, which suggests
  `sys.set_int_max_str_digits()`. That is a Python call, not something a command line can do.
- A CSV field of more than 131072 characters exits 2 with `field larger than field limit
  (131072)`. The message does not name the file.
- An unexpected `ValueError` raised inside the comparison would be reported as exit 64, a usage
  error, instead of exit 70.
- The change ceiling is `int(rows * 0.10)`, so a target of fewer than 10 rows allows no change and
  refuses a single edit with exit 1. Raise `--max-change` for a table that small.
- A number with an exponent of 19 digits or more, such as `1e-9999999999999999999`, is beyond the
  range of the decimal context that `--scale` rounds in. It is compared as text, so two such
  numbers that differ show as an update. An exponent of 18 digits rounds normally.
- A dry run does not test whether `--out` can be created. A path in a missing directory shows its
  error only under `--apply`, with exit 2.
- Line endings inside a quoted field are compared as written, so CRLF against LF is an update.
- A refusal with exit 3 prints on stdout. Usage and file errors, exit 2 and 64, print on stderr.
- If stdout is a closed pipe, such as a report piped to `head`, a `BrokenPipeError` can end the
  run with exit 70. The exit code then reflects the pipe, not the data.
- The report prints keys as they are and joins a composite key with `|`. A key that holds `|`
  can make two keys print alike, and a key with a newline can print a line such as
  `VERDICT: NO CHANGE` above the real verdict. The plan file keeps each key as a list. Read the
  exit code, not one line of the report.
- A column value padded with spaces, such as ` 5.25`, or written with a thousands separator, such
  as `1,000.50`, is not a plain number. `--scale` compares it as text and plans an update, which
  errs on the safe side.
- A duplicate-key group that matches only after rounding counts under "identical duplicate keys,
  no change", not under "same at storage scale". The rounded count and the `rounded` list in the
  plan file hold single-row keys only.
- `--scale` rounds both sides. A target that holds more decimals than the scale you declare is
  rounded too, so an edit below that place stays hidden.
- Keys and column names print to stdout and go to the plan file as they appear in the data. Cell
  values are never printed or written. The tool reads no credential and sends no request, but a
  key column that holds sensitive text puts that text in the report and the plan. The plan file is created with the process's default permissions, so anyone who can read it can read every key. Restrict the folder if the keys are sensitive.
- The Query page's paging example pairs `true` with a pagination token and says paging ends at
  `false`. It does not say what a `true` means in a file you saved. Treating such a file as the
  first page only is my reading of it.
- Build notes only: the 300,000-row timing and the 4000-rows-under-one-key figure are one-off measurements that the
  self-test does not run. A later run took 4.6 seconds for 300,000 rows a side and about 4
  seconds for 4000 rows under one key. A third audit run took about 2 seconds for the 4000-row
  case and 3.7 seconds for the 300,000-row case, so treat all of them as rough.
- Build notes only: the mutation figures below cannot be reproduced from this repository, because no harness ships.
  They were measured before `--scale` existed, so the `--scale` code has no mutation figure. Treat
  them as an old one-off.
- The Esri documentation shows a failed request as an `error` object with a code and a message,
  but I could not find that on the Query page or the feature service error codes page. The tool
  does not depend on it.
- Esri's Query page shows `true` with a pagination token and ends paging at `false`. A `true` can
  also mean the result was cut at the service's maximum record count without paging. Either way
  the file is partial, so the refusal is right, but applying the page to a saved file is my reading.
- A `--tolerance` run prints only the count of rows it hid as noise, not the tolerance value, so
  the report does not state the tolerance used. Keep the value in your own run log.
- A scheduled job that reuses one `--out` path exits 2 on its second `--apply` run, because the
  plan file is created exclusively. Give each `--apply` run a new `--out` path.
- A traceback printed on an exit 70 crash holds the local script path and the input paths as
  typed. The tool reads no credential, but the exception message can echo a key or cell text from the input. Strip the paths and any such text before pasting it into a public issue.
- On a console that cannot show a character, two keys that differ only in such characters print
  alike, both with a `?`. The plan file keeps the exact keys, so read the plan file.
- The demo tables in this README are not shipped. The self-test builds the same data, so the demo
  output can be reproduced only through it, not by running the four files on separate tables.
- The GitHub name search under "What already exists" has no recorded date. Repeat it before you
  rely on it.
- The tool picks the input format from the file name alone. A path ending in `.json`, in any
  case, is read as a feature set and every other path as a CSV. A feature set saved as
  `response.txt` is read as CSV and ends in a column or field-count error (exit 64 or 3), never a
  false clean. A CSV named `x.json` is refused as invalid JSON (exit 3).
- A killed or interrupted `--self-test` run can leave a `keydelta-selftest` directory in the
  system temporary directory, because cleanup runs in a `finally` block that a killed process
  never reaches.
- Under `--scale`, a number beyond about 1.8e308, or one with a 19-digit exponent, is compared as
  text, so two such values that round to the same stored value still read as an update.
- Under `--tolerance`, two integer texts are subtracted exactly, so a 17 to 19 digit ID that
  differs by 1 is an update. An integer against a decimal text, such as `12345678901234568` against
  `12345678901234567.0`, still goes through a binary float and can read as noise.
- Branch coverage was measured on Windows with Python 3.13.2. It was not measured on Linux,
  where `coverage` is not installed.
- A plan with changes and held-back keys prints `PLAN READY` and exits 4. The plan is ready for
  the other rows, and the held rows stay stale in the published copy. A job that applies the plan
  should treat 4 as "apply, then send a person to the held keys".
- When the plan file cannot be written, the exit code is 2 but the output still ends with
  `VERDICT: PLAN READY`. Trust the exit code.
- Rail ceilings use `int(rows * fraction)` in floating point, so `--max-change 0.29` on 100 rows
  gives 28, not 29. That errs on the strict side.
- The `--out` same-file check uses the real path, which resolves symbolic links. A hard link to an
  input is not detected. Exclusive creation still means an existing path is never overwritten.
- The self-test writes its temporary files under the system temporary directory and removes
  them on exit. Only `--apply` writes a plan file for a real run.
- A character the console cannot show prints as `?` instead of stopping the run. The plan file is
  always UTF-8.
- It reads both files into memory and has no streaming mode. Build note only: one run of 300,000 synthetic rows a
  side, three columns each, took 3.5 seconds on a Windows workstation and 2.6 seconds on an
  Ubuntu server. A later run on the same workstation took between 4.5 and 4.6 seconds. The
  self-test does not run this, and the figures are measurements, not a promise. Memory was not
  measured. Your tables will differ.
- A key that starts with `=`, `+`, `-` or `@` is written to the plan as it came. The plan is JSON,
  but a report pasted into a spreadsheet may read such a key as a formula.
- The plan file is written in place with exclusive creation, not through a temporary file. A
  failed write can leave a partial file, and the exit code is 2.
- No line is unreachable offline: coverage is 100 percent of lines and branches (1031 statements,
  210 branches). The tool opens no
  socket and imports no network module, so it has no network path to stub and no credential in it
  to leak. The self-test asserts that its own source imports no network module.
- Build note only: a mutation sweep of 519 mutants, run on the revision before the last two fixes, of the code outside the self-test (flipped comparisons, `and`
  and `or` swaps, forced branches, dropped statements, constants plus one) left 4 alive. They are
  the `if __name__ == "__main__"` guard and `return _run(argv)` in `main`, each of which makes the
  script do nothing so the self-test never starts. The self-test kills the other 515.
  The sweep was not re-run after `--scale` was added, so the new code has the 100 percent
  branch coverage and its own assertions, but no mutation figure.
- The self-test counts the same on all four interpreters. The cp1252 console assertion builds its
  own stream in memory, so it gives the same answer on a host whose console is UTF-8.
- I read the Esri and Microsoft pages above for the behaviours this README relies on. I did not
  test the tool against a live geodatabase or a SQL Server instance, and the cost line was not
  compared with real delta-table counts. The scale rounding is checked against the rule on the
  pages, not against a database that holds a rounded column.
- A scale written with a leading zero, such as `V=02`, is read as the plain number 2.
- The documentation quotes in this README were checked against saved copies of the Esri and Microsoft pages and may have changed since. The statement that the Table Compare page does not mention precision, scale or decimal places was checked the same way, not on a live fetch.
- A dry run whose `--out` names a file that already exists prints `Re-run with --apply`, but the `--apply` run then exits 2, because the plan file is created exclusively. A dry run does not test `--out`.
- Name an ordinary new file in `--out`. A reserved Windows device name such as `NUL` or `CON` was not tested.
- A feature set whose `error` value is a string, not an object, is not refused as an error response if it also has a `features` list. Only an `error` object is refused.
- SOURCE and TARGET may name the same file with no warning. The run reports NO CHANGE.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT. See `LICENSE`.

## Related

Other single-file tools in this portfolio that pair with this one:

- [nalmatch](https://github.com/uhsear/nalmatch) - reconcile two tables on an identifier and refuse
  an identifier rule that would merge two records into one key. Run it first when the two sides
  spell the key differently; this tool normalises nothing.
- [idreuse](https://github.com/uhsear/idreuse) - the same hazard from the other side. It refuses a
  shared ID that is not unique and refuses every match that the geometry disproves, by measuring
  how far apart the two rows of each pair are.
- [safe-republish](https://github.com/uhsear/safe-republish) - refuse to truncate a feature class
  when its staged replacement fails a plausibility check. Use it where the replacement is still a
  full reload.
- [fcload](https://github.com/uhsear/fcload) - load a dataset into a geodatabase while refusing the
  imports that corrupt silently. It is the writer that a reviewed plan from this tool would need.
- [replicakeep](https://github.com/uhsear/replicakeep) - plan, and under `--apply` repair, an idle
  replica that holds a compress floor. It is the other half of the delta-table story.

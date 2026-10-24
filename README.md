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

This tool plans the delta, treats a duplicate key as a fact to report, and counts a difference
inside a tolerance as no change. A plan that would delete most of the table, or that shares no
key between the two sides, is refused.

```
$ python keydelta.py --self-test
keydelta self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  a JSON null reads as blank and a boolean as true or false
...
PASS  nan and inf are text, never a number to subtract  <-- pinned defect
...
PASS  a key two source rows share is held back, with the rows named  <-- pinned defect
PASS  the held key is not inserted, updated or deleted, so the published row stays  <-- pinned defect
...
PASS  a 1e-9 difference is suppressed and listed, not rewritten every night  <-- pinned defect
...
PASS  two populated tables with no shared key are refused by name  <-- pinned defect
...
PASS  refused: a truncated first page  <-- pinned defect
...
PASS  a repeated CSV header is refused, not read as NO CHANGE  <-- pinned defect
...
PASS  --key @geometry on a CSV is exit 64, not a crash  <-- pinned defect
...
PASS  --ap is refused, not read as a longer flag  <-- pinned defect
...
PASS  --apply never replaces a plan file that exists, and the verdict still prints  <-- pinned defect
...
PASS  a refused plan writes no file even with --apply  <-- pinned defect
...
PASS  an unexpected exception exits 70, not the 1 that means a rail refused  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the exception it is looking for
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
199 assertions, 0 failed
```

The full run prints all 199 assertions. Each `...` line above is where this block is cut.

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
  deleted record.

I did not run csvdiff or daff for this README. Their pages say nothing about a key that two rows
share, so I make no claim about what they do with one. I also found no page that describes pricing
the diff against the delete-all, or listing the rows a duplicate key held back. This tool adds
those three things: the duplicate handling, the tolerance, and the refusals. A search of GitHub
for the name `keydelta` on the day this was written found this repository and one unrelated
one, a set of buttons, so the name is not taken by another tool for this.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Each snapshot is a CSV, or a saved feature set (`.json`) from a layer query.

The self-test prints the same 199 assertions on Windows (Python 3.13.2), on Python 3.9.25 and
on Ubuntu (Python 3.12.3). The three outputs are identical line for line once the Windows
carriage returns are removed.
`coverage run --branch keydelta.py --self-test` reports 100 percent of lines and branches.

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
| `--min-rows` | `1` | Refuse a source with fewer rows than this. |
| `--max-change` | `0.10` | Refuse more inserts, updates and deletes than this fraction of the target. It also caps the rows under held-back keys, as a fraction of the rows read from both files. |
| `--max-delete` | `0.02` | Refuse more deletes than this fraction of the target. |
| `--max-duplicates` | `50` | Refuse more held-back duplicate keys than this. |
| `--force-large` | off | Lift the change, delete and no-shared-key rails. Not the two duplicate rails. |
| `--sample` | `10` | How many keys to list under each heading. `0` lists none. |
| `--out` | off | Path for the plan file, a new JSON file. Needs `--apply` to be written. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Other flags are ignored. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, and `--tol` is refused instead of being read as
`--tolerance`. The self-test pins eleven prefixes, and one run through `main` that confirms
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
with its changed columns, and each held-back key with its rows. Nothing else is written. Keys in
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

## Refusals

The tool refuses before it plans, and it refuses a plan it has made.

**Input refusals, exit 3.** Each prints `REFUSED` and `VERDICT: REFUSED`.

- A blank key, or a key that is only spaces, on either side. For a composite key, one blank
  part is enough. A row with no key would be inserted and deleted on every run. The first five
  lines are named.
- A CSV that repeats a column name. Only the last column of that name would be read, so a change
  in the earlier one would show as `NO CHANGE`. The file is refused and the names are listed.
- A source with fewer rows than `--min-rows`. An empty source plans to delete the target.
- A feature set whose `exceededTransferLimit` is `true`. Esri's query documentation describes
  that property through paging: there are no more pages to fetch when it is `false`. A `true`
  means the file holds the first page only, and a plan from it would delete every row it did not
  return ([Query](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)).
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
| 0 | A plan was made and no rail tripped. A plan with no change is also 0. |
| 1 | A rail refused the plan. |
| 2 | A file could not be read or written. The verdict still prints when the write failed. |
| 3 | An input was refused: blank key, repeated column name, too few source rows, truncated or error response. |
| 4 | Only held-back duplicate keys are left, so the verdict is `HELD BACK`. The published copy is not known to match. |
| 64 | Usage error, including a bad column name, a flag prefix, two files with different columns and a file with no columns. |
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

A plan with changes and held-back keys, like the demo above, exits 0. Read the held back count.

## Limits

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
- One `--tolerance` applies to every numeric column and every coordinate, in the units of each.
  In degrees, `0.01` hides a move of about 1 km as noise. The same value applies to numbers inside
  the geometry JSON, such as a `wkid`, and to code columns that look like numbers, so keep it well
  below 1. A shape exported as one text column, such as WKT, is compared as text. The tolerance
  does not test shape equality, and a buffer-containment test is not part of this.
- A duplicated key is judged by its set of rows, matched one to one in order of appearance with the
  tolerance. It is greedy, so with a tolerance larger than the spacing of your values, a pairing
  can come out differently from the best one. Keep the tolerance small.
- The report lists up to `--sample` keys per heading. The plan file lists all of them.
- The delta cost rule is the one for traditional versioning in the default version. It assumes
  every touched row is a base-table row, and it does not model several edits to one row in one
  state. Branch versioning is not what the line describes. The cost is an estimate from the rule
  and is not read from any table.
- The small-table effect: the ceilings are fractions of the target. On a target of 41 rows 2
  percent is 0 rows, so one delete is refused. Raise `--max-delete` for a small table.
- The source rows must be complete. The tool can see a truncated feature set only through
  `exceededTransferLimit`. A CSV cut off by a failed export looks like a smaller table, and the
  delete rail is what stops it.
- Only UTF-8 CSV is supported. Other bytes are decoded lossily, so two different keys can
  collapse into one, and so can two different values. A CSV in cp1252, which Excel writes by
  default, that changes `Caf` plus an acute `e` to `Caf` plus a grave `e` reports `NO CHANGE` with
  exit 0. Save as UTF-8 first. A short row reads its missing cells as blank and a long row has its
  extra cells ignored. A UTF-8 byte order mark is removed.
- A CSV that starts with a blank line, uses semicolons or is UTF-16 exits 64, because the header
  is not read as columns. A UTF-16 feature set is refused too. All are refused cleanly.
- The parser takes two optional positional files. On Python 3.12 and older, `SOURCE --key K
  TARGET` (an option between the files) exits 64 with `unrecognized arguments`. Python 3.13
  accepts it. Put both files first.
- A group of N rows under one key is compared in time that grows with N squared. With a
  low-cardinality column as `--key` and the two files in different orders, 4000 rows under one
  key took about 2 seconds here, and tens of thousands would run for minutes before any rail can
  fire. Use a real key.
- A JSON file nested hundreds of thousands of levels deep raises `RecursionError`, and a huge
  integer in a shape can raise `OverflowError`. Both exit 70, a fault in the tool, not a finding.
- If stdout is a closed pipe, such as a report piped to `head`, a `BrokenPipeError` can end the
  run with exit 70. The exit code then reflects the pipe, not the data.
- The report prints keys as they are and joins a composite key with `|`. A key that holds `|`
  can make two keys print alike, and a key with a newline can print a line such as
  `VERDICT: NO CHANGE` above the real verdict. The plan file keeps each key as a list. Read the
  exit code, not one line of the report.
- A plan with changes and held-back keys exits 0 and prints `PLAN READY`. It means the plan is
  ready for the other rows. Exit 4 happens only when the held keys are all that is left. A job
  that reads only the exit code should also read the held back count.
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
- It reads both files into memory and has no streaming mode. One run of 300,000 synthetic rows a
  side, three columns each, took 3.5 seconds on a Windows workstation and 2.6 seconds on an
  Ubuntu server. A later run on the same workstation took between 4.5 and 4.6 seconds. The
  self-test does not run this, and the figures are measurements, not a promise. Memory was not
  measured. Your tables will differ.
- A key that starts with `=`, `+`, `-` or `@` is written to the plan as it came. The plan is JSON,
  but a report pasted into a spreadsheet may read such a key as a formula.
- The plan file is written in place with exclusive creation, not through a temporary file. A
  failed write can leave a partial file, and the exit code is 2.
- No line is unreachable offline: coverage is 100 percent of lines and branches. The tool opens no
  socket and imports no network module, so it has no network path to stub and no credential in it
  to leak. The self-test asserts that its own source imports no network module.
- A mutation sweep of 519 mutants, run on the revision before the last two fixes, of the code outside the self-test (flipped comparisons, `and`
  and `or` swaps, forced branches, dropped statements, constants plus one) left 4 alive. They are
  the `if __name__ == "__main__"` guard and `return _run(argv)` in `main`, each of which makes the
  script do nothing so the self-test never starts. The self-test kills the other 515.
- The self-test counts the same on all three interpreters. The cp1252 console assertion builds its
  own stream in memory, so it gives the same answer on a host whose console is UTF-8.
- I read the Esri pages above for the behaviours this README relies on. I did not test the tool
  against a live geodatabase, and the cost line was not compared with real delta-table counts.

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
  shared ID that is not unique and measures how far apart the two rows of each pair are.
- [safe-republish](https://github.com/uhsear/safe-republish) - refuse to truncate a feature class
  when its staged replacement fails a plausibility check. Use it where the replacement is still a
  full reload.
- [fcload](https://github.com/uhsear/fcload) - load a dataset into a geodatabase while refusing the
  imports that corrupt silently. It is the writer that a reviewed plan from this tool would need.
- [replicakeep](https://github.com/uhsear/replicakeep) - plan, and under `--apply` repair, an idle
  replica that holds a compress floor. It is the other half of the delta-table story.

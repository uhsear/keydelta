#!/usr/bin/env python
"""Plan a keyed delta between two snapshots of one table, and hold back every duplicate key.

Compares what a table should become (SOURCE) with what it is now (TARGET), row by row on one
or more key columns, and plans the smallest set of inserts, updates and deletes that turns the
target into the source. It plans only. It never opens a database and never edits a table.

A nightly job copies a source table to a published copy by deleting every target row and
inserting every source row. On a versioned table that writes two delta rows for every row of
the table, every night, to carry the handful of rows that really changed. The keyed fix is
well known: touch only the rows that differ. It fails on the first duplicate key, because a
join on a key that two rows share fans out, and a segment silently drops out of the published
copy. Nothing reports an error. The join did what it was told.

Tools already diff on a key: the csvdiff and daff programs, and in ArcGIS Pro the Append tool
(an upsert on matching fields) and Table Compare. Their documentation does not say what
happens to a key that two rows share. None is documented to hold such rows back, name them, or
price the plan against the delete-all it replaces.

    python keydelta.py --self-test
    python keydelta.py source.csv target.csv --key SEG_ID
    python keydelta.py source.csv target.csv --key CITY --key SEG_NO --ignore EDITED_ON
    python keydelta.py source.csv target.csv --key SEG_ID --tolerance 0.01
    python keydelta.py source.csv target.csv --key SEG_ID --scale LEN_FT=2
    python keydelta.py source.csv target.csv --key SEG_ID --out plan.json --apply
    python keydelta.py response.json target.csv --key SEG_ID --force-large

--scale COLUMN=N compares a column at the N decimal places it stores, so a float that was
rounded on its way into the table is not an edit on every rerun. The plan names the scales used.

A key that two rows share is never planned: it is held back and left as published, unless both
sides hold the same rows under it, which is no change. Nothing is written without --apply, and
--apply writes one plan file.

Exit codes: 0 a plan was made, no rail tripped and no key was held back, 1 a rail refused the
plan, 2 a file could not be read or written, 3 an input was refused (blank key, a row whose
field count differs from the header, truncated or error response, too few source rows), 4 a
duplicate key was held back, so the published copy is not known to match (the plan is still
written under --apply), 64 usage error, 70 an unexpected crash, which is never a finding.
"""

import argparse
import ast
import contextlib
import csv
import decimal
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

# =============================================================================
# Constants
# =============================================================================

DEFAULT_MAX_CHANGE = 0.10
DEFAULT_MAX_DELETE = 0.02
DEFAULT_MAX_DUPLICATES = 50
DEFAULT_MIN_ROWS = 1
DEFAULT_SAMPLE = 10
PLAN_FORMAT = 1
# The column a saved feature set's geometry is read into. No field name starts with @.
GEOM_COL = "@geometry"
# A plain decimal: no underscore, no space, no leading zero (02101 is a code, not 2101), no
# hex, no nan or inf. float() alone accepts all of those.
PLAIN_NUMBER = re.compile(r"[+-]?(0|[1-9][0-9]*)(\.[0-9]*)?([eE][+-]?[0-9]+)?\Z|"
                          r"[+-]?\.[0-9]+([eE][+-]?[0-9]+)?\Z")


# The most decimal places a SQL Server decimal or numeric column can store (precision 38).
MAX_SCALE = 38
# Ties round half away from zero. 400 digits holds any finite float (309 digits) plus 38 places.
SCALE_CONTEXT = decimal.Context(prec=400, rounding=decimal.ROUND_HALF_UP)
SCALE_SPEC = re.compile(r"[0-9]{1,2}\Z")


class InputRefused(Exception):
    """An input the tool will not plan from. Exit 3, never a finding about the data."""


# =============================================================================
# Pure decision core. No file, no print, no clock.
# =============================================================================

def cell_text(value):
    """One JSON attribute value as the text a CSV export of it would hold."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def to_float(text):
    """A finite float, or None. Only a plain decimal is a number. nan, inf, 1_0 and 007 are
    text, never a number to subtract."""
    if not PLAIN_NUMBER.match(text):
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def values_equal(a, b, tolerance):
    """Exact text, or two finite numbers within tolerance when tolerance is above 0."""
    if a == b:
        return True
    if tolerance > 0:
        x, y = to_float(a), to_float(b)
        if x is not None and y is not None:
            return abs(x - y) <= tolerance
    return False


def _close(x, y, tolerance):
    if isinstance(x, dict) and isinstance(y, dict):
        return x.keys() == y.keys() and all(_close(x[k], y[k], tolerance) for k in x)
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(_close(p, q, tolerance) for p, q in zip(x, y))
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
        return math.isfinite(x) and math.isfinite(y) and abs(x - y) <= tolerance
    return type(x) is type(y) and x == y


def geometry_equal(a, b, tolerance):
    """Two geometry texts that differ as text: equal when the shapes match and every number
    is within tolerance. At tolerance 0 the text must match, so this is False."""
    if tolerance <= 0:
        return False
    try:
        return _close(json.loads(a), json.loads(b), tolerance)
    except ValueError:
        return False


def at_scale(text, scale):
    """The number in text rounded half away from zero to scale decimal places, as a Decimal.
    None when text is not a finite plain number. The text is rounded as written, never
    through a binary float, so 2.675 at scale 2 is 2.68."""
    if to_float(text) is None:
        return None
    return decimal.Decimal(text).quantize(decimal.Decimal(1).scaleb(-scale),
                                          context=SCALE_CONTEXT)


def differing_full(src_cells, tgt_cells, columns, tolerance, scales=None):
    """(changed columns, noisy, rounded).

    noisy: some cell differs as text but sits inside tolerance.
    rounded: some cell differs as text but is the same number at its column's storage scale.
    scales maps a column name to the decimal places that column stores. On a column with a
    scale, two numbers are equal when they round to the same value, and --tolerance does not
    apply. A cell that is not a number falls back to the tolerance test.
    """
    scales = scales or {}
    changed = []
    noisy = False
    rounded = False
    for col in columns:
        a, b = src_cells[col], tgt_cells[col]
        if a == b:
            continue
        if col in scales:
            x, y = at_scale(a, scales[col]), at_scale(b, scales[col])
            if x is not None and y is not None:
                # A declared scale is the stored truth, so it replaces --tolerance here.
                if x == y:
                    rounded = True
                else:
                    changed.append(col)
                continue
        same = geometry_equal if col == GEOM_COL else values_equal
        if same(a, b, tolerance):
            noisy = True
        else:
            changed.append(col)
    return changed, noisy, rounded


def differing(src_cells, tgt_cells, columns, tolerance):
    """(changed columns, noisy). noisy: some cell differs as text but sits inside tolerance."""
    return differing_full(src_cells, tgt_cells, columns, tolerance)[:2]


def group_by_key(records):
    """key tuple -> list of records, in first-seen order. A record is (where, key, cells)."""
    groups = {}
    for rec in records:
        groups.setdefault(rec[1], []).append(rec)
    return groups


def same_rows(src_group, tgt_group, columns, tolerance, scales=None):
    """True when both groups hold the same rows, in any order (greedy one-to-one match)."""
    if len(src_group) != len(tgt_group):
        return False
    free = list(tgt_group)
    for src in src_group:
        for tgt in free:
            if not differing_full(src[2], tgt[2], columns, tolerance, scales)[0]:
                free.remove(tgt)
                break
        else:
            return False
    return True


def classify(src_records, tgt_records, columns, tolerance=0.0, scales=None):
    """Plan the delta that turns the target into the source.

    A key held by more than one row on either side is never inserted, updated or deleted.
    It is held back and left as published, because a join on it cannot say which row the
    other side meant. When both sides hold the same rows under it, it is no change.
    scales names the decimal places a column stores. Two numbers that are equal once rounded
    to that scale are no change, and the key is listed under "rounded".
    """
    src = group_by_key(src_records)
    tgt = group_by_key(tgt_records)
    plan = {"inserts": [], "updates": [], "deletes": [], "held": [],
            "suppressed": [], "rounded": [], "scales": dict(scales or {}),
            "held_rows": 0, "unchanged": 0, "duplicate_identical": 0,
            "shared_keys": 0, "source_rows": len(src_records),
            "target_rows": len(tgt_records)}
    for key in sorted(set(src) | set(tgt)):
        s_group, t_group = src.get(key, []), tgt.get(key, [])
        if s_group and t_group:
            plan["shared_keys"] += 1
        if len(s_group) > 1 or len(t_group) > 1:
            if same_rows(s_group, t_group, columns, tolerance, scales):
                plan["duplicate_identical"] += 1
            else:
                plan["held_rows"] += len(s_group) + len(t_group)
                plan["held"].append({
                    "key": list(key),
                    "source": [r[0] for r in s_group],
                    "target": [r[0] for r in t_group]})
        elif not t_group:
            plan["inserts"].append(list(key))
        elif not s_group:
            plan["deletes"].append(list(key))
        else:
            changed, noisy, rounded = differing_full(
                s_group[0][2], t_group[0][2], columns, tolerance, scales)
            if changed:
                plan["updates"].append({"key": list(key), "columns": changed})
            else:
                plan["unchanged"] += 1
                if noisy:
                    plan["suppressed"].append(list(key))
                if rounded:
                    plan["rounded"].append(list(key))
    return plan


def delta_cost(plan):
    """(keyed, wholesale) delta-table rows on traditional versioning.

    An insert writes one adds row, a delete one deletes row, an update one of each. A
    delete-all plus insert-all writes one deletes row per target row and one adds row per
    source row.
    """
    keyed = len(plan["inserts"]) + 2 * len(plan["updates"]) + len(plan["deletes"])
    return keyed, plan["source_rows"] + plan["target_rows"]


def always_changed_hint(updates):
    """A sentence naming the columns that differ on every updated row, or "". One column that
    differs on every row is usually a representation clash, such as a date stored as epoch
    milliseconds in a JSON file and as formatted text in a CSV, not 20 real edits."""
    if len(updates) < 2:
        return ""
    every = set(updates[0]["columns"])
    for update in updates[1:]:
        every &= set(update["columns"])
    if not every:
        return ""
    return (" Column(s) %s differ on every updated row. The same value written two ways reads "
            "this way, such as text against a number, or a full-precision number against its "
            "stored rounding. Export both sides the same way, add --scale COLUMN=PLACES for a "
            "number, or add --ignore." % ", ".join(sorted(every)))


def guard(plan, force_large, max_change, max_delete, max_duplicates):
    """Reasons to refuse the plan. An empty list means no rail tripped."""
    problems = []
    if len(plan["held"]) > max_duplicates:
        problems.append(
            "%d keys are held back as duplicates, above the ceiling of %d. Normal is a few; "
            "this many is systemic and would leave that many rows stale. --force-large does "
            "not override this." % (len(plan["held"]), max_duplicates))
    rows_read = plan["source_rows"] + plan["target_rows"]
    held_ceiling = int(rows_read * max_change)
    if plan["held_rows"] > held_ceiling:
        problems.append(
            "%d rows sit under the held-back duplicate keys, above the ceiling of %d (%.0f%% of "
            "the %d rows read). The key column is probably not a key, and this plan would "
            "compare few of the rows. --force-large does not override this." % (
                plan["held_rows"], held_ceiling, max_change * 100, rows_read))
    if force_large:
        return problems
    if plan["source_rows"] and plan["target_rows"] and not plan["shared_keys"]:
        problems.append(
            "no key is on both sides, so every target row would be deleted and every source "
            "row inserted. That is the delete-all this tool exists to avoid. The key columns "
            "probably spell the key differently on the two sides.")
    denominator = max(plan["target_rows"], 1)
    basis = ("%d target rows" % plan["target_rows"] if plan["target_rows"]
             else "0 target rows, counted as 1")
    delete_ceiling = int(denominator * max_delete)
    if len(plan["deletes"]) > delete_ceiling:
        problems.append(
            "%d deletes exceeds the ceiling of %d (%.0f%% of %s). A bad source "
            "read must not empty the target. Rerun with --force-large if the deletion is "
            "intended." % (len(plan["deletes"]), delete_ceiling, max_delete * 100, basis))
    total = len(plan["inserts"]) + len(plan["updates"]) + len(plan["deletes"])
    change_ceiling = int(denominator * max_change)
    if total > change_ceiling:
        problems.append(
            "%d changed rows exceeds the ceiling of %d (%.0f%% of %s). That is "
            "more likely a bad read than real editing.%s Rerun with --force-large if it is "
            "intended." % (total, change_ceiling, max_change * 100, basis,
                           always_changed_hint(plan["updates"])))
    return problems


def check_min_rows(count, minimum):
    """A source below the floor is a bad read, not a table that emptied itself."""
    if count < minimum:
        raise InputRefused(
            "the source has %d row(s), below --min-rows %d. An empty or near-empty source "
            "would plan to delete the target." % (count, minimum))


def key_label(key):
    return "|".join(key)


def describe(plan, columns, keys, sample):
    """The report as a list of lines."""
    keyed, wholesale = delta_cost(plan)
    lines = ["key: %s. compared columns: %d" % (", ".join(keys), len(columns))]
    scales = plan["scales"]
    if scales:
        lines.append("storage scale used, decimal places, half away from zero: %s" % ", ".join(
            "%s=%d" % (col, scales[col]) for col in sorted(scales)))
    lines += [
        "rows planned:",
        "  unchanged    %d" % plan["unchanged"],
        "  update       %d" % len(plan["updates"]),
        "  insert       %d" % len(plan["inserts"]),
        "  delete       %d" % len(plan["deletes"]),
        "  held back    %d (duplicate key, left as published)" % len(plan["held"]),
        "  identical duplicate keys, no change    %d" % plan["duplicate_identical"],
        "  noise inside tolerance, no change      %d" % len(plan["suppressed"]),
    ]
    if scales:
        lines.append("  same at storage scale, no change       %d" % len(plan["rounded"]))
    lines.append(
        "delta-table rows, traditional versioning: keyed plan %d, delete-all plus insert-all %d"
        % (keyed, wholesale))

    def section(title, items, text):
        if items:
            lines.append("")
            lines.append(title)
            for item in items[:sample]:
                lines.append("  " + text(item))
            if len(items) > sample:
                lines.append("  ... and %d more" % (len(items) - sample))

    section("held back, a person decides:", plan["held"], lambda h: "%s  source %s, target %s" % (
        key_label(h["key"]), ", ".join(h["source"]) or "none", ", ".join(h["target"]) or "none"))
    section("updates:", plan["updates"], lambda u: "%s  columns %s" % (
        key_label(u["key"]), ", ".join(u["columns"])))
    section("inserts:", plan["inserts"], key_label)
    section("deletes:", plan["deletes"], key_label)
    section("noise inside tolerance, not rewritten:", plan["suppressed"], key_label)
    section("same at storage scale, not rewritten:", plan["rounded"], key_label)
    return lines


def verdict(plan, problems):
    """(exit code, VERDICT line)."""
    if problems:
        return 1, "VERDICT: REFUSED, %d rail(s) tripped. Do not apply this plan." % len(problems)
    changes = len(plan["inserts"]) + len(plan["updates"]) + len(plan["deletes"])
    if not changes and plan["held"]:
        # Nothing to write, but the tool could not plan these keys, so it cannot call the
        # published copy in step. Exit 0 here would read as clean to a scheduled job.
        return 4, ("VERDICT: HELD BACK, %d duplicate key(s) left as published, a person "
                   "decides. Nothing else to write." % len(plan["held"]))
    if not changes:
        return 0, "VERDICT: NO CHANGE, nothing to write."
    # Held keys beside changes exit 4 as well: the plan is complete for the other rows, but the
    # published copy will still differ, and exit 0 would read as in step to a scheduled job.
    return (4 if plan["held"] else 0), (
        "VERDICT: PLAN READY, %d update(s), %d insert(s), %d delete(s), %d held back." % (
            len(plan["updates"]), len(plan["inserts"]), len(plan["deletes"]), len(plan["held"])))


def plan_document(plan, keys, columns, problems):
    """The plan as the JSON-ready dict that --apply writes."""
    keyed, wholesale = delta_cost(plan)
    return {
        "format": PLAN_FORMAT,
        "tool": "keydelta",
        "key": list(keys),
        "columns": list(columns),
        "source_rows": plan["source_rows"],
        "target_rows": plan["target_rows"],
        "delta_rows_keyed": keyed,
        "delta_rows_delete_all": wholesale,
        "refused": list(problems),
        "inserts": plan["inserts"],
        "updates": plan["updates"],
        "deletes": plan["deletes"],
        "held": plan["held"],
        "suppressed": plan["suppressed"],
        "scale": plan["scales"],
        "rounded": plan["rounded"],
    }


# =============================================================================
# Input
# =============================================================================

def read_csv(path):
    """(header, [(where, cells dict)]). where names the line a record starts on."""
    records = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        # strict: an unclosed quote is an error, not one cell that swallows the rest of the file.
        reader = csv.reader(handle, strict=True)
        header = next(reader, [])
        if not header and reader.line_num:
            raise InputRefused(
                "%s starts with a blank line, so it has no header row. Delete the blank line "
                "first." % path)
        # A repeated header name would keep only the last such column and compare the rest
        # never, so a changed value could read as NO CHANGE. Refuse it.
        repeated = sorted(set(n for n in header if header.count(n) > 1))
        if repeated:
            raise InputRefused(
                "%s repeats the column name(s) %s, so the earlier column of each would never "
                "be compared. Rename or drop the repeats first." % (path, ", ".join(repeated)))
        while True:
            start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if not row:
                continue
            if len(row) != len(header):
                # A cell beyond the header, or a missing one, is a row the tool cannot read
                # as written. An unquoted comma would hide a real edit as NO CHANGE.
                raise InputRefused(
                    "%s line %d has %d field(s) but the header has %d. An unquoted comma or a "
                    "cut-off row is the usual cause. Quote the field or fix the row first." % (
                        path, start, len(row), len(header)))
            records.append(("line %d" % start, dict(zip(header, row))))
    return header, records


def read_features(path):
    """(header, [(where, cells dict)]) from a saved feature set response (f=json)."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        # Read outside the try: a decode failure is load()'s refusal, not "not valid JSON".
        text = handle.read()
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise InputRefused("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(doc, dict) or isinstance(doc.get("error"), dict):
        raise InputRefused(
            "%s is an error response or not a feature set. A file with an error object holds "
            "no rows to plan from." % path)
    if doc.get("exceededTransferLimit") is True:
        raise InputRefused(
            "%s has exceededTransferLimit true, so it holds only the first page. Planning from "
            "a partial source would delete every row it did not return." % path)
    features = doc.get("features")
    if not isinstance(features, list):
        raise InputRefused("%s has no features list." % path)
    # Geometry is compared, as one text column, when any feature carries the key. A moved
    # shape with the same attributes is a change, not NO CHANGE.
    with_geometry = any(isinstance(f, dict) and "geometry" in f for f in features)
    header = []
    records = []
    for i, feature in enumerate(features):
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            raise InputRefused("%s: feature %d has no attributes." % (path, i + 1))
        cells = {k: cell_text(v) for k, v in attrs.items()}
        if with_geometry:
            cells[GEOM_COL] = cell_text(feature.get("geometry"))
        for name in cells:
            if name not in header:
                header.append(name)
        records.append(("feature %d" % (i + 1), cells))
    for _, cells in records:
        for name in header:
            cells.setdefault(name, "")
    return header, records


def load(path):
    """Read one snapshot. A .json path is a feature set response, anything else is a CSV."""
    try:
        return read_features(path) if path.lower().endswith(".json") else read_csv(path)
    except UnicodeDecodeError as exc:
        # Replacing the bad bytes would make two different accented letters compare equal, so a
        # real edit could read as NO CHANGE. Refuse instead.
        raise InputRefused(
            "%s is not valid UTF-8 (byte %d is not). Decoding it with replacement characters "
            "would hide edits between different accented letters. Save it as UTF-8 first, for "
            "example CSV UTF-8 in Excel." % (path, exc.start))


def check_columns(header, keys, ignore, side):
    """Raise ValueError for a column the flags name that the file does not hold."""
    # --ignore @geometry is allowed on a file that has no geometry column, so a CSV can be
    # compared with a feature set. --key @geometry is not: a CSV has no such column to key on.
    absent = [c for c in list(keys) if c not in header]
    absent += [c for c in ignore if c not in header and c != GEOM_COL]
    if absent:
        raise ValueError("%s: columns not in the file: %s. It has: %s" % (
            side, ", ".join(absent), ", ".join(header) if header else "(no columns at all)"))


def compare_columns(src_header, tgt_header, keys, ignore):
    """The columns to compare. Both sides must hold the same ones."""
    skip = set(keys) | set(ignore)
    src_cols = [c for c in src_header if c not in skip]
    tgt_cols = [c for c in tgt_header if c not in skip]
    if set(src_cols) != set(tgt_cols):
        diff = sorted(set(src_cols) ^ set(tgt_cols))
        raise ValueError(
            "the two files do not hold the same columns. Only on one side: %s. Name them with "
            "--ignore or fix the export." % ", ".join(diff))
    if not src_cols:
        raise ValueError("no column is left to compare after the key and --ignore.")
    return src_cols


def to_records(raw, keys, side):
    """[(where, cells)] -> [(where, key tuple, cells)]. A blank key is refused, never skipped."""
    records = []
    blank = []
    for where, cells in raw:
        key = tuple(cells[k] for k in keys)
        if any(not part.strip() for part in key):
            blank.append(where)
        records.append((where, key, cells))
    if blank:
        raise InputRefused(
            "%d row(s) on the %s side have a blank key (%s%s). A blank key cannot name a row, "
            "so the row would be inserted and deleted on every run. Fix the key first." % (
                len(blank), side, ", ".join(blank[:5]), ", ..." if len(blank) > 5 else ""))
    return records


def parse_scales(specs, keys, ignore):
    """['COLUMN=N', ...] -> {column: N}. Raises ValueError for a spec the tool cannot trust."""
    scales = {}
    for spec in specs:
        name, sep, digits = spec.rpartition("=")
        if not sep or not name or not SCALE_SPEC.match(digits) or int(digits) > MAX_SCALE:
            raise ValueError(
                "--scale %s must be COLUMN=N, with N a whole number of decimal places from 0 "
                "to %d." % (spec, MAX_SCALE))
        if name in scales:
            raise ValueError("--scale names %s twice." % name)
        if name in keys or name in ignore:
            raise ValueError(
                "--scale %s is a --key or --ignore column. A scale applies only to a compared "
                "column." % name)
        if name == GEOM_COL:
            raise ValueError("--scale cannot name %s. Use --tolerance for a shape." % GEOM_COL)
        scales[name] = int(digits)
    return scales


def build_plan(src_path, tgt_path, keys, ignore, tolerance, min_rows, scales=None):
    """Read both snapshots and classify. Raises ValueError, InputRefused, OSError, csv.Error."""
    src_header, src_raw = load(src_path)
    tgt_header, tgt_raw = load(tgt_path)
    check_min_rows(len(src_raw), min_rows)
    check_columns(src_header, keys, ignore, "source")
    check_columns(tgt_header, keys, ignore, "target")
    columns = compare_columns(src_header, tgt_header, keys, ignore)
    absent = [c for c in (scales or {}) if c not in columns]
    if absent:
        raise ValueError("--scale names a column that is not compared: %s. The compared "
                         "columns are: %s" % (", ".join(absent), ", ".join(columns)))
    src = to_records(src_raw, keys, "source")
    tgt = to_records(tgt_raw, keys, "target")
    return classify(src, tgt, columns, tolerance, scales), columns


# =============================================================================
# CLI
# =============================================================================

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="keydelta.py",
        description="Plan a keyed delta between two snapshots of one table and hold back "
                    "every duplicate key.",
        epilog="It plans only. Nothing is written without --apply, and --apply writes one "
               "plan file.",
        allow_abbrev=False,
    )
    ap.add_argument("source", nargs="?", metavar="SOURCE",
                    help="what the table should become: a CSV, or a saved feature set .json")
    ap.add_argument("target", nargs="?", metavar="TARGET",
                    help="what the table is now: a CSV, or a saved feature set .json")
    ap.add_argument("--key", action="append",
                    help="a key column, on both sides. Repeat it for a composite key.")
    ap.add_argument("--ignore", action="append",
                    help="a column to leave out of the comparison. Repeatable.")
    ap.add_argument("--tolerance", type=float, default=0.0,
                    help="two numbers within this are equal (default 0, exact text)")
    ap.add_argument("--scale", action="append",
                    help="COLUMN=N: the column stores N decimal places, so two numbers that "
                         "are equal rounded to N places are no change. Repeatable.")
    ap.add_argument("--min-rows", dest="min_rows", type=int, default=DEFAULT_MIN_ROWS,
                    help="refuse a source with fewer rows (default %d)" % DEFAULT_MIN_ROWS)
    ap.add_argument("--max-change", dest="max_change", type=float, default=DEFAULT_MAX_CHANGE,
                    help="refuse more changed rows than this fraction of the target "
                         "(default %.2f)" % DEFAULT_MAX_CHANGE)
    ap.add_argument("--max-delete", dest="max_delete", type=float, default=DEFAULT_MAX_DELETE,
                    help="refuse more deletes than this fraction of the target "
                         "(default %.2f)" % DEFAULT_MAX_DELETE)
    ap.add_argument("--max-duplicates", dest="max_duplicates", type=int,
                    default=DEFAULT_MAX_DUPLICATES,
                    help="refuse more held-back duplicate keys than this (default %d)"
                         % DEFAULT_MAX_DUPLICATES)
    ap.add_argument("--force-large", dest="force_large", action="store_true",
                    help="lift the change, delete and no-shared-key rails")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many keys to list under each heading (default %d)"
                         % DEFAULT_SAMPLE)
    ap.add_argument("--out", help="path for the plan file, a new JSON file")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def option_error(args):
    """The first usage error in the options, or None."""
    keys = args.key or []
    ignore = args.ignore or []
    try:
        parse_scales(args.scale or [], keys, ignore)
    except ValueError as exc:
        return str(exc)
    checks = [
        (not args.source or not args.target,
         "a SOURCE and a TARGET are required. Use --self-test to verify the tool without them."),
        (not keys, "--key is required."),
        (len(set(keys)) != len(keys), "--key names the same column twice."),
        (bool(set(keys) & set(ignore)), "a column cannot be both --key and --ignore."),
        (not 0 <= args.tolerance < math.inf, "--tolerance must be 0 or more, and finite."),
        (args.sample < 0, "--sample cannot be negative."),
        (args.min_rows < 0, "--min-rows cannot be negative."),
        (args.max_duplicates < 0, "--max-duplicates cannot be negative."),
        (not 0 <= args.max_change <= 1, "--max-change must be between 0 and 1."),
        (not 0 <= args.max_delete <= 1, "--max-delete must be between 0 and 1."),
        (args.apply and not args.out, "--apply needs --out."),
    ]
    for failed, message in checks:
        if failed:
            return message
    return None


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _tolerate_odd_characters():
    """Make print() unable to fail on a key the console cannot encode. It prints ? instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def write_plan(path, document):
    """Create path and write the plan. 'x' refuses to replace a file that exists."""
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1 is the code a
    scheduled job reads as 'a rail refused the plan'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a finding about "
              "the data." % (type(exc).__name__, exc), file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file could not be read.
        return 64 if exc.code == 2 else exc.code
    if args.self_test:
        return self_test()
    message = option_error(args)
    if message:
        print("error: %s" % message, file=sys.stderr)
        return 64
    if args.out:
        # Checked on a dry run too, so the run that would clobber an input says so early.
        for path in (args.source, args.target):
            if _same_file(args.out, path):
                print("error: --out %s is also an input. Name a new file." % args.out,
                      file=sys.stderr)
                return 64
    keys = args.key
    ignore = args.ignore or []
    try:
        plan, columns = build_plan(args.source, args.target, keys, ignore,
                                   args.tolerance, args.min_rows,
                                   parse_scales(args.scale or [], keys, ignore))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except InputRefused as exc:
        print("REFUSED   %s\n\nVERDICT: REFUSED" % exc)
        return 3
    except (IOError, OSError, csv.Error) as exc:
        # csv.Error too: a field over csv's 131072 character limit is an unreadable input.
        print("error: %s" % exc, file=sys.stderr)
        return 2
    problems = guard(plan, args.force_large, args.max_change, args.max_delete,
                     args.max_duplicates)
    print("keydelta: %s %d row(s), %s %d row(s)" % (
        args.source, plan["source_rows"], args.target, plan["target_rows"]))
    for line in describe(plan, columns, keys, args.sample):
        print(line)
    for problem in problems:
        print("\nREFUSED   %s" % problem)
    code, line = verdict(plan, problems)
    if args.out:
        if problems:
            print("\n%s not written, because the plan was refused." % args.out)
        elif args.apply:
            try:
                write_plan(args.out, plan_document(plan, keys, columns, problems))
            except (IOError, OSError) as exc:
                # The verdict still prints below, so a failed write cannot hide a refusal.
                print("error: could not write %s: %s" % (args.out, exc), file=sys.stderr)
                code = 2
            else:
                print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s not written. Re-run with --apply." % args.out)
    print("\n" + line)
    return code


# =============================================================================
# Self-test
# =============================================================================

def self_test():
    """Run the assertions in a temp directory that is removed even when one of them raises."""
    tmp = tempfile.mkdtemp(prefix="keydelta-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core and the CLI. Synthetic tables only. The only files
    are the ones written under tmp."""
    passed = [0]
    failed = []
    sep = "-" * 68

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def rec(where, key, **cells):
        return (where, tuple(key), cells)

    def rows(*specs):
        return [rec("r%d" % i, (k,), V=v) for i, (k, v) in enumerate(specs)]

    def make(name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    print("keydelta self-test: temporary files only, no network, no credentials")
    print(sep)

    # ---- cell_text, to_float, values_equal
    check(cell_text(None) == "" and cell_text(True) == "true" and cell_text(False) == "false",
          "a JSON null reads as blank and a boolean as true or false")
    check(cell_text(12) == "12" and cell_text(1.5) == "1.5" and cell_text("a") == "a",
          "numbers and text keep their plain text")
    check(cell_text({"b": 1, "a": 2}) == '{"a": 2, "b": 1}' and cell_text([1]) == "[1]",
          "an object or a list reads as sorted JSON, so two equal ones compare equal")
    check(to_float("2.5") == 2.5 and to_float("x") is None,
          "to_float reads a number and refuses text")
    check(to_float("nan") is None and to_float("inf") is None and to_float("-Infinity") is None,
          "nan and inf are text, never a number to subtract  <-- pinned defect")
    check(to_float("02101") is None and to_float("007") is None and to_float("00.5") is None,
          "a code with a leading zero is text, not a number  <-- pinned defect")
    check(to_float("1_0") is None and to_float(" 10") is None and to_float("0x10") is None
          and to_float("") is None and to_float("1e") is None,
          "underscore, padded, hex and empty text are not numbers  <-- pinned defect")
    check([to_float(t) for t in ("0", "-0.5", "+3", "5.", ".5", "1e3", "10.5E-1", "0.25")]
          == [0.0, -0.5, 3.0, 5.0, 0.5, 1000.0, 1.05, 0.25] and to_float("1e999") is None,
          "plain decimals read as numbers and an overflow is text")
    check(not values_equal("02101", "2101", 0.5) and not values_equal("007", "7", 0.5)
          and not values_equal("1_0", "10", 0.5),
          "a tolerance never makes a leading-zero or underscore code equal to a number  "
          "<-- pinned defect")
    shape = '{"paths": [[[0, 0], [100, 100]]]}'
    check(geometry_equal(shape, '{"paths": [[[0.001, 0], [100, 99.999]]]}', 0.01)
          and not geometry_equal(shape, '{"paths": [[[0, 0], [1, 1]]]}', 0.01),
          "a shape inside the tolerance is equal and a moved one is not")
    check(not geometry_equal(shape, '{"paths": [[[0.001, 0], [100, 100]]]}', 0),
          "at tolerance 0 the geometry text must match")
    check(not geometry_equal('{"a": 1}', '{"b": 1}', 1) and not geometry_equal("[1]", "[1, 2]", 1)
          and not geometry_equal("[true]", "[1]", 1) and not geometry_equal("[1]", "[NaN]", 1)
          and not geometry_equal("", "[1]", 1) and not geometry_equal('"a"', '"b"', 1)
          and geometry_equal('{"a": "x", "b": [1]}', '{"b": [1.5], "a": "x"}', 1),
          "different keys, lengths, types, nan, blank and text never pass a geometry tolerance")
    changed, noisy = differing({"@geometry": "[1]"}, {"@geometry": "[1.001]"}, ["@geometry"], 0.1)
    check(changed == [] and noisy, "geometry noise is counted as noise, like a number")
    changed, noisy = differing({"@geometry": "[1]"}, {"@geometry": "[2]"}, ["@geometry"], 0.1)
    check(changed == ["@geometry"] and not noisy, "a moved shape is a changed column")
    check(values_equal("a", "a", 0) and not values_equal("1.0", "1", 0),
          "with no tolerance the comparison is exact text")
    check(values_equal("1.0", "1", 0.01), "1.0 and 1 are equal inside a tolerance")
    check(values_equal("1.000", "1.009", 0.01) and not values_equal("1", "1.02", 0.01),
          "inside the tolerance is equal and outside is not")
    check(values_equal("1", "1.5", 0.5) is True,
          "a difference exactly at the tolerance is equal, the limit is inclusive")
    check(not values_equal("abc", "1", 0.5) and not values_equal("nan", "1", 0.5),
          "text and nan never pass a tolerance")

    # ---- differing
    changed, noisy = differing({"A": "1", "B": "x"}, {"A": "1", "B": "y"}, ["A", "B"], 0)
    check(changed == ["B"] and not noisy, "differing names the columns that changed")
    changed, noisy = differing({"A": "1.0"}, {"A": "1"}, ["A"], 0.1)
    check(changed == [] and noisy, "a difference inside tolerance is noise, not a change")

    # ---- classify: the four outcomes
    plan = classify(rows(("a", "1"), ("b", "2"), ("c", "3")),
                    rows(("a", "1"), ("b", "9"), ("d", "4")), ["V"])
    check(plan["unchanged"] == 1 and plan["updates"] == [{"key": ["b"], "columns": ["V"]}],
          "an unchanged row is left alone and a changed one is an update naming its column")
    check(plan["inserts"] == [["c"]] and plan["deletes"] == [["d"]],
          "a key only in the source is an insert and only in the target a delete")
    check(plan["shared_keys"] == 2 and plan["source_rows"] == 3 and plan["target_rows"] == 3,
          "the plan counts shared keys and the rows read")
    plan = classify(rows(("a", "1")), rows(("a", "1")), ["V"])
    check(plan["unchanged"] == 1 and not (plan["inserts"] or plan["updates"] or plan["deletes"]),
          "two identical tables plan nothing")

    # ---- classify: composite key
    src = [rec("s1", ("N", "1"), V="a"), rec("s2", ("N", "2"), V="a")]
    tgt = [rec("t1", ("N", "1"), V="a"), rec("t2", ("S", "1"), V="a")]
    plan = classify(src, tgt, ["V"])
    check(plan["inserts"] == [["N", "2"]] and plan["deletes"] == [["S", "1"]]
          and plan["unchanged"] == 1,
          "a composite key matches on all its parts, not on the first")

    # ---- classify: duplicate keys, the pinned defect
    src = rows(("a", "1"), ("dup", "x"), ("dup", "y"))
    tgt = rows(("a", "1"), ("dup", "x"))
    plan = classify(src, tgt, ["V"])
    check(plan["held"] == [{"key": ["dup"], "source": ["r1", "r2"], "target": ["r1"]}],
          "a key two source rows share is held back, with the rows named  <-- pinned defect")
    check(not plan["deletes"] and not plan["updates"] and not plan["inserts"],
          "the held key is not inserted, updated or deleted, so the published row stays  "
          "<-- pinned defect")
    # The naive join this replaces: one dict per side keeps the last row, so the target row
    # for 'dup' reads 'x', the source reads 'y', and an update would overwrite a row that
    # still has a twin. Pin that the naive answer differs from the safe one.
    naive_src = {k[0]: c["V"] for _, k, c in src}
    naive_tgt = {k[0]: c["V"] for _, k, c in tgt}
    check(naive_src["dup"] != naive_tgt["dup"],
          "the naive keyed join would have planned an update for the duplicated key  "
          "<-- pinned defect")
    plan = classify(rows(("dup", "x"), ("dup", "y")), rows(("dup", "y"), ("dup", "x")), ["V"])
    check(plan["duplicate_identical"] == 1 and not plan["held"],
          "a duplicated key with the same rows on both sides, in any order, is no change")
    plan = classify(rows(("dup", "x"), ("dup", "x")), rows(("dup", "x"), ("dup", "y")), ["V"])
    check(len(plan["held"]) == 1 and plan["duplicate_identical"] == 0,
          "same row count but different rows under a duplicated key is held back")
    plan = classify(rows(("d", "1"), ("d", "2")), rows(("d", "3"), ("d", "4")), ["V"])
    check(len(plan["held"]) == 1 and plan["duplicate_identical"] == 0,
          "a duplicated key whose rows all differ between the sides is held back, not no "
          "change  <-- pinned defect")
    plan = classify(rows(("n", "1"), ("n", "2")), rows(("z", "1")), ["V"])
    check([h["key"] for h in plan["held"]] == [["n"]] and plan["deletes"] == [["z"]]
          and not plan["inserts"],
          "two new source rows under one key are held back, not inserted twice")
    plan = classify(rows(("z", "1")), rows(("n", "1"), ("n", "2")), ["V"])
    check([h["key"] for h in plan["held"]] == [["n"]] and plan["inserts"] == [["z"]]
          and not plan["deletes"],
          "two target rows under a key the source dropped are held back, not both deleted")
    plan = classify(rows(("d", "1.0"), ("d", "2.0")), rows(("d", "2"), ("d", "1")), ["V"], 0.01)
    check(plan["duplicate_identical"] == 1,
          "the duplicate match uses the tolerance too")

    # ---- classify: tolerance noise, the pinned defect
    plan = classify(rows(("a", "10.000000001")), rows(("a", "10.0")), ["V"], 0.01)
    check(plan["unchanged"] == 1 and plan["suppressed"] == [["a"]] and not plan["updates"],
          "a 1e-9 difference is suppressed and listed, not rewritten every night  "
          "<-- pinned defect")
    plan = classify(rows(("a", "11.8")), rows(("a", "10.0")), ["V"], 0.01)
    check(len(plan["updates"]) == 1 and not plan["suppressed"],
          "a real 1.8 difference is still an update at the same tolerance")
    plan = classify(rows(("a", "10.000000001")), rows(("a", "10.0")), ["V"])
    check(len(plan["updates"]) == 1, "with no tolerance the same noise is an update")

    # ---- cost
    plan = classify(rows(("a", "1"), ("b", "2"), ("c", "3")),
                    rows(("a", "1"), ("b", "9"), ("d", "4")), ["V"])
    check(delta_cost(plan) == (4, 6),
          "1 insert + 2 for the update + 1 delete is 4 delta rows against 3 + 3 for delete-all")

    # ---- guard
    big = {"inserts": [], "updates": [], "deletes": [], "held": [], "held_rows": 0,
           "shared_keys": 1000, "source_rows": 1000, "target_rows": 1000}

    def rails(**over):
        p = dict(big)
        p.update(over)
        return guard(p, False, 0.10, 0.02, 50)

    check(rails() == [], "a plan with no change trips no rail")
    check("(2% of 1000 target rows)" in rails(deletes=[["k"]] * 21)[0]
          and "(10% of 1000 target rows)" in rails(updates=[{"columns": ["V"]}] * 101)[0],
          "a rail message names the percent and the target size it was figured from")
    check(guard(dict(big, target_rows=0, shared_keys=0, inserts=[["k"]]), False, 1.0, 1.0, 50)
          == [], "an empty target counts as one row, so one insert fits a ceiling of 100%")
    check(rails(deletes=[["k"]] * 20) == [] and len(rails(deletes=[["k"]] * 21)) == 1,
          "deletes at exactly 2% pass and one more trips the delete rail")
    check(len(rails(updates=[{"columns": ["V"]}] * 101)) == 1 and rails(updates=[{"columns": ["V"]}] * 100) == [],
          "changes at exactly 10% pass and one more trips the change rail")
    check(len(rails(deletes=[["k"]] * 200)) == 2,
          "a plan can trip both rails and each is reported")
    check(len(rails(shared_keys=0)) == 1 and "no key is on both sides" in rails(shared_keys=0)[0],
          "two populated tables with no shared key are refused by name  <-- pinned defect")
    check(rails(shared_keys=0, source_rows=0) == [] and rails(shared_keys=0, target_rows=0) == [],
          "an empty side is a first load or an empty source, not a spelling mismatch")
    check(guard(dict(big, deletes=[["k"]] * 500), True, 0.1, 0.02, 50) == [],
          "--force-large lifts the delete and change rails")
    check(len(guard(dict(big, held=[{}] * 51), True, 0.1, 0.02, 50)) == 1,
          "--force-large does not lift the duplicate ceiling")
    check(len(guard(dict(big, held=[{}] * 51), False, 0.1, 0.02, 50)) == 1
          and guard(dict(big, held=[{}] * 50), False, 0.1, 0.02, 50) == [],
          "fifty held keys pass and fifty-one trip the duplicate rail")
    check(len(guard(dict(big, target_rows=0, shared_keys=0, inserts=[["k"]] * 5), False,
                    0.1, 0.02, 50)) == 1,
          "loading an empty target needs --force-large, the ceiling cannot divide by zero")
    check(guard(dict(big, target_rows=0, shared_keys=0, inserts=[["k"]] * 2), False,
                1.0, 1.0, 50)[0].startswith("2 changed rows exceeds the ceiling of 1 (100% of 0 "
                                            "target rows, counted as 1)"),
          "an empty target counts as one row, so two inserts trip a ceiling of 100%, and the "
          "message says the target had 0 rows")
    wrong = rails(held_rows=201, held=[{}] * 3, source_rows=1000, target_rows=1000)
    check(len(wrong) == 1 and "201 rows sit under the held-back duplicate keys" in wrong[0]
          and "above the ceiling of 200 (10% of the 2000 rows read)" in wrong[0] and rails(held_rows=200) == [],
          "rows under held keys above 10% of the rows read are refused, 200 of 2000 pass  "
          "<-- pinned defect")
    check(len(guard(dict(big, held_rows=500), True, 0.1, 0.02, 50)) == 1,
          "--force-large does not lift the held-rows rail")
    raises(lambda: check_min_rows(0, 1), "an empty source is refused", InputRefused)
    check(check_min_rows(1, 1) is None, "a source at the floor passes")

    # ---- verdict and report
    check(verdict(big, ["x"])[0] == 1 and "REFUSED" in verdict(big, ["x"])[1],
          "a tripped rail gives exit 1")
    check(verdict(dict(big), []) == (0, "VERDICT: NO CHANGE, nothing to write."),
          "a plan with no change says so")
    ready = [verdict(dict(big, inserts=i, updates=u, deletes=d), [])
             for i, u, d in (([["a"]], [], []), ([], [{}], []), ([], [], [["a"]]),
                             ([["a"]], [{}], []))]
    check(all(code == 0 and text.startswith("VERDICT: PLAN READY") for code, text in ready),
          "an insert alone, an update alone, a delete alone and an insert with an update "
          "each make a plan  <-- pinned defect")
    stuck = verdict(dict(big, held=[{}]), [])
    check(stuck[0] == 4 and stuck[1].startswith("VERDICT: HELD BACK, 1 duplicate key(s)")
          and "NO CHANGE" not in stuck[1],
          "a plan with only held keys is not called no change and exits 4  <-- pinned defect")
    mixed = verdict(dict(big, held=[{}], updates=[{}]), [])
    check(mixed[0] == 4 and mixed[1].startswith("VERDICT: PLAN READY, 1 update(s)")
          and mixed[1].endswith("1 held back.")
          and verdict(dict(big, held=[{}]), ["x"])[0] == 1,
          "held keys beside a change still give a plan but exit 4, and a tripped rail still "
          "wins  <-- pinned defect")
    plan = classify(rows(("a", "1"), ("b", "2"), ("c", "3")),
                    rows(("a", "1"), ("b", "9"), ("d", "4")), ["V"])
    check(verdict(plan, [])[0] == 0 and "1 update(s), 1 insert(s), 1 delete(s)" in verdict(plan, [])[1],
          "a plan with changes gives exit 0 and counts them")
    text = "\n".join(describe(plan, ["V"], ["K"], 10))
    check("update       1" in text and "keyed plan 4, delete-all plus insert-all 6" in text,
          "the report prices the plan against delete-all")
    many = classify(rows(*[("k%02d" % i, "1") for i in range(5)]), [], ["V"])
    text = "\n".join(describe(many, ["V"], ["K"], 2))
    check("... and 3 more" in text and "k00" in text and "k02" not in text,
          "a long list is cut at --sample and says how many it left out")
    text = "\n".join(describe(many, ["V"], ["K"], 0))
    check("inserts:" in text and "k00" not in text, "--sample 0 lists no key")
    text = "\n".join(describe(many, ["V"], ["K"], 5))
    check("... and" not in text and "k04" in text,
          "a list exactly as long as --sample is not cut")
    check("deletes:" not in text and "held back, a person" not in text,
          "a heading with no key under it is not printed")
    held = classify(rows(("d", "1"), ("d", "2")), [], ["V"])
    check("source r0, r1, target none" in "\n".join(describe(held, ["V"], ["K"], 5)),
          "a held key is listed with the rows of each side")
    noisy_plan = classify(rows(("a", "10.000000001"), ("b", "1")),
                          rows(("a", "10.0"), ("b", "1")), ["V"], 0.01)
    text = "\n".join(describe(noisy_plan, ["V"], ["K"], 10))
    check("noise inside tolerance, not rewritten:\n  a" in text
          and plan_document(noisy_plan, ["K"], ["V"], [])["suppressed"] == [["a"]],
          "a key suppressed by the tolerance is listed in the report and the plan file  "
          "<-- pinned defect")
    doc = plan_document(plan, ["K"], ["V"], [])
    check(doc["delta_rows_keyed"] == 4 and doc["delta_rows_delete_all"] == 6
          and doc["format"] == 1 and json.loads(json.dumps(doc)) == doc,
          "the plan document is plain JSON and carries both costs")

    # ---- readers
    path = make("q.csv", "\ufeffK,V\r\n1,a\r\n\r\n2,\"multi\nline\"\r\n3,c\r\n4,\r\n")
    header, raw = read_csv(path)
    check(header == ["K", "V"], "a UTF-8 BOM does not become part of the first column name  "
          "<-- pinned defect")
    check([w for w, _ in raw] == ["line 2", "line 4", "line 6", "line 7"],
          "a blank line and a quoted newline do not move the reported line  <-- pinned defect")
    check(raw[3][1] == {"K": "4", "V": ""} and raw[1][1]["V"] == "multi\nline",
          "an empty last cell reads as blank")
    raises(lambda: read_csv(make("long.csv", "ID,NAME\n1,Main St, Apt 2\n")),
           "a row with more fields than the header is refused, not read with its extra cells "
           "dropped  <-- pinned defect", InputRefused)
    raises(lambda: read_csv(make("short.csv", "ID,NAME\n1\n")),
           "a row with fewer fields than the header is refused, not read as blank  "
           "<-- pinned defect", InputRefused)
    check(read_csv(make("empty.csv", "")) == ([], []), "an empty file has no header and no rows")
    check(read_csv(make("hdr.csv", "K,V\n"))[1] == [], "a header-only file has no rows")
    raises(lambda: read_csv(make("open.csv", 'K,V\n1,a\n2,"b\n3,c\n4,d\n')),
           "a quote that never closes is an error, not one cell that eats the file  "
           "<-- pinned defect", csv.Error)
    raises(lambda: read_csv(make("huge.csv", "K\n" + "x" * 200000 + "\n")),
           "a field over the csv limit raises csv.Error", csv.Error)
    raises(lambda: read_csv(os.path.join(tmp, "missing.csv")),
           "a missing file raises OSError", OSError)
    good = {"features": [{"attributes": {"K": 1, "V": None}},
                         {"attributes": {"K": 2, "V": 2.5, "W": True}}]}
    header, raw = read_features(make("g.json", json.dumps(good)))
    check(header == ["K", "V", "W"] and raw[0][1] == {"K": "1", "V": "", "W": ""}
          and raw[1][1]["W"] == "true" and raw[1][0] == "feature 2",
          "a feature set reads attributes, fills a missing one with blank, and names the feature")
    bom_header, bom_raw = read_features(make("bom.json", "\ufeff" + json.dumps(good)))
    check(bom_header == ["K", "V", "W"] and len(bom_raw) == 2,
          "a feature set saved with a UTF-8 byte order mark is read  <-- pinned defect")
    geo = {"features": [{"attributes": {"K": 1}, "geometry": {"x": 1, "y": 2}},
                        {"attributes": {"K": 2}, "geometry": None},
                        {"attributes": {"K": 3}}]}
    header, raw = read_features(make("geo.json", json.dumps(geo)))
    check(header == ["K", "@geometry"] and raw[0][1]["@geometry"] == '{"x": 1, "y": 2}'
          and raw[1][1]["@geometry"] == "" and raw[2][1]["@geometry"] == "",
          "a feature set with geometry reads it into one text column, blank when missing  "
          "<-- pinned defect")
    cases = [
        ("bad.json", "{not json", "is not valid JSON", "text that is not JSON"),
        ("err.json", '{"error": {"code": 400, "message": "x"}}', "error response",
         "an error object saved as if it were the table  <-- pinned defect"),
        ("arr.json", "[1]", "not a feature set", "a JSON list"),
        ("page.json", '{"features": [], "exceededTransferLimit": true}', "first page",
         "a truncated first page  <-- pinned defect"),
        ("nofeat.json", '{"fields": []}', "no features list", "a response with no features"),
        ("noattr.json", '{"features": [{"geometry": {}}]}', "feature 1 has no attributes",
         "a feature with no attributes"),
        ("scalar.json", '{"features": [3]}', "feature 1 has no attributes",
         "a feature that is not an object"),
    ]
    for name, text, needle, what in cases:
        message = []
        try:
            read_features(make(name, text))
        except InputRefused as exc:
            message.append(str(exc))
        check(len(message) == 1 and needle in message[0], "refused: %s" % what)
    check(read_features(make("last.json", '{"features": [], "exceededTransferLimit": false}'))
          == ([], []), "exceededTransferLimit false is a complete response")
    check(load(os.path.join(tmp, "g.json"))[0] == ["K", "V", "W"]
          and load(os.path.join(tmp, "q.csv"))[0] == ["K", "V"]
          and load(make("UP.JSON", json.dumps(good)))[0] == ["K", "V", "W"],
          "load picks the reader by extension, ignoring case")

    # ---- columns and keys
    raises(lambda: check_columns(["A"], ["K"], [], "source"),
           "a key column the file does not hold is a usage error")
    raises(lambda: check_columns([], ["K"], [], "source"),
           "a file with no header is a usage error")
    message = []
    try:
        check_columns([], ["K"], [], "source")
    except ValueError as exc:
        message.append(str(exc))
    check(message and "(no columns at all)" in message[0], "and it says the file has no columns")
    raises(lambda: check_columns(["K"], ["K"], ["Z"], "target"),
           "an --ignore column the file does not hold is a usage error")
    check(check_columns(["K", "Z"], ["K"], ["Z"], "target") is None,
          "columns that exist pass")
    check(check_columns(["K", "A"], ["K"], ["@geometry"], "target") is None,
          "--ignore @geometry passes on a file with no geometry column")
    check(compare_columns(["K", "A", "B"], ["K", "B", "A"], ["K"], []) == ["A", "B"],
          "column order does not matter")
    check(compare_columns(["K", "A", "T"], ["K", "A", "U"], ["K"], ["T", "U"]) == ["A"],
          "an --ignore column is left out of the comparison")
    raises(lambda: compare_columns(["K", "A"], ["K", "B"], ["K"], []),
           "a column on one side only is refused and named")
    raises(lambda: compare_columns(["K"], ["K"], ["K"], []),
           "a table with nothing left to compare is refused  <-- pinned defect")
    raises(lambda: to_records([("line 2", {"K": " "})], ["K"], "source"),
           "a whitespace key is blank and refused  <-- pinned defect", InputRefused)
    texts = []
    try:
        to_records([("line %d" % i, {"K": ""}) for i in range(2, 9)], ["K"], "target")
    except InputRefused as exc:
        texts.append(str(exc))
    check(len(texts) == 1 and "7 row(s) on the target side" in texts[0]
          and "line 6, ..." in texts[0] and "line 7" not in texts[0],
          "a blank key lists the first five lines and a count")
    for count, tail in ((5, False), (6, True)):
        texts = []
        try:
            to_records([("line %d" % i, {"K": ""}) for i in range(2, 2 + count)], ["K"], "t")
        except InputRefused as exc:
            texts.append(str(exc))
        check(len(texts) == 1 and (", ..." in texts[0]) == tail,
              "%d blank keys %s the list" % (count, "cut" if tail else "do not cut"))
    texts = []
    try:
        to_records([("line 2", {"A": "1", "B": ""})], ["A", "B"], "source")
    except InputRefused as exc:
        texts.append(str(exc))
    check(len(texts) == 1 and "line 2" in texts[0] and "..." not in texts[0],
          "a blank part of a composite key is refused too")
    check(to_records([("l", {"K": " a "})], ["K"], "source")[0][1] == (" a ",),
          "a key is compared verbatim, with no trimming")

    # ---- parser
    check(option_error(_parse(["--key", "K", "--apply"])) is not None,
          "no files is a usage error")
    for flag in ("--ap", "--appl", "--ou", "--tol", "--ign", "--k", "--force", "--self", "--sam",
                 "--max-d", "--min", "--scal"):
        codes = []
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                _parse(["a.csv", "b.csv", flag])
        except SystemExit as exc:
            codes.append(exc.code)
        check(codes == [2], "%s is refused, not read as a longer flag  <-- pinned defect" % flag)
    args = _parse(["a.csv", "b.csv", "--key", "K", "--key", "J", "--ignore", "Z"])
    check(args.key == ["K", "J"] and args.ignore == ["Z"] and args.apply is False
          and args.tolerance == 0.0 and args.max_change == 0.10,
          "--key and --ignore repeat and the defaults are as documented")

    def bad(extra, needle, label):
        a = _parse(["a.csv", "b.csv"] + extra)
        message = option_error(a)
        check(message is not None and needle in message, label)

    bad([], "--key is required", "no --key is a usage error")
    bad(["--key", "K", "--key", "K"], "twice", "the same --key twice is a usage error")
    bad(["--key", "K", "--ignore", "K"], "both", "a column both key and ignored is refused")
    bad(["--key", "K", "--tolerance", "-1"], "--tolerance", "a negative tolerance is refused")
    bad(["--key", "K", "--tolerance", "nan"], "--tolerance", "a nan tolerance is refused")
    bad(["--key", "K", "--tolerance", "inf"], "--tolerance", "an infinite tolerance is refused")
    bad(["--key", "K", "--sample", "-1"], "--sample", "a negative sample is refused")
    bad(["--key", "K", "--min-rows", "-1"], "--min-rows", "a negative floor is refused")
    bad(["--key", "K", "--max-duplicates", "-1"], "--max-duplicates",
        "a negative duplicate ceiling is refused")
    bad(["--key", "K", "--max-change", "1.5"], "--max-change", "a change fraction above 1 is refused")
    bad(["--key", "K", "--max-delete", "2"], "--max-delete", "a delete fraction above 1 is refused")
    bad(["--key", "K", "--max-delete", "-0.1"], "--max-delete", "a negative delete fraction is refused")
    bad(["--key", "K", "--apply"], "--apply needs --out", "--apply without --out is refused")
    check(option_error(_parse(["a.csv", "b.csv", "--key", "K"])) is None,
          "a plain run has no usage error")
    edge = ["a.csv", "b.csv", "--key", "K"]
    check(all(option_error(_parse(edge + [flag, value])) is None for flag, value in (
        ("--sample", "0"), ("--min-rows", "0"), ("--max-duplicates", "0"),
        ("--max-change", "0"), ("--max-change", "1"), ("--max-delete", "0"),
        ("--max-delete", "1"), ("--tolerance", "0"))),
          "zero, and a fraction of exactly 1, are valid option values  <-- pinned defect")

    # ---- end to end through main, with real files
    src = make("src.csv", "SEG,NAME,LEN,EDITED\n" + "".join(
        "S%02d,Road %02d,%d.5,2026-01-01\n" % (i, i, i) for i in range(1, 41)))
    tgt_text = "SEG,NAME,LEN,EDITED\n" + "".join(
        "S%02d,Road %02d,%d.5,2025-12-31\n" % (i, i, i) for i in range(1, 41))
    tgt = make("tgt.csv", tgt_text)
    out = os.path.join(tmp, "plan.json")
    base = [src, tgt, "--key", "SEG", "--ignore", "EDITED"]
    code, so, se = run_cli(base)
    check(code == 0 and "VERDICT: NO CHANGE" in so and se == "",
          "identical tables apart from an ignored column plan nothing and exit 0")
    check("keyed plan 0, delete-all plus insert-all 80" in so,
          "and the report prices the saving: 0 delta rows against 80")
    code, so, se = run_cli([src, tgt, "--key", "SEG"])
    check(code == 1 and "REFUSED" in so, "without --ignore every row differs and a rail refuses")

    def line(i, name=None, edited="2025-12-31"):
        return "S%02d,%s,%d.5,%s\n" % (i, name or "Road %02d" % i, i, edited)

    # Target: S05 renamed (update), S39 missing (insert), S99 extra (delete), S02 twice (held).
    tgt2 = make("tgt2.csv", "SEG,NAME,LEN,EDITED\n" + "".join(
        line(i, "Road 5x" if i == 5 else None) for i in range(1, 41) if i != 39)
        + "S99,Gone,1.5,2025-12-31\n" + line(2))
    loose = ["--max-delete", "0.1"]
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED"])
    check(code == 1 and "1 deletes exceeds the ceiling of 0" in so,
          "on a 41 row target one delete trips the 2% rail, which is 0 rows")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED"] + loose)
    check(code == 4 and "update       1" in so and "insert       1" in so
          and "delete       1" in so and "held back    1" in so,
          "one update, one insert, one delete and one held duplicate plan together, exit 4  <-- pinned defect")
    check("S02  source line 3, target line 3, line 42" in so,
          "the held key names the lines of both files")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED",
                            *loose, "--out", out])
    check(code == 4 and "Check only." in so and not os.path.exists(out),
          "without --apply nothing is written  <-- pinned defect")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED"] + loose)
    check(code == 4 and "not written" not in so and "Check only" not in so,
          "with no --out the report says nothing about a plan file")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED",
                            *loose, "--out", out, "--ap"])
    check(code == 64 and "unrecognized arguments: --ap" in se and not os.path.exists(out),
          "--ap through main exits 64 and writes nothing  <-- pinned defect")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED",
                            *loose, "--out", out,
                            "--apply"])
    with open(out, encoding="utf-8") as handle:
        written = json.load(handle)
    check(code == 4 and "wrote " in so and written.get("inserts") == [["S39"]],
          "--apply writes the plan file")
    check(written.get("updates") == [{"key": ["S05"], "columns": ["NAME"]}]
          and written.get("deletes") == [["S99"]] and written.get("held")[0]["key"] == ["S02"],
          "and the file holds the update with its column, the delete and the held key")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED",
                            *loose, "--out", out,
                            "--apply"])
    check(code == 2 and "could not write" in se and "VERDICT: PLAN READY" in so,
          "--apply never replaces a plan file that exists, and the verdict still prints  "
          "<-- pinned defect")
    big_out = os.path.join(tmp, "big.json")
    code, so, se = run_cli([src, tgt, "--key", "SEG", "--force-large", "--sample", "1",
                            "--out", big_out, "--apply"])
    with open(big_out, encoding="utf-8") as handle:
        big_plan = json.load(handle)
    check(code == 0 and "... and 39 more" in so and len(big_plan["updates"]) == 40,
          "--sample cuts the report and the plan file still lists every update")
    check(big_plan["refused"] == [] and big_plan["delta_rows_keyed"] == 80
          and big_plan["delta_rows_delete_all"] == 80,
          "and a plan that updates every row costs as much as the delete-all it replaces")
    code, so, se = run_cli([src, tgt, "--key", "SEG", "--ignore", "EDITED", "--out", src])
    check(code == 64 and "also an input" in se, "--out naming the source is refused, on a dry run")
    code, so, se = run_cli([src, tgt, "--key", "SEG", "--ignore", "EDITED", "--out", tgt])
    check(code == 64 and "also an input" in se, "--out naming the target is refused too")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED", "--out",
                            os.path.join(tmp, "nodir", "p.json"), "--apply",
                            *loose])
    check(code == 2, "a plan file in a directory that does not exist exits 2, not 1")

    # ---- refusals through main
    out2 = os.path.join(tmp, "refused.json")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED", "--out", out2,
                            "--apply"])
    check(code == 1 and "not written, because the plan was refused" in so
          and not os.path.exists(out2),
          "a refused plan writes no file even with --apply  <-- pinned defect")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED", "--force-large"])
    check(code == 4, "--force-large lifts the change and delete rails")
    code, so, se = run_cli([make("e.csv", "SEG,NAME,LEN,EDITED\n"), tgt, "--key", "SEG"])
    check(code == 3 and "below --min-rows 1" in so, "an empty source is refused with exit 3")
    code, so, se = run_cli([make("e.csv", "SEG,NAME,LEN,EDITED\n"), tgt, "--key", "SEG",
                            "--min-rows", "0", "--force-large"])
    check(code == 0 and "delete       40" in so, "--min-rows 0 plans the deletion of the target")
    code, so, se = run_cli([make("blank.csv", "SEG,NAME,LEN,EDITED\n,x,1,2\n"), tgt, "--key", "SEG"])
    check(code == 3 and "blank key" in so, "a blank key is refused with exit 3, never skipped")
    # A cp1252 file (Excel's default) decoded with replacement characters turned two different
    # accented letters into the same U+FFFD, so a real edit read as NO CHANGE with exit 0.
    cp_pair = []
    for name, letter in (("cp_s.csv", b"\xf1"), ("cp_t.csv", b"\xf6")):
        cp_pair.append(os.path.join(tmp, name))
        with open(cp_pair[-1], "wb") as handle:
            handle.write(b"ID,NAME\n1,Pe" + letter + b"a\n")
    code, so, se = run_cli(cp_pair + ["--key", "ID"])
    check(code == 3 and "cp_s.csv is not valid UTF-8" in so and "NO CHANGE" not in so,
          "a CSV that is not UTF-8 is refused with exit 3, not read as NO CHANGE  "
          "<-- pinned defect")
    bad_json = os.path.join(tmp, "cp.json")
    with open(bad_json, "wb") as handle:
        handle.write(b'{"features": [{"attributes": {"ID": 1, "NAME": "Pe\xf1a"}}]}')
    texts = []
    try:
        load(bad_json)
    except InputRefused as exc:
        texts.append(str(exc))
    check(len(texts) == 1 and "not valid UTF-8" in texts[0] and "not valid JSON" not in texts[0],
          "a feature set that is not UTF-8 is refused as bad encoding, not as bad JSON  "
          "<-- pinned defect")
    # A saved feature set holds a date as epoch milliseconds, a CSV export as formatted text, so
    # every row reads as an update. The rail refuses it, and the message names the column.
    mixed_src = make("date_s.json", json.dumps({"features": [
        {"attributes": {"ID": i, "NAME": "n", "EDITED": 1767225600000 + i}} for i in range(1, 41)]}))
    mixed_tgt = make("date_t.csv", "ID,NAME,EDITED\n" + "".join(
        "%d,n,2026-01-01\n" % i for i in range(1, 41)))
    code, so, se = run_cli([mixed_src, mixed_tgt, "--key", "ID"])
    check(code == 1 and "Column(s) EDITED differ on every updated row" in so,
          "a date as epoch milliseconds against text is a refusal that names the column  "
          "<-- pinned defect")
    code, so, se = run_cli([mixed_src, mixed_tgt, "--key", "ID", "--ignore", "EDITED"])
    check(code == 0 and "NO CHANGE" in so, "--ignore on that column plans no change")
    check(always_changed_hint([]) == "" and always_changed_hint([{"columns": ["A"]}]) == ""
          and always_changed_hint([{"columns": ["A"]}, {"columns": ["B"]}]) == ""
          and "Column(s) A, B differ" in always_changed_hint(
              [{"columns": ["B", "A", "C"]}, {"columns": ["A", "B"]}]),
          "the hint needs two updates and names only the columns every update shares")
    check("--scale COLUMN=PLACES" in always_changed_hint([{"columns": ["A"]}] * 2)
          and "epoch" not in always_changed_hint([{"columns": ["A"]}] * 2),
          "the hint is not worded for dates and points a number column at --scale")
    okcsv = make("ok2.csv", "ID,V\n1,2\n")
    code, so, se = run_cli([make("blank1.csv", "\nID,V\n1,2\n"), okcsv, "--key", "ID"])
    check(code == 3 and "blank1.csv starts with a blank line" in so
          and "unquoted comma" not in so,
          "a CSV that starts with a blank line is refused with exit 3 and the message says so  "
          "<-- pinned defect")
    u16 = os.path.join(tmp, "u16.csv")
    with open(u16, "wb") as handle:
        handle.write("ID,V\r\n1,2\r\n".encode("utf-16"))
    code, so, se = run_cli([u16, okcsv, "--key", "ID"])
    check(code == 3 and "not valid UTF-8" in so, "a UTF-16 CSV is refused with exit 3  "
          "<-- pinned defect")
    code, so, se = run_cli([make("semi.csv", "ID;V\n1;2\n"), okcsv, "--key", "ID"])
    check(code == 64, "a semicolon CSV exits 64, because the key is not a column")
    hdr_tgt = make("hdr_t.csv", "ID,V\n")
    first_src = make("first_s.csv", "ID,V\n" + "".join("S%d,x\n" % k for k in range(40)))
    code, so, se = run_cli([first_src, hdr_tgt, "--key", "ID"])
    check(code == 1 and "REFUSED" in so,
          "a header-only target is refused by the change rail without --force-large  "
          "<-- pinned defect")
    code, so, se = run_cli([first_src, hdr_tgt, "--key", "ID", "--force-large"])
    check(code == 0 and "40 insert(s)" in so,
          "a header-only target plans a first load with --force-large")
    pad = "".join("p%d,x" % k + "\n" for k in range(40))
    code, so, se = run_cli([make("ucs.csv", "ID,NAME\n1,Main St, Apt 2\n" + pad),
                            make("uct.csv", "ID,NAME\n1,Main St, Apt 3\n" + pad), "--key", "ID"])
    check(code == 3 and "line 2 has 3 field(s) but the header has 2" in so
          and "NO CHANGE" not in so,
          "an unquoted comma is refused with exit 3, not read as NO CHANGE  <-- pinned defect")
    dup_src = make("dh.csv", "SEG,V,V\na,1,2\nb,1,2\n")
    dup_tgt = make("dh2.csv", "SEG,V,V\na,9,2\nb,1,2\n")
    code, so, se = run_cli([dup_src, dup_tgt, "--key", "SEG"])
    check(code == 3 and "repeats the column name(s) V" in so,
          "a repeated CSV header is refused, not read as NO CHANGE  <-- pinned defect")
    code, so, se = run_cli([make("p.json", '{"features": [], "exceededTransferLimit": true}'),
                            tgt, "--key", "SEG"])
    check(code == 3 and "first page" in so, "a truncated feature set is refused with exit 3")
    code, so, se = run_cli([src, tgt, "--key", "@geometry"])
    check(code == 64 and "columns not in the file: @geometry" in se,
          "--key @geometry on a CSV is exit 64, not a crash  <-- pinned defect")
    code, so, se = run_cli([src, tgt, "--key", "NOPE"])
    check(code == 64 and "columns not in the file: NOPE" in se, "a wrong --key is exit 64")
    code, so, se = run_cli([os.path.join(tmp, "nope.csv"), tgt, "--key", "SEG"])
    check(code == 2 and "error:" in se, "a missing input is exit 2")
    code, so, se = run_cli([make("huge2.csv", "SEG\n" + "x" * 200000 + "\n"), tgt, "--key", "SEG"])
    check(code == 2 and "field larger than field limit" in se, "an oversized field is exit 2")
    code, so, se = run_cli([src, tgt, "--key", "SEG", "--ignore", "EDITED", "--sample", "-1"])
    check(code == 64 and "--sample" in se, "a usage error through main is exit 64")
    code, so, se = run_cli(["--help"])
    check(code == 0 and "keyed delta" in so, "--help exits 0")
    code, so, se = run_cli([])
    check(code == 64 and "SOURCE and a TARGET" in se, "no arguments is exit 64")
    code, so, se = run_cli([src, "--key", "SEG"])
    check(code == 64 and "SOURCE and a TARGET" in se,
          "a SOURCE with no TARGET is exit 64, not a crash  <-- pinned defect")

    # ---- held keys and nothing else: the table is not known to be in step
    pad = "".join("B%02d,%d\n" % (i, i) for i in range(20))
    hs = make("hs.csv", "ID,V\nA,1\nA,2\n" + pad)
    ht = make("ht.csv", "ID,V\nA,1\n" + pad)
    hout = os.path.join(tmp, "held.json")
    code, so, se = run_cli([hs, ht, "--key", "ID", "--out", hout, "--apply"])
    with open(hout, encoding="utf-8") as handle:
        held_plan = json.load(handle)
    check(code == 4 and "VERDICT: HELD BACK, 1 duplicate key(s)" in so and "NO CHANGE" not in so,
          "only a held duplicate key gives exit 4 and says so, not NO CHANGE  <-- pinned defect")
    check(held_plan["held"][0]["key"] == ["A"] and not held_plan["updates"],
          "and the plan file still names the held key")
    ws = make("ws.csv", "ID,V\nA,1\nA,2\n")
    code, so, se = run_cli([ws, ws, "--key", "ID"])
    check(code == 0 and "VERDICT: NO CHANGE" in so,
          "the same duplicate rows on both sides are still no change and exit 0")

    # ---- the swapped-spelling case: every key differs between the sides
    lower = make("lower.csv", "SEG,NAME,LEN,EDITED\n" + "".join(
        "s%02d,Road %02d,%d.5,x\n" % (i, i, i) for i in range(1, 41)))
    code, so, se = run_cli([lower, tgt, "--key", "SEG", "--ignore", "EDITED"])
    check(code == 1 and "no key is on both sides" in so,
          "a key spelled in another case on one side is refused, not planned as delete-all  "
          "<-- pinned defect")

    # ---- feature set input through main, against a CSV target
    feat = {"features": [{"attributes": {"SEG": "S%02d" % i, "NAME": "Road %02d" % i,
                                         "LEN": "%d.5" % i, "EDITED": 1}}
                         for i in range(1, 41)]}
    code, so, se = run_cli([make("f.json", json.dumps(feat)), tgt, "--key", "SEG",
                            "--ignore", "EDITED"])
    check(code == 0 and "NO CHANGE" in so, "a saved feature set compares to a CSV")

    # ---- tolerance through main
    noisy_t = make("nt.csv", "SEG,X\nA,10.0\nB,5.0\n")
    noisy_s = make("ns.csv", "SEG,X\nA,10.000000001\nB,5.0\n")
    code, so, se = run_cli([noisy_s, noisy_t, "--key", "SEG", "--tolerance", "0.01"])
    check(code == 0 and "NO CHANGE" in so and "noise inside tolerance, no change      1" in so,
          "tolerance turns a 1e-9 difference into a counted no-change")

    # ---- geometry, codes under a tolerance, an empty feature set, a key that is not a key
    def feature_file(name, shapes):
        doc = {"features": [{"attributes": {"ID": k, "V": "a"}, "geometry": g}
                            for k, g in shapes]}
        return make(name, json.dumps(doc))

    g1 = feature_file("g1.json", [("1", {"x": 1, "y": 2}), ("2", {"x": 5, "y": 5})])
    g2 = feature_file("g2.json", [("1", {"x": 500, "y": 900}), ("2", {"x": 5, "y": 5})])
    code, so, se = run_cli([g1, g2, "--key", "ID", "--max-change", "0.6"])
    check(code == 0 and "update       1" in so and "  1  columns @geometry" in so,
          "a feature that moved with the same attributes is an update, not NO CHANGE  "
          "<-- pinned defect")
    code, so, se = run_cli([g1, g2, "--key", "ID", "--tolerance", "0.5"])
    check("update       1" in so, "and a tolerance of 0.5 does not hide a move of hundreds")
    g3 = feature_file("g3.json", [("1", {"x": 1.001, "y": 2}), ("2", {"x": 5, "y": 5})])
    code, so, se = run_cli([g1, g3, "--key", "ID"])
    check("update       1" in so, "a shape off by 0.001 is an update with no tolerance")
    code, so, se = run_cli([g1, g3, "--key", "ID", "--tolerance", "0.01"])
    check(code == 0 and "NO CHANGE" in so and "noise inside tolerance, no change      1" in so,
          "and the same shape inside a tolerance of 0.01 is counted noise")
    g4 = feature_file("g4.json", [("1", {"x": 1.5, "y": 2}), ("2", {"x": 5, "y": 5})])
    code, so, se = run_cli([g1, g4, "--key", "ID", "--tolerance", "0.6"])
    check(code == 0 and "VERDICT: NO CHANGE" in so,
          "a move of 0.5 in x is noise at a tolerance of 0.6")
    csv_t = make("gt.csv", "ID,V\n1,a\n2,a\n")
    code, so, se = run_cli([g1, csv_t, "--key", "ID"])
    check(code == 64 and "@geometry" in se and "--ignore" in se,
          "a feature set with geometry against a CSV is refused until --ignore @geometry")
    code, so, se = run_cli([g1, csv_t, "--key", "ID", "--ignore", "@geometry"])
    check(code == 0 and "NO CHANGE" in so, "and --ignore @geometry compares the attributes only")
    zs = make("zs.csv", "K,CODE,V\nA,02101,1\nB,5,2\n")
    zt = make("zt.csv", "K,CODE,V\nA,2101,1\nB,5,2\n")
    code, so, se = run_cli([zs, zt, "--key", "K", "--tolerance", "0.5", "--max-change", "0.6"])
    check(code == 0 and "update       1" in so and "CODE" in so,
          "02101 changed to 2101 is an update even with a tolerance  <-- pinned defect")
    code, so, se = run_cli([make("empty.json", '{"features": [], "exceededTransferLimit": false}'),
                            tgt, "--key", "SEG"])
    check(code == 3 and "below --min-rows 1" in so,
          "an empty feature set source is refused with exit 3, not a column error  "
          "<-- pinned defect")
    cyc = "".join("%s,%d\n" % ("AB"[i % 2], i) for i in range(40))
    cyc_s = "".join("%s,%d\n" % ("AB"[i % 2], i + 5) for i in range(40))
    code, so, se = run_cli([make("cs.csv", "CAT,V\n" + cyc_s + "C,1\n"),
                            make("ct.csv", "CAT,V\n" + cyc), "--key", "CAT"])
    check(code == 1 and "rows sit under the held-back duplicate keys" in so
          and "VERDICT: REFUSED" in so,
          "a key column that is not a key is refused, not a PLAN READY on one insert  "
          "<-- pinned defect")

    # ---- storage scale: compare at the declared decimals, and say which were used
    D = decimal.Decimal
    check(at_scale("2.675", 2) == D("2.68") and at_scale("-2.675", 2) == D("-2.68"),
          "a tie rounds half away from zero on the text as written, not through a binary float")
    check(at_scale("2.5", 0) == D("3") and at_scale("-2.5", 0) == D("-3")
          and at_scale("3.5", 0) == D("4") and at_scale("5", 2) == D("5.00"),
          "scale 0 rounds ties away from zero both ways and an integer gains no error")
    check(at_scale("x", 2) is None and at_scale("nan", 2) is None and at_scale("02101", 2) is None
          and at_scale("", 2) is None and at_scale("1e999", 2) is None,
          "text, nan, a leading-zero code and an overflow have no scaled value")
    check(at_scale("1e308", 38) == D("1e308") and at_scale("1e-400", 2) == D("0.00")
          and at_scale("-0.001", 2) == at_scale("0.001", 2),
          "the largest and smallest floats do not overflow the decimal context, and "
          "negative zero equals zero  <-- pinned defect")
    check(at_scale("10.0000000004", 2) == at_scale("10", 2) and at_scale("1e-05", 2) == D("0.00"),
          "a float repr with an exponent is read as a number")
    changed, noisy, rounded = differing_full({"V": "10.0000000004"}, {"V": "10.00"}, ["V"], 0,
                                             {"V": 2})
    check(changed == [] and not noisy and rounded,
          "a float and the value stored at 2 places are the same at scale 2, with no tolerance")
    changed, noisy, rounded = differing_full({"V": "10.0000000004"}, {"V": "10.00"}, ["V"], 0)
    check(changed == ["V"] and not rounded,
          "with no scale declared the same pair is a change, as before  <-- pinned defect")
    changed, noisy, rounded = differing_full({"V": "10.006"}, {"V": "10.00"}, ["V"], 0.01,
                                             {"V": 2})
    check(changed == ["V"] and values_equal("10.006", "10.00", 0.01),
          "an edit of 0.006 that stores as 10.01 is a change at scale 2, though a tolerance of "
          "0.01 would hide it  <-- pinned defect")
    check(differing_full({"V": "1.005"}, {"V": "1.01"}, ["V"], 0.005, {"V": 2})[2]
          and not values_equal("1.005", "1.01", 0.005),
          "a tie at scale 2 is the same as the stored 1.01, which float tolerance 0.005 "
          "misses  <-- pinned defect")
    check(differing_full({"V": "12.344"}, {"V": "12.346"}, ["V"], 0, {"V": 2})[0] == ["V"],
          "two values that round to different stored numbers are a change")
    check(differing_full({"V": "abc"}, {"V": "abd"}, ["V"], 0, {"V": 2})[0] == ["V"]
          and differing_full({"V": "abc"}, {"V": "1"}, ["V"], 0, {"V": 2})[0] == ["V"]
          and differing_full({"V": "1"}, {"V": "abc"}, ["V"], 0, {"V": 2})[0] == ["V"],
          "a scale on a column that holds text on either side changes nothing")
    check(differing_full({"V": "02101"}, {"V": "2101"}, ["V"], 0, {"V": 2})[0] == ["V"],
          "a leading-zero code is not rounded to a number  <-- pinned defect")
    changed, noisy, rounded = differing_full(
        {"A": "1.0000004", "B": "5.001"}, {"A": "1.00", "B": "5.0"}, ["A", "B"], 0.01, {"A": 2})
    check(changed == [] and noisy and rounded,
          "a column under a scale and a column under the tolerance can each be no change")
    changed, noisy, rounded = differing_full({"A": "1.0000004", "B": "5.0"},
                                             {"A": "1.00", "B": "5.0"}, ["A", "B"], 0)
    check(changed == ["A"], "a column with no scale keeps comparing exact text")
    plan = classify(rows(("a", "10.0000000004"), ("b", "7.5"), ("c", "3.104")),
                    rows(("a", "10.00"), ("b", "7.5"), ("c", "3.10")), ["V"], 0, {"V": 2})
    check(plan["unchanged"] == 3 and plan["rounded"] == [["a"], ["c"]]
          and plan["scales"] == {"V": 2} and not plan["updates"],
          "a plan lists the keys that matched only after rounding and records the scale used")
    plan = classify(rows(("a", "10.0000000004")), rows(("a", "10.00")), ["V"], 0)
    check(plan["rounded"] == [] and plan["scales"] == {} and len(plan["updates"]) == 1,
          "with no scale the plan records none and the rows are updates")
    plan = classify(rows(("d", "1.0000004"), ("d", "2.0000004")),
                    rows(("d", "2.00"), ("d", "1.00")), ["V"], 0, {"V": 2})
    check(plan["duplicate_identical"] == 1 and not plan["held"],
          "the duplicate match uses the scale too")
    plan = classify(rows(("a", "10.0000000004"), ("b", "1.234")),
                    rows(("a", "10.00"), ("b", "1.20")), ["V"], 0, {"V": 2})
    text = "\n".join(describe(plan, ["V"], ["K"], 10))
    check("storage scale used, decimal places, half away from zero: V=2" in text
          and "same at storage scale, no change       1" in text
          and "same at storage scale, not rewritten:\n  a" in text
          and text.index("storage scale used") < text.index("rows planned:"),
          "the report says which precision was used and lists the keys it rounded")
    text = "\n".join(describe(classify(rows(("a", "1")), rows(("a", "1")), ["V"]), ["V"],
                             ["K"], 10))
    check("storage scale" not in text, "a run with no --scale prints no scale line")
    doc = plan_document(plan, ["K"], ["V"], [])
    check(doc["scale"] == {"V": 2} and doc["rounded"] == [["a"]]
          and json.loads(json.dumps(doc)) == doc,
          "the plan file records the scale and the rounded keys")
    check(parse_scales(["A=2", "B=0", "C=38", "D=E=3"], ["K"], []) == {
        "A": 2, "B": 0, "C": 38, "D=E": 3} and parse_scales([], ["K"], []) == {},
          "--scale reads COLUMN=N, from 0 to 38, and splits on the last =")
    for spec in ("A", "=2", "A=", "A=x", "A=-1", "A=39", "A=100", "A=1_0", "A= 2", "A=2.0",
                 "A=\u0662"):
        raises(lambda spec=spec: parse_scales([spec], [], []),
               "--scale %s is refused  <-- pinned defect" % ascii(spec))
    raises(lambda: parse_scales(["A=2", "A=3"], [], []), "--scale naming a column twice is refused")
    raises(lambda: parse_scales(["K=2"], ["K"], []), "--scale on a --key column is refused")
    raises(lambda: parse_scales(["Z=2"], ["K"], ["Z"]), "--scale on an --ignore column is refused")
    raises(lambda: parse_scales(["@geometry=2"], [], []), "--scale on @geometry is refused")
    check(option_error(_parse(["a.csv", "b.csv", "--key", "K", "--scale", "V=2"])) is None
          and "COLUMN=N" in option_error(_parse(["a.csv", "b.csv", "--key", "K", "--scale", "V"])),
          "--scale is checked with the other options")

    # ---- storage scale through main, on real files
    def scale_row(i, name=None, length=None):
        return "S%02d,%s,%s,2026-01-0%d\n" % (i, name or "Road %02d" % i,
                                             length or "%d.50" % (100 + i), 1 + i % 2)

    scale_head = "SEG_ID,NAME,LEN_FT,EDITED_ON\n"
    scale_src = make("scale_source.csv", scale_head + "".join(
        scale_row(i, length={21: "121.634", 30: "130.504", 31: "131.506"}.get(
            i, "%d.5000000004" % (100 + i))) for i in range(1, 61)))
    scale_tgt = make("scale_target.csv", scale_head + "".join(
        scale_row(i, "Road 7x" if i == 7 else None) for i in range(1, 61)))
    scale_base = [scale_src, scale_tgt, "--key", "SEG_ID", "--ignore", "EDITED_ON"]
    code, so, se = run_cli(scale_base)
    check(code == 1 and "60 changed rows exceeds the ceiling of 6" in so
          and "storage scale" not in so,
          "without --scale every float reads as an edit and the change rail refuses  "
          "<-- pinned defect")
    code, so, se = run_cli(scale_base + ["--scale", "LEN_FT=2"])
    check(code == 0 and "key: SEG_ID. compared columns: 2\nstorage scale used, decimal places, "
          "half away from zero: LEN_FT=2\nrows planned:\n  unchanged    57\n  update       3\n"
          in so and "same at storage scale, no change       57\n" in so
          and "keyed plan 6, delete-all plus insert-all 120" in so,
          "--scale LEN_FT=2 plans the 3 real edits and 57 rounded rows, and prints the scale")
    check("updates:\n  S07  columns NAME\n  S21  columns LEN_FT\n  S31  columns LEN_FT\n" in so
          and "S30" not in so.split("updates:")[1].split("\n\n")[0]
          and so.endswith("VERDICT: PLAN READY, 3 update(s), 0 insert(s), 0 delete(s), "
                          "0 held back.\n"),
          "the edit that stores as 131.51 is listed and the one that stores as 130.50 is not")
    code, so, se = run_cli(scale_base + ["--tolerance", "0.01", "--max-change", "0.5"])
    check(code == 0 and "S31" not in so.split("updates:")[1].split("\n\n")[0],
          "a tolerance of 0.01 hides the edit of S31, so the scale is the stricter test  "
          "<-- pinned defect")
    scale_out = os.path.join(tmp, "scale_plan.json")
    code, so, se = run_cli(scale_base + ["--scale", "LEN_FT=2", "--out", scale_out, "--apply"])
    with open(scale_out, encoding="utf-8") as handle:
        scale_doc = json.load(handle)
    check(code == 0 and scale_doc["scale"] == {"LEN_FT": 2} and len(scale_doc["rounded"]) == 57
          and len(scale_doc["updates"]) == 3,
          "--apply writes the scale and the rounded keys into the plan file")
    code, so, se = run_cli(scale_base + ["--scale", "LEN_FT=2", "--out",
                                         os.path.join(tmp, "dry_scale.json")])
    check(code == 0 and "Check only." in so
          and not os.path.exists(os.path.join(tmp, "dry_scale.json")),
          "a dry run with --scale writes nothing")
    code, so, se = run_cli(scale_base + ["--scale", "NOPE=2"])
    check(code == 64 and "not compared: NOPE" in se and so == "",
          "--scale on a column the files do not hold is exit 64, not a silent no-op  "
          "<-- pinned defect")
    code, so, se = run_cli(scale_base + ["--scale", "SEG_ID=2"])
    check(code == 64 and "--key or --ignore" in se, "--scale on the key is exit 64")
    code, so, se = run_cli(scale_base + ["--scale", "LEN_FT=two"])
    check(code == 64 and "COLUMN=N" in se and so == "", "a bad --scale value is exit 64")
    feat_rows = [{"attributes": {"SEG_ID": "S%02d" % i, "NAME": "Road %02d" % i,
                                 "LEN_FT": float(100 + i) + 0.50000000000001, "EDITED_ON": 1}}
                 for i in range(1, 41)]
    feat_src = make("scale_feat.json", json.dumps({"features": feat_rows}))
    scale_tgt40 = make("scale_t40.csv", scale_head + "".join(
        scale_row(i) for i in range(1, 41)))
    code, so, se = run_cli([feat_src, scale_tgt40, "--key", "SEG_ID", "--ignore", "EDITED_ON",
                            "--scale", "LEN_FT=2"])
    check(code == 0 and "VERDICT: NO CHANGE" in so
          and "same at storage scale, no change       40" in so,
          "a feature set that holds JSON floats matches a CSV stored at 2 places")
    code, so, se = run_cli([feat_src, scale_tgt40, "--key", "SEG_ID", "--ignore", "EDITED_ON"])
    check(code == 1 and "40 changed rows exceeds the ceiling of 4" in so,
          "and without --scale the same files are 40 false updates")

    # ---- the README demo, rebuilt, so the quoted output is exercised
    def seg(i, name=None, length=None, edited="2026-01-01"):
        return "S%02d,%s,%s,%s\n" % (i, name or "Road %02d" % i, length or "%d.5" % (100 + i),
                                     edited)

    demo_head = "SEG_ID,NAME,LEN_FT,EDITED_ON\n"
    demo_src = make("source.csv", demo_head + "".join(
        seg(i, edited="2026-01-02") for i in range(1, 61)) + seg(21, "Road 21 b"))
    demo_tgt_rows = [seg(i, "Road 7x" if i == 7 else None,
                         "113.500000000001" if i == 13 else None) for i in range(1, 60)]
    demo_tgt = make("target.csv", demo_head + "".join(demo_tgt_rows) + seg(99, "Gone", "1.5"))
    demo = [demo_src, demo_tgt, "--key", "SEG_ID", "--ignore", "EDITED_ON"]
    code, so, se = run_cli(demo)
    check(code == 4 and so.splitlines()[0].endswith("61 row(s), %s 60 row(s)" % demo_tgt)
          and "compared columns: 2\nrows planned:\n  unchanged    56\n  update       2\n"
          "  insert       1\n  delete       1\n"
          "  held back    1 (duplicate key, left as published)\n" in so,
          "the README demo plans 56 unchanged, 2 updates, 1 insert, 1 delete, 1 held back")
    check("keyed plan 6, delete-all plus insert-all 121" in so
          and "held back, a person decides:\n  S21  source line 22, line 62, target line 22\n"
          "\nupdates:\n  S07  columns NAME\n  S13  columns LEN_FT\n\ninserts:\n  S60\n"
          "\ndeletes:\n  S99\n" in so
          and so.endswith("VERDICT: PLAN READY, 2 update(s), 1 insert(s), 1 delete(s), "
                          "1 held back.\n"),
          "and the report prices it at 6 delta rows against 121 and lists each key")
    code, so, se = run_cli(demo + ["--tolerance", "0.01", "--out", os.path.join(tmp, "d.json"),
                                   "--apply"])
    check(code == 4 and "  unchanged    57\n  update       1\n" in so
          and "noise inside tolerance, no change      1\n" in so
          and "keyed plan 4, delete-all plus insert-all 121" in so
          and "noise inside tolerance, not rewritten:\n  S13\n" in so
          and "wrote " in so and so.endswith("VERDICT: PLAN READY, 1 update(s), 1 insert(s), "
                                            "1 delete(s), 1 held back.\n"),
          "with --tolerance 0.01 S13 is noise, listed, and the plan file is written")
    lower_rows = "".join(seg(i).lower() for i in range(1, 60)) + seg(99, "Gone", "1.5").lower()
    lower_tgt = make("lower2.csv", demo_head + lower_rows)
    code, so, se = run_cli([demo_src, lower_tgt, "--key", "SEG_ID", "--ignore", "EDITED_ON"])
    check(code == 1 and "no key is on both sides" in so
          and "60 deletes exceeds the ceiling of 1 (2% of 60 target rows)" in so
          and "119 changed rows exceeds the ceiling of 6 (10% of 60 target rows)" in so
          and "VERDICT: REFUSED, 3 rail(s) tripped. Do not apply this plan." in so,
          "the README lower-case key case trips three rails")
    page = make("page1.json", '{"features": [], "exceededTransferLimit": true}')
    code, so, se = run_cli([page, demo_tgt, "--key", "SEG_ID", "--ignore", "EDITED_ON"])
    check(code == 3 and so == "REFUSED   %s has exceededTransferLimit true, so it holds only "
          "the first page. Planning from a partial source would delete every row it did not "
          "return.\n\nVERDICT: REFUSED\n" % page,
          "the README truncated-page output is exact")

    # ---- unexpected crash is 70, not 1
    original = globals()["build_plan"]
    globals()["build_plan"] = lambda *a: [][0]
    try:
        code, so, se = run_cli([src, tgt, "--key", "SEG"])
    finally:
        globals()["build_plan"] = original
    check(code == 70 and "IndexError" in se and "not a finding" in se,
          "an unexpected exception exits 70, not the 1 that means a rail refused  "
          "<-- pinned defect")

    # ---- console encoding
    raw_stream = io.BytesIO()
    stream = io.TextIOWrapper(raw_stream, encoding="cp1252", errors="strict", newline="\n")
    quiet = sys.stdout
    sys.stdout = stream
    try:
        _tolerate_odd_characters()
        print("key \u2603")
        stream.flush()
    finally:
        sys.stdout = quiet
    check(raw_stream.getvalue() == b"key ?\n",
          "a key the console cannot encode prints as ? instead of a traceback  "
          "<-- pinned defect")
    sys.stdout = io.StringIO()
    try:
        _tolerate_odd_characters()
    finally:
        sys.stdout = quiet
    check(True, "a stream without reconfigure is left alone")

    # ---- the real script, run as a separate process
    script = os.path.abspath(__file__)
    done = subprocess.run([sys.executable, script, src, tgt2, "--key", "SEG", "--ignore",
                           "EDITED", "--force-large"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    check(done.returncode == 4 and "VERDICT: PLAN READY" in done.stdout,
          "the script runs as a process and exits 4 on a plan that holds a key back")
    done = subprocess.run([sys.executable, script, "--self-test", "--bogus"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    check(done.returncode == 64, "an unknown flag exits 64 as a process")

    # ---- the tool opens no socket and writes only with --apply
    with open(script, "r", encoding="utf-8") as handle:
        code_text = handle.read()
    def network_imports(text):
        found = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                found.update(n.split(".")[0] for n in names)
        return found & {"socket", "urllib", "http", "ssl", "requests", "ftplib", "smtplib"}

    check(network_imports(code_text) == set(),
          "the source imports no network module")
    check(network_imports("from urllib.request import urlopen") == {"urllib"}
          and network_imports("import http.client") == {"http"}
          and network_imports("import os") == set(),
          "and the check sees the from form and a dotted import  <-- pinned defect")
    check(all(ord(ch) < 128 for ch in code_text), "the source is pure ASCII")
    check(not any(isinstance(n, ast.JoinedStr) for n in ast.walk(ast.parse(code_text))),
          "the source has no f-strings")

    # ---- the harness itself
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the exception it is looking for")

    def summary(passed_count, failures):
        print(sep)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1  <-- pinned defect")

    # ---- importing the module runs nothing
    spec = importlib.util.spec_from_file_location("keydelta_imported", script)
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.classify([], [], ["V"])["unchanged"] == 0,
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


if __name__ == "__main__":
    sys.exit(main())

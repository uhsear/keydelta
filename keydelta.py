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
    python keydelta.py source.csv target.csv --key SEG_ID --out plan.json --apply
    python keydelta.py response.json target.csv --key SEG_ID --force-large

A key that two rows share is never planned: it is held back and left as published, unless both
sides hold the same rows under it, which is no change. Nothing is written without --apply, and
--apply writes one plan file.

Exit codes: 0 a plan was made and no rail tripped, 1 a rail refused the plan, 2 a file could
not be read or written, 3 an input was refused (blank key, truncated or error response, too
few source rows), 4 the only thing left is held-back duplicate keys, so the published copy is
not known to match, 64 usage error, 70 an unexpected crash, which is never a finding.
"""

import argparse
import ast
import contextlib
import csv
import importlib.util
import io
import json
import math
import os
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
    """A finite float, or None. nan and inf are text, never a number to subtract."""
    try:
        number = float(text)
    except ValueError:
        return None
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


def differing(src_cells, tgt_cells, columns, tolerance):
    """(changed columns, noisy). noisy: some cell differs as text but sits inside tolerance."""
    changed = []
    noisy = False
    for col in columns:
        a, b = src_cells[col], tgt_cells[col]
        if a == b:
            continue
        if values_equal(a, b, tolerance):
            noisy = True
        else:
            changed.append(col)
    return changed, noisy


def group_by_key(records):
    """key tuple -> list of records, in first-seen order. A record is (where, key, cells)."""
    groups = {}
    for rec in records:
        groups.setdefault(rec[1], []).append(rec)
    return groups


def same_rows(src_group, tgt_group, columns, tolerance):
    """True when both groups hold the same rows, in any order (greedy one-to-one match)."""
    if len(src_group) != len(tgt_group):
        return False
    free = list(tgt_group)
    for src in src_group:
        for tgt in free:
            if not differing(src[2], tgt[2], columns, tolerance)[0]:
                free.remove(tgt)
                break
        else:
            return False
    return True


def classify(src_records, tgt_records, columns, tolerance=0.0):
    """Plan the delta that turns the target into the source.

    A key held by more than one row on either side is never inserted, updated or deleted.
    It is held back and left as published, because a join on it cannot say which row the
    other side meant. When both sides hold the same rows under it, it is no change.
    """
    src = group_by_key(src_records)
    tgt = group_by_key(tgt_records)
    plan = {"inserts": [], "updates": [], "deletes": [], "held": [],
            "suppressed": [], "unchanged": 0, "duplicate_identical": 0,
            "shared_keys": 0, "source_rows": len(src_records),
            "target_rows": len(tgt_records)}
    for key in sorted(set(src) | set(tgt)):
        s_group, t_group = src.get(key, []), tgt.get(key, [])
        if s_group and t_group:
            plan["shared_keys"] += 1
        if len(s_group) > 1 or len(t_group) > 1:
            if same_rows(s_group, t_group, columns, tolerance):
                plan["duplicate_identical"] += 1
            else:
                plan["held"].append({
                    "key": list(key),
                    "source": [r[0] for r in s_group],
                    "target": [r[0] for r in t_group]})
        elif not t_group:
            plan["inserts"].append(list(key))
        elif not s_group:
            plan["deletes"].append(list(key))
        else:
            changed, noisy = differing(s_group[0][2], t_group[0][2], columns, tolerance)
            if changed:
                plan["updates"].append({"key": list(key), "columns": changed})
            else:
                plan["unchanged"] += 1
                if noisy:
                    plan["suppressed"].append(list(key))
    return plan


def delta_cost(plan):
    """(keyed, wholesale) delta-table rows on traditional versioning.

    An insert writes one adds row, a delete one deletes row, an update one of each. A
    delete-all plus insert-all writes one deletes row per target row and one adds row per
    source row.
    """
    keyed = len(plan["inserts"]) + 2 * len(plan["updates"]) + len(plan["deletes"])
    return keyed, plan["source_rows"] + plan["target_rows"]


def guard(plan, force_large, max_change, max_delete, max_duplicates):
    """Reasons to refuse the plan. An empty list means no rail tripped."""
    problems = []
    if len(plan["held"]) > max_duplicates:
        problems.append(
            "%d keys are held back as duplicates, above the ceiling of %d. Normal is a few; "
            "this many is systemic and would leave that many rows stale. --force-large does "
            "not override this." % (len(plan["held"]), max_duplicates))
    if force_large:
        return problems
    if plan["source_rows"] and plan["target_rows"] and not plan["shared_keys"]:
        problems.append(
            "no key is on both sides, so every target row would be deleted and every source "
            "row inserted. That is the delete-all this tool exists to avoid. The key columns "
            "probably spell the key differently on the two sides.")
    denominator = max(plan["target_rows"], 1)
    delete_ceiling = int(denominator * max_delete)
    if len(plan["deletes"]) > delete_ceiling:
        problems.append(
            "%d deletes exceeds the ceiling of %d (%.0f%% of %d target rows). A bad source "
            "read must not empty the target. Rerun with --force-large if the deletion is "
            "intended." % (len(plan["deletes"]), delete_ceiling, max_delete * 100, denominator))
    total = len(plan["inserts"]) + len(plan["updates"]) + len(plan["deletes"])
    change_ceiling = int(denominator * max_change)
    if total > change_ceiling:
        problems.append(
            "%d changed rows exceeds the ceiling of %d (%.0f%% of %d target rows). That is "
            "more likely a bad read than real editing. Rerun with --force-large if it is "
            "intended." % (total, change_ceiling, max_change * 100, denominator))
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
    lines = [
        "key: %s. compared columns: %d" % (", ".join(keys), len(columns)),
        "rows planned:",
        "  unchanged    %d" % plan["unchanged"],
        "  update       %d" % len(plan["updates"]),
        "  insert       %d" % len(plan["inserts"]),
        "  delete       %d" % len(plan["deletes"]),
        "  held back    %d (duplicate key, left as published)" % len(plan["held"]),
        "  identical duplicate keys, no change    %d" % plan["duplicate_identical"],
        "  noise inside tolerance, no change      %d" % len(plan["suppressed"]),
        "delta-table rows, traditional versioning: keyed plan %d, delete-all plus insert-all %d"
        % (keyed, wholesale),
    ]

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
    return 0, "VERDICT: PLAN READY, %d update(s), %d insert(s), %d delete(s), %d held back." % (
        len(plan["updates"]), len(plan["inserts"]), len(plan["deletes"]), len(plan["held"]))


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
    }


# =============================================================================
# Input
# =============================================================================

def read_csv(path):
    """(header, [(where, cells dict)]). where names the line a record starts on."""
    records = []
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        while True:
            start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if not row:
                continue
            cells = {}
            for i, name in enumerate(header):
                cells[name] = row[i] if i < len(row) else ""
            records.append(("line %d" % start, cells))
    return header, records


def read_features(path):
    """(header, [(where, cells dict)]) from a saved feature set response (f=json)."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        try:
            doc = json.load(handle)
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
    header = []
    records = []
    for i, feature in enumerate(features):
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            raise InputRefused("%s: feature %d has no attributes." % (path, i + 1))
        for name in attrs:
            if name not in header:
                header.append(name)
        records.append(("feature %d" % (i + 1), {k: cell_text(v) for k, v in attrs.items()}))
    for _, cells in records:
        for name in header:
            cells.setdefault(name, "")
    return header, records


def load(path):
    """Read one snapshot. A .json path is a feature set response, anything else is a CSV."""
    if path.lower().endswith(".json"):
        return read_features(path)
    return read_csv(path)


def check_columns(header, keys, ignore, side):
    """Raise ValueError for a column the flags name that the file does not hold."""
    absent = [c for c in list(keys) + list(ignore) if c not in header]
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


def build_plan(src_path, tgt_path, keys, ignore, tolerance, min_rows):
    """Read both snapshots and classify. Raises ValueError, InputRefused, OSError, csv.Error."""
    src_header, src_raw = load(src_path)
    tgt_header, tgt_raw = load(tgt_path)
    check_columns(src_header, keys, ignore, "source")
    check_columns(tgt_header, keys, ignore, "target")
    columns = compare_columns(src_header, tgt_header, keys, ignore)
    check_min_rows(len(src_raw), min_rows)
    src = to_records(src_raw, keys, "source")
    tgt = to_records(tgt_raw, keys, "target")
    return classify(src, tgt, columns, tolerance), columns


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
                                   args.tolerance, args.min_rows)
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
    big = {"inserts": [], "updates": [], "deletes": [], "held": [], "shared_keys": 1000,
           "source_rows": 1000, "target_rows": 1000}

    def rails(**over):
        p = dict(big)
        p.update(over)
        return guard(p, False, 0.10, 0.02, 50)

    check(rails() == [], "a plan with no change trips no rail")
    check("(2% of 1000 target rows)" in rails(deletes=[["k"]] * 21)[0]
          and "(10% of 1000 target rows)" in rails(updates=[{}] * 101)[0],
          "a rail message names the percent and the target size it was figured from")
    check(guard(dict(big, target_rows=0, shared_keys=0, inserts=[["k"]]), False, 1.0, 1.0, 50)
          == [], "an empty target counts as one row, so one insert fits a ceiling of 100%")
    check(rails(deletes=[["k"]] * 20) == [] and len(rails(deletes=[["k"]] * 21)) == 1,
          "deletes at exactly 2% pass and one more trips the delete rail")
    check(len(rails(updates=[{}] * 101)) == 1 and rails(updates=[{}] * 100) == [],
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
    check(verdict(dict(big, held=[{}], updates=[{}]), [])[0] == 0
          and verdict(dict(big, held=[{}]), ["x"])[0] == 1,
          "held keys beside a change still give a plan, and a tripped rail still wins")
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
    doc = plan_document(plan, ["K"], ["V"], [])
    check(doc["delta_rows_keyed"] == 4 and doc["delta_rows_delete_all"] == 6
          and doc["format"] == 1 and json.loads(json.dumps(doc)) == doc,
          "the plan document is plain JSON and carries both costs")

    # ---- readers
    path = make("q.csv", "\ufeffK,V\r\n1,a\r\n\r\n2,\"multi\nline\"\r\n3,c\r\n4\r\n")
    header, raw = read_csv(path)
    check(header == ["K", "V"], "a UTF-8 BOM does not become part of the first column name  "
          "<-- pinned defect")
    check([w for w, _ in raw] == ["line 2", "line 4", "line 6", "line 7"],
          "a blank line and a quoted newline do not move the reported line  <-- pinned defect")
    check(raw[3][1] == {"K": "4", "V": ""} and raw[1][1]["V"] == "multi\nline",
          "a short row reads its missing cell as blank")
    check(read_csv(make("empty.csv", "")) == ([], []), "an empty file has no header and no rows")
    check(read_csv(make("hdr.csv", "K,V\n"))[1] == [], "a header-only file has no rows")
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
                 "--max-d", "--min"):
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
    check(code == 0 and "update       1" in so and "insert       1" in so
          and "delete       1" in so and "held back    1" in so,
          "one update, one insert, one delete and one held duplicate plan together, exit 0")
    check("S02  source line 3, target line 3, line 42" in so,
          "the held key names the lines of both files")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED",
                            *loose, "--out", out])
    check(code == 0 and "Check only." in so and not os.path.exists(out),
          "without --apply nothing is written  <-- pinned defect")
    code, so, se = run_cli([src, tgt2, "--key", "SEG", "--ignore", "EDITED"] + loose)
    check(code == 0 and "not written" not in so and "Check only" not in so,
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
    check(code == 0 and "wrote " in so and written.get("inserts") == [["S39"]],
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
    check(code == 0, "--force-large lifts the change and delete rails")
    code, so, se = run_cli([make("e.csv", "SEG,NAME,LEN,EDITED\n"), tgt, "--key", "SEG"])
    check(code == 3 and "below --min-rows 1" in so, "an empty source is refused with exit 3")
    code, so, se = run_cli([make("e.csv", "SEG,NAME,LEN,EDITED\n"), tgt, "--key", "SEG",
                            "--min-rows", "0", "--force-large"])
    check(code == 0 and "delete       40" in so, "--min-rows 0 plans the deletion of the target")
    code, so, se = run_cli([make("blank.csv", "SEG,NAME,LEN,EDITED\n,x,1,2\n"), tgt, "--key", "SEG"])
    check(code == 3 and "blank key" in so, "a blank key is refused with exit 3, never skipped")
    code, so, se = run_cli([make("p.json", '{"features": [], "exceededTransferLimit": true}'),
                            tgt, "--key", "SEG"])
    check(code == 3 and "first page" in so, "a truncated feature set is refused with exit 3")
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
    hs = make("hs.csv", "ID,V\nA,1\nA,2\nB,3\n")
    ht = make("ht.csv", "ID,V\nA,1\nB,3\n")
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
    check(done.returncode == 0 and "VERDICT: PLAN READY" in done.stdout,
          "the script runs as a process and exits 0 on a ready plan")
    done = subprocess.run([sys.executable, script, "--self-test", "--bogus"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    check(done.returncode == 64, "an unknown flag exits 64 as a process")

    # ---- the tool opens no socket and writes only with --apply
    with open(script, "r", encoding="utf-8") as handle:
        code_text = handle.read()
    check(not any(("import " + m) in code_text for m in ("socket", "urllib", "http", "ssl")),
          "the source imports no network module")
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

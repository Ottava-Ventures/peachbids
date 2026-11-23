#!/usr/bin/env python3
"""Rewrite the homepage CC-4 As-of line from the latest weekday digest.

Keeps the wording fixed (Statute CC-4 / CC4-a). Only the as-of date and the
open-count change. The sources phrase is never rewritten.

The visible text stays in the static HTML (no client-side fetch). This script
rewrites only the span between <!-- ASOF:START --> and <!-- ASOF:END -->, and
writes data/as-of.json beside the site.

N is computed under CC4-b: from the latest weekday digest and sent-ledger,
count items with status Open whose last-seen due date falls after the as-of
day, from GPR and Gwinnett only, excluding unresolved D3a / D3b / D7
conflicts (and ledger excluded_note, e.g. step 4b).

Idempotent. Refuses to write if the date or count cannot be read. Prints a
unified diff of the HTML snippet (and of the JSON when it changes).

Examples:
  python3 /workspace/peachbids/scripts/update_as_of.py
  python3 /workspace/peachbids/scripts/update_as_of.py --digest /workspace/handoffs/bidhound-vulcan/digest-2026-11-20.md
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PEACHBIDS = SCRIPT_DIR.parent
DEFAULT_DIGEST_DIR = Path("/workspace/handoffs/bidhound-vulcan")
DEFAULT_LEDGER = DEFAULT_DIGEST_DIR / "sent-ledger.json"
DEFAULT_HTML = PEACHBIDS / "preview-2026-11-22-bundle" / "index.html"
DEFAULT_JSON = PEACHBIDS / "preview-2026-11-22-bundle" / "data" / "as-of.json"

MARKER_START = "<!-- ASOF:START -->"
MARKER_END = "<!-- ASOF:END -->"
DIGEST_NAME = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})\.(md|txt|html)$")
DUE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
OPEN_STATUS = re.compile(r"^Open(\b|$)")
# Frozen sources clause (CC4-a). Do not change this string.
SOURCES_PHRASE = "from the state registry and Gwinnett County’s bid page."
SOURCES_HTML = "from the state registry and Gwinnett County’s bid&nbsp;page."
ALLOWED_SOURCES = {"gpr", "gwinnett"}
CONFLICT_FIELDS = ("d3a_note", "d3b_note", "d7_note")

TEMPLATE = (
    '<span class="asof-d">As of {weekday_html}:</span> '
    "{count} open cleaning and grounds {bids_word} {sources_html}"
)


class Refuse(Exception):
    """Date or count could not be read; no files are written."""


def find_latest_digest(digest_dir: Path) -> Path:
    """Pick the newest top-level digest-YYYY-MM-DD.{md,txt,html}.

    Only the digest directory itself is scanned (not preview-* copies).
    Prefer .md, then .txt, then .html when the same date has several files.
    """
    if not digest_dir.is_dir():
        raise Refuse(f"Digest directory not found: {digest_dir}")
    rank = {".md": 0, ".txt": 1, ".html": 2}
    by_date: dict[str, list[Path]] = {}
    for p in digest_dir.iterdir():
        if not p.is_file():
            continue
        m = DIGEST_NAME.match(p.name)
        if not m:
            continue
        by_date.setdefault(m.group(1), []).append(p)
    if not by_date:
        raise Refuse(
            f"No digest-YYYY-MM-DD.md/.txt/.html files in {digest_dir}"
        )
    latest = max(by_date)
    files = sorted(by_date[latest], key=lambda p: rank.get(p.suffix, 9))
    return files[0]


def date_from_digest_path(path: Path) -> date:
    m = re.search(r"digest-(\d{4}-\d{2}-\d{2})", path.name)
    if not m:
        raise Refuse(f"Cannot read as-of date from digest filename: {path}")
    try:
        return date.fromisoformat(m.group(1))
    except ValueError as e:
        raise Refuse(f"Invalid date in digest filename {path.name}: {e}") from e


def weekday_label(d: date) -> str:
    # %a/%b are English (Fri, Nov). Day has no leading zero, matching CC4-a.
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"


def weekday_html(d: date) -> str:
    return f"{d.strftime('%a')}, {d.strftime('%b')}&nbsp;{d.day}"


def parse_due_date(value: str, rec_id: str) -> date | None:
    if not value or not str(value).strip():
        return None
    m = DUE_DATE.match(str(value).strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def load_ledger(path: Path) -> dict:
    if not path.is_file():
        raise Refuse(f"sent-ledger not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Refuse(f"sent-ledger is not valid JSON ({path}): {e}") from e
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict) or not records:
        raise Refuse(f"sent-ledger has no records object: {path}")
    return records


def compute_count(records: dict, as_of: date, digest_tag: str) -> tuple[int, list[dict]]:
    """Return (N, kept records) under CC4-b. Refuse if dues cannot be read."""
    kept: list[dict] = []
    missing_due: list[str] = []
    for rec_id, rec in records.items():
        if not isinstance(rec, dict):
            continue
        src = str(rec_id).split(":", 1)[0].lower()
        if src not in ALLOWED_SOURCES:
            continue
        issues = rec.get("issues") or []
        tags = [
            i.get("issue")
            for i in issues
            if isinstance(i, dict) and i.get("issue")
        ]
        if digest_tag not in tags:
            continue
        status = rec.get("status") or ""
        if not OPEN_STATUS.match(str(status)):
            continue
        if rec.get("cancelled") is True:
            continue
        if rec.get("excluded_note"):
            continue
        if any(rec.get(f) for f in CONFLICT_FIELDS):
            continue
        due = parse_due_date(rec.get("last_seen_due_et") or "", rec_id)
        if due is None:
            missing_due.append(rec_id)
            continue
        if due <= as_of:
            continue
        kept.append(
            {
                "id": rec_id,
                "number": rec.get("number"),
                "agency": rec.get("agency"),
                "due": rec.get("last_seen_due_et"),
            }
        )
    if missing_due:
        raise Refuse(
            "Cannot read last_seen_due_et for digest items: "
            + ", ".join(missing_due)
        )
    return len(kept), kept


def render_html(as_of: date, count: int) -> str:
    if count < 0:
        raise Refuse(f"Invalid open_count: {count}")
    return TEMPLATE.format(
        weekday_html=weekday_html(as_of),
        count=count,
        bids_word="bid" if count == 1 else "bids",
        sources_html=SOURCES_HTML,
    )


def payload(as_of: date, count: int) -> dict:
    return {
        "date_iso": as_of.isoformat(),
        "weekday_label": weekday_label(as_of),
        "open_count": count,
        "sources_phrase": SOURCES_PHRASE,
    }


def extract_marked(html: str) -> tuple[str, str, str]:
    if MARKER_START not in html or MARKER_END not in html:
        raise Refuse(
            f"HTML is missing {MARKER_START} / {MARKER_END} markers"
        )
    pre, rest = html.split(MARKER_START, 1)
    if MARKER_END not in rest:
        raise Refuse("HTML is missing the ASOF:END marker after ASOF:START")
    mid, post = rest.split(MARKER_END, 1)
    if MARKER_START in mid or MARKER_END in pre:
        raise Refuse("Duplicate ASOF markers in HTML")
    return pre, mid, post


def unified(old: str, new: str, path: str) -> str:
    if old == new:
        return f"--- {path}\n+++ {path}\n(no changes)\n"
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=path,
        tofile=path,
    )
    text = "".join(diff)
    if not text.endswith("\n"):
        text += "\n"
    return text


def json_text(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def run(args: argparse.Namespace) -> int:
    digest = (
        Path(args.digest).resolve()
        if args.digest
        else find_latest_digest(Path(args.digest_dir))
    )
    if not digest.is_file():
        raise Refuse(f"Digest file not found: {digest}")
    as_of = date_from_digest_path(digest)
    digest_tag = f"digest-{as_of.isoformat()}"
    how = (
        f"explicit --digest {digest}"
        if args.digest
        else (
            f"latest digest-YYYY-MM-DD in {Path(args.digest_dir)} "
            f"(top-level only; prefer .md then .txt then .html)"
        )
    )
    print(f"Digest: {digest}")
    print(f"How found: {how}")
    print(f"As-of date: {as_of.isoformat()} ({weekday_label(as_of)})")

    ledger_path = Path(args.ledger)
    records = load_ledger(ledger_path)
    count, kept = compute_count(records, as_of, digest_tag)
    print(f"Ledger: {ledger_path}")
    print(f"Open count (CC4-b): {count}")
    for item in kept:
        print(
            f"  - {item['id']}  {item['number']}  {item['agency']}  "
            f"due {item['due']}"
        )

    snippet = render_html(as_of, count)
    data = payload(as_of, count)

    html_path = Path(args.html)
    json_path = Path(args.json)
    if not html_path.is_file():
        raise Refuse(f"HTML file not found: {html_path}")
    html = html_path.read_text()
    pre, mid, post = extract_marked(html)
    new_html = pre + MARKER_START + snippet + MARKER_END + post
    old_json = json_path.read_text() if json_path.is_file() else ""
    new_json = json_text(data)

    print("\n--- HTML snippet diff ---")
    print(unified(mid, snippet, str(html_path) + " (ASOF snippet)"), end="")
    print("--- JSON diff ---")
    print(unified(old_json, new_json, str(json_path)), end="")

    if args.dry_run:
        print("Dry run: no files written.")
        return 0

    if new_html != html:
        html_path.write_text(new_html)
        print(f"Wrote {html_path}")
    else:
        print(f"HTML unchanged: {html_path}")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    if new_json != old_json:
        json_path.write_text(new_json)
        print(f"Wrote {json_path}")
    else:
        print(f"JSON unchanged: {json_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--digest",
        help="Use this digest file instead of the latest in --digest-dir",
    )
    p.add_argument(
        "--digest-dir",
        default=str(DEFAULT_DIGEST_DIR),
        help=f"Directory of digest-YYYY-MM-DD files (default: {DEFAULT_DIGEST_DIR})",
    )
    p.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER),
        help=f"sent-ledger.json path (default: {DEFAULT_LEDGER})",
    )
    p.add_argument(
        "--html",
        default=str(DEFAULT_HTML),
        help=f"Homepage HTML to rewrite (default: {DEFAULT_HTML})",
    )
    p.add_argument(
        "--json",
        default=str(DEFAULT_JSON),
        help=f"as-of.json output path (default: {DEFAULT_JSON})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the diff and do not write files",
    )
    args = p.parse_args(argv)
    try:
        return run(args)
    except Refuse as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

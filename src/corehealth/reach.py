#!/usr/bin/env python3
"""
reach.py — Which observers heard a given node, and how well.

For a target node (matched by name or public-key prefix), this script
finds every ADVERT transmission from that node in the capture that was
heard by ANY observer — whether directly (zero-hop) or via mesh relays —
and aggregates, per observer:

  * number of adverts heard
  * average SNR and RSSI (note: for relayed packets these describe the
    last hop — relay→observer — not the node→observer link)
  * first and last time the observer heard the node

The time window is anchored at the newest observation in the database
(the "capture end"), so ``-t 24h`` means "the last 24 hours of the
capture", not "the last 24 hours of wall-clock time".

Usage:
    python3 reach.py BAU-IPMET-0e15               # whole capture
    python3 reach.py BAU-IPMET-0e15 -t 24h        # last 24h of capture
    python3 reach.py BAU-IPMET-0e15 -t 7d --csv   # CSV output
    python3 reach.py BAU-IPMET-0e15 --json        # JSON output
    python3 reach.py 0e15                         # match by key prefix
    python3 reach.py --db /path/to/meshcore.db BAU-IPMET-0e15

Node matching: exact node name, else unique name substring, else
unique public-key prefix (case-insensitive).

Requirements: Python 3.11+, sqlite3 (stdlib).
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone


_TIME_RANGE_RE = re.compile(r'(\d+)\s*([smhdw])$', re.IGNORECASE)

_UNIT_SECONDS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800,
}


def parse_time_range(s):
    """Parse a human-readable duration string (e.g. '48h', '7d').

    Usable as an argparse *type* — returns a ``timedelta`` or raises
    ``argparse.ArgumentTypeError`` on invalid input.
    """
    m = _TIME_RANGE_RE.match(s.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid duration '{s}'.  Expected <number><unit> where "
            "unit is s(sec), m(min), h(hour), d(day), or w(week).  "
            "Examples: 48h, 7d, 2w, 30m, 90s"
        )
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])


def format_timedelta(td):
    """Format a timedelta as a short human string, e.g. '48h', '7d'."""
    total = int(td.total_seconds())
    for suffix, secs in [("w", 604800), ("d", 86400),
                         ("h", 3600), ("m", 60), ("s", 1)]:
        if total >= secs:
            return f"{total // secs}{suffix}"
    return "0s"


def display_width(s):
    """Rendered width of *s* in a terminal (emoji/wide chars count 2)."""
    width = 0
    for ch in s:
        if unicodedata.combining(ch):  # combining marks: no width
            continue
        ea = unicodedata.east_asian_width(ch)
        if ea in ("W", "F"):
            width += 2
        elif unicodedata.category(ch) in ("Mn", "Me"):
            width += 0
        else:
            width += 1
    return width


def pad(s, target, align="left"):
    """Pad *s* with spaces to a rendered width of *target*."""
    gap = max(0, target - display_width(s))
    return s + " " * gap if align == "left" else " " * gap + s


def parse_args():
    p = argparse.ArgumentParser(
        description="Which observers heard a given node "
                    "(all ADVERTs, direct or relayed)."
    )
    p.add_argument(
        "node",
        help="Target node: exact name, name substring, or public-key "
             "prefix (case-insensitive)",
    )
    p.add_argument(
        "--db", default="./db/meshcore.db",
        help="Path to the SQLite database (default: ./db/meshcore.db)",
    )
    p.add_argument(
        "-t", "--time-range", metavar="DURATION", type=parse_time_range,
        help="Only include adverts heard in the last DURATION of the "
             "capture (e.g. 48h, 7d, 2w, 30m, 90s)",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument(
        "--csv", action="store_true",
        help="Output as CSV instead of a formatted table",
    )
    fmt.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of a formatted table",
    )
    return p.parse_args()


def resolve_node(db, target):
    """Return the (public_key, name, role) row for the target node.

    Matching order: exact name → unique name substring → unique
    public-key prefix.  Also searches inactive_nodes if the node has
    been rotated out of the active table.
    """
    target_lc = target.lower()

    candidates = defaultdict(list)  # pubkey -> [rows from nodes/inactive]
    for table in ("nodes", "inactive_nodes"):
        for row in db.execute(
            f"SELECT public_key, name, role FROM {table}"
        ):
            candidates[row[0]].append(row)

    def finish(rows):
        if len(rows) == 1:
            return rows[0]
        if not rows:
            return None
        keys = sorted({r[0] for r in rows})
        raise SystemExit(
            f"Ambiguous node '{target}': matches {len(keys)} nodes "
            f"({', '.join(k[:8] + '…' for k in keys[:5])}"
            f"{'…' if len(keys) > 5 else ''}).  Use a longer prefix."
        )

    # 1. Exact public-key match
    rows = [r for r in all_rows(candidates) if r[0].lower() == target_lc]
    if rows:
        return finish(rows)

    # 2. Public-key prefix (at least 4 hex chars)
    if len(target) >= 4:
        rows = [r for r in all_rows(candidates) if r[0].lower().startswith(target_lc)]
        if rows:
            return finish(rows)

    # 3. Exact name (case-insensitive)
    rows = [r for r in all_rows(candidates)
            if (r[1] or "").lower() == target_lc]
    if rows:
        return finish(rows)

    # 4. Name substring (case-insensitive)
    rows = [r for r in all_rows(candidates)
            if target_lc in (r[1] or "").lower()]
    if rows:
        return finish(rows)

    raise SystemExit(
        f"Node '{target}' not found in nodes/inactive_nodes.  "
        "Use a full or partial name, or a public-key prefix."
    )


def all_rows(candidates):
    """Flatten the pubkey→rows map, preserving first occurrence order."""
    seen = set()
    rows = []
    for pk, group in candidates.items():
        if pk in seen:
            continue
        seen.add(pk)
        rows.append(group[0])
    return rows


def compute_reach(db_path, node_key, time_range=None):
    """Return (node_info, capture_info, per-observer stats list).

    All sightings are considered, direct or relayed.  Note that for
    relayed packets the SNR/RSSI describe the last relay→observer hop,
    not the node→observer link.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    node = resolve_node(db, node_key)
    public_key = node[0]

    row = db.execute(
        "SELECT max(timestamp) AS mx FROM observations"
    ).fetchone()
    capture_end = row["mx"]  # unixepoch seconds
    if capture_end is None:
        db.close()
        raise SystemExit("No observations found in the database.")

    cutoff = None
    if time_range is not None:
        cutoff = int(capture_end - time_range.total_seconds())

    time_clause = "AND o.timestamp >= ?" if cutoff is not None else ""
    time_params = (cutoff,) if cutoff is not None else ()

    rows = db.execute(
        f"""
        SELECT obs.name AS observer_name, obs.iata,
               o.snr, o.rssi, o.timestamp
        FROM observations o
        JOIN transmissions t ON t.id = o.transmission_id
        JOIN observers obs ON obs.rowid = o.observer_idx
        WHERE t.payload_type = 4
          AND json_extract(t.decoded_json, '$.pubKey') = ?
          {time_clause}
        """,
        (public_key, *time_params),
    ).fetchall()
    db.close()

    capture_info = {
        "capture_end": datetime.fromtimestamp(
            capture_end, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%SZ"),
        "cutoff": (
            datetime.fromtimestamp(cutoff, tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%SZ")
            if cutoff is not None else None
        ),
        "time_range": time_range,
        "total_observations": len(rows),
    }

    stats = defaultdict(lambda: {
        "observer": "", "iata": None,
        "adverts": 0,
        "snrs": [], "rssis": [],
        "first": None, "last": None,
    })
    for r in rows:
        s = stats[r["observer_name"]]
        s["observer"] = r["observer_name"]
        s["iata"] = r["iata"]
        s["adverts"] += 1
        if r["snr"] is not None:
            s["snrs"].append(r["snr"])
        if r["rssi"] is not None:
            s["rssis"].append(r["rssi"])
        ts = r["timestamp"]
        s["first"] = ts if s["first"] is None else min(s["first"], ts)
        s["last"] = ts if s["last"] is None else max(s["last"], ts)

    results = []
    for s in stats.values():
        avg_snr = sum(s["snrs"]) / len(s["snrs"]) if s["snrs"] else None
        avg_rssi = sum(s["rssis"]) / len(s["rssis"]) if s["rssis"] else None
        results.append({
            "observer": s["observer"] or "(unnamed)",
            "iata": s["iata"] or "-",
            "adverts": s["adverts"],
            "avg_snr": avg_snr,
            "avg_rssi": avg_rssi,
            "first_seen": datetime.fromtimestamp(
                s["first"], tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": datetime.fromtimestamp(
                s["last"], tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
        })
    results.sort(key=lambda x: (-x["adverts"], x["observer"]))

    node_info = {
        "public_key": public_key,
        "name": node[1] or "(unnamed)",
        "role": node[2] or "?",
    }
    return node_info, capture_info, results


def print_table(node_info, capture_info, results):
    window = ""
    if capture_info.get("time_range") is not None:
        window = (f"  [last {format_timedelta(capture_info['time_range'])},"
                  f" since {capture_info['cutoff']}]")
    print(
        f"Node: {node_info['name']}  ({node_info['public_key'][:8]}…,"
        f" role={node_info['role']})"
    )
    print(
        f"Capture end: {capture_info['capture_end']}"
        f"  |  Observations: {capture_info['total_observations']}{window}"
    )
    print()

    if not results:
        print("No observers heard this node in the selected window.")
        return

    headers = ("Observer", "IATA", "Adverts", "Avg SNR", "Avg RSSI",
               "First seen", "Last seen")

    def fmt_cell(row, col):
        v = row[col]
        if col in ("avg_snr", "avg_rssi"):
            return f"{v:.1f}" if v is not None else "-"
        return str(v)

    table = [[fmt_cell(r, c) for c in
              ("observer", "iata", "adverts",
               "avg_snr", "avg_rssi", "first_seen", "last_seen")]
             for r in results]

    # Column widths from header and content, using rendered display width
    widths = [display_width(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))

    aligns = ["left", "left", "right", "right",
              "right", "left", "left"]

    def render_row(cells):
        return "  ".join(
            pad(c, widths[i], aligns[i]) for i, c in enumerate(cells)
        ).rstrip()

    print(render_row(headers))
    print("-" * display_width(render_row(headers)))
    for row in table:
        print(render_row(row))


def print_csv(node_info, capture_info, results):
    w = csv.writer(sys.stdout)
    w.writerow(["node_name", "node_public_key", "node_role",
                "observer", "iata", "adverts",
                "avg_snr", "avg_rssi", "first_seen", "last_seen"])
    for r in results:
        w.writerow([
            node_info["name"], node_info["public_key"], node_info["role"],
            r["observer"], r["iata"], r["adverts"],
            f"{r['avg_snr']:.1f}" if r["avg_snr"] is not None else "",
            f"{r['avg_rssi']:.1f}" if r["avg_rssi"] is not None else "",
            r["first_seen"], r["last_seen"],
        ])


def print_json(node_info, capture_info, results):
    out = {
        "node": node_info,
        "meta": {
            "capture_end": capture_info["capture_end"],
            "cutoff": capture_info["cutoff"],
            "time_range": (format_timedelta(capture_info["time_range"])
                           if capture_info.get("time_range") is not None
                           else None),
            "total_observations": capture_info["total_observations"],
        },
        "observers": [
            {**r,
             "avg_snr": round(r["avg_snr"], 1)
             if r["avg_snr"] is not None else None,
             "avg_rssi": round(r["avg_rssi"], 1)
             if r["avg_rssi"] is not None else None}
            for r in results
        ],
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


def main():
    args = parse_args()
    node_info, capture_info, results = compute_reach(
        args.db, args.node, time_range=args.time_range
    )
    if args.json:
        print_json(node_info, capture_info, results)
    elif args.csv:
        print_csv(node_info, capture_info, results)
    else:
        print_table(node_info, capture_info, results)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
top_adverts.py — Top advertising nodes in the MeshCore capture.

Outputs a table of the top N nodes ranked by total ADVERT transmissions,
with columns: node name, 2-byte node ID, node role, total adverts,
median adverts/day, % zero-hop adverts, % flood adverts, and neighbor node IDs
(from the neighbor_edges table, sorted by link strength).

The daily rate is the MEDIAN of per-day advert counts over the calendar
days (UTC) spanned by the analysis window, counting 0 for days without
adverts — robust against isolated spikes.

Usage:
    python3 top_adverts.py                       # top 20 from ./meshcore.db
    python3 top_adverts.py -n 30                  # top 30
    python3 top_adverts.py --all                  # show every node (no limit)
    python3 top_adverts.py -t 48h                 # only last 48 hours
    python3 top_adverts.py --db /path/to/other.db # different database
    python3 top_adverts.py --csv                  # CSV output for piping
    python3 top_adverts.py --json                 # JSON output for piping
    python3 top_adverts.py --repeaters-only        # only repeater nodes
    python3 top_adverts.py --companions-only        # only companion nodes

Requirements: Python 3.8+, sqlite3 (stdlib).
"""
import argparse
import csv
import json
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta


_TIME_RANGE_RE = re.compile(r'(\d+)\s*([smhdw])$', re.IGNORECASE)

_UNIT_SECONDS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800,
}


def parse_time_range(s):
    """Parse a human-readable duration string (e.g. '48h', '7d', '2w').

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
    """Format a timedelta as a short human string, e.g. '48h', '7d', '2w'."""
    total = int(td.total_seconds())
    for suffix, secs in [("w", 604800), ("d", 86400),
                         ("h", 3600), ("m", 60), ("s", 1)]:
        if total >= secs:
            return f"{total // secs}{suffix}"
    return "0s"


def parse_args():
    p = argparse.ArgumentParser(
        description="Top advertising nodes in the MeshCore capture."
    )
    p.add_argument(
        "--db", default="./meshcore.db",
        help="Path to the SQLite database (default: ./meshcore.db)",
    )
    p.add_argument(
        "-n", "--top", type=int, default=20,
        help="Number of top nodes to show (default: 20)",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Show all nodes (overrides --top)",
    )
    p.add_argument(
        "-t", "--time-range", metavar="DURATION", type=parse_time_range,
        help="Only include adverts from the last DURATION "
             "(e.g. 48h, 7d, 2w, 30m, 90s)",
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
    p.add_argument(
        "--repeaters-only", action="store_true",
        help="Only include nodes whose role is 'repeater'",
    )
    p.add_argument(
        "--companions-only", action="store_true",
        help="Only include nodes whose role is 'companion'",
    )
    return p.parse_args()


def compute_stats(db_path, time_range=None, repeaters_only=False,
                  companions_only=False):
    """Return (capture_info, list_of_result_tuples).

    Each result tuple is (name, node_id, role, total_adverts,
    median_adverts_per_day, pct_zero_hop, pct_flood, neighbor_ids,
    public_key).  The daily rate is the median of per-day advert counts
    over the calendar days (UTC) spanned by the filtered capture, with
    0 for days without adverts.

    If *time_range* (a timedelta) is given, only adverts whose
    first_seen falls within the last *time_range* of the capture
    are included.
    If *repeaters_only* is True, only nodes with role 'repeater'
    are included.
    If *companions_only* is True, only nodes with role 'companion'
    are included.  Both may be True to include either role.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # --- Capture end (max first_seen across all adverts) ---
    row = db.execute(
        """
        SELECT max(first_seen) AS mx
        FROM transmissions
        WHERE payload_type = 4
        """
    ).fetchone()
    if row["mx"] is None:
        db.close()
        print("No ADVERT transmissions found in the database.", file=sys.stderr)
        sys.exit(1)

    capture_end_iso = row["mx"]
    capture_end = datetime.fromisoformat(capture_end_iso.replace("Z", "+00:00"))

    # --- Optional time-range cutoff ---
    if time_range is not None:
        cutoff_iso = (capture_end - time_range).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_clause = "AND t.first_seen >= ?"
        time_params = (cutoff_iso,)
    else:
        time_clause = ""
        time_params = ()

    roles = []
    if repeaters_only:
        roles.append("'repeater'")
    if companions_only:
        roles.append("'companion'")
    role_clause = (
        f"AND n.role IN ({', '.join(roles)})" if roles else ""
    )

    # --- Fetch all advert transmissions joined with nodes ---
    rows = db.execute(
        f"""
        SELECT t.raw_hex, t.route_type, n.name, n.public_key, n.role,
               t.first_seen
        FROM transmissions t
        JOIN nodes n
          ON n.public_key = json_extract(t.decoded_json, '$.pubKey')
        WHERE t.payload_type = 4 {time_clause} {role_clause}
        """,
        time_params,
    ).fetchall()

    # --- Neighbor edges (undirected, weighted by packet count) ---
    neighbor_edges = defaultdict(list)  # pubkey -> [(neighbor_pubkey, count)]
    try:
        for a, b, cnt in db.execute(
            "SELECT node_a, node_b, count FROM neighbor_edges"
        ):
            neighbor_edges[a].append((b, cnt or 0))
            neighbor_edges[b].append((a, cnt or 0))
    except sqlite3.OperationalError:
        pass  # neighbor_edges table not present — no neighbor info available

    db.close()

    if not rows:
        msg = "No ADVERT transmissions found"
        if time_range is not None:
            msg += f" in the last {format_timedelta(time_range)}"
        msg += "."
        print(msg, file=sys.stderr)
        sys.exit(1)

    # --- Capture span (min/max first_seen in the filtered set) ---
    capture_start_iso = min(r["first_seen"] for r in rows)
    capture_start = datetime.fromisoformat(
        capture_start_iso.replace("Z", "+00:00")
    )
    span_days = (capture_end - capture_start).total_seconds() / 86400.0

    # Calendar days (UTC) spanned by the filtered capture — the median
    # is computed over these days, counting 0 for days without adverts.
    all_days = []
    day = capture_start.date()
    while day <= capture_end.date():
        all_days.append(day)
        day += timedelta(days=1)

    # --- Aggregate per node ---
    stats = defaultdict(lambda: {"name": None, "nid": None, "role": None,
                                 "total": 0, "zero": 0, "flood": 0})
    day_counts = defaultdict(lambda: defaultdict(int))  # pubkey -> {date: n}

    for r in rows:
        key = r["public_key"]
        s = stats[key]
        seen_day = datetime.fromisoformat(
            r["first_seen"].replace("Z", "+00:00")
        ).date()
        day_counts[key][seen_day] += 1

        s["name"] = r["name"] or "(unnamed)"
        s["nid"] = r["public_key"][:4].upper()
        s["role"] = r["role"] or "?"
        s["total"] += 1

        rt = r["route_type"]
        # Path-length byte offset: 5 for transport route types, 1 otherwise
        offset = 5 if rt in (0, 3) else 1
        pl_byte = int(r["raw_hex"][offset * 2 : offset * 2 + 2], 16)

        if pl_byte == 0x00:
            s["zero"] += 1
        else:  # path-length byte nonzero — relayed through the mesh
            s["flood"] += 1

    # --- Build result list ---
    results = []
    for key, s in stats.items():
        total = s["total"]
        counts = [day_counts[key].get(d, 0) for d in all_days]
        rate = float(statistics.median(counts)) if counts else 0.0
        pct_zero = 100.0 * s["zero"] / total
        pct_flood = 100.0 * s["flood"] / total
        neighbors = neighbor_edges.get(key, [])
        neighbors.sort(key=lambda nc: (-nc[1], nc[0]))
        neighbor_ids = [pk[:4].upper() for pk, _ in neighbors]
        results.append((s["name"], s["nid"], s["role"], total, rate, pct_zero,
                        pct_flood, neighbor_ids, key))

    results.sort(key=lambda x: x[3], reverse=True)

    capture_info = {
        "start": capture_start_iso,
        "end": capture_end_iso,
        "span_days": span_days,
        "total_adverts": len(rows),
        "unique_nodes": len(stats),
        "time_range": time_range,
        "repeaters_only": repeaters_only,
        "companions_only": companions_only,
    }
    return capture_info, results


def format_neighbor_ids(neighbor_ids, max_len=44):
    """Render a comma-separated neighbor ID list, truncated to *max_len*.

    If not all IDs fit, a '+N' suffix indicates how many were omitted.
    """
    if not neighbor_ids:
        return "-"
    out = ""
    shown = 0
    for nid in neighbor_ids:
        cand = nid if not out else out + ", " + nid
        if len(cand) > max_len:
            break
        out = cand
        shown += 1
    if shown < len(neighbor_ids):
        out += f", +{len(neighbor_ids) - shown}"
    return out


def print_table(capture_info, results, top_n):
    range_note = ""
    role_note = []
    if capture_info.get("time_range") is not None:
        range_note = f"  [last {format_timedelta(capture_info['time_range'])}]"
    if capture_info.get("repeaters_only"):
        role_note.append("repeaters")
    if capture_info.get("companions_only"):
        role_note.append("companions")
    if role_note:
        range_note += f"  [{'/'.join(role_note)} only]"
    print(
        f"Capture span: {capture_info['start']} to {capture_info['end']}"
        f" ({capture_info['span_days']:.3f} days){range_note}"
    )
    print(
        f"Total advert transmissions: {capture_info['total_adverts']}"
        f"  |  Unique nodes: {capture_info['unique_nodes']}"
    )
    print()
    header = f"{'#':>3}  {'Node Name':<28} {'NodeID':<6} {'Role':<10} {'Adverts':>7} {'Med/day':>8} {'%0-hop':>7} {'%Flood':>7}  {'Neighbors':<44}"
    print(header)
    print("-" * len(header))
    for i, (name, nid, role, total, rate, pz, pf, nbrs, _pk) in enumerate(results[:top_n], 1):
        nbr_str = format_neighbor_ids(nbrs)
        print(f"{i:>3}  {name:<28} {nid:<6} {role:<10} {total:>7} {rate:>8.1f} {pz:>6.1f}% {pf:>6.1f}%  {nbr_str}")


def print_csv(capture_info, results, top_n):
    w = csv.writer(sys.stdout)
    w.writerow(["rank", "node_name", "node_id", "role", "total_adverts",
                "median_adverts_per_day", "pct_zero_hop", "pct_flood",
                "neighbors"])
    for i, (name, nid, role, total, rate, pz, pf, nbrs, _pk) in enumerate(results[:top_n], 1):
        w.writerow([i, name, nid, role, total, f"{rate:.1f}",
                    f"{pz:.1f}", f"{pf:.1f}", ",".join(nbrs)])


def print_json(capture_info, results, top_n):
    meta = {
        "capture_start": capture_info["start"],
        "capture_end": capture_info["end"],
        "span_days": round(capture_info["span_days"], 3),
        "total_adverts": capture_info["total_adverts"],
        "unique_nodes": capture_info["unique_nodes"],
        "time_range": (format_timedelta(capture_info["time_range"])
                       if capture_info.get("time_range") is not None else None),
        "repeaters_only": capture_info["repeaters_only"],
        "companions_only": capture_info["companions_only"],
    }
    nodes = []
    for i, (name, nid, role, total, rate, pz, pf, nbrs, pk) in enumerate(
            results[:top_n], 1):
        nodes.append({
            "rank": i,
            "node_name": name,
            "node_id": nid,
            "public_key": pk,
            "role": role,
            "total_adverts": total,
            "median_adverts_per_day": round(rate, 1),
            "pct_zero_hop": round(pz, 1),
            "pct_flood": round(pf, 1),
            "neighbors": nbrs,
        })
    json.dump({"meta": meta, "nodes": nodes}, sys.stdout, indent=2)
    print()


def main():
    args = parse_args()
    capture_info, results = compute_stats(
        args.db, time_range=args.time_range,
        repeaters_only=args.repeaters_only, companions_only=args.companions_only,
    )
    top_n = len(results) if args.all else args.top
    if args.json:
        print_json(capture_info, results, top_n)
    elif args.csv:
        print_csv(capture_info, results, top_n)
    else:
        print_table(capture_info, results, top_n)


if __name__ == "__main__":
    main()

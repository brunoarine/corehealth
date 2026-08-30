#!/usr/bin/env python3
"""
neighbors.py — Estimate the immediate (first-hop RF) neighbors of a node.

An observer hearing a node's ADVERT does NOT make them neighbors: the
packet may have crossed several relays.  Immediate neighbors are the
radios directly adjacent on the RF path:

  * zero-hop ADVERT:   originator ↔ observer
  * relayed ADVERT:    originator ↔ first path hop
  * any packet (rx):   last path hop ↔ observer
  * path adjacency:    path[i] ↔ path[i+1] (one heard the other)

A flood path is recorded origin → path[0] → … → path[last] → observer,
so for the target T at position i:  T heard path[i-1] ("we hear") and
path[i+1] heard T ("they hear").  When T is the last hop the observer
heard T directly; when T is the originator (ADVERT) the first relay and
any zero-hop observer heard T directly.

Evidence model, deduplicated by transmission:

  we_hear        distinct transmissions where T heard the neighbor
  they_hear      distinct transmissions where the neighbor heard T
  bottleneck     min(we_hear, they_hear) — the weaker direction
  bidirectional  both directions observed
  confidence     high   — bidirectional, bottleneck >= 3
                  medium — bidirectional, or >= 3 transmissions one way
                  low    — everything else

Path hops are short pubkey prefixes ("91CD", "19EF4B", …).  A hop is only
attributed when the prefix uniquely matches one known pubkey (nodes,
inactive_nodes, observers with hex ids); ``observations.resolved_path`` is
used as a secondary, best-effort resolution when the static prefix index
is ambiguous.  One-byte prefixes (2 hex chars) collide easily, so links
sustained only by them are the least reliable.

Observations with direction 'tx' (the observer's own outgoing traffic,
self-reported) are excluded — they are not RF receptions.

The time window is anchored at the newest observation in the database
("capture end"), so ``-t 7d`` means "the last 7 days of the capture".

Usage:
    python3 neighbors.py SAO-VLMEDEIROS-91CD            # last 7d (default)
    python3 neighbors.py SAO-VLMEDEIROS-91CD -t 24h
    python3 neighbors.py 91cd -t 7d --json              # match by key prefix
    python3 neighbors.py 91cd --csv --min-evidence 3

Requirements: Python 3.11+, sqlite3 (stdlib).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .reach import (display_width, format_timedelta, pad,
                        parse_time_range, resolve_node)
except ImportError:  # executado como script: uv run src/corehealth/neighbors.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reach import (display_width, format_timedelta, pad,
                       parse_time_range, resolve_node)

# --- Constants ---------------------------------------------------------------

ADVERT_PAYLOAD_TYPE = 4
# Hex-char prefix lengths indexed for hop resolution (1,2,3,4,6,8 bytes —
# mirrors the ingestor's prefix index in cmd/ingestor/neighbor_builder.go).
PREFIX_LENGTHS = (2, 4, 6, 8, 12, 16)
HIGH_CONFIDENCE_MIN = 3     # bottleneck transmissions for "high" confidence
DEFAULT_DB = "./db/meshcore.db"
DEFAULT_WINDOW = timedelta(days=7)
_HEX = set("0123456789abcdefABCDEF")


def is_hex_key(s, length=64):
    return bool(s) and len(s) == length and all(c in _HEX for c in s)


# --- Prefix index --------------------------------------------------------------

def build_prefix_index(db):
    """Build (index, meta) over nodes, inactive_nodes and observers.

    index maps a lowercased pubkey prefix (at PREFIX_LENGTHS hex chars)
    to the list of candidate pubkeys — a hop token is only attributable
    when exactly one candidate matches.

    meta maps a lowercased pubkey to {name, role, lat, lon}.  Precedence:
    nodes, then inactive_nodes, then observers (role 'observer').
    Observers without a hex pubkey (e.g. id 'DEVICE') get metadata but
    never enter the index — they cannot match a path hop anyway.
    """
    index = defaultdict(list)
    meta = {}
    indexed = set()

    def add(pk, name, role, lat, lon):
        pk = (pk or "").lower()
        if not pk:
            return
        if pk not in meta:
            meta[pk] = {"name": name or "(unnamed)", "role": role or "?",
                        "lat": lat, "lon": lon}
        if is_hex_key(pk) and pk not in indexed:
            indexed.add(pk)
            for n in PREFIX_LENGTHS:
                index[pk[:n]].append(pk)

    for table in ("nodes", "inactive_nodes"):
        for r in db.execute(
            f"SELECT public_key, name, role, lat, lon FROM {table}"
        ):
            add(r[0], r[1], r[2], r[3], r[4])
    for r in db.execute("SELECT id, name FROM observers"):
        add(r[0], r[1], "observer", None, None)
    return dict(index), meta


def unique_tokens(pubkey, index):
    """Uppercase path tokens that uniquely identify *pubkey* in the index."""
    pk = pubkey.lower()
    out = set()
    for n in PREFIX_LENGTHS:
        if len(pk) < n:
            continue
        tok = pk[:n]
        cands = index.get(tok, ())
        if len(cands) == 1 and cands[0] == pk:
            out.add(tok.upper())
    return out


# --- Core computation -----------------------------------------------------------

def compute_neighbors(db_path, node_key, time_range=None, min_evidence=1):
    """Return (node_info, capture_info, links).

    links is a list of per-neighbor dicts sorted by link strength
    (bidirectional first, then bottleneck, then total evidence).
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    node = resolve_node(db, node_key)
    target = node[0].lower()

    row = db.execute("SELECT max(timestamp) AS mx FROM observations").fetchone()
    capture_end = row["mx"]
    if capture_end is None:
        db.close()
        raise SystemExit("No observations found in the database.")
    cutoff = (int(capture_end - time_range.total_seconds())
              if time_range is not None else None)

    index, node_meta = build_prefix_index(db)
    tokens = unique_tokens(target, index)

    tx_cols = {r["name"] for r in db.execute("PRAGMA table_info(transmissions)")}
    obs_cols = {r["name"] for r in db.execute("PRAGMA table_info(observations)")}
    has_from_pubkey = "from_pubkey" in tx_cols
    has_resolved_path = "resolved_path" in obs_cols
    has_direction = "direction" in obs_cols

    # Adverts may predate the from_pubkey backfill; fall back to decoded_json
    # only when needed (json_extract per row is expensive).
    need_json_fallback = not has_from_pubkey or db.execute(
        "SELECT 1 FROM transmissions WHERE payload_type = 4 "
        "AND (from_pubkey IS NULL OR from_pubkey = '') "
        "AND decoded_json IS NOT NULL LIMIT 1"
    ).fetchone() is not None

    # WHERE clauses (OR): any unique token of the target in a path, any
    # advert originated by the target, or the target's full pubkey inside
    # resolved_path (covers ambiguous-token sightings).
    clauses, params = [], []
    for tok in sorted(tokens):
        clauses.append("o.path_json LIKE ?")
        params.append(f'%"{tok}"%')
    advert_expr = "t.payload_type = 4"
    if has_from_pubkey:
        advert_expr += " AND (lower(COALESCE(t.from_pubkey, '')) = ?"
        params.append(target)
        if need_json_fallback:
            advert_expr += " OR json_extract(t.decoded_json, '$.pubKey') = ?"
            params.append(target)
        advert_expr += ")"
    else:
        advert_expr += " AND json_extract(t.decoded_json, '$.pubKey') = ?"
        params.append(target)
    clauses.append(f"({advert_expr})")
    if has_resolved_path:
        clauses.append("o.resolved_path LIKE ?")
        params.append(f"%{target}%")

    where = " OR ".join(clauses)
    conds = [f"({where})"]
    if has_direction:
        conds.append("COALESCE(o.direction, '') <> 'tx'")
    if cutoff is not None:
        conds.append("o.timestamp >= ?")
        params.append(cutoff)

    from_expr = ("lower(COALESCE(t.from_pubkey, ''))"
                 if has_from_pubkey else "''")
    rp_expr = "COALESCE(o.resolved_path, '')" if has_resolved_path else "''"
    cur = db.execute(f"""
        SELECT t.id AS tx_id,
               COALESCE(t.payload_type, 0) AS payload_type,
               {from_expr} AS from_pk,
               COALESCE(o.path_json, '[]') AS path_json,
               {rp_expr} AS resolved_path,
               lower(COALESCE(obs.id, '')) AS observer_id,
               o.snr, o.timestamp
        FROM observations o
        JOIN transmissions t ON t.id = o.transmission_id
        LEFT JOIN observers obs ON obs.rowid = o.observer_idx
        WHERE {" AND ".join(conds)}
    """, params)

    # --- attribution ---
    we = defaultdict(set)      # neighbor pubkey -> {tx_id}  (we heard them)
    they = defaultdict(set)    # neighbor pubkey -> {tx_id}  (they heard us)
    snrs = defaultdict(list)  # neighbor pubkey -> SNR samples (endpoint only)
    last_seen = {}
    stats = {"rows_scanned": 0, "relay_rows": 0, "advert_rows": 0,
             "index_resolved": 0, "rp_resolved": 0, "unresolved": 0}
    memo = {}

    def resolve(tok, rp, pos):
        """Resolve a path hop to a full pubkey, or None.

        Static prefix index first (memoized); resolved_path[pos] as a
        best-effort fallback when the index is ambiguous or empty.
        """
        key = tok.lower()
        if key not in memo:
            cands = index.get(key, ())
            memo[key] = cands[0] if len(cands) == 1 else None
        hit = memo[key]
        if hit is not None:
            stats["index_resolved"] += 1
            return hit
        if rp is not None and 0 <= pos < len(rp):
            e = rp[pos]
            if isinstance(e, str) and is_hex_key(e):
                stats["rp_resolved"] += 1
                return e.lower()
        stats["unresolved"] += 1
        return None

    def is_target(tok, rp, i):
        if tok.upper() in tokens:
            return True
        if rp is not None and i < len(rp):
            e = rp[i]
            if isinstance(e, str) and e.lower() == target:
                return True
        return False

    def note(neighbor, ts, snr):
        if ts > last_seen.get(neighbor, -1):
            last_seen[neighbor] = ts
        if snr is not None:
            snrs[neighbor].append(snr)

    for r in cur:
        stats["rows_scanned"] += 1
        path = parse_path(r["path_json"])
        rp = None
        if has_resolved_path and r["resolved_path"]:
            try:
                v = json.loads(r["resolved_path"])
                if isinstance(v, list):
                    rp = v
            except (ValueError, TypeError):
                rp = None
        observer = r["observer_id"]
        from_pk = r["from_pk"]
        is_advert = r["payload_type"] == ADVERT_PAYLOAD_TYPE
        tx, ts, snr = r["tx_id"], r["timestamp"], r["snr"]

        if from_pk == target:
            stats["advert_rows"] += 1

        if not path:
            # Zero-hop advert from the target: the observer heard it directly.
            if (is_advert and from_pk == target
                    and observer and observer != target):
                they[observer].add(tx)
                note(observer, ts, snr)
            continue

        n = len(path)
        hit = False

        # The target's own advert relayed once or more: the first relay
        # heard the originator directly.
        if is_advert and from_pk == target:
            nb = resolve(path[0], rp, 0)
            if nb and nb != target:
                they[nb].add(tx)
                note(nb, ts, None)

        for i, tok in enumerate(path):
            if not is_target(tok, rp, i):
                continue
            hit = True
            # Predecessor: the target heard it.
            if i > 0:
                nb = resolve(path[i - 1], rp, i - 1)
                if nb and nb != target:
                    we[nb].add(tx)
                    note(nb, ts, None)
            elif is_advert and from_pk and from_pk != target:
                we[from_pk].add(tx)
                note(from_pk, ts, None)
            # Successor: it heard the target; if the target is the last
            # hop, the observer heard it directly (SNR describes that link).
            if i < n - 1:
                nb = resolve(path[i + 1], rp, i + 1)
                if nb and nb != target:
                    they[nb].add(tx)
                    note(nb, ts, None)
            elif observer and observer != target:
                they[observer].add(tx)
                note(observer, ts, snr)
        if hit:
            stats["relay_rows"] += 1

    db.close()

    # --- aggregation ---
    t_meta = node_meta.get(target, {})
    links = []
    for nb in set(we) | set(they):
        we_n, they_n = len(we[nb]), len(they[nb])
        union_n = len(we[nb] | they[nb])
        if union_n < min_evidence:
            continue
        m = node_meta.get(nb) or {"name": "(unknown)", "role": "?",
                                  "lat": None, "lon": None}
        dist = None
        if (t_meta.get("lat") is not None and t_meta.get("lon") is not None
                and m.get("lat") is not None and m.get("lon") is not None):
            dist = haversine_km(t_meta["lat"], t_meta["lon"],
                                m["lat"], m["lon"])
        links.append({
            "public_key": nb,
            "name": m["name"],
            "node_id": nb[:4].upper(),
            "role": m["role"],
            "we_hear": we_n,
            "they_hear": they_n,
            "bottleneck": min(we_n, they_n),
            "bidirectional": we_n > 0 and they_n > 0,
            "distinct_transmissions": union_n,
            "avg_snr": (round(sum(snrs[nb]) / len(snrs[nb]), 1)
                        if snrs[nb] else None),
            "distance_km": round(dist, 1) if dist is not None else None,
            "lat": m.get("lat"),
            "lon": m.get("lon"),
            "last_seen": datetime.fromtimestamp(
                last_seen.get(nb, 0), tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": confidence(we_n, they_n),
        })
    links.sort(key=lambda l: (not l["bidirectional"], -l["bottleneck"],
                              -(l["we_hear"] + l["they_hear"]),
                              l["public_key"]))

    node_info = {
        "public_key": node[0],
        "name": node[1] or "(unnamed)",
        "role": node[2] or "?",
        "lat": t_meta.get("lat"),
        "lon": t_meta.get("lon"),
    }
    capture_info = {
        "capture_end": datetime.fromtimestamp(
            capture_end, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%SZ"),
        "cutoff": (datetime.fromtimestamp(cutoff, tz=timezone.utc)
                   .strftime("%Y-%m-%d %H:%M:%SZ")
                   if cutoff is not None else None),
        "time_range": time_range,
        "unique_tokens": sorted(tokens),
        "rows_scanned": stats["rows_scanned"],
        "relay_observations": stats["relay_rows"],
        "advert_observations": stats["advert_rows"],
        "resolution": {k: stats[k] for k in
                       ("index_resolved", "rp_resolved", "unresolved")},
        "min_evidence": min_evidence,
        "neighbors_returned": len(links),
    }
    return node_info, capture_info, links


def parse_path(pj):
    """Parse a path_json blob into a list of hop tokens ([] when empty)."""
    if not pj or pj == "[]":
        return []
    try:
        v = json.loads(pj)
    except (ValueError, TypeError):
        return []
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        return []
    return v


def confidence(we_n, they_n):
    if we_n > 0 and they_n > 0 and min(we_n, they_n) >= HIGH_CONFIDENCE_MIN:
        return "high"
    if (we_n > 0 and they_n > 0) or max(we_n, they_n) >= HIGH_CONFIDENCE_MIN:
        return "medium"
    return "low"


def haversine_km(lat1, lon1, lat2, lon2):
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


# --- Output ----------------------------------------------------------------------

def print_table(node_info, capture_info, links):
    window = ""
    if capture_info.get("time_range") is not None:
        window = (f"  [last {format_timedelta(capture_info['time_range'])},"
                  f" since {capture_info['cutoff']}]")
    print(f"Node: {node_info['name']}  ({node_info['public_key'][:8]}…,"
          f" role={node_info['role']})")
    print(f"Capture end: {capture_info['capture_end']}"
          f"  |  Rows scanned: {capture_info['rows_scanned']}{window}")
    print(f"Relay observations (node in path): "
          f"{capture_info['relay_observations']}  |  "
          f"Node adverts observed: {capture_info['advert_observations']}")
    toks = ", ".join(capture_info["unique_tokens"]) or "(none)"
    print(f"Unique path tokens: {toks}")
    print()

    if not links:
        print("No neighbor evidence found for this node in the selected "
              "window.")
        return

    headers = ("#", "Neighbor", "ID", "Role", "We hear", "They hear",
               "Bottleneck", "Bidir", "Avg SNR", "Last seen", "Conf")
    table = []
    for i, l in enumerate(links, 1):
        table.append([
            str(i), l["name"], l["node_id"], l["role"],
            str(l["we_hear"]), str(l["they_hear"]), str(l["bottleneck"]),
            "yes" if l["bidirectional"] else "no",
            f"{l['avg_snr']:.1f}" if l["avg_snr"] is not None else "-",
            l["last_seen"], l["confidence"],
        ])

    widths = [display_width(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))

    aligns = ["right", "left", "left", "left", "right", "right", "right",
              "left", "right", "left", "left"]

    def render_row(cells):
        return "  ".join(
            pad(c, widths[i], aligns[i]) for i, c in enumerate(cells)
        ).rstrip()

    print(render_row(headers))
    print("-" * display_width(render_row(headers)))
    for row in table:
        print(render_row(row))
    print()
    print("We hear = distinct transmissions the node heard from the neighbor;")
    print("They hear = distinct transmissions the neighbor heard from the node.")
    print("Conf: high = bidirectional with >= 3 tx both ways; medium = "
          "bidirectional or >= 3 tx one way; low = rest.")


def print_csv(node_info, capture_info, links):
    w = csv.writer(sys.stdout)
    w.writerow(["node_name", "node_public_key", "neighbor_name", "neighbor_id",
                "neighbor_public_key", "role", "we_hear", "they_hear",
                "bottleneck", "bidirectional", "distinct_transmissions",
                "avg_snr", "distance_km", "last_seen", "confidence"])
    for l in links:
        w.writerow([
            node_info["name"], node_info["public_key"],
            l["name"], l["node_id"], l["public_key"], l["role"],
            l["we_hear"], l["they_hear"], l["bottleneck"],
            "yes" if l["bidirectional"] else "no",
            l["distinct_transmissions"],
            f"{l['avg_snr']:.1f}" if l["avg_snr"] is not None else "",
            f"{l['distance_km']:.1f}" if l["distance_km"] is not None else "",
            l["last_seen"], l["confidence"],
        ])


def print_json(node_info, capture_info, links):
    out = {
        "node": node_info,
        "meta": {
            "capture_end": capture_info["capture_end"],
            "cutoff": capture_info["cutoff"],
            "time_range": (format_timedelta(capture_info["time_range"])
                           if capture_info.get("time_range") is not None
                           else None),
            "unique_tokens": capture_info["unique_tokens"],
            "rows_scanned": capture_info["rows_scanned"],
            "relay_observations": capture_info["relay_observations"],
            "advert_observations": capture_info["advert_observations"],
            "resolution": capture_info["resolution"],
            "min_evidence": capture_info["min_evidence"],
            "neighbors_returned": capture_info["neighbors_returned"],
        },
        "neighbors": links,
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


# --- CLI ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Estimate the immediate (first-hop RF) neighbors of a "
                    "node, from path adjacency and ADVERT endpoints."
    )
    p.add_argument(
        "node",
        help="Target node: exact name, name substring, or public-key "
             "prefix (case-insensitive)",
    )
    p.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"Path to the SQLite database (default: {DEFAULT_DB})",
    )
    p.add_argument(
        "-t", "--time-range", metavar="DURATION", type=parse_time_range,
        default=DEFAULT_WINDOW,
        help="Only include evidence from the last DURATION of the capture "
             f"(e.g. 24h, 7d, 2w; default: {format_timedelta(DEFAULT_WINDOW)})",
    )
    p.add_argument(
        "--min-evidence", type=int, default=1, metavar="N",
        help="Only list neighbors with at least N distinct transmissions "
             "of evidence (default: 1)",
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


def main():
    args = parse_args()
    node_info, capture_info, links = compute_neighbors(
        args.db, args.node, time_range=args.time_range,
        min_evidence=args.min_evidence,
    )
    if args.json:
        print_json(node_info, capture_info, links)
    elif args.csv:
        print_csv(node_info, capture_info, links)
    else:
        print_table(node_info, capture_info, links)


if __name__ == "__main__":
    main()

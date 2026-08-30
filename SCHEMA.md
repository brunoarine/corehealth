## Answer

The CoreScope SQLite database schema (v3) is defined in `applySchema` in [1](#0-0)  and consists of these core tables:

| Table | Purpose |
|---|---|
| `nodes` | Mesh nodes learned from ADVERT packets |
| `observers` | MQTT feed sources (gateways) |
| `inactive_nodes` | Stale nodes moved out of `nodes` |
| `transmissions` | Deduplicated packets (unique by `hash`) |
| `observations` | Per-observer sightings of a transmission |
| `packets_v` | SQL view joining `observations`+`transmissions`+`observers` |
| `observer_metrics` | RF health time-series per observer |
| `dropped_packets` | Packets rejected by signature validation |
| `_migrations` | Tracks one-time schema migrations |

---

### Core tables

**`nodes`** and **`inactive_nodes`** share the same columns: `public_key` (PK), `name`, `role`, `lat`, `lon`, `last_seen`, `first_seen`, `advert_count`, `battery_mv`, `temperature_c` [2](#0-1) .

**`observers`** stores gateway metadata: `id` (PK), `name`, `iata`, `last_seen`, `first_seen`, `packet_count`, `model`, `firmware`, `client_version`, `radio`, `battery_mv`, `uptime_secs`, `noise_floor`, `inactive`, `last_packet_at` [3](#0-2) .

**`transmissions`** holds deduplicated raw packets keyed by `hash`: `id`, `raw_hex`, `hash` (UNIQUE), `first_seen`, `route_type`, `payload_type`, `payload_version`, `decoded_json`, `created_at` [4](#0-3) . A `channel_hash` column was added later via migration [5](#0-4) .

**`observations`** (v3 schema) records each observer's sighting of a transmission: `id`, `transmission_id` (FK), `observer_idx`, `direction`, `snr`, `rssi`, `score`, `path_json`, `timestamp` [6](#0-5) . Later migrations add `raw_hex` [7](#0-6)  and (per test fixtures) `resolved_path` for resolved multi-hop pubkeys [8](#0-7) .

**`packets_v`** is a view joining `observations` → `transmissions` → `observers` that the Go server reads for packet queries [9](#0-8) .

### Supporting tables (added via migrations)

- **`observer_metrics`**: RF health metrics keyed by `(observer_id, timestamp)`, storing `noise_floor`, `tx_air_secs`, `rx_air_secs`, `recv_errors`, `battery_mv`, plus later `packets_sent`/`packets_recv` [10](#0-9) [11](#0-10) . This is also documented in the RF Health Dashboard spec [12](#0-11) .
- **`dropped_packets`**: records signature-validation failures with `hash`, `raw_hex`, `reason`, observer/node identifiers, `dropped_at` [13](#0-12) .
- **`_migrations`**: a name-only table used to guard idempotent one-time migrations (e.g. `advert_count_unique_v1`, `channel_hash_v1`, `observers_last_packet_at_v1`) [14](#0-13) .

Schema evolution (v2 vs v3) is also tested directly: `cmd/server/db_test.go` builds an in-memory v3 schema for unit tests [15](#0-14) , while `setupTestDBV2` recreates the older schema where `observations.observer_id` is TEXT instead of `observer_idx` INTEGER, to test backward compatibility [16](#0-15) .

### Compatibility note

Both the Go and legacy Node.js engines share this exact v3 schema and can read/write the same SQLite file interchangeably [17](#0-16) .

## Notes

- The full, authoritative schema (with all migrations) lives in `applySchema` in `cmd/ingestor/db.go`; I've cited representative migration blocks but there may be additional ones further down the file not shown here.
- `docs/superpowers/specs/2026-04-23-scope-stats-design.md` mentions a proposed `scope_name` column on `transmissions`, but this appears to be a draft/future spec, not yet reflected in `applySchema` [18](#0-17) .

Wiki pages you might want to explore:
- [Go Backend Tests (Kpa-clawbot/CoreScope)](/wiki/Kpa-clawbot/CoreScope#6.1)

### Citations

**File:** cmd/ingestor/db.go (L87-156)
```go
func applySchema(db *sql.DB) error {
	// auto_vacuum=INCREMENTAL is set via DSN pragma (must be before journal_mode).
	// Logging of current mode is handled by CheckAutoVacuum — no duplicate log here.

	schema := `
		CREATE TABLE IF NOT EXISTS nodes (
			public_key TEXT PRIMARY KEY,
			name TEXT,
			role TEXT,
			lat REAL,
			lon REAL,
			last_seen TEXT,
			first_seen TEXT,
			advert_count INTEGER DEFAULT 0,
			battery_mv INTEGER,
			temperature_c REAL
		);

		CREATE TABLE IF NOT EXISTS observers (
			id TEXT PRIMARY KEY,
			name TEXT,
			iata TEXT,
			last_seen TEXT,
			first_seen TEXT,
			packet_count INTEGER DEFAULT 0,
			model TEXT,
			firmware TEXT,
			client_version TEXT,
			radio TEXT,
			battery_mv INTEGER,
			uptime_secs INTEGER,
			noise_floor REAL,
			inactive INTEGER DEFAULT 0,
			last_packet_at TEXT DEFAULT NULL
		);

		CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen);
		CREATE INDEX IF NOT EXISTS idx_observers_last_seen ON observers(last_seen);

		CREATE TABLE IF NOT EXISTS inactive_nodes (
			public_key TEXT PRIMARY KEY,
			name TEXT,
			role TEXT,
			lat REAL,
			lon REAL,
			last_seen TEXT,
			first_seen TEXT,
			advert_count INTEGER DEFAULT 0,
			battery_mv INTEGER,
			temperature_c REAL
		);

		CREATE INDEX IF NOT EXISTS idx_inactive_nodes_last_seen ON inactive_nodes(last_seen);

		CREATE TABLE IF NOT EXISTS transmissions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			raw_hex TEXT NOT NULL,
			hash TEXT NOT NULL UNIQUE,
			first_seen TEXT NOT NULL,
			route_type INTEGER,
			payload_type INTEGER,
			payload_version INTEGER,
			decoded_json TEXT,
			created_at TEXT DEFAULT (datetime('now'))
		);

		CREATE INDEX IF NOT EXISTS idx_transmissions_hash ON transmissions(hash);
		CREATE INDEX IF NOT EXISTS idx_transmissions_first_seen ON transmissions(first_seen);
		CREATE INDEX IF NOT EXISTS idx_transmissions_payload_type ON transmissions(payload_type);
	`
```

**File:** cmd/ingestor/db.go (L170-186)
```go
		obs := `
			CREATE TABLE observations (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				transmission_id INTEGER NOT NULL REFERENCES transmissions(id),
				observer_idx INTEGER,
				direction TEXT,
				snr REAL,
				rssi REAL,
				score INTEGER,
				path_json TEXT,
				timestamp INTEGER NOT NULL
			);
			CREATE INDEX idx_observations_transmission_id ON observations(transmission_id);
			CREATE INDEX idx_observations_observer_idx ON observations(observer_idx);
			CREATE INDEX idx_observations_timestamp ON observations(timestamp);
			CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_dedup ON observations(transmission_id, observer_idx, COALESCE(path_json, ''));
		`
```

**File:** cmd/ingestor/db.go (L192-209)
```go
	// Create/rebuild packets_v view (v3 schema: observer_idx → observers.rowid)
	// The Go server reads this view; without it fresh installs get "no such table: packets_v".
	db.Exec(`DROP VIEW IF EXISTS packets_v`)
	_, vErr := db.Exec(`
		CREATE VIEW packets_v AS
			SELECT o.id, COALESCE(o.raw_hex, t.raw_hex) AS raw_hex,
				   datetime(o.timestamp, 'unixepoch') AS timestamp,
				   obs.id AS observer_id, obs.name AS observer_name,
				   o.direction, o.snr, o.rssi, o.score, t.hash, t.route_type,
				   t.payload_type, t.payload_version, o.path_json, t.decoded_json,
				   t.created_at
			FROM observations o
			JOIN transmissions t ON t.id = o.transmission_id
			LEFT JOIN observers obs ON obs.rowid = o.observer_idx AND (obs.inactive IS NULL OR obs.inactive = 0)
	`)
	if vErr != nil {
		return fmt.Errorf("packets_v view: %w", vErr)
	}
```

**File:** cmd/ingestor/db.go (L211-226)
```go
	// One-time migration: recalculate advert_count to count unique transmissions only
	db.Exec(`CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY)`)
	var migDone int
	row = db.QueryRow("SELECT 1 FROM _migrations WHERE name = 'advert_count_unique_v1'")
	if row.Scan(&migDone) != nil {
		log.Println("[migration] Recalculating advert_count (unique transmissions only)...")
		db.Exec(`
			UPDATE nodes SET advert_count = (
				SELECT COUNT(*) FROM transmissions t
				WHERE t.payload_type = 4
				  AND t.decoded_json LIKE '%' || nodes.public_key || '%'
			)
		`)
		db.Exec(`INSERT INTO _migrations (name) VALUES ('advert_count_unique_v1')`)
		log.Println("[migration] advert_count recalculated")
	}
```

**File:** cmd/ingestor/db.go (L310-331)
```go
	// observer_metrics table for RF health dashboard
	row = db.QueryRow("SELECT 1 FROM _migrations WHERE name = 'observer_metrics_v1'")
	if row.Scan(&migDone) != nil {
		log.Println("[migration] Creating observer_metrics table...")
		_, err := db.Exec(`
			CREATE TABLE IF NOT EXISTS observer_metrics (
				observer_id TEXT NOT NULL,
				timestamp TEXT NOT NULL,
				noise_floor REAL,
				tx_air_secs INTEGER,
				rx_air_secs INTEGER,
				recv_errors INTEGER,
				battery_mv INTEGER,
				PRIMARY KEY (observer_id, timestamp)
			)
		`)
		if err != nil {
			return fmt.Errorf("observer_metrics schema: %w", err)
		}
		db.Exec(`INSERT INTO _migrations (name) VALUES ('observer_metrics_v1')`)
		log.Println("[migration] observer_metrics table created")
	}
```

**File:** cmd/ingestor/db.go (L358-366)
```go
	// Migration: add packets_sent and packets_recv columns to observer_metrics
	row = db.QueryRow("SELECT 1 FROM _migrations WHERE name = 'observer_metrics_packets_v1'")
	if row.Scan(&migDone) != nil {
		log.Println("[migration] Adding packets_sent/packets_recv columns to observer_metrics...")
		db.Exec(`ALTER TABLE observer_metrics ADD COLUMN packets_sent INTEGER`)
		db.Exec(`ALTER TABLE observer_metrics ADD COLUMN packets_recv INTEGER`)
		db.Exec(`INSERT INTO _migrations (name) VALUES ('observer_metrics_packets_v1')`)
		log.Println("[migration] packets_sent/packets_recv columns added")
	}
```

**File:** cmd/ingestor/db.go (L368-388)
```go
	// Migration: add channel_hash column for fast channel queries (#762)
	row = db.QueryRow("SELECT 1 FROM _migrations WHERE name = 'channel_hash_v1'")
	if row.Scan(&migDone) != nil {
		log.Println("[migration] Adding channel_hash column to transmissions...")
		db.Exec(`ALTER TABLE transmissions ADD COLUMN channel_hash TEXT DEFAULT NULL`)
		db.Exec(`CREATE INDEX IF NOT EXISTS idx_tx_channel_hash ON transmissions(channel_hash) WHERE payload_type = 5`)
		// Backfill: extract channel name for decrypted (CHAN) packets
		res, err := db.Exec(`UPDATE transmissions SET channel_hash = json_extract(decoded_json, '$.channel') WHERE payload_type = 5 AND channel_hash IS NULL AND json_extract(decoded_json, '$.type') = 'CHAN'`)
		if err == nil {
			n, _ := res.RowsAffected()
			log.Printf("[migration] Backfilled channel_hash for %d CHAN packets", n)
		}
		// Backfill: extract channelHashHex for encrypted (GRP_TXT) packets, prefixed with 'enc_'
		res, err = db.Exec(`UPDATE transmissions SET channel_hash = 'enc_' || json_extract(decoded_json, '$.channelHashHex') WHERE payload_type = 5 AND channel_hash IS NULL AND json_extract(decoded_json, '$.type') = 'GRP_TXT'`)
		if err == nil {
			n, _ := res.RowsAffected()
			log.Printf("[migration] Backfilled channel_hash for %d GRP_TXT packets", n)
		}
		db.Exec(`INSERT INTO _migrations (name) VALUES ('channel_hash_v1')`)
		log.Println("[migration] channel_hash column added and backfilled")
	}
```

**File:** cmd/ingestor/db.go (L390-414)
```go
	// Migration: dropped_packets table for signature validation failures (#793)
	row = db.QueryRow("SELECT 1 FROM _migrations WHERE name = 'dropped_packets_v1'")
	if row.Scan(&migDone) != nil {
		log.Println("[migration] Creating dropped_packets table...")
		_, err := db.Exec(`
			CREATE TABLE IF NOT EXISTS dropped_packets (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				hash TEXT,
				raw_hex TEXT,
				reason TEXT NOT NULL,
				observer_id TEXT,
				observer_name TEXT,
				node_pubkey TEXT,
				node_name TEXT,
				dropped_at DATETIME DEFAULT CURRENT_TIMESTAMP
			);
			CREATE INDEX IF NOT EXISTS idx_dropped_observer ON dropped_packets(observer_id);
			CREATE INDEX IF NOT EXISTS idx_dropped_node ON dropped_packets(node_pubkey);
		`)
		if err != nil {
			return fmt.Errorf("dropped_packets schema: %w", err)
		}
		db.Exec(`INSERT INTO _migrations (name) VALUES ('dropped_packets_v1')`)
		log.Println("[migration] dropped_packets table created")
	}
```

**File:** cmd/ingestor/db.go (L416-423)
```go
	// Migration: add raw_hex column to observations (#881)
	row = db.QueryRow("SELECT 1 FROM _migrations WHERE name = 'observations_raw_hex_v1'")
	if row.Scan(&migDone) != nil {
		log.Println("[migration] Adding raw_hex column to observations...")
		db.Exec(`ALTER TABLE observations ADD COLUMN raw_hex TEXT`)
		db.Exec(`INSERT INTO _migrations (name) VALUES ('observations_raw_hex_v1')`)
		log.Println("[migration] observations.raw_hex column added")
	}
```

**File:** cmd/server/db_test.go (L24-98)
```go
	schema := `
		CREATE TABLE nodes (
			public_key TEXT PRIMARY KEY,
			name TEXT,
			role TEXT,
			lat REAL,
			lon REAL,
			last_seen TEXT,
			first_seen TEXT,
			advert_count INTEGER DEFAULT 0,
			battery_mv INTEGER,
			temperature_c REAL
		);

		CREATE TABLE observers (
			id TEXT PRIMARY KEY,
			name TEXT,
			iata TEXT,
			last_seen TEXT,
			first_seen TEXT,
			packet_count INTEGER DEFAULT 0,
			model TEXT,
			firmware TEXT,
			client_version TEXT,
			radio TEXT,
			battery_mv INTEGER,
			uptime_secs INTEGER,
			noise_floor REAL,
			inactive INTEGER DEFAULT 0,
			last_packet_at TEXT DEFAULT NULL
		);

		CREATE TABLE transmissions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			raw_hex TEXT NOT NULL,
			hash TEXT NOT NULL UNIQUE,
			first_seen TEXT NOT NULL,
			route_type INTEGER,
			payload_type INTEGER,
			payload_version INTEGER,
			decoded_json TEXT,
			channel_hash TEXT DEFAULT NULL,
			created_at TEXT DEFAULT (datetime('now'))
		);

		CREATE TABLE observations (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			transmission_id INTEGER NOT NULL REFERENCES transmissions(id),
			observer_idx INTEGER,
			direction TEXT,
			snr REAL,
			rssi REAL,
			score INTEGER,
			path_json TEXT,
			timestamp INTEGER NOT NULL,
			resolved_path TEXT,
			raw_hex TEXT
		);

		CREATE TABLE IF NOT EXISTS observer_metrics (
			observer_id TEXT NOT NULL,
			timestamp TEXT NOT NULL,
			noise_floor REAL,
			tx_air_secs INTEGER,
			rx_air_secs INTEGER,
			recv_errors INTEGER,
			battery_mv INTEGER,
			packets_sent INTEGER,
			packets_recv INTEGER,
			PRIMARY KEY (observer_id, timestamp)
		);

		CREATE INDEX IF NOT EXISTS idx_observer_metrics_timestamp ON observer_metrics(timestamp);

	`
```

**File:** cmd/server/db_test.go (L1155-1220)
```go
// setupTestDBV2 creates an in-memory SQLite database with the v2 schema
// where observations use observer_id TEXT instead of observer_idx INTEGER.
func setupTestDBV2(t *testing.T) *DB {
	t.Helper()
	conn, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	conn.SetMaxOpenConns(1)

	schema := `
		CREATE TABLE nodes (
			public_key TEXT PRIMARY KEY,
			name TEXT,
			role TEXT,
			lat REAL,
			lon REAL,
			last_seen TEXT,
			first_seen TEXT,
			advert_count INTEGER DEFAULT 0,
			battery_mv INTEGER,
			temperature_c REAL
		);

		CREATE TABLE observers (
			id TEXT PRIMARY KEY,
			name TEXT,
			iata TEXT,
			last_seen TEXT,
			first_seen TEXT,
			packet_count INTEGER DEFAULT 0,
			last_packet_at TEXT DEFAULT NULL
		);

		CREATE TABLE transmissions (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			raw_hex TEXT NOT NULL,
			hash TEXT NOT NULL UNIQUE,
			first_seen TEXT NOT NULL,
			route_type INTEGER,
			payload_type INTEGER,
			payload_version INTEGER,
			decoded_json TEXT,
			channel_hash TEXT DEFAULT NULL,
			created_at TEXT DEFAULT (datetime('now'))
		);

		CREATE TABLE observations (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			transmission_id INTEGER NOT NULL REFERENCES transmissions(id),
			observer_id TEXT,
			observer_name TEXT,
			direction TEXT,
			snr REAL,
			rssi REAL,
			score INTEGER,
			path_json TEXT,
			timestamp INTEGER NOT NULL,
			raw_hex TEXT
		);
	`
	if _, err := conn.Exec(schema); err != nil {
		t.Fatal(err)
	}

	return &DB{conn: conn, isV3: false}
```

**File:** docs/specs/rf-health-dashboard.md (L31-46)
```markdown
### New table: `observer_metrics`

```sql
CREATE TABLE IF NOT EXISTS observer_metrics (
    observer_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,       -- ISO 8601, rounded to nearest sample interval
    noise_floor REAL,              -- dBm, from radio stats (nullable — may arrive without airtime)
    tx_air_secs INTEGER,           -- cumulative TX seconds since boot (nullable)
    rx_air_secs INTEGER,           -- cumulative RX seconds since boot (nullable)
    packets_sent INTEGER,          -- cumulative packets sent since boot (nullable)
    packets_recv INTEGER,          -- cumulative packets received since boot (nullable)
    recv_errors INTEGER,           -- cumulative CRC/decode failures since boot (nullable)
    battery_mv INTEGER,            -- battery voltage in millivolts (nullable, for field/solar nodes)
    PRIMARY KEY (observer_id, timestamp)
);
```
```

**File:** docs/go-migration.md (L187-220)
```markdown
## DB Compatibility

### Schema

Both engines use the same **v3 schema**:

| Table | Purpose | Shared? |
|-------|---------|---------|
| `nodes` | Mesh nodes from adverts | ✅ Both read/write |
| `observers` | MQTT feed sources | ✅ Both read/write |
| `inactive_nodes` | Nodes past retention window | ✅ Both read/write |
| `transmissions` | Deduplicated packets | ✅ Both read/write |
| `observations` | Per-observer sightings | ✅ Both read/write |
| `_migrations` | One-time migration tracking | ✅ Both read/write |

### Can Go read a Node.js DB?

**Yes.** The Go ingestor and server open existing v3 databases with no issues. If the database is pre-v3 (no `observations` table), Go creates it automatically using the same v3 schema.

### Can Node.js read a Go-modified DB?

**Yes.** Go writes the same schema and data formats. You can switch back to Node.js and it will read the DB normally.

### SQLite WAL mode

Both engines use WAL (Write-Ahead Logging) mode for concurrent access. The Go image runs two processes (ingestor + server) writing to the same DB file — same as Node.js running a single process.

### Migration on first run

When Go opens a database for the first time:
1. Creates missing tables (`transmissions`, `observations`, `nodes`, `observers`, `inactive_nodes`) with `CREATE TABLE IF NOT EXISTS`
2. Runs the `advert_count_unique_v1` migration if not already done (recalculates advert counts)
3. Does NOT modify existing data

```

**File:** docs/superpowers/specs/2026-04-23-scope-stats-design.md (L84-88)
```markdown
### Migration

```sql
ALTER TABLE transmissions ADD COLUMN scope_name TEXT DEFAULT NULL;
CREATE INDEX idx_tx_scope_name ON transmissions(scope_name) WHERE scope_name IS NOT NULL;
```


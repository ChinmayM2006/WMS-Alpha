import os
import sqlite3
from datetime import datetime
from flask import Flask, g, render_template, request, redirect, url_for, flash, session

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "wms.db")

SIZE_ORDER = {"S": 0, "M": 1, "L": 2}
MAX_SIZE_BY_SHELF = {1: "S", 2: "M", 3: "L"}

app = Flask(__name__)
app.secret_key = "wms-dev-secret"


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def size_fits(sku_size, slot_max_size):
    return SIZE_ORDER[sku_size] <= SIZE_ORDER[slot_max_size]


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sku_master (
            sku_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            size TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS racks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            zone TEXT NOT NULL,
            row INTEGER NOT NULL,
            col INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rack_id INTEGER NOT NULL,
            shelf INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            max_size TEXT NOT NULL,
            occupied INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (rack_id) REFERENCES racks (id)
        );

        CREATE TABLE IF NOT EXISTS skus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            size TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sku_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_id TEXT NOT NULL,
            slot_id INTEGER,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            picked_at TEXT,
            shipped_at TEXT,
            FOREIGN KEY (sku_id) REFERENCES skus (sku_id),
            FOREIGN KEY (slot_id) REFERENCES slots (id)
        );

        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_unit_id INTEGER NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            moved_at TEXT NOT NULL,
            FOREIGN KEY (sku_unit_id) REFERENCES sku_units (id)
        );
        """
    )


def seed_settings(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
    if row is None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("mode", "FIFO"))


def seed_sku_master(conn):
    existing = conn.execute("SELECT COUNT(*) AS count FROM sku_master").fetchone()["count"]
    if existing:
        return

    timestamp = now_iso()
    samples = [
        ("SKU-1001", "Widget Clamp", "S"),
        ("SKU-1002", "Pallet Wrap", "M"),
        ("SKU-1003", "Gear Housing", "L"),
        ("SKU-1004", "Fork Sleeve", "M"),
        ("SKU-1005", "Seal Kit", "S"),
        ("SKU-1006", "Valve Cover", "M"),
        ("SKU-1007", "Bearing Set", "S"),
        ("SKU-1008", "Hydraulic Pump", "L"),
        ("SKU-1009", "Control Cable", "S"),
        ("SKU-1010", "Dock Bumper", "L"),
        ("SKU-2001", "Gear Rack", "M"),
        ("SKU-2002", "Drive Belt", "S"),
        ("SKU-2003", "Bulk Hopper", "L"),
    ]
    conn.executemany(
        "INSERT INTO sku_master (sku_id, name, size, active, created_at) VALUES (?, ?, ?, 1, ?)",
        [(sku_id, name, size, timestamp) for sku_id, name, size in samples],
    )


def get_master_sku(conn, sku_id):
    return conn.execute(
        "SELECT sku_id, name, size, active FROM sku_master WHERE sku_id=?",
        (sku_id,),
    ).fetchone()


def zone_for_col(col):
    if col == 0:
        return "Receiving"
    if col == 3:
        return "Shipping"
    return "Storage"


def seed_racks(conn):
    existing = conn.execute("SELECT COUNT(*) AS count FROM racks").fetchone()["count"]
    if existing:
        return

    rack_id = 1
    for row in range(2):
        for col in range(4):
            code = f"R{rack_id}"
            zone = zone_for_col(col)
            conn.execute(
                "INSERT INTO racks (code, zone, row, col) VALUES (?, ?, ?, ?)",
                (code, zone, row, col),
            )
            rack_id += 1

    racks = conn.execute("SELECT id FROM racks ORDER BY id").fetchall()
    for rack in racks:
        for shelf in range(1, 4):
            max_size = MAX_SIZE_BY_SHELF[shelf]
            for slot in range(1, 5):
                conn.execute(
                    "INSERT INTO slots (rack_id, shelf, slot, max_size, occupied) VALUES (?, ?, ?, ?, 0)",
                    (rack["id"], shelf, slot, max_size),
                )


def find_best_slot(conn, sku_size):
    slots = conn.execute(
        """
        SELECT s.id, s.rack_id, s.shelf, s.slot, s.max_size, s.occupied,
               r.code, r.row, r.col
        FROM slots s
        JOIN racks r ON r.id = s.rack_id
        ORDER BY r.id, s.shelf, s.slot
        """
    ).fetchall()

    rack_stats = {}
    for slot in slots:
        rack_id = slot["rack_id"]
        stats = rack_stats.setdefault(
            rack_id,
            {"total": 0, "occupied": 0, "eligible": [], "code": slot["code"], "row": slot["row"], "col": slot["col"]},
        )
        stats["total"] += 1
        if slot["occupied"]:
            stats["occupied"] += 1
        if not slot["occupied"] and size_fits(sku_size, slot["max_size"]):
            stats["eligible"].append(slot)

    candidates = []
    for rack_id, stats in rack_stats.items():
        if stats["eligible"]:
            fill_ratio = stats["occupied"] / stats["total"]
            candidates.append((fill_ratio, stats["occupied"], rack_id, stats))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x[0], -x[1], x[2]))
    best = candidates[0][3]
    best["eligible"].sort(key=lambda s: (s["shelf"], s["slot"]))
    return best["eligible"][0]


def reserve_slots(conn, sku_size, quantity):
    reserved = []
    for _ in range(quantity):
        slot = find_best_slot(conn, sku_size)
        if slot is None:
            raise ValueError("Not enough eligible slots available")
        conn.execute("UPDATE slots SET occupied=1 WHERE id=?", (slot["id"],))
        reserved.append(slot)
    return reserved


def add_sku_internal(conn, sku_id, name, size, quantity):
    master = get_master_sku(conn, sku_id)
    if not master or not master["active"]:
        raise ValueError("SKU not in master catalog")

    existing = conn.execute("SELECT 1 FROM skus WHERE sku_id=?", (sku_id,)).fetchone()
    if existing:
        raise ValueError("SKU already exists. Use Receive Stock to add units")

    if size not in SIZE_ORDER:
        raise ValueError("Invalid size")

    if master["size"] != size:
        raise ValueError(f"Size mismatch for {sku_id}. Expected {master['size']}")

    if master["name"].strip().lower() != name.strip().lower():
        raise ValueError(f"Name mismatch for {sku_id}. Expected '{master['name']}'")

    timestamp = now_iso()
    with conn:
        reserved = reserve_slots(conn, size, quantity)
        conn.execute(
            "INSERT INTO skus (sku_id, name, size, created_at) VALUES (?, ?, ?, ?)",
            (sku_id, master["name"], master["size"], timestamp),
        )
        for slot in reserved:
            cur = conn.execute(
                "INSERT INTO sku_units (sku_id, slot_id, status, created_at) VALUES (?, ?, ?, ?)",
                (sku_id, slot["id"], "Stored", timestamp),
            )
            conn.execute(
                "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, "Inbound", "Docked", timestamp),
            )
            conn.execute(
                "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, "Docked", "Stored", timestamp),
            )

    return [format_slot_label(conn, slot["id"]) for slot in reserved]


def receive_stock(conn, sku_id, quantity, destination):
    sku = conn.execute("SELECT sku_id, name, size FROM skus WHERE sku_id=?", (sku_id,)).fetchone()
    if not sku:
        raise ValueError("SKU not found. Use Add SKU to create it first")

    master = get_master_sku(conn, sku_id)
    if not master or not master["active"]:
        raise ValueError("SKU not in master catalog")

    if master["name"] != sku["name"] or master["size"] != sku["size"]:
        raise ValueError("SKU master mismatch. Check catalog details")

    timestamp = now_iso()
    slots = []
    with conn:
        if destination == "stored":
            reserved = reserve_slots(conn, sku["size"], quantity)
            for slot in reserved:
                cur = conn.execute(
                    "INSERT INTO sku_units (sku_id, slot_id, status, created_at) VALUES (?, ?, ?, ?)",
                    (sku_id, slot["id"], "Stored", timestamp),
                )
                conn.execute(
                    "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, "Inbound", "Docked", timestamp),
                )
                conn.execute(
                    "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, "Docked", "Stored", timestamp),
                )
                slots.append(format_slot_label(conn, slot["id"]))
        elif destination == "docked":
            for _ in range(quantity):
                cur = conn.execute(
                    "INSERT INTO sku_units (sku_id, slot_id, status, created_at) VALUES (?, NULL, 'Docked', ?)",
                    (sku_id, timestamp),
                )
                conn.execute(
                    "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                    (cur.lastrowid, "Inbound", "Docked", timestamp),
                )
        else:
            raise ValueError("Invalid destination")

    return slots


def putaway_docked(conn, sku_id=None, quantity=None):
    params = []
    query = (
        "SELECT u.id, u.sku_id, s.size "
        "FROM sku_units u "
        "JOIN skus s ON s.sku_id = u.sku_id "
        "WHERE u.status='Docked'"
    )
    if sku_id:
        query += " AND u.sku_id=?"
        params.append(sku_id)
    query += " ORDER BY u.created_at"
    if quantity is not None:
        query += " LIMIT ?"
        params.append(quantity)

    units = conn.execute(query, params).fetchall()
    if not units:
        return []

    timestamp = now_iso()
    stored_slots = []
    with conn:
        for unit in units:
            slot = find_best_slot(conn, unit["size"])
            if slot is None:
                raise ValueError("Not enough eligible slots available for putaway")
            conn.execute("UPDATE slots SET occupied=1 WHERE id=?", (slot["id"],))
            conn.execute(
                "UPDATE sku_units SET status='Stored', slot_id=? WHERE id=?",
                (slot["id"], unit["id"]),
            )
            conn.execute(
                "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                (unit["id"], "Docked", "Stored", timestamp),
            )
            stored_slots.append(format_slot_label(conn, slot["id"]))

    return stored_slots


def seed_skus(conn):
    existing = conn.execute("SELECT COUNT(*) AS count FROM skus").fetchone()["count"]
    if existing:
        return

    samples = [
        ("SKU-1001", "Widget Clamp", "S", 4),
        ("SKU-1002", "Pallet Wrap", "M", 6),
        ("SKU-1003", "Gear Housing", "L", 2),
        ("SKU-1004", "Fork Sleeve", "M", 5),
        ("SKU-1005", "Seal Kit", "S", 3),
        ("SKU-1006", "Valve Cover", "M", 4),
        ("SKU-1007", "Bearing Set", "S", 6),
        ("SKU-1008", "Hydraulic Pump", "L", 2),
        ("SKU-1009", "Control Cable", "S", 5),
        ("SKU-1010", "Dock Bumper", "L", 1),
    ]
    for sku_id, name, size, quantity in samples:
        add_sku_internal(conn, sku_id, name, size, quantity)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with conn:
        ensure_schema(conn)
        seed_settings(conn)
        seed_racks(conn)
        seed_sku_master(conn)
        seed_skus(conn)
    conn.close()


def format_slot_label(conn, slot_id):
    row = conn.execute(
        """
        SELECT s.shelf, s.slot, r.code
        FROM slots s
        JOIN racks r ON r.id = s.rack_id
        WHERE s.id=?
        """,
        (slot_id,),
    ).fetchone()
    if not row:
        return ""
    return f"{row['code']}-S{row['shelf']}-{row['slot']:02d}"


def build_rack_layout(conn):
    racks = conn.execute("SELECT * FROM racks ORDER BY row, col").fetchall()
    slots = conn.execute(
        "SELECT * FROM slots ORDER BY rack_id, shelf, slot"
    ).fetchall()

    rack_map = {rack["id"]: {"info": rack, "shelves": {1: [], 2: [], 3: []}} for rack in racks}
    for slot in slots:
        rack_map[slot["rack_id"]]["shelves"][slot["shelf"]].append(slot)

    grid = [[None for _ in range(4)] for _ in range(2)]
    for rack in racks:
        grid[rack["row"]][rack["col"]] = rack_map[rack["id"]]

    return grid


def get_mode(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
    return row["value"] if row else "FIFO"


def set_mode(conn, value):
    with conn:
        conn.execute("UPDATE settings SET value=? WHERE key='mode'", (value,))


def inventory_summary(conn):
    rows = conn.execute(
        """
        SELECT s.sku_id, s.name, s.size,
               SUM(CASE WHEN u.status != 'Shipped' THEN 1 ELSE 0 END) AS qty,
               SUM(CASE WHEN u.status = 'Docked' THEN 1 ELSE 0 END) AS docked,
               SUM(CASE WHEN u.status = 'Stored' THEN 1 ELSE 0 END) AS stored,
               SUM(CASE WHEN u.status = 'Picked' THEN 1 ELSE 0 END) AS picked
        FROM skus s
        LEFT JOIN sku_units u ON u.sku_id = s.sku_id
        GROUP BY s.sku_id, s.name, s.size
        """
    ).fetchall()

    last_moves = conn.execute(
        """
        SELECT u.sku_id, MAX(m.moved_at) AS last_move
        FROM sku_units u
        LEFT JOIN movements m ON m.sku_unit_id = u.id
        GROUP BY u.sku_id
        """
    ).fetchall()

    last_move_map = {row["sku_id"]: row["last_move"] for row in last_moves}

    slot_rows = conn.execute(
        """
        SELECT u.sku_id, r.code AS rack_code, s.shelf, s.slot
        FROM sku_units u
        JOIN slots s ON s.id = u.slot_id
        JOIN racks r ON r.id = s.rack_id
        WHERE u.status = 'Stored'
        ORDER BY u.sku_id, r.id, s.shelf, s.slot
        """
    ).fetchall()

    slot_map = {}
    for row in slot_rows:
        label = f"{row['rack_code']}-S{row['shelf']}-{row['slot']:02d}"
        slot_map.setdefault(row["sku_id"], []).append(label)

    summary = []
    for row in rows:
        slots = slot_map.get(row["sku_id"], [])
        summary.append(
            {
                "sku_id": row["sku_id"],
                "name": row["name"],
                "size": row["size"],
                "qty": row["qty"] or 0,
                "docked": row["docked"] or 0,
                "stored": row["stored"] or 0,
                "picked": row["picked"] or 0,
                "slots": slots,
                "last_move": last_move_map.get(row["sku_id"]),
            }
        )

    summary.sort(key=lambda item: item["last_move"] or "", reverse=True)
    return summary


def stored_count(conn, sku_id):
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM sku_units WHERE sku_id=? AND status='Stored'",
        (sku_id,),
    ).fetchone()
    return row["count"] if row else 0


def pick_units(conn, sku_id, quantity, mode):
    order = "ASC" if mode == "FIFO" else "DESC"
    units = conn.execute(
        f"""
        SELECT u.id, u.slot_id, u.created_at, s.shelf, s.slot, r.row, r.col, r.code
        FROM sku_units u
        JOIN slots s ON s.id = u.slot_id
        JOIN racks r ON r.id = s.rack_id
        WHERE u.sku_id=? AND u.status='Stored'
        ORDER BY u.created_at {order}
        LIMIT ?
        """,
        (sku_id, quantity),
    ).fetchall()

    if len(units) < quantity:
        raise ValueError("Not enough stored units to pick")

    picked_at = now_iso()
    with conn:
        for unit in units:
            conn.execute(
                "UPDATE sku_units SET status='Picked', picked_at=? WHERE id=?",
                (picked_at, unit["id"]),
            )
            conn.execute(
                "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                (unit["id"], "Stored", "Picked", picked_at),
            )

    return units


def ship_picked(conn):
    picked = conn.execute(
        """
        SELECT u.id, u.slot_id
        FROM sku_units u
        WHERE u.status='Picked'
        """
    ).fetchall()

    shipped_at = now_iso()
    with conn:
        for unit in picked:
            if unit["slot_id"] is not None:
                conn.execute("UPDATE slots SET occupied=0 WHERE id=?", (unit["slot_id"],))
            conn.execute(
                "UPDATE sku_units SET status='Shipped', shipped_at=?, slot_id=NULL WHERE id=?",
                (shipped_at, unit["id"]),
            )
            conn.execute(
                "INSERT INTO movements (sku_unit_id, from_status, to_status, moved_at) VALUES (?, ?, ?, ?)",
                (unit["id"], "Picked", "Shipped", shipped_at),
            )

    return len(picked)


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def nearest_neighbor_route(units):
    remaining = []
    seen = set()
    for unit in units:
        key = (unit["slot_id"], unit["row"], unit["col"], unit["shelf"], unit["slot"], unit["code"])
        if key not in seen:
            remaining.append(
                {
                    "slot_id": unit["slot_id"],
                    "row": unit["row"],
                    "col": unit["col"],
                    "shelf": unit["shelf"],
                    "slot": unit["slot"],
                    "code": unit["code"],
                }
            )
            seen.add(key)

    route = []
    current = (0, 0)
    while remaining:
        next_stop = min(remaining, key=lambda s: manhattan(current, (s["row"], s["col"])))
        route.append(next_stop)
        current = (next_stop["row"], next_stop["col"])
        remaining.remove(next_stop)

    return route


def parse_pick_list(raw_text):
    items = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError("Each line must be SKU_ID:QTY")
        sku_id, qty = line.split(":", 1)
        sku_id = sku_id.strip()
        qty = int(qty.strip())
        if qty <= 0:
            raise ValueError("Quantity must be positive")
        items.append((sku_id, qty))
    return items


def delete_sku(conn, sku_id):
    units = conn.execute("SELECT slot_id FROM sku_units WHERE sku_id=?", (sku_id,)).fetchall()
    with conn:
        for unit in units:
            if unit["slot_id"] is not None:
                conn.execute("UPDATE slots SET occupied=0 WHERE id=?", (unit["slot_id"],))
        conn.execute("DELETE FROM sku_units WHERE sku_id=?", (sku_id,))
        conn.execute("DELETE FROM skus WHERE sku_id=?", (sku_id,))


def recent_activity(conn, limit=10):
    rows = conn.execute(
        """
        SELECT m.moved_at, m.from_status, m.to_status,
               u.sku_id, s.name,
               r.code AS rack_code, sl.shelf, sl.slot
        FROM movements m
        JOIN sku_units u ON u.id = m.sku_unit_id
        JOIN skus s ON s.sku_id = u.sku_id
        LEFT JOIN slots sl ON sl.id = u.slot_id
        LEFT JOIN racks r ON r.id = sl.rack_id
        ORDER BY m.moved_at DESC, m.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    activity = []
    for row in rows:
        if row["rack_code"]:
            slot_label = f"{row['rack_code']}-S{row['shelf']}-{row['slot']:02d}"
        else:
            slot_label = "-"
        activity.append(
            {
                "moved_at": row["moved_at"],
                "sku_id": row["sku_id"],
                "name": row["name"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "slot": slot_label,
            }
        )

    return activity


@app.route("/")
def dashboard():
    conn = get_db()
    mode = get_mode(conn)
    layout = build_rack_layout(conn)
    inventory = inventory_summary(conn)
    activity = recent_activity(conn)
    last_route = session.pop("last_route", None)
    return render_template(
        "index.html",
        mode=mode,
        layout=layout,
        inventory=inventory,
        activity=activity,
        last_route=last_route,
    )


@app.route("/toggle_mode", methods=["POST"])
def toggle_mode():
    conn = get_db()
    mode = get_mode(conn)
    new_mode = "LIFO" if mode == "FIFO" else "FIFO"
    set_mode(conn, new_mode)
    flash(f"Mode set to {new_mode}")
    return redirect(url_for("dashboard"))


@app.route("/add_sku", methods=["POST"])
def add_sku():
    conn = get_db()
    sku_id = request.form.get("sku_id", "").strip()
    name = request.form.get("name", "").strip()
    size = request.form.get("size", "").strip().upper()
    quantity_raw = request.form.get("quantity", "1").strip()

    if not sku_id or not name or size not in SIZE_ORDER:
        flash("SKU ID, name, and size are required")
        return redirect(url_for("dashboard"))

    try:
        quantity = int(quantity_raw)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be positive")
        return redirect(url_for("dashboard"))

    try:
        slots = add_sku_internal(conn, sku_id, name, size, quantity)
        flash(f"New SKU {sku_id} created and stored in: {', '.join(slots)}")
    except ValueError as exc:
        flash(str(exc))

    return redirect(url_for("dashboard"))


@app.route("/receive_stock", methods=["POST"])
def receive_stock_route():
    conn = get_db()
    sku_id = request.form.get("sku_id", "").strip()
    quantity_raw = request.form.get("quantity", "1").strip()
    destination = request.form.get("destination", "stored")

    if not sku_id:
        flash("SKU ID required")
        return redirect(url_for("dashboard"))

    try:
        quantity = int(quantity_raw)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be positive")
        return redirect(url_for("dashboard"))

    try:
        slots = receive_stock(conn, sku_id, quantity, destination)
        if destination == "stored":
            flash(f"Received {quantity} units of {sku_id} into storage: {', '.join(slots)}")
        else:
            flash(f"Received {quantity} units of {sku_id} to dock staging")
    except ValueError as exc:
        flash(str(exc))

    return redirect(url_for("dashboard"))


@app.route("/putaway_docked", methods=["POST"])
def putaway_docked_route():
    conn = get_db()
    sku_id = request.form.get("sku_id", "").strip()
    quantity_raw = request.form.get("quantity", "").strip()
    quantity = None

    if quantity_raw:
        try:
            quantity = int(quantity_raw)
            if quantity <= 0:
                raise ValueError
        except ValueError:
            flash("Quantity must be positive")
            return redirect(url_for("dashboard"))

    try:
        slots = putaway_docked(conn, sku_id or None, quantity)
        if not slots:
            flash("No docked units available for putaway")
        else:
            flash(f"Put away {len(slots)} units into storage: {', '.join(slots)}")
    except ValueError as exc:
        flash(str(exc))

    return redirect(url_for("dashboard"))


@app.route("/delete_sku", methods=["POST"])
def delete_sku_route():
    conn = get_db()
    sku_id = request.form.get("sku_id", "").strip()
    if not sku_id:
        flash("SKU ID required")
        return redirect(url_for("dashboard"))
    delete_sku(conn, sku_id)
    flash(f"SKU {sku_id} deleted")
    return redirect(url_for("dashboard"))


@app.route("/pick_sku", methods=["POST"])
def pick_sku_route():
    conn = get_db()
    sku_id = request.form.get("sku_id", "").strip()
    quantity_raw = request.form.get("quantity", "1").strip()
    if not sku_id:
        flash("SKU ID required")
        return redirect(url_for("dashboard"))

    try:
        quantity = int(quantity_raw)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be positive")
        return redirect(url_for("dashboard"))

    mode = get_mode(conn)
    try:
        units = pick_units(conn, sku_id, quantity, mode)
        route = nearest_neighbor_route(units)
        session["last_route"] = [
            f"{item['code']}-S{item['shelf']}-{item['slot']:02d} (row {item['row']}, col {item['col']})"
            for item in route
        ]
        flash(f"Picked {quantity} units of {sku_id} using {mode}")
    except ValueError as exc:
        flash(str(exc))

    return redirect(url_for("dashboard"))


@app.route("/pick_list", methods=["POST"])
def pick_list_route():
    conn = get_db()
    raw_list = request.form.get("pick_list", "")
    mode = get_mode(conn)

    try:
        items = parse_pick_list(raw_list)
        for sku_id, qty in items:
            if stored_count(conn, sku_id) < qty:
                raise ValueError(f"Not enough stored units for {sku_id}")
        all_units = []
        for sku_id, qty in items:
            units = pick_units(conn, sku_id, qty, mode)
            all_units.extend(units)
        route = nearest_neighbor_route(all_units)
        session["last_route"] = [
            f"{item['code']}-S{item['shelf']}-{item['slot']:02d} (row {item['row']}, col {item['col']})"
            for item in route
        ]
        flash(f"Picked {len(all_units)} units across {len(items)} SKUs using {mode}")
    except (ValueError, TypeError) as exc:
        flash(str(exc))

    return redirect(url_for("dashboard"))


@app.route("/ship_picked", methods=["POST"])
def ship_picked_route():
    conn = get_db()
    count = ship_picked(conn)
    flash(f"Shipped {count} picked units")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)

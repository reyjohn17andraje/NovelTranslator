import os
import json
import threading
import time
import requests
import boto3
from datetime import datetime
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from openai import OpenAI

# ===================== ENV =====================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "").strip()
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()

if not all([OPENAI_API_KEY, R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT, R2_BUCKET]):
    raise RuntimeError("Missing environment variables")

client = OpenAI(api_key=OPENAI_API_KEY)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

app = FastAPI()

# ===================== STORAGE KEYS =====================

STATE_KEY = "state.json"
ERROR_KEY = "errors.json"
CHAPTER_PREFIX = "chapters/"

# ===================== STATE =====================

state = {
    "running": False,
    "current_url": None,
    "chapter": 0,
    "visited": [],
    "last_action": "Idle",
}

errors = []

def load_json(key, default):
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode())
    except:
        return default

def save_json(key, data):
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=json.dumps(data),
        ContentType="application/json"
    )

state.update(load_json(STATE_KEY, {}))
errors.extend(load_json(ERROR_KEY, []))

# ===================== HELPERS =====================

def list_chapters():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=CHAPTER_PREFIX)
    if "Contents" not in res:
        return []
    return sorted(obj["Key"].split("/")[-1] for obj in res["Contents"])

def save_chapter(num, text):
    html = "".join(f"<p>{p}</p>" for p in text.split("\n\n"))
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=f"{CHAPTER_PREFIX}{num:03d}.html",
        Body=html,
        ContentType="text/html"
    )

def clean(text):
    return "\n\n".join(l.strip() for l in text.splitlines() if l.strip())

def translate(text):
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Translate Chinese web novel into fluent English. Preserve structure."},
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content

def scrape(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.encoding = "gb2312"
    soup = BeautifulSoup(r.text, "html.parser")

    content_div = soup.find("div", class_="content")
    if not content_div:
        raise RuntimeError("Content not found")

    text = clean("\n".join(p.get_text(strip=True) for p in content_div.find_all("p")))

    next_url = None
    pages = soup.find("div", class_="artic_pages")
    if pages:
        links = pages.find_all("a")
        if len(links) >= 2:
            next_url = requests.compat.urljoin(url, links[1].get("href"))

    return text, next_url

# ===================== WORKER =====================

def worker():
    while state["running"] and state["current_url"]:
        url = state["current_url"]

        if url in state["visited"]:
            state["running"] = False
            state["last_action"] = "Stopped (duplicate detected)"
            save_json(STATE_KEY, state)
            break

        try:
            state["last_action"] = f"Fetching chapter {state['chapter'] + 1}"
            save_json(STATE_KEY, state)

            raw, next_url = scrape(url)
            translated = translate(raw)

            state["chapter"] += 1
            save_chapter(state["chapter"], translated)

            state["visited"].append(url)
            state["current_url"] = next_url
            state["last_action"] = f"Saved Chapter {state['chapter']}"

            save_json(STATE_KEY, state)

            if not next_url:
                state["running"] = False
                state["last_action"] = "Completed"
                save_json(STATE_KEY, state)
                break

            time.sleep(2)

        except Exception as e:
            errors.append({
                "chapter": state["chapter"] + 1,
                "url": url,
                "error": str(e),
                "time": datetime.utcnow().isoformat()
            })
            save_json(ERROR_KEY, errors)
            state["running"] = False
            state["last_action"] = "Error occurred"
            save_json(STATE_KEY, state)
            break

# ===================== ROUTES =====================

@app.get("/status")
def status():
    return {
        "running": state["running"],
        "chapter": state["chapter"],
        "last_action": state["last_action"],
        "errors": len(errors)
    }

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["current_url"] = url

    if not state["current_url"]:
        return RedirectResponse("/", 303)

    if not state["running"]:
        state["running"] = True
        state["last_action"] = "Started"
        save_json(STATE_KEY, state)
        threading.Thread(target=worker, daemon=True).start()

    return RedirectResponse("/", 303)

@app.post("/stop")
def stop():
    state["running"] = False
    state["last_action"] = "Paused"
    save_json(STATE_KEY, state)
    return RedirectResponse("/", 303)

@app.post("/reset")
def reset():
    objs = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=CHAPTER_PREFIX)
    for o in objs.get("Contents", []):
        s3.delete_object(Bucket=R2_BUCKET, Key=o["Key"])

    state.clear()
    state.update({
        "running": False,
        "current_url": None,
        "chapter": 0,
        "visited": [],
        "last_action": "Reset"
    })
    errors.clear()

    save_json(STATE_KEY, state)
    save_json(ERROR_KEY, errors)

    return RedirectResponse("/", 303)

# ===================== UI =====================

@app.get("/", response_class=HTMLResponse)
def home():
    return f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FictionMe</title>
<style>
body {{
  margin:0;
  background:linear-gradient(180deg,#020617,#020617);
  color:#e5e7eb;
  font-family:-apple-system,BlinkMacSystemFont,sans-serif;
}}
.card {{
  margin:20px;
  padding:20px;
  background:#020617;
  border-radius:16px;
}}
button,input {{
  width:100%;
  padding:14px;
  margin-top:10px;
  border-radius:12px;
  border:none;
}}
.bottom {{
  position:fixed;
  bottom:0;
  width:100%;
  display:flex;
  background:#020617;
}}
.bottom a {{
  flex:1;
  padding:14px;
  text-align:center;
  color:#9ca3af;
  text-decoration:none;
}}
</style>
<script>
async function poll(){{
  const r = await fetch('/status');
  const s = await r.json();
  document.getElementById('stat').innerText =
    s.running ? 'Running' : 'Stopped';
  document.getElementById('chap').innerText = s.chapter;
  document.getElementById('act').innerText = s.last_action;
}}
setInterval(poll,2000);
</script>
</head>
<body>

<div class="card">
<h2>📘 Current Book</h2>
<p>Status: <b id="stat">{'Running' if state['running'] else 'Stopped'}</b></p>
<p>Chapters: <b id="chap">{state['chapter']}</b></p>
<p>Action: <span id="act">{state['last_action']}</span></p>
</div>

<div class="card">
<h3>Import</h3>
<form method="post" action="/start"
onsubmit="return confirm('Start or resume importing?')">
<input name="url" placeholder="Chapter 1 URL">
<button>▶ Start / Resume</button>
</form>

<form method="post" action="/stop"
onsubmit="return confirm('Pause importing?')">
<button>⏸ Pause</button>
</form>

<form method="post" action="/reset"
onsubmit="return confirm('This will DELETE all chapters. Continue?')">
<button style="background:#7c2d12;color:white;">🗑 Reset & Start Over</button>
</form>
</div>

<div class="bottom">
<a href="/">Home</a>
<a href="/book">Book</a>
<a href="/settings">Settings</a>
</div>

</body>
</html>
"""

@app.get("/book", response_class=HTMLResponse)
def book():
    items = list_chapters()
    links = "".join(
        f'<li><a href="/read/{c}">Chapter {int(c[:3])}</a></li>' for c in items
    )
    return f"""
<body style="background:#020617;color:#e5e7eb;padding:20px;">
<h2>📖 Chapters</h2>
<ul>{links}</ul>
<a href="/">← Back</a>
</body>
"""

@app.get("/settings", response_class=HTMLResponse)
def settings():
    rows = "".join(
        f"<li>Chapter {e['chapter']}: {e['error']}</li>" for e in errors
    )
    return f"""
<body style="background:#020617;color:#e5e7eb;padding:20px;">
<h2>⚙️ Errors</h2>
<ul>{rows or '<li>No errors</li>'}</ul>
<a href="/">← Back</a>
</body>
"""

@app.get("/read/{chapter}", response_class=HTMLResponse)
def read(chapter: str):
    obj = s3.get_object(Bucket=R2_BUCKET, Key=f"{CHAPTER_PREFIX}{chapter}")
    content = obj["Body"].read().decode()
    num = int(chapter[:3])

    return f"""
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body {{
  background:#020617;
  color:#e5e7eb;
  font-family:Georgia,serif;
}}
.reader {{
  padding:20px;
  max-width:700px;
  margin:auto;
}}
p {{ font-size:18px; line-height:1.8; }}
.nav {{
  position:fixed;
  bottom:0;
  width:100%;
  background:#020617;
  display:flex;
  gap:10px;
  padding:10px;
}}
.nav a,select {{
  flex:1;
  padding:12px;
}}
</style>
</head>
<body>

<div class="reader">{content}</div>

<div class="nav">
<a href="/read/{num-1:03d}.html">⬅ Prev</a>
<select onchange="location=this.value">
<option>Chapter {num}</option>
</select>
<a href="/read/{num+1:03d}.html">Next ➡</a>
</div>

</body>
</html>
"""

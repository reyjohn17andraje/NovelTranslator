import os, json, threading, time, requests
import boto3
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from openai import OpenAI

# ================= ENV =================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "").strip()
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()

if not all([OPENAI_API_KEY, R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT, R2_BUCKET]):
    raise RuntimeError("Missing env vars")

client = OpenAI(api_key=OPENAI_API_KEY)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

app = FastAPI()

BOOK = "book1"
BASE = f"books/{BOOK}"
STATE_KEY = f"{BASE}/state.json"
VISITED_KEY = f"{BASE}/visited.json"
CHAPTERS_PREFIX = f"{BASE}/chapters/"

state = {
    "running": False,
    "current_url": None,
    "chapter": 0
}
visited = []

# ================= STORAGE =================

def load_json(key, default):
    try:
        return json.loads(s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read())
    except:
        return default

def save_json(key, data):
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=json.dumps(data))

state.update(load_json(STATE_KEY, state))
visited[:] = load_json(VISITED_KEY, [])

def list_chapters():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=CHAPTERS_PREFIX)
    if "Contents" not in res:
        return []
    return sorted(obj["Key"].split("/")[-1] for obj in res["Contents"])

def delete_all_chapters():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=CHAPTERS_PREFIX)
    if "Contents" in res:
        for obj in res["Contents"]:
            s3.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])

# ================= TRANSLATION =================

def translate(text):
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Translate Chinese novel text into natural English. Preserve paragraphs."},
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content

def scrape(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gb2312"
    soup = BeautifulSoup(r.text, "html.parser")

    content = soup.find("div", class_="content")
    if not content:
        raise RuntimeError("No content")

    text = "\n\n".join(p.get_text(strip=True) for p in content.find_all("p") if p.get_text(strip=True))

    next_url = None
    nav = soup.find("div", class_="artic_pages")
    if nav:
        links = nav.find_all("a")
        if len(links) >= 2:
            next_url = requests.compat.urljoin(url, links[1]["href"])

    return text, next_url

# ================= WORKER =================

def worker():
    while state["running"] and state["current_url"]:
        if state["current_url"] in visited:
            state["running"] = False
            save_json(STATE_KEY, state)
            break

        visited.append(state["current_url"])
        save_json(VISITED_KEY, visited)

        raw, next_url = scrape(state["current_url"])
        translated = translate(raw)

        state["chapter"] += 1
        num = state["chapter"]

        html = "".join(f"<p>{p}</p>" for p in translated.split("\n\n"))
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=f"{CHAPTERS_PREFIX}{num:03d}.html",
            Body=html,
            ContentType="text/html"
        )

        state["current_url"] = next_url
        save_json(STATE_KEY, state)
        time.sleep(2)

# ================= UI LAYOUT =================

def shell(content, active="home"):
    return f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Novel Reader</title>
<style>
body {{
    margin:0;
    background:#0b1220;
    color:#e5e7eb;
    font-family:-apple-system,BlinkMacSystemFont,sans-serif;
}}
.header {{
    padding:18px;
    font-size:22px;
    font-weight:700;
}}
.container {{
    padding:16px;
    padding-bottom:90px;
}}
.card {{
    background:#111827;
    border-radius:16px;
    padding:16px;
    margin-bottom:16px;
}}
.cover {{
    height:180px;
    border-radius:12px;
    background:linear-gradient(135deg,#7c3aed,#06b6d4);
}}
a {{ color:#60a5fa; text-decoration:none; }}
.bottom {{
    position:fixed;
    bottom:0;
    left:0;
    right:0;
    background:#020617;
    display:flex;
    justify-content:space-around;
    padding:14px 0;
}}
.bottom a {{
    color:{'#60a5fa' if active=='home' else '#9ca3af'};
}}
button,select,input {{
    width:100%;
    padding:14px;
    border-radius:12px;
    border:none;
    margin-top:10px;
}}
</style>
</head>
<body>
<div class="header">📖 FictionMe</div>
<div class="container">{content}</div>
<div class="bottom">
<a href="/">Home</a>
<a href="/book">Book</a>
<a href="/settings">Settings</a>
</div>
</body>
</html>
"""

# ================= ROUTES =================

@app.get("/", response_class=HTMLResponse)
def home():
    chapters = list_chapters()
    last = chapters[-1] if chapters else None
    content = f"""
<div class="card">
<div class="cover"></div>
<h2>Current Novel</h2>
<p>{len(chapters)} chapters</p>
<a href="/read/{last}">▶ Continue Reading</a>
</div>
"""
    return shell(content, "home")

@app.get("/book", response_class=HTMLResponse)
def book():
    chapters = list_chapters()
    items = "".join(f'<li><a href="/read/{c}">{c}</a></li>' for c in chapters)
    return shell(f"<div class='card'><h2>Chapters</h2><ul>{items}</ul></div>", "book")

@app.get("/read/{chapter}", response_class=HTMLResponse)
def read(chapter: str):
    chapters = list_chapters()
    idx = chapters.index(chapter)
    prev = chapters[idx-1] if idx > 0 else None
    nxt = chapters[idx+1] if idx < len(chapters)-1 else None

    html = s3.get_object(Bucket=R2_BUCKET, Key=f"{CHAPTERS_PREFIX}{chapter}")["Body"].read().decode()

    dropdown = "".join(f"<option value='{c}' {'selected' if c==chapter else ''}>{c}</option>" for c in chapters)

    return shell(f"""
<div class="card">{html}</div>
<div style="display:flex;gap:10px;">
{f'<a href="/read/{prev}">◀ Prev</a>' if prev else ''}
<select onchange="location=this.value ? '/read/'+this.value : '#'">{dropdown}</select>
{f'<a href="/read/{nxt}">Next ▶</a>' if nxt else ''}
</div>
""", "book")

@app.get("/settings", response_class=HTMLResponse)
def settings():
    return shell(f"""
<div class="card">
<h2>Import</h2>
<form method="post" action="/start">
<input name="url" placeholder="Chapter 1 URL">
<button>Start / Resume</button>
</form>
<form method="post" action="/stop"><button>Stop</button></form>
<form method="post" action="/reset"><button style="background:#7c2d12;color:white">Reset & Start Over</button></form>
</div>
""", "settings")

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["current_url"] = url
    state["running"] = True
    save_json(STATE_KEY, state)
    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/settings", 303)

@app.post("/stop")
def stop():
    state["running"] = False
    save_json(STATE_KEY, state)
    return RedirectResponse("/settings", 303)

@app.post("/reset")
def reset():
    state.update({"running": False, "current_url": None, "chapter": 0})
    visited.clear()
    delete_all_chapters()
    save_json(STATE_KEY, state)
    save_json(VISITED_KEY, visited)
    return RedirectResponse("/settings", 303)

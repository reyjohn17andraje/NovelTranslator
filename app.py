import os
import json
import threading
import time
import requests
import boto3
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from openai import OpenAI

# ======================================================
# ENV
# ======================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "").strip()
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()

if not all([OPENAI_API_KEY, R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT, R2_BUCKET]):
    raise RuntimeError("Missing required environment variables")

client = OpenAI(api_key=OPENAI_API_KEY)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

app = FastAPI()

# ======================================================
# STORAGE KEYS
# ======================================================

STATE_KEY = "state.json"
META_KEY = "chapters/meta.json"

# ======================================================
# STATE
# ======================================================

state = {
    "running": False,
    "current_url": None,
    "chapter": 0,
    "last_read": None
}

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
        Body=json.dumps(data, ensure_ascii=False),
        ContentType="application/json"
    )

state.update(load_json(STATE_KEY, {}))
meta = load_json(META_KEY, [])

# ======================================================
# HELPERS
# ======================================================

def clean_text(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n\n".join(lines)

def translate(text):
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Translate Chinese web novel text into fluent English. "
                    "Preserve paragraphs, tone, and storytelling. "
                    "Do not summarize or add content."
                )
            },
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content.strip()

def scrape_chapter(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gb2312"
    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.find("h1").get_text(strip=True)

    content_div = soup.find("div", class_="content")
    paragraphs = [
        p.get_text(strip=True)
        for p in content_div.find_all("p")
        if p.get_text(strip=True)
    ]
    content = clean_text("\n\n".join(paragraphs))

    next_url = None
    pages = soup.find("div", class_="artic_pages")
    if pages:
        links = pages.find_all("a")
        if len(links) >= 2:
            next_url = requests.compat.urljoin(url, links[1].get("href"))

    return title, content, next_url

def save_chapter(num, title, body):
    html = f"<h1>{title}</h1>"
    for p in body.split("\n\n"):
        html += f"<p>{p}</p>"

    key = f"chapters/ch{num}.html"
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=html,
        ContentType="text/html"
    )

    meta.append({"num": num, "title": title, "key": key})
    save_json(META_KEY, meta)

# ======================================================
# WORKER
# ======================================================

def worker():
    while state["running"] and state["current_url"]:
        state["chapter"] += 1

        title_cn, raw, next_url = scrape_chapter(state["current_url"])
        title_en = translate(title_cn)
        body_en = translate(raw)

        save_chapter(state["chapter"], title_en, body_en)

        state["current_url"] = next_url
        save_json(STATE_KEY, state)

        if not next_url:
            state["running"] = False
            save_json(STATE_KEY, state)
            break

        time.sleep(2)

# ======================================================
# ROUTES
# ======================================================

BASE_META = """
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#020617">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
"""

BASE_CSS = """
html,body{
margin:0;padding:0;height:100%;
background:radial-gradient(1200px circle at top,#0f172a 0%,#020617 60%);
color:#e5e7eb;
font-family:-apple-system,BlinkMacSystemFont,sans-serif;
}
.card{
max-width:420px;
margin:40px auto;
padding:24px;
border-radius:20px;
background:rgba(15,23,42,.85);
backdrop-filter:blur(14px);
box-shadow:0 20px 40px rgba(0,0,0,.4);
}
button,input{
width:100%;
padding:14px;
border-radius:14px;
border:none;
font-size:16px;
margin-top:10px;
}
.btn-primary{background:linear-gradient(135deg,#22c55e,#4ade80);color:#020617;}
.btn-danger{background:linear-gradient(135deg,#ef4444,#f87171);}
.btn-reset{background:linear-gradient(135deg,#7c2d12,#ea580c);}
a{color:#60a5fa;text-decoration:none;}
"""

@app.get("/", response_class=HTMLResponse)
def home():
    continue_btn = ""
    if state.get("last_read"):
        continue_btn = f'<a href="/chapter/{state["last_read"]}" class="btn-primary" style="display:block;text-align:center;padding:14px;border-radius:14px;margin-top:12px;">📖 Continue Reading</a>'

    return f"""
<!DOCTYPE html>
<html>
<head>{BASE_META}<style>{BASE_CSS}</style></head>
<body>
<div class="card">
<h2>📘 Novel Translator</h2>
<p>Status: <b>{"Running" if state["running"] else "Stopped"}</b></p>
<p>Chapters translated: {len(meta)}</p>

<form action="/start" method="post">
<input name="url" placeholder="Paste Chapter 1 URL (first time only)">
<button class="btn-primary">▶ Start / Resume</button>
</form>

<form action="/stop" method="post">
<button class="btn-danger">⏸ Stop</button>
</form>

<form action="/reset" method="post">
<button class="btn-reset">🗑 Reset & Start Over</button>
</form>

{continue_btn}

<br>
<a href="/read">📚 Read Chapters</a>
</div>
</body>
</html>
"""

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["current_url"] = url
        state["chapter"] = len(meta)

    if not state["running"]:
        state["running"] = True
        save_json(STATE_KEY, state)
        threading.Thread(target=worker, daemon=True).start()

    return RedirectResponse("/", 303)

@app.post("/stop")
def stop():
    state["running"] = False
    save_json(STATE_KEY, state)
    return RedirectResponse("/", 303)

@app.post("/reset")
def reset():
    state.update({"running": False, "current_url": None, "chapter": 0, "last_read": None})
    save_json(STATE_KEY, state)
    save_json(META_KEY, [])
    return RedirectResponse("/", 303)

@app.get("/read", response_class=HTMLResponse)
def read():
    items = "".join(
        f'<li><a href="/chapter/{m["num"]}">Chapter {m["num"]} – {m["title"]}</a></li>'
        for m in meta
    )
    return f"""
<html><head>{BASE_META}<style>{BASE_CSS}</style></head>
<body>
<div class="card">
<h2>📚 Chapters</h2>
<ul>{items}</ul>
<a href="/">← Back</a>
</div>
</body>
</html>
"""

@app.get("/chapter/{num}", response_class=HTMLResponse)
def chapter(num: int):
    idx = num - 1
    ch = meta[idx]
    state["last_read"] = num
    save_json(STATE_KEY, state)

    html = s3.get_object(Bucket=R2_BUCKET, Key=ch["key"])["Body"].read().decode()

    prev_btn = f'<a href="/chapter/{num-1}">← Prev</a>' if num > 1 else ""
    next_btn = f'<a href="/chapter/{num+1}">Next →</a>' if num < len(meta) else ""

    options = "".join(
        f'<option value="/chapter/{m["num"]}" {"selected" if m["num"]==num else ""}>Chapter {m["num"]}</option>'
        for m in meta
    )

    return f"""
<html>
<head>{BASE_META}
<style>
{BASE_CSS}
.reader{{max-width:720px;margin:auto;padding:24px;font-family:Georgia,serif;}}
p{{font-size:18px;line-height:1.85}}
.nav{{position:fixed;bottom:0;left:0;right:0;
background:rgba(2,6,23,.95);display:flex;
justify-content:space-between;align-items:center;
padding:12px 16px}}
select{{background:#020617;color:#e5e7eb;border-radius:12px;padding:8px}}
</style>
</head>
<body>
<div class="reader">{html}</div>

<div class="nav">
{prev_btn}
<select onchange="location=this.value">{options}</select>
{next_btn}
</div>
</body>
</html>
"""

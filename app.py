import os
import json
import threading
import time
import requests
import boto3
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form
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
    raise RuntimeError("Missing environment variables")

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
VISITED_KEY = "visited.json"

# ======================================================
# STATE
# ======================================================

state = {
    "running": False,
    "current_url": None,
    "chapter": 0
}

visited = []

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

state.update(load_json(STATE_KEY, state))
visited = load_json(VISITED_KEY, [])

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
                    "Preserve paragraphs and storytelling. "
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

    return content, next_url

def save_chapter(num, body):
    html = ""
    for p in body.split("\n\n"):
        html += f"<p>{p}</p>"

    s3.put_object(
        Bucket=R2_BUCKET,
        Key=f"chapters/{num:03d}.html",
        Body=html,
        ContentType="text/html"
    )

def list_chapters():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="chapters/")
    if "Contents" not in res:
        return []
    return sorted(obj["Key"].split("/")[-1] for obj in res["Contents"])

# ======================================================
# WORKER (FIXED)
# ======================================================

def worker():
    while state["running"] and state["current_url"]:
        if state["current_url"] in visited:
            print("⏭ Skipping already-visited URL")
            break

        visited.append(state["current_url"])
        save_json(VISITED_KEY, visited)

        state["chapter"] += 1

        raw, next_url = scrape_chapter(state["current_url"])
        translated = translate(raw)
        save_chapter(state["chapter"], translated)

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

@app.get("/", response_class=HTMLResponse)
def home():
    return f"""
<html>
<body style="background:#020617;color:#e5e7eb;padding:24px;font-family:sans-serif;">
<h2>📘 Novel Translator</h2>
<p>Status: {"Running" if state["running"] else "Stopped"}</p>
<p>Chapters translated: {len(list_chapters())}</p>

<form action="/start" method="post">
<input name="url" placeholder="Paste Chapter 1 URL" style="width:100%;padding:12px;">
<button style="width:100%;margin-top:8px;">▶ Start / Resume</button>
</form>

<form action="/stop" method="post">
<button style="width:100%;margin-top:8px;">⏸ Stop</button>
</form>

<form action="/reset" method="post">
<button style="width:100%;margin-top:8px;background:#7c2d12;color:white;">🗑 Reset & Start Over</button>
</form>

<a href="/read">📚 Read Chapters</a>
</body>
</html>
"""

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["current_url"] = url

    if not state["running"] and state["current_url"]:
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
    # stop worker
    state.update({"running": False, "current_url": None, "chapter": 0})
    save_json(STATE_KEY, state)

    # delete chapters
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="chapters/")
    for obj in res.get("Contents", []):
        s3.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])

    # clear visited
    save_json(VISITED_KEY, [])

    return RedirectResponse("/", 303)

@app.get("/read", response_class=HTMLResponse)
def read():
    chapters = list_chapters()
    links = "".join(f'<li><a href="/chapter/{c}">{c}</a></li>' for c in chapters)
    return f"""
<html>
<body style="background:#020617;color:#e5e7eb;padding:24px;">
<h2>📚 Chapters</h2>
<ul>{links}</ul>
<a href="/">← Back</a>
</body>
</html>
"""

@app.get("/chapter/{chapter}", response_class=HTMLResponse)
def chapter(chapter: str):
    obj = s3.get_object(Bucket=R2_BUCKET, Key=f"chapters/{chapter}")
    content = obj["Body"].read().decode()
    return f"""
<html>
<body style="background:#020617;color:#e5e7eb;padding:24px;line-height:1.8;">
{content}
<a href="/read">← Back</a>
</body>
</html>
"""

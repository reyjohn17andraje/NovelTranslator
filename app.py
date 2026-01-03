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

# -------------------- ENV VALIDATION --------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "").strip()
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()

print("DEBUG ENV:")
print("OPENAI_API_KEY:", bool(OPENAI_API_KEY))
print("R2_ENDPOINT:", repr(R2_ENDPOINT))
print("R2_BUCKET:", repr(R2_BUCKET))

if not all([OPENAI_API_KEY, R2_ACCESS_KEY, R2_SECRET_KEY, R2_ENDPOINT, R2_BUCKET]):
    raise RuntimeError("One or more required environment variables are missing")

# -------------------- CLIENTS --------------------

client = OpenAI(api_key=OPENAI_API_KEY)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

# -------------------- APP --------------------

app = FastAPI()

from fastapi import Request

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("🔥 UNHANDLED EXCEPTION:", repr(exc))
    raise exc

STATE_KEY = "state.json"

state = {
    "running": False,
    "current_url": None,
    "last_url": None,
    "chapter": 0
}

# -------------------- STATE --------------------

def load_state():
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=STATE_KEY)
        state.update(json.loads(obj["Body"].read().decode()))
    except:
        pass

def save_state():
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state),
        ContentType="application/json"
    )

load_state()

# -------------------- HELPERS --------------------

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
                    "Preserve paragraph structure, tone, and storytelling. "
                    "Do not summarize or add content."
                )
            },
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content

def scrape_chapter(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gb2312"  # IMPORTANT
    soup = BeautifulSoup(r.text, "html.parser")

    # -------- Title --------
    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled Chapter"

    # -------- Content --------
    content_div = soup.find("div", class_="content")
    if not content_div:
        raise RuntimeError("Content div not found")

    paragraphs = []
    for p in content_div.find_all("p"):
        text = p.get_text(strip=True)
        if text:
            paragraphs.append(text)

    content = "\n\n".join(paragraphs)

    # -------- Next chapter --------
    next_url = None
    pages = soup.find("div", class_="artic_pages")
    if pages:
        links = pages.find_all("a")
        if len(links) >= 2:
            # second <a> is always "next chapter"
            next_url = requests.compat.urljoin(url, links[1].get("href"))

    return title, clean_text(content), next_url

def save_chapter(num, title, body):
    html = f"<h1>{title}</h1>"
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

def chapter_count():
    return len(list_chapters())

# -------------------- WORKER --------------------

def worker():
    while state["running"] and state["current_url"]:
        state["chapter"] += 1

        title, raw, next_url = scrape_chapter(state["current_url"])
        translated = translate(raw)
        save_chapter(state["chapter"], title, translated)

        state["current_url"] = next_url
        state["last_url"] = next_url or state["last_url"]
        save_state()

        if not next_url:
            state["running"] = False
            save_state()
            break

        time.sleep(2)

# -------------------- ROUTES --------------------

@app.get("/", response_class=HTMLResponse)
def home():
    status = "Running" if state["running"] else "Stopped"
    color = "#22c55e" if state["running"] else "#ef4444"
    progress = min(chapter_count() * 10, 100)

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Novel Translator</title>
<style>
body {{
    background:#0f172a;
    color:#e5e7eb;
    font-family:-apple-system,BlinkMacSystemFont,sans-serif;
}}
.container {{
    max-width:420px;
    margin:60px auto;
    background:#020617;
    padding:24px;
    border-radius:16px;
}}
input,button {{
    width:100%;
    padding:12px;
    border-radius:10px;
    border:none;
    margin-bottom:10px;
}}
.start {{ background:#22c55e; }}
.stop {{ background:#ef4444; }}
.progress {{
    background:#020617;
    border-radius:8px;
    overflow:hidden;
}}
.bar {{
    height:8px;
    width:{progress}%;
    background:#38bdf8;
}}
a {{ color:#60a5fa; text-decoration:none; display:block; text-align:center; }}
</style>
</head>
<body>
<div class="container">
<h2>📖 Novel Translator</h2>
<p>Status: <b style="color:{color}">{status}</b></p>

<div>Chapters translated: {chapter_count()}</div>
<div class="progress"><div class="bar"></div></div><br>

<form action="/start" method="post">
<input name="url" placeholder="Paste chapter URL (only first time)">
<button class="start">▶ Start / Resume</button>
</form>

<form action="/stop" method="post">
<button class="stop">⏸ Stop</button>
</form>

<a href="/read">📚 Read Chapters</a>
</div>
</body>
</html>
"""

@app.post("/start")
def start(url: str = Form(None)):
    if not state["running"]:
        if url:
            state["current_url"] = url
            state["last_url"] = url
        elif state["last_url"]:
            state["current_url"] = state["last_url"]
        else:
            return RedirectResponse("/", status_code=303)

        state["running"] = True
        save_state()
        threading.Thread(target=worker, daemon=True).start()

    return RedirectResponse("/", status_code=303)

@app.post("/stop")
def stop():
    state["running"] = False
    save_state()
    return RedirectResponse("/", status_code=303)

@app.get("/read", response_class=HTMLResponse)
def read():
    chapters = list_chapters()
    links = "".join(f'<li><a href="/chapter/{c}">{c}</a></li>' for c in chapters)

    return f"""
<html>
<body style="background:#0f172a;color:#e5e7eb;padding:20px;">
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
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{
    background:#020617;
    color:#e5e7eb;
    font-family:Georgia,serif;
    line-height:1.8;
}}
.reader {{
    max-width:720px;
    margin:auto;
    padding:24px;
}}
p {{ font-size:18px; margin-bottom:18px; }}
a {{ color:#60a5fa; }}
</style>
</head>
<body>
<div class="reader">
{content}
<a href="/read">← Back</a>
</div>
</body>
</html>
"""

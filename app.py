import os, json, threading, time, re, requests, boto3
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
    raise RuntimeError("Missing environment variables")

# ================= CLIENTS =================

client = OpenAI(api_key=OPENAI_API_KEY)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

app = FastAPI()

STATE_KEY = "state.json"

DEFAULT_STATE = {
    "running": False,
    "current_url": None,
    "base_url": None,
}

state = DEFAULT_STATE.copy()

# ================= STATE =================

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
state["running"] = False
save_state()

# ================= R2 HELPERS =================

def list_chapters():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="chapters/")
    if "Contents" not in res:
        return []
    return sorted([c["Key"].split("/")[-1] for c in res["Contents"]])

def chapter_count():
    return len(list_chapters())

def delete_all_chapters():
    chapters = list_chapters()
    for c in chapters:
        s3.delete_object(Bucket=R2_BUCKET, Key=f"chapters/{c}")

# ================= TRANSLATION =================

def translate(text):
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Translate Chinese web novel text into fluent English. "
                    "Preserve tone, paragraph structure, and storytelling. "
                    "Do not summarize or add content."
                )
            },
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content

# ================= SCRAPER =================

def scrape_chapter(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gb2312"
    soup = BeautifulSoup(r.text, "html.parser")

    raw_title = soup.find("h1").get_text(strip=True)
    title = translate(raw_title)

    content_div = soup.find("div", class_="content")
    if not content_div:
        raise RuntimeError("Content not found")

    paragraphs = []
    for p in content_div.find_all("p"):
        text = p.get_text(strip=True)
        if not text:
            continue
        if "作者" in text or "书名" in text:
            continue
        paragraphs.append(text)

    body = translate("\n\n".join(paragraphs))
    return title, body

def next_url(base_url, chapter_num):
    return base_url.replace(f"/{chapter_num}.html", f"/{chapter_num+1}.html")

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

# ================= WORKER =================

def worker():
    print("🚀 WORKER STARTED")

    while state["running"] and state["base_url"]:
        existing = chapter_count()
        chapter_num = existing + 1

        url = next_url(state["base_url"], chapter_num)
        print("📄 FETCHING", url)

        try:
            title, body = scrape_chapter(url)
            save_chapter(chapter_num, title, body)
            print("✅ SAVED CHAPTER", chapter_num)
        except Exception as e:
            print("❌ ERROR:", e)
            state["running"] = False
            save_state()
            break

        time.sleep(2)

# ================= ROUTES =================

@app.get("/", response_class=HTMLResponse)
def home():
    running = state["running"]
    progress = min(chapter_count() * 5, 100)

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Novel Translator</title>
<style>
body {{
  background:linear-gradient(180deg,#020617,#020617);
  color:#e5e7eb;
  font-family:-apple-system,BlinkMacSystemFont,sans-serif;
}}
.card {{
  max-width:420px;
  margin:40px auto;
  background:#020617;
  padding:24px;
  border-radius:18px;
}}
button,input {{
  width:100%;
  padding:14px;
  border-radius:14px;
  border:none;
  margin:10px 0;
  font-size:16px;
}}
.start {{ background:#22c55e; color:black; }}
.stop {{ background:#ef4444; color:white; }}
.reset {{ background:#7c2d12; color:white; }}
.bar {{
  height:8px;
  background:#38bdf8;
  width:{progress}%;
  border-radius:8px;
}}
.progress {{ background:#020617; border-radius:8px; }}
a {{ color:#60a5fa; text-decoration:none; display:block; text-align:center; }}
</style>
</head>
<body>
<div class="card">
<h2>📖 Novel Translator</h2>
<p>Status: <b>{'Running' if running else 'Stopped'}</b></p>
<p>Chapters translated: {chapter_count()}</p>
<div class="progress"><div class="bar"></div></div>

<form action="/start" method="post">
<input name="url" placeholder="Paste chapter 1 URL (first time only)">
<button class="start">▶ Start / Resume</button>
</form>

<form action="/stop" method="post"><button class="stop">⏸ Stop</button></form>
<form action="/reset" method="post"><button class="reset">🗑 Reset & Start Over</button></form>

<a href="/read">📚 Read Chapters</a>
</div>
</body>
</html>
"""

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["base_url"] = url
    if not state["base_url"]:
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

@app.post("/reset")
def reset():
    delete_all_chapters()
    state.clear()
    state.update(DEFAULT_STATE)
    save_state()
    return RedirectResponse("/", status_code=303)

@app.get("/read", response_class=HTMLResponse)
def read():
    items = ""
    for c in list_chapters():
        items += f'<a class="item" href="/chapter/{c}">📖 {c}</a>'

    return f"""
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ background:#020617;color:#e5e7eb;font-family:sans-serif; }}
.item {{
  display:block;
  background:#020617;
  padding:14px;
  border-radius:12px;
  margin:10px;
  text-decoration:none;
  color:#e5e7eb;
}}
</style>
</head>
<body>
<h2 style="padding:16px">📚 Chapters</h2>
{items}
<a href="/" style="display:block;text-align:center">← Back</a>
</body>
</html>
"""

@app.get("/chapter/{chapter}", response_class=HTMLResponse)
def chapter(chapter: str):
    chapters = list_chapters()
    idx = chapters.index(chapter)
    next_link = f'<a href="/chapter/{chapters[idx+1]}">Next →</a>' if idx + 1 < len(chapters) else ""

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
p {{ font-size:18px; }}
.nav {{ display:flex; justify-content:space-between; margin-top:40px; }}
a {{ color:#60a5fa; text-decoration:none; }}
</style>
</head>
<body>
<div class="reader">
{content}
<div class="nav">
<a href="/read">← Chapters</a>
{next_link}
</div>
</div>
</body>
</html>
"""

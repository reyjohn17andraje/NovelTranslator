import os, json, threading, time, requests, boto3
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
# STORAGE
# ======================================================

STATE_KEY = "state.json"
META_KEY = "chapters/meta.json"
ERROR_KEY = "errors.json"

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

# ======================================================
# STATE
# ======================================================

state = {
    "running": False,
    "current_url": None,
    "chapter": 0,
    "seen_urls": [],
    "action": "Idle"
}

state.update(load_json(STATE_KEY, {}))
meta = load_json(META_KEY, [])
errors = load_json(ERROR_KEY, [])

# ======================================================
# CORE
# ======================================================

def log_error(msg):
    errors.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": msg})
    save_json(ERROR_KEY, errors)

def clean_text(text):
    return "\n\n".join(l.strip() for l in text.splitlines() if l.strip())

def translate(text):
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Translate Chinese web novel text into fluent English. Preserve paragraphs."},
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content.strip()

def scrape_chapter(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gb2312"
    soup = BeautifulSoup(r.text, "html.parser")

    content_div = soup.find("div", class_="content")
    if not content_div:
        raise RuntimeError("Content not found")

    paragraphs = [p.get_text(strip=True) for p in content_div.find_all("p") if p.get_text(strip=True)]
    content = clean_text("\n\n".join(paragraphs))

    next_url = None
    nav = soup.find("div", class_="artic_pages")
    if nav:
        links = nav.find_all("a")
        if len(links) >= 2:
            next_url = requests.compat.urljoin(url, links[1].get("href"))

    return content, next_url

def save_chapter(num, body):
    html = "".join(f"<p>{p}</p>" for p in body.split("\n\n"))
    key = f"chapters/ch{num}.html"
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=html, ContentType="text/html")
    meta.append({"num": num, "key": key})
    save_json(META_KEY, meta)

def delete_all():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="chapters/")
    for obj in res.get("Contents", []):
        s3.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])

# ======================================================
# WORKER
# ======================================================

def worker():
    while state["running"] and state["current_url"]:
        if state["current_url"] in state["seen_urls"]:
            break

        state["seen_urls"].append(state["current_url"])
        state["chapter"] += 1
        state["action"] = "Fetching"

        try:
            content, next_url = scrape_chapter(state["current_url"])
            state["action"] = "Translating"
            body = translate(content)
            state["action"] = "Saving"
            save_chapter(state["chapter"], body)
            state["current_url"] = next_url
        except Exception as e:
            log_error(str(e))
            state["action"] = "Error"
            break

        save_json(STATE_KEY, state)
        if not next_url:
            break
        time.sleep(2)

    state["running"] = False
    state["action"] = "Idle"
    save_json(STATE_KEY, state)

# ======================================================
# LAYOUT
# ======================================================

BASE_STYLE = """
:root {{
 --bg:#020617;
 --panel:#0f172a;
 --soft:#1e293b;
 --text:#e5e7eb;
 --muted:#94a3b8;
 --accent:#38bdf8;
}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system;background:var(--bg);color:var(--text)}}
a{{color:var(--accent);text-decoration:none}}
.header{{padding:14px 20px;background:var(--panel);display:flex;justify-content:space-between}}
.container{{max-width:1100px;margin:auto;padding:20px}}
.tabs{{display:flex;gap:20px;border-bottom:1px solid var(--soft)}}
.tab{{padding:10px 0;color:var(--muted)}}
.tab.active{{color:var(--accent);border-bottom:2px solid var(--accent)}}
.card{{background:var(--panel);border-radius:16px;padding:20px;margin-top:20px}}
button,input{{padding:12px;border-radius:12px;border:none;width:100%;margin-top:10px}}
.reader{{max-width:720px;margin:auto;padding:20px;line-height:1.75;font-family:Georgia}}
.nav{{position:fixed;bottom:0;left:0;right:0;background:var(--panel);display:flex;gap:10px;padding:12px}}
.nav a{{flex:1;text-align:center}}
@media(max-width:640px){{
 .container{{padding:12px}}
 .tabs{{overflow-x:auto}}
}}
"""

def layout(title, tab, content):
    def t(name): return "tab active" if tab == name else "tab"
    return f"""
<html>
<head><style>{BASE_STYLE}</style></head>
<body>
<div class="header">
<b>{title}</b>
<a href="/settings">⚙</a>
</div>
<div class="container">
<div class="tabs">
<a class="{t('chapters')}" href="/">Chapters</a>
<a class="{t('about')}" href="/about">About</a>
<a class="{t('settings')}" href="/settings">Settings</a>
</div>
{content}
</div>
</body>
</html>
"""

# ======================================================
# ROUTES
# ======================================================

@app.get("/", response_class=HTMLResponse)
def chapters():
    items = "".join(
        f'<li><a href="/read/{m["num"]}">Chapter {m["num"]}</a></li>' for m in meta
    ) or "<p>No chapters yet.</p>"
    return layout("Web Novel", "chapters", f"<div class='card'><ul>{items}</ul></div>")

@app.get("/read/{num}", response_class=HTMLResponse)
def read(num: int):
    if num < 1 or num > len(meta):
        return RedirectResponse("/")
    ch = meta[num - 1]
    html = s3.get_object(Bucket=R2_BUCKET, Key=ch["key"])["Body"].read().decode()
    prev = f'<a href="/read/{num-1}">← Prev</a>' if num > 1 else ""
    next = f'<a href="/read/{num+1}">Next →</a>' if num < len(meta) else ""
    return f"""
<html>
<head><style>{BASE_STYLE}</style></head>
<body>
<div class="reader">{html}</div>
<div class="nav">{prev}{next}</div>
</body>
</html>
"""

@app.get("/settings", response_class=HTMLResponse)
def settings():
    status = "Running" if state["running"] else "Idle"
    return layout("Settings", "settings", f"""
<div class="card">
<b>Status:</b> {status}<br>
<b>Action:</b> {state["action"]}
<form method="post" action="/start">
<input name="url" placeholder="First chapter URL">
<button>Start / Resume</button>
</form>
<form method="post" action="/stop"><button>Pause</button></form>
<form method="post" action="/reset"><button>Reset Book</button></form>
<a href="/errors">View Errors</a>
</div>
""")

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["current_url"] = url
    if not state["running"]:
        state["running"] = True
        threading.Thread(target=worker, daemon=True).start()
    save_json(STATE_KEY, state)
    return RedirectResponse("/settings", 303)

@app.post("/stop")
def stop():
    state["running"] = False
    save_json(STATE_KEY, state)
    return RedirectResponse("/settings", 303)

@app.post("/reset")
def reset():
    delete_all()
    meta.clear()
    state.update({"running": False, "current_url": None, "chapter": 0, "seen_urls": [], "action": "Idle"})
    save_json(STATE_KEY, state)
    save_json(META_KEY, meta)
    return RedirectResponse("/settings", 303)

@app.get("/errors", response_class=HTMLResponse)
def view_errors():
    items = "".join(f"<li>{e['time']} — {e['msg']}</li>" for e in errors) or "<li>No errors</li>"
    return layout("Errors", "settings", f"<div class='card'><ul>{items}</ul></div>")

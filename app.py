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
# STORAGE KEYS
# ======================================================

STATE_KEY = "state.json"
META_KEY = "chapters/meta.json"
ERROR_KEY = "errors.json"

# ======================================================
# HELPERS
# ======================================================

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
    "last_read": None,
    "seen_urls": [],
    "action": "Idle"
}

state.update(load_json(STATE_KEY, {}))
meta = load_json(META_KEY, [])
errors = load_json(ERROR_KEY, [])

# ======================================================
# CORE LOGIC
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
        raise RuntimeError("Content div not found")

    paragraphs = [p.get_text(strip=True) for p in content_div.find_all("p") if p.get_text(strip=True)]
    content = clean_text("\n\n".join(paragraphs))

    next_url = None
    pages = soup.find("div", class_="artic_pages")
    if pages:
        links = pages.find_all("a")
        if len(links) >= 2:
            next_url = requests.compat.urljoin(url, links[1].get("href"))

    return content, next_url

def save_chapter(num, body):
    html = "".join(f"<p>{p}</p>" for p in body.split("\n\n"))
    key = f"chapters/ch{num}.html"
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=html, ContentType="text/html")
    meta.append({"num": num, "key": key})
    save_json(META_KEY, meta)

def delete_all_chapters():
    res = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="chapters/")
    for obj in res.get("Contents", []):
        s3.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])

# ======================================================
# WORKER
# ======================================================

def worker():
    while state["running"] and state["current_url"]:
        if state["current_url"] in state["seen_urls"]:
            state["running"] = False
            break

        state["seen_urls"].append(state["current_url"])
        state["chapter"] += 1

        try:
            state["action"] = "Fetching"
            content, next_url = scrape_chapter(state["current_url"])

            state["action"] = "Translating"
            body_en = translate(content)

            state["action"] = "Saving"
            save_chapter(state["chapter"], body_en)

            state["current_url"] = next_url
        except Exception as e:
            log_error(str(e))
            state["running"] = False
            state["action"] = "Error"
            break

        save_json(STATE_KEY, state)

        if not next_url:
            state["running"] = False
            state["action"] = "Idle"
            break

        time.sleep(2)

    save_json(STATE_KEY, state)

# ======================================================
# ROUTES
# ======================================================

BASE_STYLE = """
body{margin:0;background:#020617;color:#e5e7eb;font-family:-apple-system}
.card{max-width:420px;margin:40px auto;padding:24px;border-radius:18px;
background:rgba(15,23,42,.9);box-shadow:0 20px 40px rgba(0,0,0,.4)}
button,input{width:100%;padding:14px;border-radius:14px;border:none;margin-top:10px}
a{color:#60a5fa;text-decoration:none}
"""

@app.get("/", response_class=HTMLResponse)
def home():
    book = state["current_url"].split("/")[2] if state["current_url"] else "No Book"
    return f"""
<html><head><style>{BASE_STYLE}</style>
<script>
setInterval(async()=>{
  const r=await fetch('/status');const d=await r.json();
  document.getElementById('st').innerText=d.running?'Running':'Stopped';
  document.getElementById('act').innerText=d.action;
  document.getElementById('cnt').innerText=d.count;
},2000)
</script>
</head>
<body>
<div class="card">
<h2>📘 {book}</h2>
<p>Status: <b id="st">{'Running' if state['running'] else 'Stopped'}</b></p>
<p>Action: <span id="act">{state['action']}</span></p>
<p>Chapters: <span id="cnt">{len(meta)}</span></p>

<form method="post" action="/start">
<input name="url" placeholder="Paste first chapter URL">
<button>▶ Start / Resume</button>
</form>

<form method="post" action="/stop"><button>⏸ Pause</button></form>
<form method="post" action="/reset" onsubmit="return confirm('Reset all chapters?')">
<button>🗑 Reset</button></form>

<a href="/read">📚 Read</a><br>
<a href="/errors">⚠ Errors</a>
</div>
</body></html>
"""

@app.get("/status")
def status():
    return {"running": state["running"], "action": state["action"], "count": len(meta)}

@app.post("/start")
def start(url: str = Form(None)):
    if url:
        state["current_url"] = url
    if not state["running"]:
        state["running"] = True
        state["action"] = "Starting"
        threading.Thread(target=worker, daemon=True).start()
    save_json(STATE_KEY, state)
    return RedirectResponse("/", 303)

@app.post("/stop")
def stop():
    state["running"] = False
    state["action"] = "Paused"
    save_json(STATE_KEY, state)
    return RedirectResponse("/", 303)

@app.post("/reset")
def reset():
    delete_all_chapters()
    meta.clear()
    state.update({"running": False, "current_url": None, "chapter": 0,
                  "last_read": None, "seen_urls": [], "action": "Idle"})
    save_json(STATE_KEY, state)
    save_json(META_KEY, meta)
    return RedirectResponse("/", 303)

@app.get("/read", response_class=HTMLResponse)
def read():
    items="".join(f'<li><a href="/chapter/{m["num"]}">Chapter {m["num"]}</a></li>' for m in meta)
    return f"<html><body><div class='card'><h2>Chapters</h2><ul>{items}</ul><a href='/'>← Back</a></div></body></html>"

@app.get("/chapter/{num}", response_class=HTMLResponse)
def chapter(num: int):
    ch=meta[num-1]
    html=s3.get_object(Bucket=R2_BUCKET,Key=ch["key"])["Body"].read().decode()
    prev=f'<a href="/chapter/{num-1}">← Prev</a>' if num>1 else ""
    next=f'<a href="/chapter/{num+1}">Next →</a>' if num<len(meta) else ""
    return f"""
<html><head><style>
body{{background:#020617;color:#e5e7eb;font-family:Georgia}}
.reader{{max-width:720px;margin:auto;padding:24px}}
.nav{{position:fixed;bottom:0;left:0;right:0;
background:rgba(2,6,23,.95);display:flex;gap:10px;padding:12px}}
.nav a{{flex:1;text-align:center}}
</style></head>
<body>
<div class="reader">{html}</div>
<div class="nav">{prev}{next}</div>
</body></html>
"""

@app.get("/errors", response_class=HTMLResponse)
def view_errors():
    items="".join(f"<li>{e['time']} — {e['msg']}</li>" for e in errors) or "<li>No errors</li>"
    return f"<html><body><div class='card'><h2>Errors</h2><ul>{items}</ul><a href='/'>← Back</a></div></body></html>"

import os, json, threading, time, requests, boto3
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from bs4 import BeautifulSoup
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
app.mount("/static", StaticFiles(directory="static"), name="static")

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

state = {
    "running": False,
    "current_url": None,
    "chapter": 0,
    "seen_urls": [],
    "action": "Idle",
    "book_title": "Untitled Novel",
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

    title = soup.find("h1")
    if title and state["chapter"] == 0:
        state["book_title"] = title.get_text(strip=True)

    content_div = soup.find("div", class_="content")
    if not content_div:
        raise RuntimeError("Content not found")

    paragraphs = [
        p.get_text(strip=True)
        for p in content_div.find_all("p")
        if p.get_text(strip=True)
    ]
    content = "\n\n".join(paragraphs)

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
# ROUTES
# ======================================================

@app.get("/", response_class=HTMLResponse)
def ui():
    with open("static/reader.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/book")
def book():
    return {
        "title": state.get("book_title"),
        "chapters": len(meta),
        "last_read": state.get("chapter", 1)
    }

@app.get("/api/chapters")
def chapters():
    return meta

@app.get("/api/chapter/{num}")
def chapter(num: int):
    if num < 1 or num > len(meta):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    ch = meta[num - 1]
    html = s3.get_object(Bucket=R2_BUCKET, Key=ch["key"])["Body"].read().decode()
    return {"num": num, "html": html}

@app.get("/api/status")
def status():
    return {
        "running": state["running"],
        "action": state["action"],
        "chapter": state["chapter"],
        "errors": len(errors)
    }

@app.post("/api/import/start")
def start(url: str = Form(...)):
    state["current_url"] = url
    if not state["running"]:
        state["running"] = True
        threading.Thread(target=worker, daemon=True).start()
    save_json(STATE_KEY, state)
    return {"ok": True}

@app.post("/api/import/stop")
def stop():
    state["running"] = False
    save_json(STATE_KEY, state)
    return {"ok": True}

@app.post("/api/import/reset")
def reset():
    delete_all()
    meta.clear()
    state.update({
        "running": False,
        "current_url": None,
        "chapter": 0,
        "seen_urls": [],
        "action": "Idle"
    })
    save_json(STATE_KEY, state)
    save_json(META_KEY, meta)
    return {"ok": True}

@app.get("/api/errors")
def get_errors():
    return errors

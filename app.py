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

# ---- OpenAI ----
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---- Cloudflare R2 (S3 compatible) ----
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
    aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
)

BUCKET = os.getenv("R2_BUCKET")

app = FastAPI()

# ---- Persistent state (stored in R2) ----
state = {
    "running": False,
    "current_url": None,
    "chapter": 0
}

STATE_KEY = "state.json"

def load_state():
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=STATE_KEY)
        state.update(json.loads(obj["Body"].read().decode()))
    except:
        pass

def save_state():
    s3.put_object(
        Bucket=BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state),
        ContentType="application/json"
    )

load_state()

# ---- Helpers ----

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
                    "Preserve paragraph structure and storytelling tone. "
                    "Do not summarize or add content."
                )
            },
            {"role": "user", "content": text}
        ]
    )
    return res.choices[0].message.content

def scrape_chapter(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.find("h1").get_text(strip=True)
    content = soup.find("div", id="content").get_text("\n", strip=True)

    next_url = None
    for a in soup.find_all("a"):
        if "下一章" in a.get_text():
            next_url = requests.compat.urljoin(url, a.get("href"))
            break

    return title, clean_text(content), next_url

def save_chapter(num, title, body):
    html = f"<h1>{title}</h1>"
    for p in body.split("\n\n"):
        html += f"<p>{p}</p>"

    s3.put_object(
        Bucket=BUCKET,
        Key=f"chapters/{num:03d}.html",
        Body=html,
        ContentType="text/html"
    )

def list_chapters():
    res = s3.list_objects_v2(Bucket=BUCKET, Prefix="chapters/")
    if "Contents" not in res:
        return []
    return sorted(obj["Key"].split("/")[-1] for obj in res["Contents"])

# ---- Background worker ----

def worker():
    while state["running"] and state["current_url"]:
        state["chapter"] += 1

        title, raw, next_url = scrape_chapter(state["current_url"])
        translated = translate(raw)
        save_chapter(state["chapter"], title, translated)

        state["current_url"] = next_url
        save_state()

        if not next_url:
            state["running"] = False
            save_state()
            break

        time.sleep(2)

# ---- Routes ----

@app.get("/", response_class=HTMLResponse)
def home():
    status = "Running" if state["running"] else "Stopped"
    return f"""
    <h2>Novel Translator</h2>
    <p>Status: {status}</p>
    <form action="/start" method="post">
        <input name="url" placeholder="Start chapter URL" style="width:300px">
        <button type="submit">Start</button>
    </form>
    <form action="/stop" method="post">
        <button type="submit">Stop</button>
    </form>
    <a href="/read">Read Chapters</a>
    """

@app.post("/start")
def start(url: str = Form(...)):
    if not state["running"]:
        state["running"] = True
        state["current_url"] = url
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
    links = "".join(
        f'<li><a href="/chapter/{c}">{c}</a></li>' for c in chapters
    )
    return f"<h2>Chapters</h2><ul>{links}</ul><a href='/'>Back</a>"

@app.get("/chapter/{chapter}", response_class=HTMLResponse)
def chapter(chapter: str):
    obj = s3.get_object(Bucket=BUCKET, Key=f"chapters/{chapter}")
    return obj["Body"].read().decode()

import os
import json
import threading
import time
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

BASE_DIR = "novels"
NOVEL_DIR = f"{BASE_DIR}/tamer"
STATE_FILE = f"{NOVEL_DIR}/state.json"

os.makedirs(NOVEL_DIR, exist_ok=True)

state = {
    "running": False,
    "current_url": None,
    "chapter": 0
}

if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state.update(json.load(f))

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def clean_text(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n\n".join(lines)

def translate(text):
    response = client.chat.completions.create(
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
    return response.choices[0].message.content

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

def worker():
    while state["running"] and state["current_url"]:
        state["chapter"] += 1
        chapter_id = f"{state['chapter']:03d}"

        title, raw, next_url = scrape_chapter(state["current_url"])
        translated = translate(raw)

        with open(f"{NOVEL_DIR}/{chapter_id}.html", "w", encoding="utf-8") as f:
            f.write(f"<h1>{title}</h1>")
            for p in translated.split("\n\n"):
                f.write(f"<p>{p}</p>")

        state["current_url"] = next_url
        save_state()

        if not next_url:
            state["running"] = False
            save_state()
            break

        time.sleep(2)

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
    chapters = sorted(f for f in os.listdir(NOVEL_DIR) if f.endswith(".html"))
    links = "".join(
        f'<li><a href="/chapter/{c}">{c}</a></li>' for c in chapters
    )
    return f"<h2>Chapters</h2><ul>{links}</ul><a href='/'>Back</a>"

@app.get("/chapter/{chapter}", response_class=HTMLResponse)
def chapter(chapter: str):
    path = f"{NOVEL_DIR}/{chapter}"
    if not os.path.exists(path):
        return "Not found"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

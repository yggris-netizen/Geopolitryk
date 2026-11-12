# -*- coding: utf-8 -*-
"""PubSubHubbub listener (ручной) — real-time уведомления о новых видео."""
import os
import threading
import time
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

from config import CHANNELS_TO_WATCH
from db.queue import _add_task_to_queue

HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"


def _subscribe_all(callback_url: str) -> None:
    for ch in CHANNELS_TO_WATCH:
        topic = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={ch['id']}"
        data = {
            "hub.mode": "subscribe",
            "hub.topic": topic,
            "hub.callback": callback_url,
            "hub.verify": "async",
            "hub.lease_seconds": "864000",
        }
        try:
            r = httpx.post(HUB_URL, data=data, timeout=30)
            print(f"[LIVE] subscribe {ch['name']}: HTTP {r.status_code}")
        except Exception as e:
            print(f"[ERR] subscribe {ch['name']}: {e}")


def _parse_video_id(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
        ns = {"yt": "http://www.youtube.com/xml/schemas/2015"}
        vid = root.find(".//yt:videoId", ns)
        if vid is not None:
            return vid.text or ""
    except Exception as e:
        print(f"[ERR] parse xml: {e}")
    return ""


def _on_video(video_id: str) -> None:
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[LIVE] Новое видео: {video_url}")
    try:
        tid = _add_task_to_queue(
            video_url=video_url, video_id=video_id,
            priority=5, source="auto",
        )
        if tid:
            print(f"[LIVE] → в очередь #{tid}")
    except Exception as e:
        print(f"[ERR] _add_task_to_queue: {e}")


app = FastAPI()


@app.get("/")
def verify(request: Request):
    """Google Hub verify: вернуть hub.challenge как plain text."""
    params = dict(request.query_params)
    print(f"[LIVE] verify: {params}")
    challenge = params.get("hub.challenge", "")
    return Response(content=challenge, media_type="text/plain")


@app.post("/")
async def callback(request: Request):
    body = await request.body()
    vid = _parse_video_id(body)
    if vid:
        _on_video(vid)
    return Response(status_code=200)


def _run_uvicorn():
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning", access_log=False)


def run_listener() -> None:
    """1) uvicorn (поток) → 2) ngrok → 3) subscribe."""
    from pyngrok import ngrok, conf

    path = os.getenv("NGROK_PATH", "").strip()
    if path:
        conf.get_default().ngrok_path = path

    # 1. uvicorn в отдельном потоке
    t = threading.Thread(target=_run_uvicorn, daemon=True, name="uvicorn")
    t.start()
    time.sleep(3)  # дать uvicorn подняться
    print("[LIVE] uvicorn запущен (порт 8000)")

    # 2. ngrok
    tunnel = ngrok.connect(8000)
    callback_url = tunnel.public_url
    print(f"[LIVE] ngrok: {callback_url}")

    # 3. subscribe
    _subscribe_all(callback_url)

    # 4. держим поток
    while True:
        time.sleep(60)


def start_listener_thread() -> threading.Thread:
    t = threading.Thread(target=run_listener, daemon=True, name="pubsub")
    t.start()
    print("[LIVE] PubSubHubbub thread запущен")
    return t

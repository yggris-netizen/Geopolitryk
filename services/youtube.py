"""Всё, что связано с YouTube: RSS, API, кэши, квоты, yt-dlp."""

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
import yt_dlp
from googleapiclient.errors import HttpError

from config import (
    DB_PATH, DISPLAY_TIMEZONE_OFFSET, HOURS_BACK, DAYS_BACK,
    MIN_VIDEO_DURATION_SECONDS, MAX_VIDEO_DURATION_SECONDS,
    MAX_VIDEOS_PER_CHANNEL_PER_CHECK,
    YOUTUBE_DAILY_QUOTA, YOUTUBE_VIDEOS_CACHE_TTL, YOUTUBE_DURATIONS_CACHE_TTL,
    YOUTUBE_CACHE_MAX_ENTRIES,
)
from services.youtube_api import youtube
from db.videos import _is_video_processed


# ─── Кэши (в памяти) ───────────────────────────────────────

_YOUTUBE_UPLOADS_CACHE = {}
_YOUTUBE_VIDEOS_CACHE = {}
_YOUTUBE_DURATIONS_CACHE = {}


# ─── Квоты ────────────────────────────────────────────────

_QUOTA_USED = {"count": 0, "date": "", "by_op": {}}


def _quota_tick(op: str, cost: int = 1):
    global _QUOTA_USED
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _QUOTA_USED["date"] != today_utc:
        _QUOTA_USED = {"count": 0, "date": today_utc, "by_op": {}}
    _QUOTA_USED["count"] += cost
    _QUOTA_USED["by_op"][op] = _QUOTA_USED["by_op"].get(op, 0) + cost


def _quota_info() -> dict:
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _QUOTA_USED["date"] != today_utc:
        return {"count": 0, "date": today_utc, "by_op": {}}
    return dict(_QUOTA_USED)


def _is_quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return "quotaexceeded" in s or ("quota" in s and "exceeded" in s)


# ─── Кэши: обрезка ────────────────────────────────────────

def _maybe_trim_caches():
    if len(_YOUTUBE_VIDEOS_CACHE) > YOUTUBE_CACHE_MAX_ENTRIES:
        keys = list(_YOUTUBE_VIDEOS_CACHE.keys())[:len(_YOUTUBE_VIDEOS_CACHE) // 2]
        for k in keys:
            _YOUTUBE_VIDEOS_CACHE.pop(k, None)
    if len(_YOUTUBE_DURATIONS_CACHE) > YOUTUBE_CACHE_MAX_ENTRIES:
        keys = list(_YOUTUBE_DURATIONS_CACHE.keys())[:len(_YOUTUBE_DURATIONS_CACHE) // 2]
        for k in keys:
            _YOUTUBE_DURATIONS_CACHE.pop(k, None)


# ─── RSS / API ────────────────────────────────────────────

def _get_uploads_playlist_id(channel_id: str) -> str:
    if channel_id in _YOUTUBE_UPLOADS_CACHE:
        return _YOUTUBE_UPLOADS_CACHE[channel_id]
    try:
        _quota_tick("channels.list")
        response = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    except HttpError as e:
        if _is_quota_error(e):
            print(f"   ⚠️  quotaExceeded в channels.list")
        else:
            print(f"   ❌ channels.list: {e}")
        return ""
    except Exception as e:
        print(f"   ❌ channels.list: {e}")
        return ""
    items = response.get("items", [])
    if not items:
        return ""
    uploads_id = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    if uploads_id:
        _YOUTUBE_UPLOADS_CACHE[channel_id] = uploads_id
        print(f"   🔑 uploads playlist: {uploads_id}")
    return uploads_id


def _get_channel_videos_rss(channel_id: str) -> list:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        xml_text = resp.text
    except Exception as e:
        print(f"   ❌ RSS fetch: {e}")
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"   ❌ RSS parse: {e}")
        return []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    videos = []
    for entry in root.findall("atom:entry", ns):
        try:
            vid_el = entry.find("yt:videoId", ns)
            if vid_el is None or not vid_el.text:
                continue
            title_el = entry.find("atom:title", ns)
            pub_el = entry.find("atom:published", ns)
            desc_el = entry.find(".//media:description", ns)
            videos.append({
                "video_id": vid_el.text,
                "title": title_el.text if title_el is not None else "",
                "url": f"https://www.youtube.com/watch?v={vid_el.text}",
                "channel": "",
                "description": desc_el.text if desc_el is not None and desc_el.text else "",
                "published_at": pub_el.text if pub_el is not None else "",
            })
        except Exception:
            continue
    print(f"   📡 RSS: получено {len(videos)} (без квоты)")
    return videos


def _get_channel_videos_raw(channel_id: str, max_results: int = 50) -> list:
    now = time.time()
    cached = _YOUTUBE_VIDEOS_CACHE.get(channel_id)
    if cached:
        ts, videos = cached
        if now - ts < YOUTUBE_VIDEOS_CACHE_TTL:
            print(f"   💾 кэш видео ({len(videos)} шт., {int(now - ts)}с назад)")
            return videos

    print(f"   📡 RSS-first discovery")
    videos = _get_channel_videos_rss(channel_id)
    if videos:
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, videos)
        _maybe_trim_caches()
        return videos

    print(f"   🔄 RSS пуст → API fallback")
    uploads_id = _get_uploads_playlist_id(channel_id)
    if not uploads_id:
        print(f"   ❌ API: uploads playlist не получен")
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, [])
        _maybe_trim_caches()
        return []

    try:
        _quota_tick("playlistItems.list")
        response = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=uploads_id,
            maxResults=min(max_results, 50),
        ).execute()
    except HttpError as e:
        print(f"   ❌ playlistItems.list: {e}")
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, [])
        _maybe_trim_caches()
        return []
    except Exception as e:
        print(f"   ❌ playlistItems.list: {e}")
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, [])
        _maybe_trim_caches()
        return []

    videos = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        video_id = snippet.get("resourceId", {}).get("videoId") or content.get("videoId", "")
        title = snippet.get("title", "")
        if not video_id or title in ("Private video", "Deleted video", "[Private video]", "[Deleted video]"):
            continue
        published_at = content.get("videoPublishedAt") or snippet.get("publishedAt", "")
        videos.append({
            "video_id": video_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel": snippet.get("channelTitle", ""),
            "description": snippet.get("description", ""),
            "published_at": published_at,
        })

    _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, videos)
    _maybe_trim_caches()
    print(f"   📡 playlistItems (fallback): получено {len(videos)}")
    return videos


# ─── Длительности ─────────────────────────────────────────

def _parse_iso_duration(duration: str) -> int:
    if not duration:
        return 0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def _get_video_durations(video_ids: list) -> dict:
    if not video_ids:
        return {}
    now = time.time()
    durations = {}
    uncached = []
    for vid in video_ids:
        cached = _YOUTUBE_DURATIONS_CACHE.get(vid)
        if cached:
            ts, dur = cached
            if now - ts < YOUTUBE_DURATIONS_CACHE_TTL:
                durations[vid] = dur
                continue
        uncached.append(vid)

    if not uncached:
        print(f"   💾 длительности из кэша: {len(durations)}")
        return durations

    for i in range(0, len(uncached), 50):
        batch = uncached[i:i + 50]
        try:
            _quota_tick("videos.list")
            response = youtube.videos().list(
                part="contentDetails", id=",".join(batch), maxResults=50,
            ).execute()
            for item in response.get("items", []):
                vid = item["id"]
                dur_str = item.get("contentDetails", {}).get("duration", "")
                dur = _parse_iso_duration(dur_str)
                durations[vid] = dur
                _YOUTUBE_DURATIONS_CACHE[vid] = (now, dur)
            print(f"   📡 videos.list: получено {len(batch)} длительностей")
        except HttpError as e:
            if _is_quota_error(e):
                print(f"   ⚠️  quotaExceeded в videos.list — yt-dlp fallback")
                for vid in batch[:5]:
                    if vid in durations:
                        continue
                    try:
                        info = get_video_info_ytdlp(f"https://www.youtube.com/watch?v={vid}")
                        dur = int(info.get("duration", 0) or 0)
                        durations[vid] = dur
                        _YOUTUBE_DURATIONS_CACHE[vid] = (now, dur)
                    except Exception:
                        durations[vid] = 0
            else:
                print(f"   ⚠️  durations: {e}")
        except Exception as e:
            print(f"   ⚠️  durations: {e}")

    _maybe_trim_caches()
    return durations


# ─── Форматирование ───────────────────────────────────────

def _format_duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _format_published_date(published_at_iso: str) -> str:
    if not published_at_iso:
        return ""
    try:
        dt_utc = datetime.fromisoformat(published_at_iso.replace("Z", "+00:00"))
        dt_local = dt_utc + timedelta(hours=DISPLAY_TIMEZONE_OFFSET)
        tz = "МСК" if DISPLAY_TIMEZONE_OFFSET == 3 else f"UTC+{DISPLAY_TIMEZONE_OFFSET}"
        return f"{dt_local.strftime('%d.%m.%Y')} · {dt_local.strftime('%H:%M')} {tz}"
    except Exception:
        return ""


# ─── Получение видео канала ───────────────────────────────

def _fetch_videos_from_channel(channel_id: str, days_back: float) -> list:
    raw_videos = _get_channel_videos_raw(channel_id, max_results=50)
    if not raw_videos:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    candidates = []
    for v in raw_videos:
        published = v.get("published_at", "")
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except Exception:
                pass
        if _is_video_processed(v["video_id"]):
            continue
        candidates.append(v)
    if not candidates:
        return []
    durations = _get_video_durations([c["video_id"] for c in candidates])
    filtered = []
    for video in candidates:
        dur = durations.get(video["video_id"], 0)
        if dur == 0:
            print(f"   ⏭  нет длительности: {video['title'][:60]}")
            continue
        if dur < MIN_VIDEO_DURATION_SECONDS:
            print(f"   ⏭  короткое ({_format_duration(dur)}): {video['title'][:60]}")
            continue
        if dur > MAX_VIDEO_DURATION_SECONDS:
            print(f"   ⏭  длинное ({_format_duration(dur)}): {video['title'][:60]}")
            continue
        video["duration_seconds"] = dur
        filtered.append(video)
    return filtered


def get_new_videos_from_channel(channel_id: str) -> list:
    videos = _fetch_videos_from_channel(channel_id, DAYS_BACK)
    if videos:
        print(f"   📅 Окно: последние {HOURS_BACK} ч — найдено {len(videos)}")
    else:
        print(f"   📭 За {HOURS_BACK} ч новых нет")
    return videos


# ─── yt-dlp ───────────────────────────────────────────────

def get_video_info_ytdlp(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True,
            'skip_download': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['mweb', 'tv', 'web_safari'],
                }
            },
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            upload_date = info.get("upload_date", "")
            published_at = ""
            if upload_date and len(upload_date) == 8:
                published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
            return {
                "title": info.get("title", ""),
                "duration": int(info.get("duration", 0) or 0),
                "channel": info.get("channel", "") or info.get("uploader", ""),
                "description": info.get("description", ""),
                "published_at": published_at,
            }
    except Exception as e:
        print(f"⚠️  yt-dlp: {e}")
        return {"title": "", "duration": 0, "channel": "", "description": "", "published_at": ""}


# ─── Утилиты URL ──────────────────────────────────────────

def _extract_video_id(url: str) -> str:
    m = re.search(r'(?:v=|/shorts/|youtu\.be/)([0-9A-Za-z_-]{11})', url)
    return m.group(1) if m else ""


def _get_thumbnail_url(video_id: str) -> str:
    if not video_id:
        return ""
    return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"


def _is_youtube_url(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r'(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]{11}', text))


def _extract_youtube_url(text: str) -> str:
    m = re.search(r'(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{11})', text)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None
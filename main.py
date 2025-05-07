import os
import json
import time
import asyncio
import re
import requests
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# === ENV ===
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
LONG_LIVED_USER_TOKEN = os.getenv("LONG_LIVED_USER_TOKEN")

SESSION_FILE = "telegram_session"
RESULT_FILE = "results.json"

# === Load all previously posted texts from results.json ===
def load_posted_texts_from_results():
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(entry["original_text"].strip() for entry in data if entry.get("original_text"))
    except:
        return set()

# === Append new entries to results.json (no overwrite) ===
def log_result(new_entries):
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            existing_entries = json.load(f)
    except:
        existing_entries = []

    combined = existing_entries + new_entries

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

# === Translate ===
def translate_to_malay(text):
    cleaned = re.sub(r'@\w+', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'https?://\S+', '', cleaned)
    cleaned = re.sub(r'\[.*?\]\(.*?\)', '', cleaned)
    cleaned = re.sub(r'\n+', '\n', cleaned).strip()

    prompt = f"""
Translate the following post into Malay.
Do not include any usernames, mentions, links, or Telegram source references.
If the original post starts with 'JUST IN:' or '**JUST IN:**', please translate it as 'TERKINI:'.
Write it as a casual, friendly FB caption in one paragraph — no heading, no explanation.
Do not use slang or shouting. Keep it natural, chill, and neutral.

'{cleaned}'
"""
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]}
        )
        return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[Gemini Error] {e}")
        return "Translation failed"

# === Facebook Posting ===
def get_fb_token():
    try:
        res = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?access_token={LONG_LIVED_USER_TOKEN}")
        return res.json()["data"][0]["access_token"]
    except:
        return None

def post_text_only_to_fb(caption):
    token = get_fb_token()
    if not token:
        return False
    try:
        r = requests.post(
            f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
            data={"message": caption, "access_token": token}
        )
        print("[FB] Text-only post success." if r.status_code == 200 else f"[FB Text Error] {r.status_code}: {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"[FB Text Exception] {e}")
        return False

def post_photos_to_fb(image_paths, caption):
    token = get_fb_token()
    if not token:
        return False

    media_ids = []
    for path in image_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'rb') as f:
                r = requests.post(
                    f"https://graph.facebook.com/{FB_PAGE_ID}/photos",
                    data={"published": "false", "access_token": token},
                    files={"source": f}
                )
                if r.status_code == 200:
                    media_ids.append({"media_fbid": r.json()["id"]})
        except Exception as e:
            print(f"[FB Upload Image Error] {e}")

    if not media_ids:
        return False

    try:
        r = requests.post(
            f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
            data={
                "message": caption,
                "attached_media": json.dumps(media_ids),
                "access_token": token
            }
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[FB Image Post Error] {e}")
        return False

def post_video_to_fb(video_path, caption):
    token = get_fb_token()
    if not token:
        return False
    try:
        with open(video_path, 'rb') as f:
            r = requests.post(
                f"https://graph.facebook.com/{FB_PAGE_ID}/videos",
                data={"description": caption, "access_token": token},
                files={"source": f}
            )
        print("[FB] Video post success." if r.status_code == 200 else f"[FB Video Error] {r.status_code}: {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"[FB Video Exception] {e}")
        return False

# === MAIN ===
async def main():
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start()

    posted_texts = load_posted_texts_from_results()
    media_group_ids_done = set()
    results = []

    async for msg in client.iter_messages("ChatGPTMastermind", limit=20):
        original_text = (msg.text or "").strip()

        if not original_text or len(original_text.split()) < 3:
            continue

        if original_text in posted_texts:
            print(f"[SKIP] Already posted content: {original_text[:60]}...")
            continue

        if hasattr(msg, "media_group_id") and msg.media_group_id in media_group_ids_done:
            continue

        translated = translate_to_malay(original_text)
        if translated == "Translation failed":
            continue

        image_paths = []
        video_path = None
        success = False

        if hasattr(msg, "media_group_id") and msg.media_group_id:
            group_msgs = []
            async for grouped in client.iter_messages("WatcherGuru", min_id=msg.id - 15, max_id=msg.id + 15):
                if (
                    hasattr(grouped, "media_group_id") and
                    grouped.media_group_id == msg.media_group_id
                ):
                    group_msgs.append(grouped)
            media_group_ids_done.add(msg.media_group_id)

            for media_msg in reversed(group_msgs):
                if isinstance(media_msg.media, MessageMediaPhoto):
                    path = f"temp_{media_msg.id}.jpg"
                    await client.download_media(media_msg.media, file=path)
                    image_paths.append(path)
        elif isinstance(msg.media, MessageMediaPhoto):
            path = f"temp_{msg.id}.jpg"
            await client.download_media(msg.media, file=path)
            image_paths.append(path)
        elif isinstance(msg.media, MessageMediaDocument) and msg.file.mime_type and "video" in msg.file.mime_type:
            video_path = f"temp_{msg.id}.mp4"
            await client.download_media(msg.media, file=video_path)

        if video_path:
            success = post_video_to_fb(video_path, translated)
        elif image_paths:
            success = post_photos_to_fb(image_paths, translated)
        else:
            success = post_text_only_to_fb(translated)

        if success:
            results.append({
                "telegram_id": msg.id,
                "original_text": original_text,
                "translated_caption": translated,
                "fb_status": "Posted",
                "date_posted": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        for path in image_paths + ([video_path] if video_path else []):
            if os.path.exists(path):
                os.remove(path)

        time.sleep(1)

    log_result(results)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())

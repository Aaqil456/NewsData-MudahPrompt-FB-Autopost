import os
import json
import time
import re
import requests
from datetime import datetime

# === ENV ===
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
LONG_LIVED_USER_TOKEN = os.getenv("LONG_LIVED_USER_TOKEN")

RESULT_FILE = "results.json"

# === Load posted texts ===
def load_posted_texts_from_results():
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            return set(entry["original_text"].strip() for entry in json.load(f) if entry.get("original_text"))
    except:
        return set()

# === Log new posted entries ===
def log_result(new_entries):
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except:
        existing = []
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing + new_entries, f, ensure_ascii=False, indent=2)

# === Translate ===
def translate_to_malay(text):
    cleaned = re.sub(r'@\w+|https?://\S+|\[.*?\]\(.*?\)', '', text).strip()
    prompt = f"""
Translate this post into Malay as a casual, friendly FB caption also make the structure easy to read. Avoid slang, uppercase, and do not explain.

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
        print("[Gemini Error]", e)
        return "Translation failed"

# === Facebook posting ===
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
    r = requests.post(
        f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
        data={"message": caption, "access_token": token}
    )
    print("[FB] Text posted." if r.status_code == 200 else f"[FB Text Error] {r.status_code}")
    return r.status_code == 200

def post_photos_to_fb(image_paths, caption):
    token = get_fb_token()
    if not token:
        return False
    media_ids = []
    for path in image_paths:
        if not os.path.exists(path): continue
        with open(path, 'rb') as f:
            r = requests.post(
                f"https://graph.facebook.com/{FB_PAGE_ID}/photos",
                data={"published": "false", "access_token": token},
                files={"source": f}
            )
            if r.status_code == 200:
                media_ids.append({"media_fbid": r.json()["id"]})
    if not media_ids: return False
    r = requests.post(
        f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
        data={
            "message": caption,
            "attached_media": json.dumps(media_ids),
            "access_token": token
        }
    )
    return r.status_code == 200

# === Fetch tweets from RapidAPI ===
def fetch_tweets_rapidapi(username, max_tweets=20):
    url = "https://twttrapi.p.rapidapi.com/user-tweets"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "twttrapi.p.rapidapi.com"
    }
    response = requests.get(url, headers=headers, params={"username": username})
    if response.status_code != 200:
        print("[API ERROR]", response.text)
        return []

    data = response.json()
    timeline = data.get("user_result", {}).get("result", {}).get("timeline_response", {}).get("timeline", {})
    entries = []
    for ins in timeline.get("instructions", []):
        if ins.get("__typename") == "TimelineAddEntries":
            entries = ins.get("entries", [])
            break

    tweets = []
    for entry in entries:
        try:
            tweet = entry["content"]["content"]["tweetResult"]["result"]
            tid = tweet.get("rest_id", "")
            legacy = tweet.get("legacy", {})
            text = tweet.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {}).get("text") or \
                   legacy.get("full_text") or legacy.get("text", "")
            if not text or not tid: continue
            media_urls = []
            for m in legacy.get("extended_entities", {}).get("media", []) + legacy.get("entities", {}).get("media", []):
                if m.get("type") == "photo":
                    media_urls.append(m.get("media_url_https") or m.get("media_url"))
            translated = translate_to_malay(text)
            if translated and translated != "Translation failed":
                tweets.append({
                    "id": tid,
                    "text": translated,
                    "images": media_urls,
                    "tweet_url": f"https://x.com/{username}/status/{tid}"
                })
            if len(tweets) >= max_tweets:
                break
        except Exception as e:
            print("[Tweet Parse Error]", e)
    return tweets

# === Main function ===
def fetch_and_post_tweets():
    posted = load_posted_texts_from_results()
    results = []
    tweets = fetch_tweets_rapidapi("WatcherGuru", 20)  # Tukar username jika perlu

    for i, tweet in enumerate(tweets):
        if tweet["text"] in posted:
            print(f"[SKIP] Already posted: {tweet['text'][:60]}")
            continue

        success = False
        if tweet["images"]:
            img_paths = []
            for j, url in enumerate(tweet["images"]):
                try:
                    path = f"temp_{i}_{j}.jpg"
                    with open(path, "wb") as f:
                        f.write(requests.get(url).content)
                    img_paths.append(path)
                except Exception as e:
                    print("[Image DL Error]", e)
            success = post_photos_to_fb(img_paths, tweet["text"])
            for p in img_paths:
                if os.path.exists(p): os.remove(p)
        else:
            success = post_text_only_to_fb(tweet["text"])

        if success:
            results.append({
                "tweet_url": tweet["tweet_url"],
                "original_text": tweet["text"],
                "translated_caption": tweet["text"],
                "fb_status": "Posted",
                "date_posted": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        time.sleep(1)

    log_result(results)

if __name__ == "__main__":
    fetch_and_post_tweets()

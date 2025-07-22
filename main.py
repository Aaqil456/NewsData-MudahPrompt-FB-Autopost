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

# === Load posted tweet IDs
def load_posted_ids():
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            return set(entry["id"] for entry in json.load(f) if entry.get("id"))
    except:
        return set()

# === Log posted
def log_result(new_entries):
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except:
        existing = []
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing + new_entries, f, ensure_ascii=False, indent=2)

# === Translate
def translate_to_malay(text):
    cleaned = re.sub(r'@\w+|https?://\S+', '', text).strip()
    prompt = f"""

Translate the following post into Malay as a casual, friendly Facebook caption.
Use proper, natural language without slang, emojis, or all caps.
Format the caption with clear and readable spacing.
Do not explain anything or include any introduction—return only the translated caption.
If the post is clearly an advertisement in which it have things like link in bio then, do not translate. Instead, return a short fun fact about prompt engineering in Malay.

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

# === Facebook Token
def get_fb_token():
    try:
        res = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?access_token={LONG_LIVED_USER_TOKEN}")
        return res.json()["data"][0]["access_token"]
    except Exception as e:
        print("[FB Token Error]", e)
        return None

# === Post Text
def post_text_only_to_fb(caption):
    token = get_fb_token()
    if not token:
        return False
    r = requests.post(
        f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
        data={"message": caption, "access_token": token}
    )
    return r.status_code == 200

# === Post Photos
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
    if not media_ids:
        return False
    r = requests.post(
        f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
        data={"message": caption, "attached_media": json.dumps(media_ids), "access_token": token}
    )
    return r.status_code == 200

# === Post Video
def post_video_to_fb(video_path, caption):
    token = get_fb_token()
    if not token or not os.path.exists(video_path):
        return False
    with open(video_path, 'rb') as f:
        r = requests.post(
            f"https://graph.facebook.com/{FB_PAGE_ID}/videos",
            data={"description": caption, "access_token": token},
            files={"source": f}
        )
    return r.status_code == 200

# === Fetch Tweets
def fetch_tweets_rapidapi(username, max_tweets=30):
    url = "https://twttrapi.p.rapidapi.com/user-tweets"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "twttrapi.p.rapidapi.com"
    }
    try:
        response = requests.get(url, headers=headers, params={"username": username})
        if response.status_code != 200:
            print("[API ERROR]", response.text[:300])
            return []

        data = response.json()
        timeline = (
            data.get("data", {}).get("user_result", {}).get("result", {}).get("timeline_response", {}).get("timeline", {})
            or data.get("user_result", {}).get("result", {}).get("timeline_response", {}).get("timeline", {})
        )
        instructions = timeline.get("instructions", [])
        entries = []
        for ins in instructions:
            if ins.get("__typename") == "TimelineAddEntries":
                entries.extend(ins.get("entries", []))
            elif ins.get("__typename") == "TimelinePinEntry":
                pinned_entry = ins.get("entry")
                if pinned_entry:
                    entries.append(pinned_entry)

        tweets = []
        for entry in entries:
            try:
                tweet = entry["content"]["content"]["tweetResult"]["result"]
                tid = tweet.get("rest_id", "")
                legacy = tweet.get("legacy", {})
                text = tweet.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {}).get("text") or \
                       legacy.get("full_text") or legacy.get("text", "")
                if not text or not tid:
                    continue

                images, video_url = [], None
                for m in legacy.get("extended_entities", {}).get("media", []) + legacy.get("entities", {}).get("media", []):
                    if m.get("type") == "photo":
                        images.append(m.get("media_url_https") or m.get("media_url"))
                    elif m.get("type") == "video":
                        variants = m.get("video_info", {}).get("variants", [])
                        mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
                        best = sorted(mp4s, key=lambda x: x.get("bitrate", 0), reverse=True)
                        if best:
                            video_url = best[0]["url"]

                translated = translate_to_malay(text)
                if translated and translated != "Translation failed":
                    tweets.append({
                        "id": tid,
                        "text": translated,
                        "images": images,
                        "video": video_url,
                        "tweet_url": f"https://x.com/{username}/status/{tid}"
                    })
                if len(tweets) >= max_tweets:
                    break
            except Exception as e:
                print("[Tweet Parse Error]", e)
        return tweets
    except Exception as e:
        print("[Twitter API Error]", e)
        return []

# === Main Flow
def fetch_and_post_tweets():
    posted_ids = load_posted_ids()
    results = []
    usernames = ["prompthero","GptPromptsTips", "Prompt__ChatGPT"]

    for username in usernames:
        tweets = fetch_tweets_rapidapi(username, 20)
        print(f"[INFO] Total fetched for @{username}:", len(tweets))

        for tweet in tweets:
            if tweet["id"] in posted_ids:
                print(f"[SKIP] Already posted: {tweet['tweet_url']}")
                continue

            success = False
            video_path = None

            if tweet.get("video"):
                try:
                    video_path = f"temp_{tweet['id']}.mp4"
                    with open(video_path, "wb") as f:
                        f.write(requests.get(tweet["video"]).content)
                    success = post_video_to_fb(video_path, tweet["text"])
                except Exception as e:
                    print("[Video DL Error]", e)
            elif tweet["images"]:
                img_paths = []
                for j, url in enumerate(tweet["images"]):
                    try:
                        path = f"temp_{tweet['id']}_{j}.jpg"
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
                    "id": tweet["id"],
                    "tweet_url": tweet["tweet_url"],
                    "original_text": tweet["text"],
                    "translated_caption": tweet["text"],
                    "images": tweet["images"],
                    "video": tweet["video"],
                    "fb_status": "Posted",
                    "date_posted": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                print(f"[✅ POSTED] {tweet['tweet_url']}")
            else:
                print(f"[❌ FAILED] {tweet['tweet_url']}")

            if video_path and os.path.exists(video_path):
                os.remove(video_path)

            time.sleep(1)

    if results:
        log_result(results)
        print(f"[✅ LOGGED] {len(results)} entries added.")
    else:
        print("[⚠️ NOTHING TO POST]")

if __name__ == "__main__":
    fetch_and_post_tweets()

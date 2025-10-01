import os
import json
import time
import re
import requests
from datetime import datetime

# ==== NEW GEMINI SDK ====
from google import genai
from google.genai import types

# =========================
# ENV
# =========================
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
LONG_LIVED_USER_TOKEN = os.getenv("LONG_LIVED_USER_TOKEN")
RESULT_FILE = "results.json"

# =========================
# Helpers: results.json
# =========================
def load_posted_ids():
    """Return a set of tweet IDs already logged as posted."""
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            return set(entry["id"] for entry in json.load(f) if entry.get("id"))
    except Exception:
        return set()

def log_result(new_entries):
    """Append new entries to results.json."""
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        existing = []
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing + new_entries, f, ensure_ascii=False, indent=2)

# =========================
# Gemini Translator (new SDK)
# =========================
class GeminiTranslator:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is missing.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        # Disable "thinking" to avoid extra costs/latency
        self.config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )

    @staticmethod
    def _clean_text(text: str) -> str:
        # strip @mentions, URLs, and reduce whitespace
        text = re.sub(r'@\w+|https?://\S+|http?://\S+', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def translate_to_malay(self, text: str, retries: int = 3, backoff: float = 1.5) -> str:
        """
        Translate post to Malay casual FB caption using the new SDK.
        If the content looks like an ad (e.g., 'link in bio'), ask the model to output a short fun fact instead.
        """
        if not text or not isinstance(text, str) or not text.strip():
            return ""

        cleaned = self._clean_text(text)

        prompt = (
            "Translate the following post into Malay as a casual, friendly Facebook caption.\n"
            "Use proper, natural language without heavy slang, emojis, or ALL CAPS.\n"
            "Format the caption with clear readable spacing.\n"
            "Do not add any intro or explanation—return only the caption.\n"
            "If the post is clearly an advertisement (e.g., 'link in bio', 'shop now', coupon codes, giveaways, etc.),\n"
            "then DO NOT translate it. Instead, return ONE short, interesting fun fact about prompt engineering in Malay.\n\n"
            f"'{cleaned}'"
        )

        last_err = None
        for i in range(retries):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=self.config,
                )
                # SDK returns a rich object; .text gives the best-effort text aggregate
                out = (resp.text or "").strip()
                if out:
                    return out
            except Exception as e:
                last_err = e
                time.sleep(backoff ** (i + 1))
        print("[Gemini Error]", last_err)
        return "Translation failed"

# =========================
# Facebook Posting
# =========================
def get_fb_token():
    """Exchange long-lived user token for page token (first page)."""
    try:
        res = requests.get(
            "https://graph.facebook.com/v19.0/me/accounts",
            params={"access_token": LONG_LIVED_USER_TOKEN},
            timeout=20
        )
        data = res.json()
        return data["data"][0]["access_token"]
    except Exception as e:
        print("[FB Token Error]", getattr(e, 'message', str(e)))
        return None

def post_text_only_to_fb(caption: str) -> bool:
    token = get_fb_token()
    if not token:
        return False
    r = requests.post(
        f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
        data={"message": caption, "access_token": token},
        timeout=60
    )
    if r.status_code != 200:
        print("[FB Text Post Error]", r.text[:300])
    return r.status_code == 200

def post_photos_to_fb(image_paths, caption) -> bool:
    token = get_fb_token()
    if not token:
        return False

    media_ids = []
    for path in image_paths:
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            r = requests.post(
                f"https://graph.facebook.com/{FB_PAGE_ID}/photos",
                data={"published": "false", "access_token": token},
                files={"source": f},
                timeout=120
            )
        if r.status_code == 200:
            try:
                media_ids.append({"media_fbid": r.json()["id"]})
            except Exception:
                pass
        else:
            print("[FB Photo Upload Error]", r.text[:300])

    if not media_ids:
        return False

    r = requests.post(
        f"https://graph.facebook.com/{FB_PAGE_ID}/feed",
        data={
            "message": caption,
            "attached_media": json.dumps(media_ids),
            "access_token": token
        },
        timeout=60
    )
    if r.status_code != 200:
        print("[FB Photo Publish Error]", r.text[:300])
    return r.status_code == 200

def post_video_to_fb(video_path, caption) -> bool:
    token = get_fb_token()
    if not token or not os.path.exists(video_path):
        return False
    with open(video_path, "rb") as f:
        r = requests.post(
            f"https://graph.facebook.com/{FB_PAGE_ID}/videos",
            data={"description": caption, "access_token": token},
            files={"source": f},
            timeout=600
        )
    if r.status_code != 200:
        print("[FB Video Post Error]", r.text[:300])
    return r.status_code == 200

# =========================
# Twitter via RapidAPI
# =========================
def fetch_tweets_rapidapi(username, max_tweets=30):
    """
    Fetch original tweets (not replies/retweets) using twttrapi RapidAPI.
    Extract text, images, and best MP4 video URL when present.
    """
    url = "https://twttrapi.p.rapidapi.com/user-tweets"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "twttrapi.p.rapidapi.com"
    }
    try:
        response = requests.get(url, headers=headers, params={"username": username}, timeout=60)
        if response.status_code != 200:
            print("[API ERROR]", response.text[:300])
            return []

        data = response.json()

        # Handle both possible nestings defensively
        timeline = (
            data.get("data", {}).get("user_result", {}).get("result", {})
            .get("timeline_response", {}).get("timeline", {})
        ) or (
            data.get("user_result", {}).get("result", {})
            .get("timeline_response", {}).get("timeline", {})
        )

        instructions = timeline.get("instructions", [])
        entries = []
        for ins in instructions:
            t = ins.get("__typename")
            if t == "TimelineAddEntries":
                entries.extend(ins.get("entries", []))
            elif t == "TimelinePinEntry":
                pinned_entry = ins.get("entry")
                if pinned_entry:
                    entries.append(pinned_entry)

        tweets = []
        for entry in entries:
            try:
                result = entry["content"]["content"]["tweetResult"]["result"]
                tid = result.get("rest_id", "")
                legacy = result.get("legacy", {}) or {}
                if not tid:
                    continue

                # Prefer full Note Tweet text if available, otherwise legacy text
                note = result.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
                text = note.get("text") or legacy.get("full_text") or legacy.get("text", "")
                if not text:
                    continue

                # Skip retweets/replies if needed (optional safety)
                if legacy.get("retweeted_status_id_str") or legacy.get("in_reply_to_status_id_str"):
                    continue

                images, video_url = [], None
                media_blocks = []
                media_blocks += legacy.get("extended_entities", {}).get("media", []) or []
                media_blocks += legacy.get("entities", {}).get("media", []) or []
                for m in media_blocks:
                    mtype = m.get("type")
                    if mtype == "photo":
                        images.append(m.get("media_url_https") or m.get("media_url"))
                    elif mtype in ("video", "animated_gif"):
                        variants = m.get("video_info", {}).get("variants", [])
                        mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
                        best = sorted(mp4s, key=lambda x: x.get("bitrate", 0), reverse=True)
                        if best:
                            video_url = best[0].get("url")

                tweets.append({
                    "id": tid,
                    "raw_text": text,  # keep original (for logging)
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

# =========================
# Main Flow
# =========================
def fetch_and_post_tweets():
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY is missing.")
    if not FB_PAGE_ID:
        raise RuntimeError("FB_PAGE_ID is missing.")
    if not LONG_LIVED_USER_TOKEN:
        raise RuntimeError("LONG_LIVED_USER_TOKEN is missing.")

    translator = GeminiTranslator(api_key=GEMINI_API_KEY, model="gemini-2.5-flash")

    posted_ids = load_posted_ids()
    results = []
    usernames = ["prompthero"]  # add more if you want

    session = requests.Session()  # reuse TCP connections
    for username in usernames:
        tweets = fetch_tweets_rapidapi(username, max_tweets=20)
        print(f"[INFO] Total fetched for @{username}: {len(tweets)}")

        for tweet in tweets:
            if tweet["id"] in posted_ids:
                print(f"[SKIP] Already posted: {tweet['tweet_url']}")
                continue

            # Translate
            translated = translator.translate_to_malay(tweet["raw_text"])
            if not translated or translated == "Translation failed":
                print(f"[SKIP] Translation failed for {tweet['tweet_url']}")
                continue

            success = False
            video_path = None
            img_paths = []

            try:
                if tweet.get("video"):
                    # Download video
                    video_path = f"temp_{tweet['id']}.mp4"
                    with session.get(tweet["video"], timeout=120) as r:
                        r.raise_for_status()
                        with open(video_path, "wb") as f:
                            f.write(r.content)
                    success = post_video_to_fb(video_path, translated)

                elif tweet["images"]:
                    # Download images
                    for j, url in enumerate(tweet["images"]):
                        try:
                            path = f"temp_{tweet['id']}_{j}.jpg"
                            with session.get(url, timeout=60) as r:
                                r.raise_for_status()
                                with open(path, "wb") as f:
                                    f.write(r.content)
                            img_paths.append(path)
                        except Exception as e:
                            print("[Image DL Error]", e)

                    if img_paths:
                        success = post_photos_to_fb(img_paths, translated)
                    else:
                        # fallback to text-only if media download failed
                        success = post_text_only_to_fb(translated)

                else:
                    success = post_text_only_to_fb(translated)

                if success:
                    results.append({
                        "id": tweet["id"],
                        "tweet_url": tweet["tweet_url"],
                        "original_text": tweet["raw_text"],
                        "translated_caption": translated,
                        "images": tweet["images"],
                        "video": tweet["video"],
                        "fb_status": "Posted",
                        "date_posted": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    print(f"[✅ POSTED] {tweet['tweet_url']}")
                else:
                    print(f"[❌ FAILED] {tweet['tweet_url']}")

            finally:
                # cleanup temp files
                if video_path and os.path.exists(video_path):
                    try:
                        os.remove(video_path)
                    except Exception:
                        pass
                for p in img_paths:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass

            # be nice to APIs
            time.sleep(1)

    if results:
        log_result(results)
        print(f"[✅ LOGGED] {len(results)} entries added.")
    else:
        print("[⚠️ NOTHING TO POST]")

if __name__ == "__main__":
    fetch_and_post_tweets()

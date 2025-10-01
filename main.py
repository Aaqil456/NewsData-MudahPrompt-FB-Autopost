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
# ENV (required)
# =========================
NEWSDATA_API_KEY = os.getenv("NEWSDATA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
LONG_LIVED_USER_TOKEN = os.getenv("LONG_LIVED_USER_TOKEN")

RESULT_FILE = "results.json"
MAX_ITEMS = 10
FIXED_QUERY = "AI Automation"

# Simple default headers (helps avoid 403 on some CDNs)
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

# =========================
# Helpers: results.json (DEDUPE BY article_id)
# =========================
def load_posted_ids():
    """Return a set of article_ids already logged."""
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            return set(entry["article_id"] for entry in json.load(f) if entry.get("article_id"))
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
# Text cleaners
# =========================
PAID_PLACEHOLDERS = {
    "ONLY AVAILABLE IN PAID PLANS",
    "ONLY AVAILABLE IN PROFESSIONAL AND CORPORATE PLANS",
    "ONLY AVAILABLE IN CORPORATE PLANS"
}

PRESSWIRE_PREFIXES = [
    r"^\(MENAFN\s*-\s*[^)]+\)\s*",
    r"^EINPresswire/\s*--\s*",
    r"^GlobeNewsWire\s*-\s*Nasdaq\s*-\s*",
]

_presswire_regex = re.compile("|".join(PRESSWIRE_PREFIXES), flags=re.IGNORECASE)

def _is_paid_placeholder(text: str) -> bool:
    if not text:
        return False
    t = str(text).strip()
    for ph in PAID_PLACEHOLDERS:
        if ph.lower() in t.lower():
            return True
    return False

def _is_nullish(text: str) -> bool:
    if text is None:
        return True
    t = str(text).strip().lower()
    return t in {"", "null", "none", "n/a"}

def _strip_presswire_boilerplate(s: str) -> str:
    if not s:
        return s
    s = _presswire_regex.sub("", s.strip())
    return re.sub(r"\s+", " ", s).strip()

# =========================
# Gemini Translator (new SDK)
# =========================
class GeminiTranslator:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is missing.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )

    @staticmethod
    def _clean_text(text: str) -> str:
        # Strip URLs and reduce whitespace
        text = re.sub(r'https?://\S+|http?://\S+', '', text or "")
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def translate_description(self, description: str, retries: int = 3, backoff: float = 1.5) -> str:
        """
        Translate description into Malay (casual FB caption).
        If obviously an ad, return a short Malay fun fact about prompt engineering.
        """
        if _is_nullish(description):
            return ""

        cleaned = self._clean_text(description)
        prompt = (
            "Translate the following news description into Malay as a casual, friendly Facebook caption.\n"
            "Use proper, natural language without heavy slang, emojis, or ALL CAPS.\n"
            "Space the lines clearly. Return ONLY the caption (no intro/explanation).\n"
            "If the text is obviously an advertisement (e.g., 'link in bio', coupon codes, giveaways),\n"
            "do NOT translate it. Instead, return ONE short, interesting fun fact about prompt engineering in Malay.\n\n"
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
            headers=HTTP_HEADERS,
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
        headers=HTTP_HEADERS,
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
                headers=HTTP_HEADERS,
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
        headers=HTTP_HEADERS,
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
            headers=HTTP_HEADERS,
            timeout=600
        )
    if r.status_code != 200:
        print("[FB Video Post Error]", r.text[:300])
    return r.status_code == 200

# =========================
# NewsData.io fetcher (fixed query, no language, max_items=10)
# =========================
def fetch_news_newsdata(max_items: int = MAX_ITEMS):
    """
    Fetch news from NewsData.io /api/1/latest with fixed query "AI Automation".
    Return list of dicts with exact fields:
    { "article_id", "title", "link", "description", "image_url", "video_url" }
    Skip items that contain paid-plan placeholders.
    """
    if not NEWSDATA_API_KEY:
        raise RuntimeError("NEWSDATA_API_KEY is missing.")

    url = "https://newsdata.io/api/1/latest"
    params = {
        "apikey": NEWSDATA_API_KEY,
        "q": FIXED_QUERY
    }
    try:
        r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=60)
        if r.status_code != 200:
            print("[NewsData ERROR]", r.text[:400])
            return []

        payload = r.json()
        results = payload.get("results", []) or []

        items = []
        for res in results:
            article_id = res.get("article_id")
            title = res.get("title")
            link = res.get("link")
            description = res.get("description")
            image_url = res.get("image_url")
            video_url = res.get("video_url")  # may be None

            # Require id + link, skip paid placeholders
            if _is_nullish(article_id) or _is_nullish(link):
                continue
            if _is_paid_placeholder(description) or _is_paid_placeholder(title):
                continue

            # Clean (optional)
            if not _is_nullish(title):
                title = _strip_presswire_boilerplate(title)
            if not _is_nullish(description):
                description = _strip_presswire_boilerplate(description)

            items.append({
                "article_id": str(article_id),
                "title": title if not _is_nullish(title) else None,
                "link": link,
                "description": description if not _is_nullish(description) else None,
                "image_url": image_url if not _is_nullish(image_url) else None,
                "video_url": video_url if not _is_nullish(video_url) else None,
            })

            if len(items) >= max_items:
                break

        return items

    except Exception as e:
        print("[NewsData Fetch Error]", e)
        return []

# =========================
# Main Flow
# =========================
def fetch_translate_post():
    if not FB_PAGE_ID:
        raise RuntimeError("FB_PAGE_ID is missing.")
    if not LONG_LIVED_USER_TOKEN:
        raise RuntimeError("LONG_LIVED_USER_TOKEN is missing.")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    translator = GeminiTranslator(api_key=GEMINI_API_KEY, model="gemini-2.5-flash")

    posted_ids = load_posted_ids()
    results = []
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)

    articles = fetch_news_newsdata(max_items=MAX_ITEMS)
    print(f"[INFO] Total fetched from NewsData.io: {len(articles)}")

    for art in articles:
        aid = art["article_id"]
        if aid in posted_ids:
            print(f"[SKIP] Duplicate (article_id): {aid}")
            continue

        # Translate ONLY the description
        desc = art.get("description") or ""
        translated = translator.translate_description(desc)
        if not translated or translated == "Translation failed":
            print(f"[SKIP] Translation failed for article_id={aid}")
            continue

        # Post logic with guaranteed fallback to text-only
        success = False
        video_path = None
        img_paths = []

        try:
            # Try video first if available
            if art.get("video_url"):
                try:
                    video_path = f"temp_news_{aid}.mp4"
                    with session.get(art["video_url"], timeout=120) as r:
                        r.raise_for_status()
                        with open(video_path, "wb") as f:
                            f.write(r.content)
                    success = post_video_to_fb(video_path, translated)
                except Exception as e:
                    print("[Video DL/Upload Error] Falling back to text-only.", e)
                    success = post_text_only_to_fb(translated)

            # Else try image if available
            elif art.get("image_url"):
                try:
                    path = f"temp_news_{aid}.jpg"
                    with session.get(art["image_url"], timeout=60) as r:
                        r.raise_for_status()
                        with open(path, "wb") as f:
                            f.write(r.content)
                    img_paths.append(path)
                    success = post_photos_to_fb(img_paths, translated)
                    if not success:
                        print("[Photo Upload Error] Falling back to text-only.")
                        success = post_text_only_to_fb(translated)
                except Exception as e:
                    print("[Image DL Error] Falling back to text-only.", e)
                    success = post_text_only_to_fb(translated)

            # If no media → text-only
            else:
                success = post_text_only_to_fb(translated)

            # Log result
            if success:
                results.append({
                    "article_id": art["article_id"],
                    "title": art["title"],
                    "link": art["link"],
                    "description": art["description"],
                    "image_url": art["image_url"],
                    "video_url": art["video_url"],
                    "translated_description": translated,
                    "fb_status": "Posted",
                    "date_posted": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                print(f"[✅ POSTED] article_id={aid}")
            else:
                print(f"[❌ FAILED] article_id={aid}")

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

        time.sleep(1)

    if results:
        log_result(results)
        print(f"[✅ LOGGED] {len(results)} entries added.")
    else:
        print("[⚠️ NOTHING TO POST]")

if __name__ == "__main__":
    fetch_translate_post()

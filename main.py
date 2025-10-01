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
NEWSDATA_API_KEY = os.getenv("NEWSDATA_API_KEY")  # required
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")      # required
FB_PAGE_ID = os.getenv("FB_PAGE_ID")              # required
LONG_LIVED_USER_TOKEN = os.getenv("LONG_LIVED_USER_TOKEN")  # required

RESULT_FILE = "results.json"

# =========================
# Helpers: results.json
# =========================
def load_posted_ids():
    """Return a set of IDs already logged (we'll use article link as the ID)."""
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
        self.config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )

    @staticmethod
    def _clean_text(text: str) -> str:
        # Strip @mentions, URLs, reduce whitespace
        text = re.sub(r'@\w+|https?://\S+|http?://\S+', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def translate_to_malay(self, text: str, retries: int = 3, backoff: float = 1.5) -> str:
        """
        Translate the content into Malay (casual FB caption).
        If obviously an ad, return a short Malay fun fact about prompt engineering.
        """
        if not text or not isinstance(text, str) or not text.strip():
            return ""

        cleaned = self._clean_text(text)
        prompt = (
            "Translate the following news snippet into Malay as a casual, friendly Facebook caption.\n"
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

# =========================
# NewsData.io fetcher (fixed query, no language, max_items=10)
# =========================
def fetch_news_newsdata(max_items: int = 10):
    """
    Fetch news from NewsData.io /api/1/latest with fixed query "AI Automation".
    Returns list of dicts:
    {
      "id": link,                 # used for dedupe
      "raw_text": combined_text,  # title + description/content
      "images": [image_url] or [],
      "article_url": link
    }
    """
    if not NEWSDATA_API_KEY:
        raise RuntimeError("NEWSDATA_API_KEY is missing.")

    url = "https://newsdata.io/api/1/latest"
    params = {
        "apikey": NEWSDATA_API_KEY,
        "q": "AI Automation"   # fixed query per requirement
    }
    try:
        r = requests.get(url, params=params, timeout=60)
        if r.status_code != 200:
            print("[NewsData ERROR]", r.text[:400])
            return []

        payload = r.json()
        results = payload.get("results", []) or []

        items = []
        for res in results:
            link = res.get("link")
            title = res.get("title") or ""
            description = res.get("description") or ""
            content = res.get("content") or ""
            image_url = res.get("image_url")

            if not link or not (title or description or content):
                continue

            # Choose best summary text: title + description (fallback content)
            text_parts = [title.strip()]
            if description and description.strip() and description.strip().lower() != "null":
                text_parts.append(description.strip())
            elif content and content.strip() and content.strip().lower() != "null":
                text_parts.append(content.strip())

            combined = "\n\n".join([p for p in text_parts if p])

            items.append({
                "id": link,  # use link as unique ID
                "raw_text": combined,
                "images": [image_url] if image_url else [],
                "article_url": link
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
def fetch_and_post_news():
    if not FB_PAGE_ID:
        raise RuntimeError("FB_PAGE_ID is missing.")
    if not LONG_LIVED_USER_TOKEN:
        raise RuntimeError("LONG_LIVED_USER_TOKEN is missing.")

    translator = GeminiTranslator(api_key=GEMINI_API_KEY, model="gemini-2.5-flash")

    posted_ids = load_posted_ids()
    results = []
    session = requests.Session()  # reuse TCP connections

    # Fetch from NewsData.io (fixed query, max 10)
    articles = fetch_news_newsdata(max_items=10)
    print(f"[INFO] Total fetched from NewsData.io: {len(articles)}")

    for art in articles:
        if art["id"] in posted_ids:
            print(f"[SKIP] Already posted: {art['article_url']}")
            continue

        # Translate
        translated = translator.translate_to_malay(art["raw_text"])
        if not translated or translated == "Translation failed":
            print(f"[SKIP] Translation failed for {art['article_url']}")
            continue

        # Decide posting mode
        success = False
        img_paths = []
        try:
            if art["images"]:
                # Download image(s) (NewsData usually provides one)
                for j, url in enumerate(art["images"]):
                    if not url:
                        continue
                    try:
                        path = f"temp_news_{int(time.time())}_{j}.jpg"
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
                    success = post_text_only_to_fb(translated)
            else:
                success = post_text_only_to_fb(translated)

            if success:
                results.append({
                    "id": art["id"],  # link
                    "article_url": art["article_url"],
                    "original_text": art["raw_text"],
                    "translated_caption": translated,
                    "images": art["images"],
                    "video": None,
                    "fb_status": "Posted",
                    "date_posted": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                print(f"[✅ POSTED] {art['article_url']}")
            else:
                print(f"[❌ FAILED] {art['article_url']}")

        finally:
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
    fetch_and_post_news()

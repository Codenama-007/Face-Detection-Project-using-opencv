from dotenv import load_dotenv

load_dotenv()

from duckduckgo_search import DDGS
import os
import io
import time
import base64
import hashlib
import requests

from PIL import Image
from tqdm import tqdm

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage


# ============================================================
# CONFIG
# ============================================================

OUTPUT_DIR = "ExamCheatingDataset/candidates"

# class_name -> (search queries, description used to prompt Gemini for validation)
TARGETS = {
    "cheating": {
        "queries": [
            "student cheating during exam",
            "student using cheat sheet exam",
            "student cheating classroom",
            "student copying answers exam",
            "student looking at hidden notes exam",
        ],
        "description": (
            "a student appearing to cheat during an exam — for example looking at "
            "hidden notes, a cheat sheet, another student's paper, or a phone"
        ),
    },
    "giving object": {
        "queries": [
            "student passing object during exam",
            "student giving object to another student exam",
            "students exchanging object classroom",
            "student passing paper during examination",
            "student handing item to another student exam",
        ],
        "description": (
            "one student physically passing or handing an object (paper, note, "
            "phone, pen, etc.) to another student during an exam or in a classroom"
        ),
    },
}

IMAGES_PER_QUERY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120.0 Safari/537.36"
    )
}

# ============================================================
# GEMINI VALIDATION (via langchain-google-genai)
# ============================================================

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "No Gemini API key found. Set GOOGLE_API_KEY (or GEMINI_API_KEY) in your .env file."
    )

_llm = ChatGoogleGenerativeAI(
    model=GEMINI_MODEL,
    google_api_key=GEMINI_API_KEY,
    temperature=0,
)

# How many times to retry a Gemini call before giving up on an image
GEMINI_MAX_RETRIES = 2
GEMINI_RETRY_DELAY = 3  # seconds


def _image_to_data_url(image, quality=85):
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def validate_image_with_gemini(image, description):
    """
    Asks Gemini whether the image actually matches the target class description.
    Returns True/False. Returns None if the call failed after retries (caller
    decides whether to keep or skip on uncertainty).
    """
    prompt = (
        "You are labeling images for a dataset used to train an exam-proctoring "
        "computer vision model.\n\n"
        f"Does this image clearly show {description}?\n\n"
        "Answer with exactly one word: YES or NO. "
        "If the image is unrelated, a stock photo with no such action, a logo, "
        "text-only, too ambiguous to tell, contains nudity or sexual content, "
        "or is otherwise inappropriate/not a genuine classroom photo, answer NO."
    )

    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": _image_to_data_url(image)},
        ]
    )

    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            response = _llm.invoke([message])
            answer = (response.content or "").strip().upper()
            return answer.startswith("YES")
        except Exception as e:
            if attempt < GEMINI_MAX_RETRIES:
                time.sleep(GEMINI_RETRY_DELAY)
            else:
                print(f"Gemini validation failed after retries: {e}")
                return None


# ============================================================
# CREATE DIRECTORIES
# ============================================================

for class_name in TARGETS:
    os.makedirs(os.path.join(OUTPUT_DIR, class_name), exist_ok=True)
    os.makedirs(os.path.join(OUTPUT_DIR, class_name, "_rejected"), exist_ok=True)


# ============================================================
# DOWNLOAD IMAGE
# ============================================================

def download_image(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)

        if response.status_code != 200:
            return None

        image = Image.open(io.BytesIO(response.content))
        image = image.convert("RGB")

        width, height = image.size
        if width < 200 or height < 200:
            return None

        return image

    except Exception:
        return None


# ============================================================
# SEARCH IMAGES
# ============================================================

def search_images(query, limit=100):
    results = []
    try:
        with DDGS() as ddgs:
            search_results = ddgs.images(
                keywords=query,
                max_results=limit,
                safesearch="on",  # strictest option; default is "moderate"
            )
            for result in search_results:
                image_url = result.get("image")
                if image_url:
                    results.append(image_url)
    except Exception as e:
        print(f"Search failed: {query} -> {e}")

    return results


# ============================================================
# HASH IMAGE
# ============================================================

def image_hash(image):
    return hashlib.md5(image.tobytes()).hexdigest()


# ============================================================
# MAIN SCRAPER
# ============================================================

def scrape_class(class_name, queries, description):
    output_path = os.path.join(OUTPUT_DIR, class_name)
    rejected_path = os.path.join(output_path, "_rejected")

    existing_hashes = set()

    for filename in os.listdir(output_path):
        filepath = os.path.join(output_path, filename)
        try:
            image = Image.open(filepath)
            existing_hashes.add(image_hash(image))
        except Exception:
            pass

    counter = len(existing_hashes)
    rejected_counter = 0
    uncertain_counter = 0

    print("\n================================")
    print(f"CLASS: {class_name}")
    print("================================")

    for query in queries:
        print(f"\nSearching: {query}")

        urls = search_images(query, IMAGES_PER_QUERY)
        print(f"Found {len(urls)} candidates")

        for url in tqdm(urls):
            image = download_image(url)

            if image is None:
                continue

            h = image_hash(image)

            if h in existing_hashes:
                continue

            existing_hashes.add(h)

            # ---- Gemini validation gate ----
            verdict = validate_image_with_gemini(image, description)

            if verdict is False:
                rejected_counter += 1
                filename = f"{class_name.replace(' ', '_')}_rej_{rejected_counter:05d}.jpg"
                image.save(os.path.join(rejected_path, filename), "JPEG", quality=90)
                continue

            if verdict is None:
                # Gemini call failed after retries — keep the image but flag it
                # in the filename so you can review it manually later.
                uncertain_counter += 1

            counter += 1
            tag = "_uncertain" if verdict is None else ""
            filename = f"{class_name.replace(' ', '_')}_{counter:05d}{tag}.jpg"
            filepath = os.path.join(output_path, filename)
            image.save(filepath, "JPEG", quality=90)

            # Small delay to stay within Gemini/API rate limits
            time.sleep(0.5)

    print(
        f"\nSaved {counter} images for {class_name} "
        f"({rejected_counter} rejected by Gemini, {uncertain_counter} unverified)"
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    for class_name, cfg in TARGETS.items():
        scrape_class(class_name, cfg["queries"], cfg["description"])

    print("\n================================")
    print("SCRAPING COMPLETE")
    print("================================")
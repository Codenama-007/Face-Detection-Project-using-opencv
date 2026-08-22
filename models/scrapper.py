# from dotenv import load_dotenv


# load_dotenv()
from duckduckgo_search import DDGS
import os
import io
import time
import hashlib
import requests

from PIL import Image
from bs4 import BeautifulSoup
from tqdm import tqdm


# ============================================================
# CONFIG
# ============================================================

OUTPUT_DIR = "ExamCheatingDataset/candidates"

TARGETS = {
    "cheating": [
        "student cheating during exam",
        "student using cheat sheet exam",
        "student cheating classroom",
        "student copying answers exam",
        "student looking at hidden notes exam"
    ],

    "giving object": [
        "student passing object during exam",
        "student giving object to another student exam",
        "students exchanging object classroom",
        "student passing paper during examination",
        "student handing item to another student exam"
    ]
}

IMAGES_PER_QUERY = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120.0 Safari/537.36"
    )
}


# ============================================================
# CREATE DIRECTORIES
# ============================================================

for class_name in TARGETS:

    os.makedirs(
        os.path.join(
            OUTPUT_DIR,
            class_name
        ),
        exist_ok=True
    )


# ============================================================
# DOWNLOAD IMAGE
# ============================================================

def download_image(url):

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=10
        )

        if response.status_code != 200:
            return None

        image = Image.open(
            io.BytesIO(response.content)
        )

        # Convert to RGB
        image = image.convert("RGB")

        # Reject tiny images
        width, height = image.size

        if width < 200 or height < 200:
            return None

        return image

    except Exception:
        return None


# ============================================================
# SEARCH BING IMAGES
# ============================================================

def search_images(query, limit=100):

    results = []

    try:
        with DDGS() as ddgs:

            search_results = ddgs.images(
                keywords=query,
                max_results=limit
            )

            for result in search_results:

                image_url = result.get("image")

                if image_url:
                    results.append(image_url)

    except Exception as e:

        print(
            f"Search failed: {query} -> {e}"
        )

    return results

# ============================================================
# HASH IMAGE
# ============================================================

def image_hash(image):

    return hashlib.md5(
        image.tobytes()
    ).hexdigest()


# ============================================================
# MAIN SCRAPER
# ============================================================

def scrape_class(class_name, queries):

    output_path = os.path.join(
        OUTPUT_DIR,
        class_name
    )

    existing_hashes = set()

    # Load existing images
    for filename in os.listdir(output_path):

        filepath = os.path.join(
            output_path,
            filename
        )

        try:

            image = Image.open(filepath)

            existing_hashes.add(
                image_hash(image)
            )

        except Exception:
            pass


    counter = len(existing_hashes)

    print("\n================================")
    print(f"CLASS: {class_name}")
    print("================================")


    for query in queries:

        print(
            f"\nSearching: {query}"
        )

        urls = search_images(
            query,
            IMAGES_PER_QUERY
        )

        print(
            f"Found {len(urls)} candidates"
        )


        for url in tqdm(urls):

            image = download_image(url)

            if image is None:
                continue

            h = image_hash(image)

            # Duplicate
            if h in existing_hashes:
                continue

            existing_hashes.add(h)

            counter += 1

            filename = (
                f"{class_name.replace(' ', '_')}"
                f"_{counter:05d}.jpg"
            )

            filepath = os.path.join(
                output_path,
                filename
            )

            image.save(
                filepath,
                "JPEG",
                quality=90
            )

            time.sleep(0.1)


    print(
        f"\nSaved {counter} images "
        f"for {class_name}"
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    for class_name, queries in TARGETS.items():

        scrape_class(
            class_name,
            queries
        )

    print("\n================================")
    print("SCRAPING COMPLETE")
    print("================================")
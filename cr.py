import asyncio
import json
import os
import re
from pathlib import Path
import numpy as np
from playwright.async_api import async_playwright
from collections import deque


# ============================================================
# CONFIGURATION
# ============================================================

MAX_LEVEL = 2
MAX_SCROLLS = 200

SCROLL_DISTANCE = 5000
SCROLL_WAIT_MS = 1500

# Stop scrolling if absolutely nothing new has appeared
# for this many consecutive scrolls.
MAX_IDLE_SCROLLS = 5

BASE_DIR = Path(__file__).resolve().parent

STATE_PATH = BASE_DIR / "state.json"
RES_PATH = BASE_DIR / "res.json"
PROFILE_PATH = BASE_DIR / "pinterest_firefox_profile"


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path, default):
    """Load a JSON file, or return default if it doesn't exist/is invalid."""

    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except (json.JSONDecodeError, EOFError):
        print(f"Warning: {path} is empty or invalid.")
        return default


def save_json(path, data):
    """Safely save JSON."""

    with open(path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=4,
            ensure_ascii=False
        )


# ============================================================
# STATE NORMALIZATION
# ============================================================

def normalize_node(node):
    """
    Make sure every node has the structure:

    {
        "link": "...",
        "pinlist": [...],
        "scraped": True/False
    }

    This also converts old pinlist entries that are just strings
    into proper node dictionaries.
    """

    if not isinstance(node, dict):
        raise ValueError(f"Invalid node: {node}")

    if "link" not in node:
        raise ValueError(f"Node has no 'link': {node}")

    # Old state files won't have "pinlist"
    if "pinlist" not in node:
        node["pinlist"] = []

    # Backward compatibility:
    # if old code saved:
    #
    # "pinlist": [
    #     "https://www.pinterest.com/pin/123/",
    #     ...
    # ]
    #
    # convert them to nodes.
    normalized_pinlist = []

    for child in node["pinlist"]:

        if isinstance(child, str):

            normalized_pinlist.append({
                "link": child,
                "pinlist": [],
                "scraped": False
            })

        elif isinstance(child, dict):

            normalize_node(child)
            normalized_pinlist.append(child)

        else:
            print("Ignoring invalid child:", child)

    node["pinlist"] = normalized_pinlist

    # New state format has an explicit scraped flag.
    #
    # For old files:
    # - non-empty pinlist => assume already scraped
    # - empty pinlist => assume not scraped
    if "scraped" not in node:
        node["scraped"] = len(node["pinlist"]) > 0

    return node


# ============================================================
# PIN EXTRACTION
# ============================================================
async def extract_pin_links(page):

    hrefs = await page.locator(
        'a[href*="/pin/"]'
    ).evaluate_all("""
        anchors => anchors.map(a => a.href)
    """)

    pin_links = set()

    for href in hrefs:

        match = re.search(
            r'/pin/(\d+)(?:/|[?#]|$)',
            href
        )

        if match:

            pin_id = match.group(1)

            pin_links.add(
                f"https://fr.pinterest.com/pin/{pin_id}/"
            )

    print(
        f"    Pin anchors currently in DOM: {len(hrefs)}"
    )

    print(
        f"    Valid Pin URLs: {len(pin_links)}"
    )

    

    return pin_links

# ============================================================
# IMAGE EXTRACTION
# ============================================================

def extract_best_image_urls(html):
    """
    Extract Pinterest image URLs and keep one high-resolution
    URL per underlying image.

    Preference:

        originals
        1200x
        736x
        564x

    The same image often appears with several resolutions,
    so we deduplicate using the part after the resolution.
    """

    urls = re.findall(
        r'https?://i\.pinimg\.com/[^"\'\s\\]+',
        html
    )

    # Higher number = better preference
    resolution_priority = {
        "originals": 4,
        "1200x": 3,
        "736x": 2,
        "564x": 1,
    }

    best_images = {}

    for url in urls:

        # Find resolution directory
        match = re.search(
            r'https?://i\.pinimg\.com/([^/]+)/(.+)',
            url
        )

        if not match:
            continue

        resolution = match.group(1)
        image_path = match.group(2)

        if resolution not in resolution_priority:
            continue

        priority = resolution_priority[resolution]

        # Use the underlying image path as the deduplication key
        key = image_path

        if (
            key not in best_images
            or priority > best_images[key][0]
        ):
            best_images[key] = (
                priority,
                url
            )

    return {
        image_url
        for _, image_url in best_images.values()
    }


# ============================================================
# RES.JSON
# ============================================================

def add_images_to_res(image_urls):
    """
    Add new image URLs to res.json without duplicates.
    """

    data = load_json(
        RES_PATH,
        {"res": []}
    )

    if "res" not in data or not isinstance(data["res"], list):
        data["res"] = []

    existing_urls = {
        item["img"]
        for item in data["res"]
        if isinstance(item, dict) and "img" in item
    }

    # Find next available ID
    existing_ids = [
        item["id"]
        for item in data["res"]
        if isinstance(item, dict)
        and isinstance(item.get("id"), int)
    ]

    if existing_ids:
        next_id = max(existing_ids) + 1
    else:
        next_id = 0

    added = 0

    for image_url in sorted(image_urls):

        if image_url in existing_urls:
            continue

        data["res"].append({
            "id": next_id,
            "img": image_url
        })

        existing_urls.add(image_url)

        next_id += 1
        added += 1

    save_json(RES_PATH, data)

    return added, len(data["res"])


# ============================================================
# SCRAPE ONE PAGE
# ============================================================

async def scrape(page, link):
    """
    Visit one Pinterest search/pin page and collect:

        - image URLs
        - child Pin URLs
    """

    print()
    print("==============================================")
    print("SCRAPING")
    print(link)
    print("==============================================")

    await page.goto(
        link,
        wait_until="domcontentloaded",
        timeout=60000
    )
    # await page.wait_for_timeout(100000)
    print("Page loaded:", await page.title())
    
    # Initial loading time
    delay2 = max(-1000, np.random.randn() * 500 + 1000)
    await page.wait_for_timeout(4000)

    all_images = set()
    all_pins = set()

    idle_scrolls = 0

    # ========================================================
    # SCROLL
    # ========================================================

    for i in range(MAX_SCROLLS):

        print(
            f"Scrolling {i + 1}/{MAX_SCROLLS}, ",
            end=""
        )

        # Scroll
        rdm_dist = max(-500, np.random.randn() * 500)
        await page.mouse.wheel(
            0,
            SCROLL_DISTANCE + rdm_dist
        )

        delay = max(-500, np.random.randn() * 500)
        # Let Pinterest load more content
        await page.wait_for_timeout(
            SCROLL_WAIT_MS + delay
        )

        # Get current DOM/HTML
        html = await page.content()

        images = extract_best_image_urls(html)
        pins = await extract_pin_links(page)

        previous_images = len(all_images)
        previous_pins = len(all_pins)

        all_images.update(images)
        all_pins.update(pins)

        new_images = len(all_images) - previous_images
        new_pins = len(all_pins) - previous_pins

        print(
            f"new images: {new_images}, "
            f"new pins: {new_pins}, "
            f"total images: {len(all_images)}, "
            f"total pins: {len(all_pins)}"
        )

        # Stop if nothing new is appearing
        if new_images == 0 and new_pins == 0:
            idle_scrolls += 1
        else:
            idle_scrolls = 0

        if idle_scrolls >= MAX_IDLE_SCROLLS:

            print(
                f"\nNo new content for "
                f"{MAX_IDLE_SCROLLS} consecutive scrolls."
            )

            break

    # ========================================================
    # REMOVE THE CURRENT PAGE FROM ITS OWN PINLIST
    # ========================================================

    current_match = re.search(
        r'/pin/(\d+)',
        link
    )

    current_pin_id = (
        current_match.group(1)
        if current_match
        else None
    )

    if current_pin_id is not None:

        all_pins = {
            pin
            for pin in all_pins
            if f"/pin/{current_pin_id}/" not in pin
        }

    print()
    print("==============================================")
    print("SCRAPE COMPLETE")
    print(f"Images found: {len(all_images)}")
    print(f"Pins found:   {len(all_pins)}")
    print("==============================================")

    return all_images, all_pins


# ============================================================
# CRAWLER
# ============================================================

async def crawl(level, node, page):
    """
    Find and scrape ONE unprocessed node using Breadth-First Search.

    Returns:
        True  -> one node was successfully processed
        False -> no unprocessed node exists within MAX_LEVEL
    """

    # FIFO queue: (level, node)
    queue = deque([(level, node)])

    while queue:
        current_level, current_node = queue.popleft()

        # Never scrape beyond MAX_LEVEL.
        if current_level > MAX_LEVEL:
            continue

        print()
        print(
            f"[LEVEL {current_level}] "
            f"{current_node['link']} "
            f"| scraped={current_node.get('scraped', False)} "
            f"| children={len(current_node.get('pinlist', []))}"
        )

        # ========================================================
        # THIS NODE HAS NOT BEEN SCRAPED YET
        # ========================================================

        if not current_node.get("scraped", False):

            images, pins = await scrape(
                page,
                current_node["link"]
            )

            # Save images
            added, total = add_images_to_res(images)

            print(
                f"Added {added} new images "
                f"to res.json "
                f"(total: {total})"
            )

            # Create child nodes
            current_node["pinlist"] = [
                {
                    "link": pin,
                    "pinlist": [],
                    "scraped": False
                }
                for pin in sorted(pins)
            ]

            # The page was processed successfully, even if it
            # contained no child Pins.
            current_node["scraped"] = True

            print(
                f"Created {len(current_node['pinlist'])} child nodes."
            )

            # Process only ONE node per invocation.
            return True

        # ========================================================
        # NODE ALREADY SCRAPED
        # -> ADD ITS CHILDREN TO THE END OF THE QUEUE
        # ========================================================

        if current_level < MAX_LEVEL:
            for child in current_node.get("pinlist", []):
                queue.append((current_level + 1, child))

    # No unprocessed nodes remain within MAX_LEVEL.
    return False


# ============================================================
# MAIN
# ============================================================

async def main():

    # --------------------------------------------------------
    # Load state
    # --------------------------------------------------------

    state = load_json(
        STATE_PATH,
        None
    )

    if state is None:
        raise FileNotFoundError(
            f"Could not load {STATE_PATH}"
        )

    normalize_node(state)

    print("state loaded ")

    # --------------------------------------------------------
    # Start Firefox once
    # --------------------------------------------------------

    async with async_playwright() as p:

        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE_PATH),
            headless=True
        )

        page = await context.new_page()
        
        # ----------------------------------------------------
        # Process EXACTLY ONE missing node
        # ----------------------------------------------------

        processed = await crawl(
            0,
            state,
            page
        )

        # ----------------------------------------------------
        # Save state after the crawl
        # ----------------------------------------------------

        save_json(
            STATE_PATH,
            state
        )

        if processed:

            print()
            print("==============================================")
            print("ONE NODE PROCESSED")
            print("state.json saved")
            print("==============================================")

        else:

            print()
            print("==============================================")
            print("NOTHING LEFT TO CRAWL")
            print("==============================================")

        await context.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
#!/usr/bin/env python3
"""StubHub ticket price monitor with ntfy push notifications."""

import base64
import json
import logging
import os
import random
import re
import signal
import sys
import time
import urllib.parse

import requests
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

EVENT_ID = "107094148"
EVENT_URL = (
    "https://www.stubhub.co.uk/ufc-fight-night-evolev-vs-murphy-tickets"
    "-the-o2-arena-21-3-2026/event/107094148/"
)
PRICE_THRESHOLD = float(os.getenv("PRICE_THRESHOLD", "200"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else ""
NOTIFICATION_COOLDOWN = 1800  # 30 minutes between repeated alerts

STUBHUB_API_BASE = "https://api.stubhub.net"
STUBHUB_TOKEN_URL = "https://account.stubhub.com/oauth2/token"
CLIENT_ID = os.getenv("STUBHUB_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("STUBHUB_CLIENT_SECRET", "")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="[%(asctime)s] [%(levelname)-5s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("stubhub_monitor")

# ── Globals ───────────────────────────────────────────────────────────────────

_cached_token = None
_token_expiry = 0
_shutting_down = False


def _handle_signal(signum, frame):
    global _shutting_down
    _shutting_down = True
    log.info("Shutdown signal received, exiting gracefully...")
    sys.exit(0)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Tier A: StubHub Catalog API ───────────────────────────────────────────────


def get_oauth_token():
    """Get an OAuth2 bearer token using client credentials flow."""
    global _cached_token, _token_expiry

    if _cached_token and time.time() < _token_expiry - 300:
        return _cached_token

    if not CLIENT_ID or not CLIENT_SECRET:
        return None

    encoded_id = urllib.parse.quote(CLIENT_ID, safe="")
    encoded_secret = urllib.parse.quote(CLIENT_SECRET, safe="")
    credentials = base64.b64encode(
        f"{encoded_id}:{encoded_secret}".encode()
    ).decode()

    resp = requests.post(
        STUBHUB_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=read:events",
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    _cached_token = data["access_token"]
    _token_expiry = time.time() + data.get("expires_in", 86400)
    log.info("OAuth token refreshed (expires in %ds)", data.get("expires_in", 0))
    return _cached_token


def fetch_min_price_api():
    """Tier A: Fetch minimum ticket price from the official StubHub API."""
    token = get_oauth_token()
    if not token:
        return None

    resp = requests.get(
        f"{STUBHUB_API_BASE}/catalog/events/{EVENT_ID}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/hal+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    min_price = data.get("min_ticket_price", {})
    amount = min_price.get("amount")
    if amount is not None:
        return float(amount)
    return None


# ── Tier B: HTML Scraping ─────────────────────────────────────────────────────


def fetch_min_price_scrape():
    """Tier B: Scrape the event page for __INITIAL_STATE__ pricing data."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
        }
    )

    resp = session.get(EVENT_URL, timeout=30)
    resp.raise_for_status()

    # Try to extract __INITIAL_STATE__ or __NEXT_DATA__ JSON
    patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>",
        r"window\.__NEXT_DATA__\s*=\s*({.*?});\s*</script>",
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.*?})\s*</script>',
    ]

    raw_json = None
    for pattern in patterns:
        match = re.search(pattern, resp.text, re.DOTALL)
        if match:
            raw_json = match.group(1)
            break

    if not raw_json:
        log.warning("Tier B: Could not find embedded JSON data in page")
        return None

    data = json.loads(raw_json)

    # Search recursively for price-related fields
    prices = _extract_prices(data)
    if prices:
        min_price = min(prices)
        log.info("Tier B: Found %d prices, min = %.2f", len(prices), min_price)
        return min_price

    log.warning("Tier B: Parsed JSON but found no prices")
    return None


def _extract_prices(obj, depth=0):
    """Recursively search a JSON structure for ticket prices in GBP."""
    if depth > 15:
        return []

    prices = []

    if isinstance(obj, dict):
        # Look for common price field patterns
        for key in ("amount", "price", "currentPrice", "listingPrice", "rawPrice",
                     "minPrice", "min_ticket_price", "totalPrice"):
            val = obj.get(key)
            if isinstance(val, (int, float)) and 1 < val < 50000:
                prices.append(float(val))
            elif isinstance(val, str):
                try:
                    num = float(val.replace("£", "").replace(",", "").strip())
                    if 1 < num < 50000:
                        prices.append(num)
                except ValueError:
                    pass
            elif isinstance(val, dict):
                inner = val.get("amount") or val.get("value")
                if isinstance(inner, (int, float)) and 1 < inner < 50000:
                    prices.append(float(inner))

        # Recurse into values
        for v in obj.values():
            prices.extend(_extract_prices(v, depth + 1))

    elif isinstance(obj, list):
        for item in obj:
            prices.extend(_extract_prices(item, depth + 1))

    return prices


# ── Tier C: Playwright Headless Browser ───────────────────────────────────────


def fetch_min_price_playwright():
    """Tier C: Use a headless browser to extract prices."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Tier C: Playwright not installed, skipping")
        return None

    prices = []

    def handle_response(response):
        """Intercept API responses that contain listing/price data."""
        url = response.url
        if any(kw in url for kw in ("listing", "inventory", "search", "catalog",
                                     "event", "offer", "ticket", "price")):
            try:
                if "json" in response.headers.get("content-type", ""):
                    body = response.json()
                    found = _extract_prices(body)
                    prices.extend(found)
            except Exception:
                pass

    min_price = None
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1440, "height": 900},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            page = context.new_page()
            page.on("response", handle_response)

            # Use domcontentloaded instead of networkidle to avoid timeout
            page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=30000)

            # Wait a bit for JS to render and API calls to complete
            page.wait_for_timeout(8000)

            # Try extracting prices from the rendered DOM
            # Look for price text anywhere on the page matching £XX.XX pattern
            body_text = page.inner_text("body")
            for match in re.finditer(r"£\s*([\d,]+(?:\.\d{2})?)", body_text):
                try:
                    val = float(match.group(1).replace(",", ""))
                    if 1 < val < 50000:
                        prices.append(val)
                except ValueError:
                    pass

            # Also try specific selectors
            for selector in ['[data-testid*="price"]', '[class*="rice"]',
                             '[class*="amount"]', '[class*="cost"]']:
                try:
                    elements = page.query_selector_all(selector)
                    for el in elements:
                        text = el.inner_text()
                        for m in re.finditer(r"£\s*([\d,]+(?:\.\d{2})?)", text):
                            try:
                                val = float(m.group(1).replace(",", ""))
                                if 1 < val < 50000:
                                    prices.append(val)
                            except ValueError:
                                pass
                except Exception:
                    pass

            browser.close()
            browser = None

    except Exception as e:
        log.warning("Tier C: Playwright error: %s", e)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        return None

    if prices:
        # Deduplicate and get minimum
        unique_prices = list(set(prices))
        min_price = min(unique_prices)
        log.info("Tier C: Found %d unique prices, min = £%.2f", len(unique_prices), min_price)

    return min_price


# ── Price Fetching Orchestrator ───────────────────────────────────────────────


def get_lowest_price():
    """Try all tiers in order and return the lowest price found."""
    # Tier A: Official API
    if CLIENT_ID and CLIENT_SECRET:
        try:
            price = fetch_min_price_api()
            if price is not None:
                log.info("Tier A (API): Lowest price = £%.2f", price)
                return price, "A"
        except Exception as e:
            log.warning("Tier A failed: %s", e)

    # Tier B: HTML scraping
    try:
        price = fetch_min_price_scrape()
        if price is not None:
            return price, "B"
    except Exception as e:
        log.warning("Tier B failed: %s", e)

    # Tier C: Playwright
    try:
        price = fetch_min_price_playwright()
        if price is not None:
            return price, "C"
    except Exception as e:
        log.warning("Tier C failed: %s", e)

    return None, None


# ── Notifications ─────────────────────────────────────────────────────────────


def send_notification(price):
    """Send a push notification via ntfy.sh."""
    if not NTFY_URL:
        log.warning("NTFY_TOPIC not set, skipping notification")
        return False

    try:
        resp = requests.post(
            NTFY_URL,
            headers={
                "Title": "StubHub Price Alert - UFC London",
                "Priority": "urgent",
                "Tags": "ticket,rotating_light",
                "Click": EVENT_URL,
            },
            data=f"UFC London ticket at £{price:.2f}! (Threshold: £{PRICE_THRESHOLD:.2f})\n{EVENT_URL}",
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Notification sent successfully")
            return True
        else:
            log.warning("ntfy returned status %d", resp.status_code)
            return False
    except Exception as e:
        log.warning("Failed to send notification: %s", e)
        return False


# ── Single Check (--once mode) ────────────────────────────────────────────────


def check_once():
    """Run a single price check, notify if threshold met, then exit."""
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC not set — please configure it first")
        sys.exit(1)

    log.info("Single check | Event: UFC London | Threshold: £%.2f", PRICE_THRESHOLD)

    price, tier = get_lowest_price()

    if price is None:
        log.warning("All tiers failed to get price")
        sys.exit(1)

    log.info("Tier %s | Lowest: £%.2f", tier, price)

    if price <= PRICE_THRESHOLD:
        sent = send_notification(price)
        if sent:
            log.info("ALERT SENT — price £%.2f is at or below £%.2f", price, PRICE_THRESHOLD)
        else:
            log.error("Failed to send notification")
            sys.exit(1)
    else:
        log.info("No alert — price £%.2f is above £%.2f threshold", price, PRICE_THRESHOLD)


# ── Loop Mode (for GitHub Actions hourly trigger) ────────────────────────────

LOOP_DURATION = int(os.getenv("LOOP_DURATION", "3300"))  # 55 minutes default


def check_loop():
    """Run price checks every 3 minutes for ~55 minutes, then exit."""
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC not set — please configure it first")
        sys.exit(1)

    log.info("=" * 60)
    log.info("StubHub Price Monitor — Loop Mode")
    log.info("Event: UFC London | Threshold: £%.2f", PRICE_THRESHOLD)
    log.info("Will check every %ds for %d minutes", CHECK_INTERVAL, LOOP_DURATION // 60)
    log.info("=" * 60)

    start_time = time.time()
    last_notified = None
    check_count = 0

    while time.time() - start_time < LOOP_DURATION and not _shutting_down:
        check_count += 1
        elapsed = int(time.time() - start_time)

        try:
            price, tier = get_lowest_price()

            if price is not None:
                if price <= PRICE_THRESHOLD:
                    cooldown_remaining = 0
                    if last_notified:
                        cooldown_remaining = NOTIFICATION_COOLDOWN - (time.time() - last_notified)

                    if cooldown_remaining <= 0:
                        sent = send_notification(price)
                        if sent:
                            last_notified = time.time()
                        log.info(
                            "Check #%d [%dm] | Tier %s | £%.2f | ALERT SENT",
                            check_count, elapsed // 60, tier, price,
                        )
                    else:
                        log.info(
                            "Check #%d [%dm] | Tier %s | £%.2f | Cooldown (%dm left)",
                            check_count, elapsed // 60, tier, price,
                            int(cooldown_remaining / 60),
                        )
                else:
                    log.info(
                        "Check #%d [%dm] | Tier %s | £%.2f | Above threshold",
                        check_count, elapsed // 60, tier, price,
                    )
            else:
                log.warning("Check #%d [%dm] | All tiers failed", check_count, elapsed // 60)

        except Exception as e:
            log.error("Check #%d [%dm] | Error: %s", check_count, elapsed // 60, e)

        # Sleep until next check, but don't sleep past the loop duration
        remaining = LOOP_DURATION - (time.time() - start_time)
        if remaining > CHECK_INTERVAL:
            time.sleep(CHECK_INTERVAL)
        else:
            break

    log.info("Loop complete — %d checks in %d minutes", check_count, int((time.time() - start_time) / 60))


# ── Main Loop (for local use) ────────────────────────────────────────────────


def main():
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC not set in .env — please configure it first")
        sys.exit(1)

    log.info("=" * 60)
    log.info("StubHub Ticket Price Monitor")
    log.info("Event: UFC Fight Night - Evolev vs Murphy")
    log.info("Venue: The O2 Arena, London | Date: 21 March 2026")
    log.info("Threshold: £%.2f | Interval: %ds", PRICE_THRESHOLD, CHECK_INTERVAL)
    log.info("Notifications: ntfy.sh/%s", NTFY_TOPIC)
    log.info("API credentials: %s", "configured" if CLIENT_ID else "not set (Tier A disabled)")
    log.info("=" * 60)

    last_notified = None
    check_count = 0
    backoff = CHECK_INTERVAL

    while not _shutting_down:
        check_count += 1

        try:
            price, tier = get_lowest_price()

            if price is not None:
                if price <= PRICE_THRESHOLD:
                    cooldown_remaining = 0
                    if last_notified:
                        cooldown_remaining = NOTIFICATION_COOLDOWN - (time.time() - last_notified)

                    if cooldown_remaining <= 0:
                        sent = send_notification(price)
                        if sent:
                            last_notified = time.time()
                        log.info(
                            "Check #%d | Tier %s | Lowest: £%.2f | ALERT SENT",
                            check_count, tier, price,
                        )
                    else:
                        log.info(
                            "Check #%d | Tier %s | Lowest: £%.2f | Alert cooldown (%d min remaining)",
                            check_count, tier, price, int(cooldown_remaining / 60),
                        )
                else:
                    log.info(
                        "Check #%d | Tier %s | Lowest: £%.2f | No alert",
                        check_count, tier, price,
                    )
                backoff = CHECK_INTERVAL  # Reset backoff on success
            else:
                log.warning("Check #%d | All tiers failed to get price", check_count)
                backoff = min(backoff * 2, 900)  # Exponential backoff, max 15 min

        except Exception as e:
            log.error("Check #%d | Unexpected error: %s", check_count, e)
            backoff = min(backoff * 2, 900)

        time.sleep(backoff)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        check_loop()
    elif "--once" in sys.argv:
        check_once()
    else:
        main()

#!/usr/bin/env python3
"""
eBay Store Scraper — Production Grade
Scrapes all products from any eBay store with full pagination, anti-block stealth,
duplicate detection, and CSV export.

Usage: python ebay_scraper.py [STORE_URL]
       Default: https://www.ebay.com/str/ninjanodedc
"""

import csv
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("ebay_scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)
log = logging.getLogger("ebay_scraper")

STORE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.ebay.com/str/ninjanodedc"

_STORE_NAME = re.search(r'/str/([^/?]+)', STORE_URL)
_STORE_NAME = _STORE_NAME.group(1) if _STORE_NAME else "store"
OUTPUT_CSV = f"{_STORE_NAME}_products.csv"
MAX_RETRIES = 4
PAGE_TIMEOUT = 30
DETAIL_TIMEOUT = 15
MIN_DELAY = 0.3
MAX_DELAY = 1.0
DETAIL_MIN_DELAY = 0.15
DETAIL_MAX_DELAY = 0.5
MAX_WORKERS = 15
MAX_PAGES = 500

# Shared state for UI progress tracking (updated by ListingScraper and main)
SCRAPE_STATE = {
    "phase": "",           # "listing", "details", "done"
    "page": 0,
    "products_found": 0,
    "detail_progress": 0,
    "detail_total": 0,
    "total": 0,            # total products after listing phase
    "running": False,
    "done": False,
    "error": None,
    "output_file": None,
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


def rand_delay(a=MIN_DELAY, b=MAX_DELAY):
    time.sleep(random.uniform(a, b))


def build_page_url(base, page):
    if page == 1:
        return base.rstrip("/")
    sep = "&" if "?" in base else "?"
    return f"{base.rstrip('/')}{sep}_pgn={page}"


def clean_url(url):
    if not url:
        return ""
    return url.split("?")[0].split("#")[0]


def extract_item_id(url):
    m = re.search(r'/itm/(\d+)', url)
    return m.group(1) if m else ""


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""


def find_chromedriver():
    """Find a system chromedriver, or let Selenium Manager auto-detect."""
    candidates = [
        # Windows — selenium cache
        os.path.expanduser(r"~\.cache\selenium\chromedriver\win64\148.0.7778.178\chromedriver.exe"),
        os.path.expanduser(r"~\.cache\selenium\chromedriver\win64\148.0.7778.167\chromedriver.exe"),
        os.path.expanduser(r"~\.cache\selenium\chromedriver\win64\147.0.7727.117\chromedriver.exe"),
        os.path.expanduser(r"~\.cache\selenium\chromedriver\win64\147.0.7727.56\chromedriver.exe"),
        # Linux — apt
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/lib/chromium/chromedriver",
        # macOS
        "/usr/local/bin/chromedriver",
        "/opt/homebrew/bin/chromedriver",
    ]
    for p in candidates:
        if os.path.isfile(p):
            log.info(f"Using chromedriver: {p}")
            return p
    # None found — Selenium Manager will handle it automatically
    log.info("No system chromedriver found — Selenium Manager will auto-resolve")
    return None


try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException,
        StaleElementReferenceException,
        WebDriverException,
    )
    SELENIUM_AVAILABLE = True
except ImportError:
    log.error("selenium required: pip install selenium")
    SELENIUM_AVAILABLE = False


# ----- detectors -----
SECURITY_KEYWORDS = ["captcha", "recaptcha", "verify you are human", "robot check",
                     "are you a robot", "prove you're human", "unusual traffic",
                     "automated access", "security measure", "please verify",
                     "ensure you are a human"]

SECURITY_SELECTORS = [
    "iframe[src*='captcha']", "iframe[src*='recaptcha']",
    "#captcha", ".captcha", "iframe[title*='captcha']",
]


def is_security_page(driver):
    title = driver.title.lower()
    if "security measure" in title or "verify" in title:
        return True
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        for kw in SECURITY_KEYWORDS:
            if kw in body:
                return True
        for sel in SECURITY_SELECTORS:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
    except Exception:
        pass
    return False


# =====================================================================
# Phase 1 — Selenium listing scraper
# =====================================================================

class ListingScraper:

    # Store page layout
    STORE_CARD_SELECTORS = [
        "article.str-item-card",
        ".str-item-card",
        ".StoreFrontItemCard",
        ".store-item-card",
        "[data-testid='store-item-card']",
        ".str-card",
        "li.s-item",
    ]

    # Search results layout (fallback)
    SEARCH_SELECTORS = [
        ".s-item",
        ".brwrvr-item",
        "li[data-view*='mi:']",
        ".b-list__items_nolist li",
        "[data-testid='product-card']",
        ".rst-scroll-items li",
        ".srp-results li",
    ]

    PAGINATION_NEXT = [
        "a.pagination__next[href]",
        "a[aria-label*='next page']:not([aria-disabled='true'])",
        "a[aria-label*='Next']:not([aria-disabled='true'])",
        ".pagination__item--next:not(.pagination__item--disabled) a",
        "button.pagination__next:not([disabled])",
        "a.pagination__item[href*='_pgn=']:not([aria-current])",
    ]

    def __init__(self, store_url):
        self.store_url = store_url
        self.seller_name = None
        self.driver = None
        self.seen_urls = set()
        self.products = []

    @property
    def search_url(self):
        """Return the eBay search URL for this seller (more reliable than store page)."""
        if self.seller_name:
            return f"https://www.ebay.com/sch/i.html?_ssn={self.seller_name}"
        return self.store_url

    def _extract_seller_name(self):
        m = re.search(r'/str/([^/?]+)', self.store_url)
        if m:
            self.seller_name = m.group(1)
            log.info(f"Seller: {self.seller_name}")
            return True
        return False

    def _init_driver(self):
        chromedriver_path = find_chromedriver()
        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--log-level=3")
        opts.add_argument("--blink-settings=imagesEnabled=false")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")

        if chromedriver_path:
            service = Service(executable_path=chromedriver_path)
            self.driver = webdriver.Chrome(service=service, options=opts)
        else:
            log.info("No chromedriver path specified, letting Selenium auto-discover...")
            try:
                self.driver = webdriver.Chrome(options=opts)
            except Exception:
                log.error("Selenium could not find ChromeDriver. Install chromedriver or run: pip install webdriver-manager")
                sys.exit(1)

        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
        self.driver.set_page_load_timeout(PAGE_TIMEOUT)
        self.driver.implicitly_wait(2)

    def _quit_driver(self):
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = None

    # ----- extraction -----

    def _extract_from_card(self, card):
        item = {}

        # Title and URL from the link
        for link in card.find_elements(By.CSS_SELECTOR, "a[href*='itm/'], a[href*='sch/']"):
            href = link.get_attribute("href") or ""
            if "ebay.com" in href.lower():
                item["url"] = clean_url(href)
                item["item_id"] = extract_item_id(item["url"])
                txt = link.text.strip()
                if txt:
                    item["title"] = txt
                break

        if not item.get("url"):
            try:
                a = card.find_element(By.CSS_SELECTOR, "a")
                href = a.get_attribute("href") or ""
                if "ebay.com" in href.lower():
                    item["url"] = clean_url(href)
                    item["item_id"] = extract_item_id(item["url"])
                    if not item.get("title"):
                        item["title"] = a.text.strip()
            except Exception:
                pass

        if not item.get("url"):
            return None

        if item["url"] in self.seen_urls:
            return None

        # Price: look for elements containing $
        try:
            price_el = card.find_element(By.CSS_SELECTOR, ".str-item-card__price, [class*='price'], [class*='Price'], .s-item__price")
            item["price"] = price_el.text.strip()
        except Exception:
            pass

        if not item.get("price"):
            try:
                full = card.text
                m = re.search(r'\$[\d,]+\.?\d*', full)
                if m:
                    item["price"] = m.group()
            except Exception:
                pass

        # Image
        try:
            img = card.find_element(By.CSS_SELECTOR, "img[src*='ebayimg']")
            item["image_url"] = img.get_attribute("src") or ""
        except Exception:
            try:
                for img in card.find_elements(By.TAG_NAME, "img"):
                    src = img.get_attribute("src") or ""
                    if "ebayimg" in src.lower() or "ebay" in src.lower():
                        item["image_url"] = src
                        break
            except Exception:
                pass

        if not item.get("title"):
            item["title"] = item.get("url", "")

        return item

    def _extract_from_search_item(self, elem):
        item = {}

        for sel in [".s-item__title span", ".s-item__title", ".item__title", "h3", "[data-testid='item-title']"]:
            try:
                t = elem.find_element(By.CSS_SELECTOR, sel).text.strip()
                if t and t.lower() not in ("", "shop on ebay"):
                    item["title"] = t
                    break
            except Exception:
                continue

        href = None
        for sel in [".s-item__link", "a[href*='itm/']", "a[href*='/sch/']"]:
            try:
                el = elem.find_element(By.CSS_SELECTOR, sel)
                href = el.get_attribute("href")
                if href and "ebay.com" in href.lower():
                    break
            except Exception:
                continue
        if not href:
            try:
                href = elem.find_element(By.TAG_NAME, "a").get_attribute("href")
            except Exception:
                pass
        if not href:
            return None

        item["url"] = clean_url(href)
        item["item_id"] = extract_item_id(item["url"])

        if item["url"] in self.seen_urls:
            return None

        for sel in [".s-item__price", ".item__price", "[data-testid='x-price']", ".price", ".s-item__details .s-item__price"]:
            try:
                p = elem.find_element(By.CSS_SELECTOR, sel).text.strip()
                if p:
                    prices = re.findall(r'[\d,]+\.?\d*', p.replace(",", ""))
                    if prices:
                        item["price"] = f"${float(prices[0]):.2f}" if "." in prices[0] else prices[0]
                    else:
                        item["price"] = p
                    break
            except Exception:
                continue

        for sel in [".s-item__image img", "img[src*='ebayimg']", ".item__image img", "img.s-item__image-img"]:
            try:
                src = elem.find_element(By.CSS_SELECTOR, sel).get_attribute("src") or ""
                if "ebayimg" in src.lower():
                    item["image_url"] = src
                    break
            except Exception:
                continue

        if not item.get("image_url"):
            try:
                for img in elem.find_elements(By.TAG_NAME, "img"):
                    src = img.get_attribute("src") or ""
                    if "ebayimg" in src.lower():
                        item["image_url"] = src
                        break
            except Exception:
                pass

        return item

    def _parse_page(self):
        items = []
        seen_on_page = set()
        layout_type = None

        # 1) Try store card selectors (no wait — just check what's rendered)
        for sel in self.STORE_CARD_SELECTORS:
            try:
                cards = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if cards:
                    for card in cards:
                        try:
                            item = self._extract_from_card(card)
                            if item and item["url"] not in seen_on_page:
                                seen_on_page.add(item["url"])
                                items.append(item)
                        except StaleElementReferenceException:
                            continue
                    if items:
                        layout_type = "store"
                        break
            except Exception:
                continue

        # 2) Also try search layout (catches any items the store layout missed)
        for sel in self.SEARCH_SELECTORS:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if els and len(els) > len(items):
                    layout_type = "search_full"
                    for el in els:
                        try:
                            item = self._extract_from_search_item(el)
                            if item and item["url"] not in seen_on_page:
                                seen_on_page.add(item["url"])
                                items.append(item)
                        except StaleElementReferenceException:
                            continue
                    if items:
                        break
            except Exception:
                continue

        # 3) Final catch-all: grab EVERY product link not yet captured
        all_links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='itm/'], a[href*='sch/']")
        for link in all_links:
            try:
                href = link.get_attribute("href") or ""
                if "ebay.com" not in href.lower():
                    continue
                url = clean_url(href)
                if url in self.seen_urls or url in seen_on_page:
                    continue
                item_id = extract_item_id(url)
                if not item_id:
                    continue
                item = {
                    "url": url,
                    "item_id": item_id,
                    "title": link.text.strip() or item_id,
                }
                seen_on_page.add(url)
                items.append(item)
            except Exception:
                continue

        if not layout_type and not items:
            log.warning("No products found on page")

        return items

    def _has_next_page(self):
        # Find the highest page number in pagination
        try:
            page_items = self.driver.find_elements(By.CSS_SELECTOR, "a.pagination__item[href*='_pgn=']")
            max_page = 0
            for p in page_items:
                m = re.search(r'_pgn=(\d+)', p.get_attribute("href") or "")
                if m:
                    n = int(m.group(1))
                    if n > max_page:
                        max_page = n
            if max_page:
                # Find current page
                current = self.driver.find_elements(By.CSS_SELECTOR, ".pagination__item--current, [aria-current='page']")
                if current:
                    m = re.search(r'_pgn=(\d+)', current[0].get_attribute("href") or "")
                    if not m:
                        m = re.search(r'\b(\d+)\b', current[0].text)
                    if m:
                        cur = int(m.group(1))
                        if cur >= max_page:
                            return False
        except Exception:
            pass

        # Check for disabled next button
        try:
            disabled_els = self.driver.find_elements(By.CSS_SELECTOR,
                "a.pagination__next.pagination__item--disabled, "
                "button.pagination__next[disabled], "
                "a[aria-label*='next page'][aria-disabled='true'], "
                "a[aria-label*='Next'][aria-disabled='true']")
            if disabled_els:
                return False
        except Exception:
            pass

        # Fallback: check for next button
        for sel in self.PAGINATION_NEXT:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_enabled() and el.is_displayed():
                    return True
            except Exception:
                continue
        return False

    # ----- main loop -----

    def _recreate_driver(self):
        self._quit_driver()
        time.sleep(random.uniform(2, 5))
        self._init_driver()

    def scrape_listings(self):
        self._extract_seller_name()
        # Phase A: scrape store pages with more items per page
        store_url = self.store_url
        if "?" in store_url:
            store_url += "&_ipg=240"
        else:
            store_url += "?_ipg=240"
        self._scrape_pages(store_url, "store")
        # Phase B: scrape search pages for any missed products (active listings)
        if self.seller_name and self.products:
            search_url = f"https://www.ebay.com/sch/i.html?_ssn={self.seller_name}&_ipg=240"
            self._scrape_pages(search_url, "search")
            log.info(f"Total after search: {len(self.products)} products")
        # Phase C: also check completed/sold items
        if self.seller_name and self.products:
            completed_url = f"https://www.ebay.com/sch/i.html?_ssn={self.seller_name}&_ipg=240&LH_Complete=1&LH_Sold=0"
            self._scrape_pages(completed_url, "completed")
            log.info(f"Total after completed: {len(self.products)} products")

    def _scrape_pages(self, base_url, mode="store"):
        page = 1
        consecutive_empty = 0
        consecutive_security = 0

        while page <= MAX_PAGES:
            url = build_page_url(base_url, page)
            log.info(f"[{mode}] Page {page} -> {url}")

            SCRAPE_STATE["phase"] = "listing"
            SCRAPE_STATE["page"] = page
            SCRAPE_STATE["products_found"] = len(self.products)

            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    self.driver.set_page_load_timeout(15)
                    try:
                        self.driver.get(url)
                    except TimeoutException:
                        log.debug(f"Page load timeout on {url}, parsing anyway...")

                    if is_security_page(self.driver):
                        consecutive_security += 1
                        pause = random.uniform(30, 60)
                        log.warning(f"[{mode}] Security page on {page} (attempt {attempt}/{consecutive_security}), pausing {pause:.0f}s...")
                        time.sleep(pause)
                        if consecutive_security >= 3:
                            log.warning("Too many security pages - restarting browser session...")
                            self._recreate_driver()
                            consecutive_security = 0
                        continue

                    consecutive_security = 0

                    # Quick lazy-load scroll
                    try:
                        for _ in range(3):
                            self.driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight);")
                            time.sleep(0.3)
                        self.driver.execute_script("window.scrollTo(0, 0);")
                        time.sleep(0.2)
                    except Exception:
                        pass

                    try:
                        self.driver.execute_script("window.stop();")
                    except Exception:
                        pass

                    self.driver.set_page_load_timeout(PAGE_TIMEOUT)

                    items = self._parse_page()
                    log.info(f"[{mode}] Found {len(items)} items on page {page}")

                    if not items:
                        consecutive_empty += 1
                        if consecutive_empty >= 2:
                            log.info(f"[{mode}] Two consecutive empty pages - pagination done")
                            return
                    else:
                        consecutive_empty = 0
                        new_count = 0
                        for it in items:
                            if it["url"] not in self.seen_urls:
                                self.seen_urls.add(it["url"])
                                self.products.append(it)
                                new_count += 1
                        log.info(f"[{mode}] New on page {page}: {new_count}")

                    success = True
                    break

                except (TimeoutException, WebDriverException) as e:
                    log.error(f"[{mode}] Error page {page}, attempt {attempt}: {e}")
                    if attempt < MAX_RETRIES:
                        rand_delay(4, 8)
                    continue

            if not success:
                log.error(f"[{mode}] Skipping page {page} after {MAX_RETRIES} failed attempts")
                if page > 1:
                    break
                page += 1
                continue

            # Always try next page — let consecutive_empty handle the stop
            if items:
                page += 1
                rand_delay(MIN_DELAY, MAX_DELAY)
            elif consecutive_empty < 2:
                # Single empty page — try one more before giving up
                page += 1
                rand_delay(MIN_DELAY, MAX_DELAY)
            else:
                break

            # Periodic save every 3 pages
            if page % 3 == 0 and self.products:
                export_csv(self.products, OUTPUT_CSV)


# =====================================================================
# Phase 2 — Product detail scraper (requests, threaded)
# =====================================================================

class DetailScraper:

    # Known tech brands for title-based extraction (longest first for greedy match)
    KNOWN_BRANDS = sorted([
        "APPROVED NETWORKS", "WESTERN DIGITAL", "ALLEN BRADLEY", "PALO ALTO",
        "CHECK POINT", "PULSE SECURE", "ALLIED TELESIS", "ALCATEL-LUCENT",
        "EXTREME NETWORKS", "SERVER TECHNOLOGY", "DONALDSON", "MICRON TECHNOLOGY",
        "SK HYNIX", "SILICON POWER", "STAR MICRONICS",
        "DELL", "HPE", "INTEL", "CISCO", "JUNIPER", "SAMSUNG", "MICRON",
        "HGST", "SEAGATE", "TOSHIBA", "AMD", "NVIDIA", "IBM", "LENOVO",
        "HITACHI", "EMC", "BROCADE", "ARISTA", "NETAPP", "FORTINET",
        "FORTIGATE", "FORTISWITCH", "F5", "ARUBA", "MERAKI", "QLOGIC",
        "EMULEX", "MELLANOX", "AMPERE", "LIGHTBITS", "ORACLE", "KINGSTON",
        "LOGITECH", "STARTECH", "SUPERMICRO", "QUANTUM", "ADTRAN", "CIENA",
        "NORTEL", "AVOCENT", "OPENGEAR", "FINISAR", "MOLEX", "PANDUIT",
        "TRIPP LITE", "APC", "EATON", "BELKIN", "DIGI", "LANTRONIX",
        "SIIG", "VISIONTEK", "ASUS", "MSI", "GIGABYTE", "CORSAIR", "EVGA",
        "ZOTAC", "PNY", "TRANSCEND", "ADATA", "PLEXTOR", "CRUCIAL",
        "HYNIX", "INFOBLOX", "RUCKUS", "SERVER TECH", "RARITAN", "VERTIV",
        "SONICWALL", "WATCHGUARD", "SOPHOS", "TRENDNET", "NETGEAR",
        "D-LINK", "LINKSYS", "UBIQUITI", "MIKROTIK", "AVAYA", "MITEL",
        "SHORETEL", "POLYCOM", "HUAWEI", "ZTE", "ERICSSON", "NOKIA",
        "MOTOROLA", "SIEMENS", "HEWLETT PACKARD", "PACKARD BELL",
        "LANTRONIX", "ADDER", "ROSEWILL", "COOLER MASTER", "THERMALTAKE",
        "ANTEC", "NZXT", "INNOLIGHT", "GENERIC", "PREMIUM TONER",
        "GREENBOX", "LEXMARK", "ECOSENSE", "VANTEC", "BELKIN",
        "CRADLEPOINT", "ISEEVY", "J-TECH", "JVC", "ADESSO",
        "COMMSCOPE", "CORNING", "AXIOM", "PLUGABLE", "STARVIEW",
        "SUN", "SERVERTECH", "CHIEF", "NCR", "VINTAGE",
    ], key=len, reverse=True)

    TITLE_BRAND_PREFIXES = [
        "new", "sealed", "brand new", "new sealed", "lot of",
        "1pcs", "2pcs", "3pcs", "5pcs", "10pcs",
    ]

    DESCR_CLEAN_PATTERNS = [
        r"Features Details.*",  # strip from "Features Details" onward
    ]

    def __init__(self):
        self._session = None

    def _get_session(self):
        if self._session is None:
            s = requests.Session()
            s.headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            })
            s.max_redirects = 10
            self._session = s
        return self._session

    @staticmethod
    def _clean_title(title):
        """Remove common prefixes/suffixes from a title for parsing."""
        if not title:
            return ""
        t = title.strip().strip("*").strip()
        # Keep stripping known prefixes until none match
        PREFIX_RE = re.compile(
            r'^(?:Brand\s+New\s+(?:Factory\s+)?Sealed\s*[-–—]*\s*'
            r'|New\s+Sealed\s*'
            r'|Sealed\s+New\s*'
            r'|New\s+Seald(?:ed|e)\s*'  # handle typos
            r'|New\s+'
            r'|Sealed\s+'
            r'|Seald(?:ed|e)\s+'  # handle typos
            r'|Factory\s+Sealed\s*[-–—]*\s*'
            r'|Brand\s+New\s*[-–—]*\s*'
            r'|1PCS?\s+'
            r'|Lot\s+of\s+\d+\s+'
            r'|\*\*NEW\s+SEALED\s+'
            r'|\*\*)',
            re.IGNORECASE,
        )
        while True:
            m = PREFIX_RE.match(t)
            if not m:
                break
            t = t[m.end():].strip()
        return t

    @staticmethod
    def _brand_from_title(title):
        if not title:
            return None
        t = DetailScraper._clean_title(title)
        parts = t.split()
        if not parts:
            return None

        # Words that should NEVER be treated as brand names
        NON_BRAND_WORDS = {
            "PLATINUM", "GOLD", "SILVER", "BRONZE", "TITANIUM",
            "XEON", "CORE", "GEN", "GENUINE", "ORIGINAL", "AUTHENTIC",
            "OEM", "RECERTIFIED", "REFURBISHED", "USED", "NEW",
            "SEALED", "SEALDED", "SEALDE", "BRAND", "FACTORY", "LOT", "BUNDLE", "KIT",
            "PREMIUM", "PRO", "PROFESSIONAL", "ENTERPRISE",
            "STANDARD", "BASIC", "ADVANCED", "ULTRA", "ELITE",
            "SFF", "LFF", "HH", "LP", "FH", "SERVER",
        }

        # Check for multi-word brands first (e.g., "WESTERN DIGITAL")
        brand = None
        for i in range(len(parts) - 1, -1, -1):
            candidate = " ".join(parts[:i+1]).upper()
            if candidate in DetailScraper.KNOWN_BRANDS:
                brand = candidate
                break

        if brand:
            # Make sure it's not just a non-brand word when multi-word
            if brand not in NON_BRAND_WORDS:
                return brand.title()

        # Handle titles starting with a model number (no brand prefix)
        # e.g., "SR2N7 Intel Xeon Processor E5..." -> brand = Intel after the first token
        # e.g., "CISCO1921/K9 Integrated Services Router" -> brand = Cisco
        first = parts[0].rstrip(",/").upper()
        if re.match(r'^[A-Z0-9]+[-/]?\d', first) and len(first) >= 4:
            # Check if first token starts with a known brand (e.g. CISCO1921)
            for brand in DetailScraper.KNOWN_BRANDS:
                brand_key = brand.upper().replace(" ", "")
                if first.startswith(brand_key) and len(first) > len(brand_key):
                    return brand.title()
            # Scan remaining words for first known brand
            for p in parts[1:]:
                w = p.rstrip(",").upper()
                if w in DetailScraper.KNOWN_BRANDS:
                    return w.title()
            return None

        # Fallback: first word (but only if it's a known brand or all-caps non-generic word)
        if first in DetailScraper.KNOWN_BRANDS:
            return first.title()
        if first.isalpha() and first.isupper() and len(first) >= 2 and first not in NON_BRAND_WORDS:
            return first.title()

        return None

    @staticmethod
    def _model_from_title(title, brand=None):
        """Extract model number from title using a scan-and-filter approach."""
        if not title:
            return None
        t = DetailScraper._clean_title(title)

        BRAND_NAMES = {b.upper() for b in DetailScraper.KNOWN_BRANDS}

        # Regex patterns for known non-model tokens
        NON_MODEL_PATTERNS = [
            r'^F?C?LGA\d+$',           # socket types: LGA3647, FCLGA3647, LGA1151
            r'^[\d.]+[-]?INCH$',        # drive sizes: 2.5-INCH, 3.5INCH, 2.5INCH
            r'^\d+-(?:PORT|WATT|WATTS|CORE|CORES|GBIT|GHZ|LANE|LANES|BAY|BAYS|SLOT|SLOTS|PACK|PACKS|THREAD|U\b)$',  # 2-PORT, 8-CORE, 12-THREAD, 1U
            r'^\d+P\w*$',               # like 2P, 4P
            r'^\d+M$',                  # cache sizes: 18M, 25M, 40M (but not part of model)
            r'^\d+-WATTS?$',            # power ratings
            r'^\d+W$',                  # wattage: 500W, 650W
            r'^\d+GBIT$',              # gigabit
            r'^DDR[2345]-\d+MHZ$',     # DDR3-1333MHZ, DDR4-2133MHZ
            r'^PC[234]-\d+',            # PC3-10600, PC4-17000
            r'^GDDR[345]',              # GDDR3, GDDR5
            r'^\d+CORE$',              # 18CORE, 28CORE
            r'^PCI.EXPRESS$',          # PCI-EXPRESS
            r'^\d+METER$',             # 6METER, 3METER
            r'^[\d.]+[X][\d.]+',       # riser configs: 10X8X16X8, 8X4X1
            r'^PORT$',                 # bare PORT
            r'^RAILS$',                # bare RAILS
            r'^\d+X$',                 # 48X, 16X, 24X
            r'^[\d.]+[-][\d.]+[V]',    # voltage: 18-60V-DC
            r'^QSA',                   # QSA adapter
            r'^U\d+$',                 # U320, U160
            r'^X\d+$',                 # X16, X8 (PCIe slot)
            r'^\d+GBASE',              # 1000BASE, 10GBASE (generic)
            r'^R[0-9]+$',              # R710, R720 (Dell servers - model already captured as brand)
        ]

        STOP_WORDS = {
            "CPU", "PROCESSOR", "PROCESSORS", "SWITCH", "SWITCHES", "ADAPTER", "ADAPTERS",
            "TRANSCEIVER", "TRANSCEIVERS", "MODULE", "MODULES", "CABLE", "CABLES",
            "FAN", "FANS", "POWER", "SUPPLY", "SUPPLIES", "NETWORK", "NETWORKING",
            "CONTROLLER", "CONTROLLERS", "MEMORY", "RAM", "DRIVE", "DRIVES",
            "HARD", "SSD", "HDD", "WITH", "FOR", "NO", "LOT", "THE", "AND", "OR",
            "WIRELESS", "ACCESS", "POINT", "POINTS", "NIC", "HBA",
            "FIREWALL", "SECURITY", "GATEWAY", "ROUTER", "SWITCH",
            "MANAGED", "UNCLAIMED", "NEW", "SEALED", "BRAND", "FACTORY",
            "PORT", "PORTS", "DUAL", "QUAD", "SINGLE", "OCTAL",
            "XEON", "GOLD", "PLATINUM", "SILVER", "BRONZE", "TITANIUM",
            "CORE", "GEN", "GEN10", "GEN8", "GEN9", "GEN7",
            "FC", "GHZ", "MHZ", "RPM", "LGA", "PCI", "PCIE", "HH",
            "VFLT", "MOUNTING", "BRACKET", "BRACKETS", "CABINET",
            "CHASSIS", "ASSEMBLY", "BOARD", "BOARDS", "HEATSINK", "COOLING",
            "BATTERY", "BATTERIES", "SMART", "ARRAY", "CONNECT",
            "ETHERNET", "INFINIBAND", "FIBRE", "FIBER", "CHANNEL",
            "HOST", "BUS", "STORAGE", "SERVER", "SERVERS", "BLADE",
            "HIGH", "PERFORMANCE", "ENTERPRISE", "DATACENTER", "DATA",
            "CENTER", "DESKTOP", "MOBILE", "WORKSTATION", "LAPTOP",
            "NOTEBOOK", "REPLACEMENT", "OPTIONAL", "KIT", "KITS",
            "TRAY", "TRAYS", "UNIT", "UNITS", "SYSTEM", "SYSTEMS",
            "SOLUTION", "SOLUTIONS", "SET", "SETS", "BUNDLE", "BUNDLES",
            "ORIGINAL", "GENUINE", "AUTHENTIC", "OEM", "RECERTIFIED",
            "REFURBISHED", "USED", "TESTED", "PULL", "PULLS",
            "10GBASE-LR", "10GBASE-SR", "10GBASE-ER", "10GBASE-ZR",
            "10GBASE-CU", "10GBASE-T", "10GBASE-LRM", "10GBASE-SX",
            "25GBASE-LR", "25GBASE-SR", "25GBASE-CU",
            "40GBASE-LR", "40GBASE-SR", "40GBASE-CR",
            "100GBASE-LR", "100GBASE-SR", "100GBASE-CR",
            "10GBE", "25GBE", "40GBE", "100GBE", "1GBE",
            "10G", "25G", "40G", "100G", "1G",
            "SAS", "SATA", "SAS-2", "SAS-3", "SATA-2", "SATA-3",
            "NVME", "NVM-E", "M.2", "U.2", "U.3",
            "GIGABIT", "GIGABITS", "KEYBOARD", "KEYBOARDS", "MOUSE",
            "CACHE", "SOCKET", "REMOTE", "ULTRIUM", "LTO",
            "BUILT-IN", "ASYNCHRONOUS", "MULTI-MODE", "SINGLE-MODE",
            "GRAPHICS", "VIDEO", "AUDIO", "OPTICAL", "COPPER",
            "PASSIVE", "ACTIVE", "DIRECT", "ATTACH",
            "RAID", "HBA", "NIC",
            "VGA", "DVI", "HDMI", "DP", "USB", "RJ-45", "RJ45",
            "SFP", "SFP+", "SFP28", "QSFP", "QSFP+", "QSFP28",
            "CFP", "CFP2", "CFP4", "XFP", "X2",
            # Additional stop words for improved accuracy
            "THUNDERBOLT", "THINKSYSTEM", "CATALYST", "GAMING", "SERIES",
            "EXTERNAL", "MANAGEMENT", "PORTSERVER", "EWS", "COMPAQ",
            "NATIVE", "VOLTAGE", "CARTRIDGE", "DL380P", "DL360P", "DL580",
            "DL385", "ML350", "ML310", "ML150", "QUADRO", "100BASE-FX",
            "10X8X16X8", "8X4X1", "QSFP-QSFP", "18-60V-DC", "G-ELECTRONIC",
            "C2G", "802", "5X0", "ENT", "SPS", "SERVICES", "SERVICE", "INTEGRATED",
            "RISER", "RISERS", "MINISAS", "MINI-SAS", "SAS4",
        }

        # Remove brand from title
        if brand:
            for bp in brand.upper().split():
                t = re.sub(r'\b' + re.escape(bp) + r'\b\s*[/\-–—]*\s*', '', t, flags=re.IGNORECASE)

        t = re.sub(r'^Lot\s+of\s+\d+\s+', '', t, flags=re.IGNORECASE)
        t = re.sub(r'^\d+PCS?\s+', '', t, flags=re.IGNORECASE)

        tokens = re.split(r'[\s,;]+', t)
        candidates = []

        def matches_non_model(word):
            for pat in NON_MODEL_PATTERNS:
                if re.match(pat, word):
                    return True
            return False

        for c in tokens:
            c = c.strip().strip("*").strip('"').strip("'").strip("()").strip("[]").strip(".")
            if not c:
                continue
            word = c.upper()

            # Size tokens
            if re.match(r'^\d+[.]\d+["”]?$', c):
                continue
            if re.match(r'^\d+U$', word):
                continue

            # Capacity/size
            if re.match(r'^[\d.]+(GB|TB|MB|KB|GHZ|MHZ|RPM|GBPS|MBPS|W|V|A|MW)$', word):
                continue
            if re.match(r'^[\d.]+["”]$', c):
                continue

            # Pure numbers
            if re.match(r'^\d+$', c):
                if len(c) >= 3:
                    candidates.append(("DIGIT", c))
                continue

            # Known brands
            if word in BRAND_NAMES:
                continue

            # Speed
            if re.match(r'^\d+K$', word):
                continue

            # Core count
            if re.match(r'^\d+-CORE(S)?$', word):
                continue

            # BASE standards
            if re.match(r'^\d+GBASE-', word):
                continue

            # Non-model regex patterns
            if matches_non_model(word):
                continue

            # Stop words
            if word in STOP_WORDS:
                continue

            # Model-like token check
            is_model = False
            if re.match(r'^[A-Za-z0-9][-A-Za-z0-9./+]*[A-Za-z0-9+]$', c) and len(c) >= 3:
                is_model = True
                if re.match(r'^[A-Za-z]{3,4}$', c):
                    is_model = False

            if is_model:
                candidates.append(("MODEL", c.upper()))

        model_cands = [v for t, v in candidates if t == "MODEL"]
        digit_cands = [v for t, v in candidates if t == "DIGIT"]

        # Single-word brand names for penalty check
        SINGLE_BRANDS = {b.upper() for b in DetailScraper.KNOWN_BRANDS if ' ' not in b}

        chosen = None
        if model_cands:
            def score(token):
                s = 0
                if re.search(r'\d', token):
                    s += 2
                if "-" in token:
                    s += 2
                if "." in token:
                    s += 1
                if 6 <= len(token) <= 16:
                    s += 1
                if re.match(r'^[A-Za-z]+\d+', token):
                    s += 1
                if re.match(r'^[A-Za-z]+\d+-[A-Za-z0-9]+', token):
                    s += 1
                if re.match(r'^\d+[A-Za-z]', token):
                    s += 1
                # Penalize known generic patterns
                if re.match(r'^[A-Z0-9]{4,}$', token) and not re.search(r'\d', token):
                    s -= 3  # all-alpha, no digits: generic word
                if token in ("KEYBOARD", "MOUSE", "CACHE", "SOCKET", "REMOTE",
                             "ULTRIUM", "GIGABIT", "GDDR3", "GDDR5", "GDDR6",
                             "2-PORT", "4-PORT", "8-PORT", "16-PORT", "24-PORT",
                             "48-PORT", "36-PORT", "32-PORT"):
                    s -= 10
                # Penalize tokens starting with a known brand name (e.g. FORTIGATE-60E, CISCO1921)
                for sb in SINGLE_BRANDS:
                    if token.startswith(sb) and len(token) > len(sb):
                        s -= 20
                        break
                # Additional penalty patterns
                if token in ("THUNDERBOLT", "THINKSYSTEM", "CATALYST", "GAMING",
                             "SERIES", "EXTERNAL", "MANAGEMENT", "PORTSERVER",
                             "EWS", "COMPAQ", "NATIVE", "VOLTAGE", "CARTRIDGE",
                             "DL380P", "DL360P", "DL580", "DL385",
                             "QUADRO", "100BASE-FX", "C2G", "RAILS",
                             "NETWORK", "POWER", "SUPPLY", "CONTROLLER",
                             "STORAGE", "SERVER", "CHASSIS", "BLADE",
                             "RISER", "MINISAS"):
                    s -= 15
                if re.match(r'^\d+[A-Z]+$', token) and len(token) >= 5:
                    # e.g. 10X8X16X8, 18CORE
                    s -= 20
                if token in ("PORT",):
                    s -= 30
                if token.count("X") >= 2 and len(token) >= 5:
                    # riser configs like 10X8X16X8
                    s -= 25
                return s
            chosen = max(model_cands, key=score)
        elif digit_cands:
            chosen = max(digit_cands, key=len)

        return chosen

    @staticmethod
    def _clean_description(desc):
        if not desc:
            return ""
        # Strip boilerplate after common policy section markers
        cleaned = re.sub(r"Features Details.*", "", desc, flags=re.DOTALL | re.IGNORECASE).strip()
        # Remove leading "eBay " prefix if present
        cleaned = re.sub(r"^eBay\s+", "", cleaned).strip()
        # Remove "C4/F5" and similar condition codes at start
        cleaned = re.sub(r"^[CF]?\d+/[CF]?\d+\s*", "", cleaned).strip()
        cleaned = re.sub(r"^C\d+F\d+\s*", "", cleaned).strip()
        # Remove "New Sealed/Sealed New/Factory Sealed" boilerplate prefix
        cleaned = re.sub(
            r"^(?:New\s+)?(?:Sealed|Seald(?:ed|e)?)\s+(?:New\s+)?\s*[-–—]*\s*",
            "", cleaned, flags=re.IGNORECASE
        ).strip()
        # If everything was boilerplate, keep original truncated
        if not cleaned:
            cleaned = desc[:2000]
        elif len(cleaned) > 2000:
            cleaned = cleaned[:2000]
        return cleaned

    def scrape(self, product):
        url = product.get("url", "")
        if not url:
            return product

        session = self._get_session()
        retries = 2

        for attempt in range(retries):
            try:
                resp = session.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=DETAIL_TIMEOUT)
                rand_delay(DETAIL_MIN_DELAY, DETAIL_MAX_DELAY)

                if resp.status_code != 200:
                    log.debug(f"Detail {url} -> {resp.status_code}")
                    if attempt < retries - 1:
                        time.sleep(2)
                        continue
                    break

                soup = BeautifulSoup(resp.text, "lxml")

                brand = self._extract_brand(soup)
                model = self._extract_model(soup)
                mpn = self._extract_mpn(soup)

                # Fallback: extract brand/model from title if HTML failed
                title = product.get("title", "")
                if not brand:
                    brand = self._brand_from_title(title)
                if not model:
                    model = self._model_from_title(title, brand)
                # If still no MPN, model can serve as MPN
                if not mpn and model and len(model) >= 4:
                    mpn = model

                if brand:
                    product["brand"] = brand
                if model:
                    product["model"] = model
                if mpn:
                    product["mpn"] = mpn

                # Description: first try HTML, then title-based fallback
                desc = self._extract_description(soup)
                if desc:
                    desc = self._clean_description(desc)
                if desc:
                    product["description"] = desc

                break

            except requests.RequestException as e:
                log.debug(f"Request failed {url} (attempt {attempt+1}): {e}")
                if attempt < retries - 1:
                    time.sleep(random.uniform(2, 4))
                continue

        return product

    def _extract_from_specs(self, soup, label_pattern):
        sections = soup.select(".ux-labels-values, .itemSpecifics, .attrLabels, .vi-specs")
        if not sections:
            sections = soup.find_all("div", class_=re.compile(r"ux-labels-values|itemSpecifics|attrLabels|vi-specs"))

        for section in sections:
            labels = section.find_all(["td", "th", "span", "div"], class_=re.compile(r"label|attrLabel|ux-label"))
            if not labels:
                labels = section.select(".ux-labels-values__labels, .attrLabel, td.attrLabel")
            for label in labels:
                text = label.get_text(strip=True).lower().replace(":", "").strip()
                if re.search(label_pattern, text):
                    value_el = label.find_next(["td", "span", "div"], class_=re.compile(r"value|attrValue|ux-value"))
                    if not value_el:
                        value_el = label.find_next_sibling(["td", "span", "div"])
                    if value_el:
                        return value_el.get_text(strip=True)

        for tr in soup.select("table.vi-apr-a1 tr, .itemAttr tr"):
            cells = tr.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower().replace(":", "").strip()
                if re.search(label_pattern, label):
                    return cells[1].get_text(strip=True)

        for script in soup.select("script[type='application/ld+json']"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict):
                    if label_pattern == "brand" and "brand" in data:
                        b = data["brand"]
                        return b.get("name", "") if isinstance(b, dict) else str(b)
                    if label_pattern == "model" and "model" in data:
                        return str(data["model"])
                    if label_pattern in ("mpn", "manufacturer") and "mpn" in data:
                        return str(data["mpn"])
                    if label_pattern == "mpn" and "sku" in data:
                        return str(data["sku"])
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            if label_pattern == "brand" and "brand" in item:
                                b = item["brand"]
                                return b.get("name", "") if isinstance(b, dict) else str(b)
                            if label_pattern == "model" and "model" in item:
                                return str(item["model"])
            except Exception:
                continue

        return None

    def _extract_brand(self, soup):
        brand = self._extract_from_specs(soup, r"^(brand|manufacturer|made by|produced by)$")
        if not brand:
            try:
                el = soup.select_one("[data-testid='x-brand-name'], .ux-textspans--BOLD, span.vi-original-brand")
                if el:
                    brand = el.get_text(strip=True)
            except Exception:
                pass
        return brand

    def _extract_model(self, soup):
        return self._extract_from_specs(soup, r"^(model|model number|model no|part number|part no)$")

    def _extract_mpn(self, soup):
        mpn = self._extract_from_specs(soup, r"^(mpn|manufacturer part number|manufacturer part #|manufacturer#|upc|ean|isbn)$")
        if not mpn:
            try:
                el = soup.select_one("[data-testid='x-item-number'], span.vi-mpn, .mpnValue")
                if el:
                    mpn = el.get_text(strip=True)
            except Exception:
                pass
        return mpn

    def _extract_description(self, soup):
        MIN_DESC_LEN = 10

        # 1) Try specific eBay description containers
        desc_selectors = [
            "[data-testid='item-description']",
            ".item-description",
            "#desc_div",
            "#itemDescription",
            "#vi-description",
            ".desc_wrapper",
            ".desc_container",
            ".it-about-desc",
            ".item-description__text",
            ".it-about",
            "[class*='desc_']",
            "[class*='description']",
            "[class*='DetailSection']",
            "[class*='detail-section']",
            "#detailsSection",
            ".section-title",
            "[itemprop='description']",
            "#ds_div",
            "#viTabs_0_is",
            ".product-description",
            "#itemTitleDesc",
            "div.iti-dsc",
        ]
        for sel in desc_selectors:
            try:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > MIN_DESC_LEN:
                        return text[:2000]
            except Exception:
                continue

        # 2) Try iframe description
        iframe_selectors = [
            "iframe#iframeDescription",
            "iframe[name='desc_ifr']",
            "#desc_ifr",
            "iframe[class*='desc']",
            "iframe[title*='Description']",
        ]
        for sel in iframe_selectors:
            iframe = soup.select_one(sel)
            if iframe:
                src = iframe.get("src", "")
                if src:
                    try:
                        r = self._session.get(src, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=10)
                        if r.status_code == 200:
                            text = BeautifulSoup(r.text, "lxml").get_text(separator=" ", strip=True)
                            if len(text) > MIN_DESC_LEN:
                                return text[:2000]
                    except Exception:
                        pass

        # 3) Try structured data (JSON-LD)
        try:
            for script in soup.select("script[type='application/ld+json']"):
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        desc = item.get("description", "")
                        if desc and len(desc) > MIN_DESC_LEN:
                            return desc[:2000]
        except Exception:
            pass

        # 4) Meta tags
        for sel in ["meta[name='description']", "meta[property='og:description']", "meta[name='DESCRIPTION']"]:
            m = soup.select_one(sel)
            if m and m.get("content"):
                content = m["content"]
                if len(content) > MIN_DESC_LEN:
                    return content[:2000]

        # 5) Known eBay description parent containers
        try:
            for tag in ["#CenterPanel", ".vi-desc-maincntr", ".it-l-desc", "#itmDscCntr",
                        "div[class*='item-description']", "div[class*='ItemDescription']",
                        ".prodDetail", "#product-details", "#descPanel",
                        ".x-item-description", "#itemDescriptionContainer"]:
                el = soup.select_one(tag)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > MIN_DESC_LEN:
                        return text[:2000]
        except Exception:
            pass

        # 6) Last resort: scan body but exclude nav/footer boilerplate
        try:
            body = soup.find("body")
            if body:
                # Remove boilerplate sections first
                for exclude in body.select("nav, footer, header, aside, script, style, .nav, .footer, .header, .topbar, .gh-w"):
                    exclude.decompose()
                # Now find the largest meaningful text block
                best = ""
                for tag in ["p", "div", "section", "article", "span"]:
                    for el in body.find_all(tag, recursive=True):
                        txt = el.get_text(separator=" ", strip=True)
                        if len(txt) > len(best) and len(txt) > MIN_DESC_LEN:
                            # Skip if it looks like navigation/links/boilerplate
                            if len(txt) < 30:
                                continue
                            # Skip if mostly links or common boilerplate words
                            lower = txt.lower()
                            if lower.count("http") > 3 or lower.count("eBay") > 5:
                                continue
                            best = txt
                if best:
                    return best[:2000]
        except Exception:
            pass

        return ""


# =====================================================================
# CSV Export
# =====================================================================

def export_csv(products, path=OUTPUT_CSV):
    if not products:
        log.warning("No products to export")
        return

    fieldnames = [
        "title", "price", "brand", "model", "mpn",
        "description", "image_url", "url",
    ]

    for attempt in range(3):
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for p in products:
                    writer.writerow({k: p.get(k, "") for k in fieldnames})
            log.info(f"Exported {len(products)} products -> {os.path.abspath(path)}")
            return
        except PermissionError:
            if attempt < 2:
                log.warning(f"CSV locked (attempt {attempt+1}), retrying in 3s...")
                time.sleep(3)
            else:
                log.error(f"Cannot write CSV (file locked), saved as {path}.bak")
                with open(path + ".bak", "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    for p in products:
                        writer.writerow({k: p.get(k, "") for k in fieldnames})


# =====================================================================
# Main
# =====================================================================

def main():
    start = time.time()
    log.info("=" * 60)
    log.info("eBay Store Scraper starting")
    log.info(f"Target: {STORE_URL}")
    log.info("=" * 60)

    all_products = []

    if not SELENIUM_AVAILABLE:
        log.error("selenium is required. pip install selenium")
        sys.exit(1)

    listing_scraper = ListingScraper(STORE_URL)
    try:
        listing_scraper._init_driver()
        listing_scraper.scrape_listings()
    except Exception as e:
        log.error(f"Listing phase failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        listing_scraper._quit_driver()
    SCRAPE_STATE["products_found"] = len(listing_scraper.products)

    all_products = listing_scraper.products

    if not all_products:
        log.warning("No products found - exiting")
        export_csv([])
        return

    log.info(f"--- Phase 1 complete: {len(all_products)} unique products ---")

    log.info("--- Phase 2: scraping product details (threaded) ---")
    SCRAPE_STATE["phase"] = "details"
    SCRAPE_STATE["detail_total"] = len(all_products)
    detail_scraper = DetailScraper()

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(detail_scraper.scrape, p): p for p in all_products}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                p = futures[future]
                log.error(f"Detail worker failed for {p.get('url', '?')}: {e}")
            completed += 1
            SCRAPE_STATE["detail_progress"] = completed
            if completed % 100 == 0:
                export_csv(all_products)
                log.info(f"  Details: {completed}/{len(all_products)}")

    log.info("--- Phase 2 complete ---")
    export_csv(all_products)
    SCRAPE_STATE["phase"] = "done"

    elapsed = time.time() - start
    log.info(f"Total time: {elapsed:.1f}s | Products: {len(all_products)}")
    log.info(f"Output: {os.path.abspath(OUTPUT_CSV)}")
    log.info("Done.")


if __name__ == "__main__":
    main()

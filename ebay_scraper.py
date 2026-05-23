import os
import re
import json
import math
import time
import threading
import requests
import streamlit as st
import pandas as pd
from io import BytesIO
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

try:
    import undetected_chromedriver as uc
    UC_AVAILABLE = True
except ImportError:
    uc = None
    UC_AVAILABLE = False

try:
    from openpyxl import Workbook
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

def _strip_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_store_url(url):
    url = _strip_text(url)
    if not url:
        raise ValueError("URL is empty")
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    if "ebay.com" not in url.lower():
        raise ValueError("URL must be an eBay link (ebay.com)")

    url = re.sub(r"[?&]_pgn=\d+", "", url)
    url = url.rstrip("?&")
    if "_ipg=" not in url:
        url += ("&" if "?" in url else "?") + "_ipg=240"
    return url


def _pick_text(soup, selectors, attr=None):
    for selector in selectors:
        try:
            elem = soup.select_one(selector)
            if not elem:
                continue
            value = elem.get(attr, "") if attr else elem.get_text(" ", strip=True)
            value = clean_text(value)
            if value:
                return value
        except Exception:
            continue
    return ""


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_title(value):
    value = clean_text(value)
    if value.startswith("Details about"):
        value = value.replace("Details about", "", 1).strip()
    for suffix in (" | eBay", " | eBay.com", " - eBay"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
    return value


def clean_price(value):
    if not value:
        return ""
    price = re.sub(r"[^0-9.,$€£¥]", " ", value)
    price = re.sub(r"\s+", " ", price).strip()
    match = re.search(r"[\$€£¥]?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)", price)
    if match:
        num = match.group(1)
        symbol = "$" if "$" in value or "US" in value.upper() else ""
        return f"{symbol}{num}" if symbol else num
    return clean_text(value)


def create_http_session():
    from requests.adapters import Retry
    session = requests.Session()
    retry_strategy = Retry(
        total=4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        backoff_factor=1.5,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy, pool_connections=100, pool_maxsize=100)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.ebay.com/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120", "Not A(Brand)";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "Windows",
        }
    )
    try:
        session.get("https://www.ebay.com/", timeout=12)
    except Exception:
        pass
    return session


def extract_item_id(url):
    try:
        match = re.search(r"/itm/(\d+)", url)
        return match.group(1) if match else None
    except Exception:
        return None


def is_valid_item_link(href):
    if not href or len(href) < 20 or "/itm/" not in href:
        return False
    item_id = extract_item_id(href)
    return bool(item_id and len(item_id) >= 8)


def normalize_url(url):
    try:
        url = _strip_text(url)
        if not url:
            return url
        item_id = extract_item_id(url)
        if item_id:
            return f"https://www.ebay.com/itm/{item_id}"
        if "?" not in url:
            return url
        base_url, query = url.split("?", 1)
        params = []
        for param in query.split("&"):
            if param.startswith("hash=") or param.startswith("itmmeta="):
                continue
            params.append(param)
        return base_url + ("?" + "&".join(params) if params else "")
    except Exception:
        return url


def get_driver(use_headless=True):
    use_uc = UC_AVAILABLE and os.environ.get("USE_UC", "0") == "1"
    chrome_options = uc.ChromeOptions() if use_uc else Options()

    if use_headless:
        chrome_options.add_argument("--headless=chrome")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument(f"--user-agent={DEFAULT_USER_AGENT}")

    if not use_uc:
        chrome_options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation", "enable-logging", "enable-blink-features=AutomationControlled"],
        )
        chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_experimental_option(
        "prefs",
        {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_settings.popups": 0,
            "download.prompt_for_download": False,
        },
    )

    driver = None
    if use_uc:
        try:
            driver = uc.Chrome(options=chrome_options)
        except Exception:
            driver = None
    if driver is None:
        driver = webdriver.Chrome(service=Service(), options=chrome_options)

    driver.set_page_load_timeout(45)
    driver.set_script_timeout(25)
    driver.implicitly_wait(3)

    if not use_uc:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    "window.navigator.chrome = {runtime: {}};"
                )
            },
        )

    warm_up_ebay_driver(driver)
    return driver


def warm_up_ebay_driver(driver):
    try:
        driver.get("https://www.ebay.com/")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1.0)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.4)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.3)
    except Exception:
        pass


def is_blocked_page(html, soup=None):
    if not html:
        return False
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    blocked_phrases = [
        "checking your browser before you access ebay",
        "access denied",
        "you don't have permission",
        "browser check",
        "security check",
        "bot check",
        "please enable cookies",
        "verifying your browser",
    ]
    return any(phrase in text for phrase in blocked_phrases)


def is_valid_product_page(html, url=None, soup=None):
    if not html or len(html) < 500:
        return False
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")
    if is_blocked_page(html, soup=soup):
        return False
    title_candidate = (
        _pick_text(soup, ["meta[property='og:title']", "meta[name='twitter:title']"], attr="content")
        or _pick_text(soup, ["h1#itemTitle", "h1[itemprop='name']", "h1"])
        or _pick_text(soup, ["[data-testid='title']", ".it-ttl", "div.it-ttl"])
    )
    if title_candidate:
        title_text = clean_title(title_candidate)
        if len(title_text) > 5:
            return True
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "{}")
            if isinstance(payload, dict) and payload.get("@type") == "Product":
                return True
        except Exception:
            continue
    return False


def get_total_products(url, driver):
    try:
        url = normalize_store_url(url)
        driver.get(url)
        time.sleep(2)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "[class*='results'], [class*='count'], .srp-controls__count-heading")
                )
            )
        except TimeoutException:
            pass

        page_source = driver.page_source
        patterns = [
            r"([0-9,]+)\s+results",
            r"of\s+([0-9,]+)",
            r"([0-9,]+)\s+items",
            r"([0-9,]+)\s+listings",
        ]
        for pattern in patterns:
            match = re.search(pattern, page_source, flags=re.IGNORECASE)
            if match:
                total_str = match.group(1).replace(",", "")
                if total_str.isdigit() and int(total_str) > 0:
                    return int(total_str)
    except Exception:
        pass
    return None


def _extract_links_from_page(driver):
    links = []
    seen_ids = set()

    def add_link(href):
        if href and is_valid_item_link(href):
            item_id = extract_item_id(href)
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                links.append(normalize_url(href))

    try:
        for item in driver.find_elements(By.CSS_SELECTOR, "a.s-item__link"):
            try:
                add_link(item.get_attribute("href"))
            except Exception:
                continue
    except Exception:
        pass

    if not links:
        for selector in (
            "a[href*='/itm/']",
            ".s-item a[href*='/itm/']",
            ".s-result-item a[href*='/itm/']",
        ):
            try:
                for elem in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        add_link(elem.get_attribute("href"))
                    except Exception:
                        continue
                if links:
                    break
            except Exception:
                continue

    if not links:
        for link_elem in driver.find_elements(By.TAG_NAME, "a"):
            try:
                add_link(link_elem.get_attribute("href"))
            except Exception:
                continue

    if not links:
        html = driver.page_source
        for match in re.findall(r'https?://www\.ebay\.com/itm/[0-9]+[^"\']*', html):
            add_link(match)
        if not links:
            for match in re.findall(r'/itm/[0-9]+[^"\']*', html):
                add_link("https://www.ebay.com" + match)

    return links


def get_all_product_links(store_url, driver, total_products=None, on_progress=None):
    store_url = normalize_store_url(store_url)
    all_links = set()
    page = 1
    no_change_counter = 0
    items_per_page = 240
    if total_products and total_products > 0:
        max_pages = min(math.ceil(total_products / items_per_page) + 2, 100)
    else:
        max_pages = 50

    base_url = store_url.split("&_pgn=")[0] if "_pgn=" in store_url else store_url
    separator = "&" if "?" in base_url else "?"

    while page <= max_pages:
        url = f"{base_url}{separator}_pgn={page}"
        try:
            driver.get(url)
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/itm/'], a.s-item__link"))
            )
            time.sleep(1.0)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.5)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.3)

            links = _extract_links_from_page(driver)
            new_items = 0
            for link in links:
                if link not in all_links:
                    all_links.add(link)
                    new_items += 1

            if on_progress:
                on_progress(page, max_pages, len(links), new_items, len(all_links))

            if new_items == 0:
                no_change_counter += 1
                if no_change_counter >= 3:
                    break
            else:
                no_change_counter = 0

            if not links:
                break

            page += 1
            time.sleep(0.5)
        except Exception:
            no_change_counter += 1
            if no_change_counter >= 3:
                break
            page += 1

    return list(all_links)


def _fill_specs_from_soup(soup, data):
    spec_selectors = [
        "motion.div.ux-labels-values__labels-content + div",
        "motion.div.ux-labels-values__labels",
        "motion.div.ux-labels-values",
        "div.ux-labels-values",
        "motion.div.ux-labels-values__labels-content",
        "div.itemAttr tr",
        "table.item-details tr",
        "motion.div.ux-labels-values__labels-content",
        ".ux-item-specifications tr",
        ".itemAttr tr",
    ]
    for selector in spec_selectors:
        for row in soup.select(selector):
            text = clean_text(row.get_text(" ", strip=True))
            if not text:
                continue
            lower = text.lower()
            if "brand" in lower and not data["brand"]:
                m = re.search(r"brand[:\s]+([^\n\r|]+)", text, re.IGNORECASE)
                if m:
                    data["brand"] = clean_text(m.group(1))
            if "model" in lower and not data["model"]:
                m = re.search(r"model[:\s]+([^\n\r|]+)", text, re.IGNORECASE)
                if m:
                    data["model"] = clean_text(m.group(1))
            if any(k in lower for k in ("mpn", "part number", "manufacturer part")) and not data["part_num_mpn"]:
                m = re.search(
                    r"(?:mpn|part number|manufacturer part)[:\s]+([^\n\r|]+)",
                    text,
                    re.IGNORECASE,
                )
                if m:
                    data["part_num_mpn"] = clean_text(m.group(1))

    for dt in soup.select("dt"):
        dd = dt.find_next("dd")
        if not dd:
            continue
        label = clean_text(dt.get_text(" ", strip=True)).lower()
        value = clean_text(dd.get_text(" ", strip=True))
        if "brand" in label and not data["brand"]:
            data["brand"] = value
        elif "model" in label and not data["model"]:
            data["model"] = value
        elif any(k in label for k in ("mpn", "part number", "manufacturer part")) and not data["part_num_mpn"]:
            data["part_num_mpn"] = value

    for row in soup.select("motion.div.ux-labels-values, div.ux-labels-values, div.ux-labels-values__row"):
        labels = row.select(".ux-labels-values__labels, .ux-labels-values__labels-content")
        values = row.select(".ux-labels-values__values, .ux-labels-values__values-content")
        if labels and values:
            label = clean_text(labels[0].get_text(" ", strip=True)).lower()
            value = clean_text(values[0].get_text(" ", strip=True))
            if "brand" in label and not data["brand"]:
                data["brand"] = value
            elif "model" in label and not data["model"]:
                data["model"] = value
            elif any(k in label for k in ("mpn", "part number", "manufacturer part")) and not data["part_num_mpn"]:
                data["part_num_mpn"] = value


def _fill_from_json_ld(soup, data):
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "{}")
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not data["title"] and item.get("name"):
                    data["title"] = clean_title(str(item["name"]))
                if not data["description"] and item.get("description"):
                    data["description"] = clean_text(str(item["description"]))
                if not data["brand"] and item.get("brand"):
                    brand = item["brand"]
                    data["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand)
                if not data["model"] and item.get("model"):
                    data["model"] = str(item["model"])
                if not data["part_num_mpn"] and item.get("mpn"):
                    data["part_num_mpn"] = str(item["mpn"])
                if not data["price"] and item.get("offers"):
                    offers = item["offers"]
                    if isinstance(offers, dict) and offers.get("price"):
                        data["price"] = clean_price(str(offers["price"]))
                    elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
                        data["price"] = clean_price(str(offers[0].get("price", "")))
                if not data["image"] and item.get("image"):
                    img = item["image"]
                    data["image"] = img[0] if isinstance(img, list) else str(img)
        except Exception:
            continue


def _empty_product(url):
    return {
        "title": "",
        "price": "",
        "description": "",
        "part_num_mpn": "",
        "brand": "",
        "model": "",
        "image": "",
        "url": url,
    }


def _extract_product_data(html, soup, url):
    data = _empty_product(url)

    data["title"] = clean_title(
        _pick_text(soup, ["meta[property='og:title']", "meta[name='twitter:title']"], attr="content")
        or _pick_text(soup, ["h1#itemTitle", "h1[itemprop='name']", "h1"])
        or _pick_text(soup, ["[data-testid='title']", ".it-ttl", "div.it-ttl"])
    )
    if not data["title"]:
        for h1 in soup.find_all("h1"):
            text = clean_title(h1.get_text(" ", strip=True))
            if text and len(text) > 5 and text.lower() not in ["ebay", "shop", "save", "sign in"]:
                data["title"] = text
                break

    data["price"] = clean_price(
        _pick_text(
            soup,
            ["meta[property='product:price:amount']", "meta[itemprop='price']"],
            attr="content",
        )
        or _pick_text(
            soup,
            [
                "span#prcIsum",
                "span.x-price-primary",
                ".x-price-primary",
                "span.s-item__price",
                "*[itemprop='price']",
            ],
        )
    )

    data["image"] = _pick_text(
        soup,
        ["meta[property='og:image']", "meta[property='og:image:secure_url']"],
        attr="content",
    )
    if not data["image"]:
        img_elem = soup.select_one("img#icImg") or soup.select_one(".ux-image-carousel-item img")
        if img_elem:
            data["image"] = clean_text(img_elem.get("src", "") or img_elem.get("data-src", ""))

    data["description"] = _pick_text(
        soup,
        ["meta[name='description']", "meta[property='og:description']"],
        attr="content",
    ) or _pick_text(
        soup,
        ["#viTabs_0_is", ".ux-item-description-text", ".x-item-description-inner"],
    )

    _fill_specs_from_soup(soup, data)
    _fill_from_json_ld(soup, data)

    if not data["price"]:
        m = re.search(r"\$\s*([0-9,]+(?:\.[0-9]{2})?)", html)
        if m:
            data["price"] = clean_price(m.group(0))

    for key in data:
        if isinstance(data[key], str):
            data[key] = clean_text(data[key])
            if len(data[key]) > 500:
                data[key] = data[key][:500]

    if data["title"] and not is_blocked_page(html, soup=soup):
        return data
    return None


def _scrape_product_with_selenium(url, driver, debug=False):
    if driver is None:
        return None
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1.5)
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        if not is_valid_product_page(html, url=url, soup=soup):
            if debug:
                print(f"[DEBUG] Selenium page validation failed for {url}")
            return None
        return _extract_product_data(html, soup, url)
    except Exception as e:
        if debug:
            print(f"[DEBUG] Selenium fallback failed for {url}: {e}")
        return None


def scrape_product(url, session=None, selenium_driver=None, selenium_lock=None, debug=False):
    url = normalize_url(url)
    max_retries = 3

    for attempt in range(max_retries):
        try:
            if session is None:
                session = create_http_session()
            response = session.get(url, timeout=25)
            if debug:
                print(f"[DEBUG] URL: {url} | Status: {response.status_code}")
            if response.status_code != 200:
                if attempt < max_retries - 1:
                    time.sleep(3 + (attempt * 2))
                    continue
                return None
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            if is_blocked_page(html, soup=soup) or not is_valid_product_page(html, url=url, soup=soup):
                if debug:
                    print(f"[DEBUG] HTTP blocked/invalid page for {url}")
                if selenium_driver is not None:
                    if selenium_lock is not None:
                        with selenium_lock:
                            return _scrape_product_with_selenium(url, selenium_driver, debug=debug)
                    return _scrape_product_with_selenium(url, selenium_driver, debug=debug)
                if attempt < max_retries - 1:
                    time.sleep(3 + (attempt * 2))
                    continue
                return None
            data = _extract_product_data(html, soup, url)
            if data:
                return data
            if attempt < max_retries - 1:
                time.sleep(3 + (attempt * 2))
                continue
            return None
        except Exception as e:
            if debug:
                print(f"[DEBUG] scrape_product exception for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(3 + (attempt * 2))
                continue
            return None
    return None


def scrape_all_products(urls, selenium_driver=None, use_headless=True, max_workers=12, on_progress=None):
    if not urls:
        return []
    all_data = []
    seen_items = set()
    total = len(urls)
    progress = {"done": 0, "scraped": 0}
    progress_lock = threading.Lock()
    batch_size = 8
    max_workers_actual = 4

    def report():
        if on_progress:
            on_progress(progress["done"], total, progress["scraped"])

    session = create_http_session()
    selenium_lock = threading.Lock()

    def scrape_request(index, url):
        delay = (index % max_workers_actual) * 1.2
        time.sleep(delay)
        return url, scrape_product(
            url,
            session=session,
            selenium_driver=selenium_driver,
            selenium_lock=selenium_lock,
            debug=(index < 3),
        )

    for batch_start in range(0, len(urls), batch_size):
        batch_end = min(batch_start + batch_size, len(urls))
        batch_urls = [(i, url) for i, url in enumerate(urls[batch_start:batch_end], start=batch_start)]
        with ThreadPoolExecutor(max_workers=max_workers_actual) as executor:
            futures = {executor.submit(scrape_request, idx, url): url for idx, url in batch_urls}
            for future in as_completed(futures):
                try:
                    url, data = future.result()
                    item_id = extract_item_id(url)
                    with progress_lock:
                        progress["done"] += 1
                        if data and data.get("title"):
                            if item_id and item_id not in seen_items:
                                seen_items.add(item_id)
                                all_data.append(data)
                                progress["scraped"] += 1
                        report()
                except Exception as e:
                    with progress_lock:
                        progress["done"] += 1
                        print(f"[ERROR] Exception in scrape_request: {e}")
                        report()
        if batch_end < len(urls):
            time.sleep(3)
    return all_data

st.set_page_config(
    page_title="eBay Store Scraper",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🛒 eBay Store Scraper")
st.markdown("---")


def _get_all_product_links_ui(store_url, driver, progress_bar, total_products=None):
    def on_progress(page, max_pages, found, new_items, total_links):
        progress_ratio = min(page / max(max_pages, 1), 1.0)
        progress_bar.progress(
            progress_ratio,
            text=(
                f"Page {page}/{max_pages}: Found {found} links, "
                f"+{new_items} unique (Total: {total_links})"
            ),
        )

    return get_all_product_links(
        store_url, driver, total_products=total_products, on_progress=on_progress
    )


def _scrape_all_products_ui(urls, progress_bar, selenium_driver=None, use_headless=True):
    status_placeholder = st.empty()
    batch_size = 50
    total_batches = (len(urls) + batch_size - 1) // batch_size

    def on_progress(done, total, scraped_count):
        progress_ratio = min(done / max(total, 1), 1.0)
        current_batch = (done // batch_size) + 1
        progress_bar.progress(
            progress_ratio, 
            text=f"Batch {current_batch}/{total_batches}: Scraped {scraped_count}/{total} products"
        )
        status_placeholder.markdown(
            f"**Processed:** {min(done, total)} / {total}  \n"
            f"**Saved:** {scraped_count}  \n"
            f"**Batch:** {current_batch}/{total_batches}  \n"
            f"**Batch Size:** 50 products"
        )

    all_data = scrape_all_products(
        urls,
        selenium_driver=selenium_driver,
        use_headless=use_headless,
        max_workers=12,
        on_progress=on_progress,
    )

    progress_bar.progress(1.0, text=f"✅ Completed! Scraped {len(all_data)} products")
    st.write(f"🎉 Total products scraped: {len(all_data)}")

    if all_data:
        st.write("📋 Sample scraped data:")
        st.dataframe(pd.DataFrame(all_data[:3]))
        with_prices = sum(1 for item in all_data if item.get("price"))
        with_brands = sum(1 for item in all_data if item.get("brand"))
        with_images = sum(1 for item in all_data if item.get("image"))
        st.write("📊 Data Quality:")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("With Prices", f"{with_prices}/{len(all_data)}")
        with col2:
            st.metric("With Brands", f"{with_brands}/{len(all_data)}")
        with col3:
            st.metric("With Images", f"{with_images}/{len(all_data)}")
    else:
        st.warning("⚠️ No data was scraped. Check the URLs and try again.")

    return all_data


def is_streamlit_cloud():
    """Detect if running on Streamlit Cloud"""
    return os.environ.get("STREAMLIT_SERVER_HEADLESS") == "true" or "/mount/src/" in os.getcwd()


def main():
    st.sidebar.header("🔧 Scraper Settings")

    headless = st.sidebar.checkbox(
        "Run Chrome headless",
        value=True,
        help="Browser runs in background (recommended). Uncheck only for debugging.",
    )

    store_url = st.sidebar.text_input(
        "eBay Store URL",
        placeholder="https://www.ebay.com/sch/i.html?_nkw=...",
        help="Enter the eBay store or search URL you want to scrape",
    )

    if store_url and "ebay.com" not in store_url.lower():
        st.sidebar.error("❌ Please enter a valid eBay URL")
        return

    if st.sidebar.button("🚀 Start Scraping", type="primary", disabled=not store_url):
        try:
            store_url = normalize_store_url(store_url)
        except ValueError as e:
            st.error(f"❌ Invalid URL: {e}")
            return

        driver = None
        on_cloud = is_streamlit_cloud()
        
        if on_cloud:
            st.info("ℹ️ Running on Streamlit Cloud: Using HTTP-only scraping (no browser automation)")
        else:
            with st.spinner("🔄 Initializing scraper..."):
                try:
                    driver = get_driver(use_headless=headless)
                    st.success("✅ Scraper initialized successfully!")
                except Exception as e:
                    st.warning(f"⚠️ Browser automation failed, using HTTP-only mode: {str(e)[:100]}")
                    driver = None

        st.header("📊 Step 1: Detecting Total Products")
        total_progress = st.progress(0, text="Analyzing store...")
        total_products = None
        if driver:
            total_products = get_total_products(store_url, driver)
            if total_products:
                total_progress.progress(1.0, text=f"✅ Found {total_products:,} total products")
                st.success(f"🎯 Total Products Detected: {total_products:,}")
            else:
                total_progress.progress(1.0, text="ℹ️ Will scrape all available products")
                st.info("ℹ️ Product count detection failed. Will scrape all available products from the store.")
        else:
            total_progress.progress(1.0, text="ℹ️ Will scrape all available products")
            st.info("ℹ️ Skipping product count (browser not available). Will scrape all available products from the store.")

        st.header("🔗 Step 2: Collecting Product Links")
        links_progress = st.progress(0, text="Starting link collection...")
        if driver:
            all_links = _get_all_product_links_ui(store_url, driver, links_progress, total_products)
        else:
            st.warning("⚠️ Cannot collect links without browser. Using HTTP requests to scrape products directly (slower).")
            all_links = []

        st.header("📦 Step 3: Scraping Product Details")
        
        if all_links:
            links_progress.progress(1.0, text=f"✅ Found {len(all_links):,} product links")
            st.success(f"🔗 Product Links Collected: {len(all_links):,}")
        else:
            if driver:
                st.error("❌ No product links found to scrape!")
                driver.quit()
                return
            else:
                st.info("ℹ️ Proceeding with direct HTTP scraping (may get fewer results)")
                all_links = []

        if not all_links:
            st.error("❌ No product links found to scrape!")
            if driver:
                driver.quit()
            return

        with st.spinner("🔄 Scraping products... This may take a few minutes"):
            scrape_progress = st.progress(0, text="Starting product scraping...")
            scraped_data = _scrape_all_products_ui(
                all_links, scrape_progress, selenium_driver=driver, use_headless=headless
            )
            st.success(f"📦 Products Scraped: {len(scraped_data):,}")

        if driver:
            driver.quit()

        st.header("📥 Step 4: Download Results")
        if scraped_data:
            df = pd.DataFrame(scraped_data)
            columns_order = [
                "title", "price", "description", "part_num_mpn",
                "brand", "model", "image", "url",
            ]
            df = df[columns_order]

            st.subheader("📋 Sample Data")
            st.dataframe(df.head(10))

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Products", len(df))
            with col2:
                st.metric("Products with Price", df["price"].astype(bool).sum())
            with col3:
                st.metric("Products with Images", df["image"].astype(bool).sum())

            st.subheader("💾 Download Options")
            st.download_button(
                label="📄 Download CSV",
                data=df.to_csv(index=False),
                file_name=f"ebay_products_{int(time.time())}.csv",
                mime="text/csv",
            )

            if EXCEL_AVAILABLE:
                excel_buffer = BytesIO()
                with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                    df.to_excel(writer, index=False, sheet_name="Products")
                    worksheet = writer.sheets["Products"]
                    for column in worksheet.columns:
                        max_length = 0
                        column_letter = column[0].column_letter
                        for cell in column:
                            try:
                                max_length = max(max_length, len(str(cell.value)))
                            except Exception:
                                pass
                        worksheet.column_dimensions[column_letter].width = min(max_length + 2, 50)
                excel_buffer.seek(0)
                st.download_button(
                    label="📊 Download Excel",
                    data=excel_buffer.getvalue(),
                    file_name=f"ebay_products_{int(time.time())}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            else:
                st.info("💡 Install openpyxl for Excel export: `pip install openpyxl`")

            st.success("🎉 Scraping completed successfully!")
        else:
            st.error("❌ No products were scraped. Please check the store URL and try again.")

    with st.sidebar.expander("📖 Instructions"):
        st.markdown("""
        1. **Enter eBay Store URL**: Paste any eBay store or search URL
        2. **Click Start Scraping**: The scraper will:
           - Detect total product count
           - Collect all product links (all pages)
           - Scrape detailed product information (headless)
        3. **Download Results**: Get data in CSV or Excel format

        **Data Fields:** Title, Price, Description, MPN, Brand, Model, Image, URL
        """)

    with st.sidebar.expander("🔗 Sample URLs"):
        st.code("""
https://www.ebay.com/sch/i.html?_nkw=iphone
https://www.ebay.com/sch/i.html?_ssn=apple
https://www.ebay.com/sch/i.html?_in_kw=1&_ipg=240&_sop=12
        """)


if __name__ == "__main__":
    main()

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
from urllib.parse import urlparse, parse_qs, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException
    SELENIUM_AVAILABLE = True
except ImportError:
    webdriver = None
    By = None
    Options = None
    Service = None
    WebDriverWait = None
    EC = None
    TimeoutException = Exception
    SELENIUM_AVAILABLE = False

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
    if is_ebay_store_url(url) and "_ipg=" not in url:
        url += ("&" if "?" in url else "?") + "_ipg=240"
    return url


def is_ebay_store_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    return "/str/" in path or "/usr/" in path or "/store/" in path or path.startswith("/b/")


def is_ebay_search_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    return "/sch/" in path or path.endswith("/i.html") or "_nkw=" in query or "/search" in path


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
    import http.cookiejar
    
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        backoff_factor=1.2,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy, pool_connections=100, pool_maxsize=100)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # More comprehensive headers to mimic real browser behavior
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
            "Cache-Control": "max-age=0",
            "DNT": "1",
            "Referer": "https://www.ebay.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="120", "Chromium";v="120", "Not A(Brand)";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": "\"Windows\"",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Connection": "keep-alive",
            "Pragma": "no-cache",
        }
    )
    
    # Try warmup with home page and common endpoints to establish session
    warmup_urls = [
        "https://www.ebay.com/",
        "https://www.ebay.com/n/all-categories",
    ]
    for warmup_url in warmup_urls:
        try:
            session.get(warmup_url, timeout=10)
            time.sleep(0.5)
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


def get_seller_name_from_store_url(url):
    try:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return None
        path_parts = path.split("/")
        if len(path_parts) < 2:
            return None
        if path_parts[0].lower() in {"str", "usr", "stores"}:
            return path_parts[1]
    except Exception:
        pass
    return None


def build_search_url_for_seller(seller, per_page=200, sort="12"):
    if not seller:
        return None
    seller = quote_plus(_strip_text(seller))
    return f"https://www.ebay.com/sch/i.html?_nkw=&_saslop=1&_sasl={seller}&_sop={sort}&_ipg={per_page}"


def get_search_fallback_urls(store_url):
    seller = get_seller_name_from_store_url(store_url)
    if not seller:
        return []
    return [
        build_search_url_for_seller(seller, per_page=200, sort="12"),
        build_search_url_for_seller(seller, per_page=96, sort="12"),
        build_search_url_for_seller(seller, per_page=48, sort="12"),
    ]


def find_first_nonblocked_http_url(url, session):
    candidates = [url]
    if is_ebay_store_url(url):
        candidates.extend([u for u in get_search_fallback_urls(url) if u])
    
    for candidate in candidates:
        try:
            response = session.get(candidate, timeout=20)
            if response.status_code != 200:
                continue
            return candidate
        except Exception:
            continue
    return None


def get_driver(use_headless=True):
    if not SELENIUM_AVAILABLE:
        raise RuntimeError("Selenium is not installed")
    use_uc = UC_AVAILABLE and os.environ.get("USE_UC", "0") == "1"
    chrome_options = uc.ChromeOptions() if use_uc else Options()

    if use_headless:
        chrome_options.add_argument("--headless")
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
    chrome_options.add_argument("--remote-debugging-port=0")
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

    # Streamlit Cloud: locate chrome/chromedriver binaries
    import shutil
    chrome_bin = None
    for candidate in ["/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/chromium"]:
        if os.path.exists(candidate):
            chrome_bin = candidate
            break
    if chrome_bin:
        chrome_options.binary_location = chrome_bin

    chromedriver_path = None
    for candidate in ["/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver"]:
        if os.path.exists(candidate):
            chromedriver_path = candidate
            break
    if not chromedriver_path:
        chromedriver_path = shutil.which("chromedriver")

    driver = None
    if use_uc:
        try:
            driver = uc.Chrome(options=chrome_options)
        except Exception:
            driver = None
    if driver is None:
        service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
        driver = webdriver.Chrome(service=service, options=chrome_options)

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
        "security measure",
        "bot check",
        "please enable cookies",
        "pardon our interruption",
        "verifying your browser",
    ]
    return any(phrase in text for phrase in blocked_phrases)


def is_valid_product_page(html, url=None, soup=None):
    if not html or len(html) < 300:
        return False
    if soup is None:
        soup = BeautifulSoup(html, "html.parser")
    if is_blocked_page(html, soup=soup):
        return False
    
    # Check for any product indicator: title, price, image, or structured data
    has_title = (
        _pick_text(soup, ["meta[property='og:title']", "meta[name='twitter:title']"], attr="content")
        or _pick_text(soup, ["h1#itemTitle", "h1[itemprop='name']", ".it-ttl"])
    )
    
    has_price = _pick_text(
        soup,
        ["meta[property='product:price:amount']", "span#prcIsum", ".x-price-primary", "span.s-item__price"],
    )
    
    has_image = _pick_text(
        soup,
        ["meta[property='og:image']", "img#icImg"],
        attr="src" if "img" in str(soup.select_one("img#icImg")) else "content"
    )
    
    has_json_ld = any(
        "Product" in (json.loads(script.string or "{}").get("@type", ""))
        for script in soup.find_all("script", {"type": "application/ld+json"})
        if script.string
    )
    
    # Page is valid if it has title OR (price + image) OR JSON-LD Product
    return bool(has_title or (has_price and has_image) or has_json_ld)


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


def get_total_products_http(store_url, session=None):
    try:
        url = normalize_store_url(store_url)
        own_session = False
        if session is None:
            session = create_http_session()
            own_session = True

        response = session.get(url, timeout=20)
        if response.status_code != 200:
            return None

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        patterns = [
            (r"of\s+([0-9,]+)\s+results", 1),
            (r"([0-9,]+)\s+results", 1),
            (r"of\s+([0-9,]+)\s+items", 1),
            (r"([0-9,]+)\s+items", 1),
            (r"of\s+([0-9,]+)\s+listings", 1),
            (r"([0-9,]+)\s+listings", 1),
            (r"([0-9,]+)\s+products", 1),
        ]
        for pattern, group in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                total_str = match.group(group).replace(",", "")
                if total_str.isdigit() and int(total_str) > 0:
                    return int(total_str)

        # Try from srp-controls
        count_el = soup.select_one(".srp-controls__count-heading, .rcnt, span.BOLD:first-child")
        if count_el:
            count_text = count_el.get_text(" ", strip=True)
            match = re.search(r"([0-9,]+)", count_text)
            if match:
                total_str = match.group(1).replace(",", "")
                if total_str.isdigit() and int(total_str) > 0:
                    return int(total_str)

        if own_session:
            session.close()
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


def get_all_product_links_http(store_url, on_progress=None, max_pages=50, session=None):
    """Collect product links using HTTP requests only (no Selenium)"""
    original_url = _strip_text(store_url)
    try:
        normalized_store_url = normalize_store_url(store_url)
    except ValueError:
        normalized_store_url = original_url

    all_links = set()
    own_session = False
    if session is None:
        session = create_http_session()
        own_session = True

    active_url = find_first_nonblocked_http_url(normalized_store_url, session)
    if not active_url:
        active_url = normalized_store_url

    base_url = active_url.split("&_pgn=")[0] if "_pgn=" in active_url else active_url
    separator = "&" if "?" in base_url else "?"
    no_change_counter = 0
    last_page_with_links = 0

    for page in range(1, max_pages + 1):
        url = f"{base_url}{separator}_pgn={page}"
        try:
            response = session.get(url, timeout=15)
            if response.status_code != 200:
                break
            
            soup = BeautifulSoup(response.text, "html.parser")
            page_links = []

            # Try multiple selectors to find product links
            selectors_to_try = [
                ("a[href*='/itm/']", None),
                ("a.s-item__link", None),
                (".s-item a[href*='/itm/']", None),
                ("a", "href"),
            ]
            
            for selector, attr in selectors_to_try:
                elements = soup.select(selector) if attr is None else [e for e in soup.find_all(attr) if '/itm/' in str(e)]
                for elem in elements:
                    try:
                        href = elem.get("href", "") if attr is None else str(elem)
                        if is_valid_item_link(href):
                            normalized = normalize_url(href)
                            if normalized not in all_links:
                                all_links.add(normalized)
                                page_links.append(normalized)
                    except Exception:
                        continue
                
                if page_links:
                    break

            if on_progress:
                on_progress(page, max_pages, len(page_links), len(page_links), len(all_links))

            if not page_links:
                no_change_counter += 1
                if no_change_counter >= 3:
                    break
            else:
                no_change_counter = 0
                last_page_with_links = page

            time.sleep(0.8)
        except Exception as e:
            break

    if own_session:
        session.close()
    
    return list(all_links)


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
        or _pick_text(soup, ["h1#itemTitle", "h1[itemprop='name']", ".it-ttl"])
        or _pick_text(soup, ["h1"])
    )
    if not data["title"]:
        for h1 in soup.find_all("h1"):
            text = clean_title(h1.get_text(" ", strip=True))
            if text and len(text) > 3 and text.lower() not in ["ebay", "shop", "save", "sign in"]:
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
                "span.BOLD",
            ],
        )
    )

    data["image"] = _pick_text(
        soup,
        ["meta[property='og:image']", "meta[property='og:image:secure_url']"],
        attr="content",
    )
    if not data["image"]:
        for img_sel in ["img#icImg", ".ux-image-carousel-item img", "img.s-item__img", "img[alt*='product']"]:
            img_elem = soup.select_one(img_sel)
            if img_elem:
                src = img_elem.get("src", "") or img_elem.get("data-src", "")
                if src:
                    data["image"] = clean_text(src)
                    break

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
        for match in re.finditer(r"\$\s*([0-9,]+(?:\.[0-9]{2})?)", html):
            data["price"] = clean_price(match.group(0))
            break

    for key in data:
        if isinstance(data[key], str):
            data[key] = clean_text(data[key])
            if len(data[key]) > 500:
                data[key] = data[key][:500]

    # Accept page if it has a title, regardless of other fields
    if data["title"] and len(data["title"]) > 3 and not is_blocked_page(html, soup=soup):
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
    max_retries = 5

    for attempt in range(max_retries):
        try:
            if session is None:
                session = create_http_session()
            response = session.get(url, timeout=20)
            if debug and attempt == 0:
                print(f"[DEBUG] URL: {url} | Status: {response.status_code}")
            if response.status_code != 200:
                if attempt < max_retries - 1:
                    time.sleep(2 + (attempt * 1.5))
                    continue
                return None
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            if is_blocked_page(html, soup=soup):
                # Retry blocked pages a few more times
                if attempt < max_retries - 2:
                    time.sleep(3 + (attempt * 2))
                    continue
                # Fall back to Selenium if available
                if selenium_driver is not None:
                    if selenium_lock is not None:
                        with selenium_lock:
                            return _scrape_product_with_selenium(url, selenium_driver, debug=debug)
                    return _scrape_product_with_selenium(url, selenium_driver, debug=debug)
                return None
            
            # Try to extract data even if page validation is loose
            data = _extract_product_data(html, soup, url)
            if data:
                return data
            
            if attempt < max_retries - 1:
                time.sleep(1 + (attempt * 1))
                continue
            return None
        except Exception as e:
            if debug and attempt == 0:
                print(f"[DEBUG] scrape_product exception for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 + (attempt * 1.5))
                continue
            return None
    return None


def scrape_all_products(urls, selenium_driver=None, use_headless=True, max_workers=12, on_progress=None, session=None):
    if not urls:
        return []
    all_data = []
    seen_items = set()
    total = len(urls)
    progress = {"done": 0, "scraped": 0}
    progress_lock = threading.Lock()
    batch_size = 16
    max_workers_actual = 8

    def report():
        if on_progress:
            on_progress(progress["done"], total, progress["scraped"])

    own_session = False
    if session is None:
        session = create_http_session()
        own_session = True
    selenium_lock = threading.Lock()

    def scrape_request(index, url):
        delay = (index % max_workers_actual) * 0.5
        time.sleep(delay)
        return url, scrape_product(
            url,
            session=session,
            selenium_driver=selenium_driver,
            selenium_lock=selenium_lock,
            debug=False,
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
                        report()
        if batch_end < len(urls):
            time.sleep(1.5)
    if own_session:
        session.close()
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


def _scrape_all_products_ui(urls, progress_bar, selenium_driver=None, use_headless=True, session=None):
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
        session=session,
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
    cwd = os.getcwd()
    return (
        os.environ.get("STREAMLIT_RUNTIME_ENVIRONMENT") == "production"
        or os.environ.get("STREAMLIT_SERVER_HEADLESS") == "true"
        or "/mount/src/" in cwd
        or "/app/" in cwd
        or os.environ.get("IS_STREAMLIT_CLOUD") == "1"
    )


def main():
    st.sidebar.header("🔧 Scraper Settings")

    on_cloud = is_streamlit_cloud()

    if on_cloud:
        mode_options = ["Auto (HTTP Only)", "Manual Links"]
        default_mode = "Auto (HTTP Only)"
    else:
        mode_options = ["Auto (Browser + HTTP)", "Manual Links", "Local Only"]
        default_mode = "Auto (Browser + HTTP)"

    mode = st.sidebar.radio(
        "Select scraping mode:",
        mode_options,
        index=mode_options.index(default_mode),
        help="Auto: Try browser first, then HTTP. Manual: Paste/upload links. Local: Selenium only (no Cloud)"
    )

    headless = st.sidebar.checkbox(
        "Run Chrome headless",
        value=True,
        disabled=on_cloud,
        help="Browser runs in background (recommended). Uncheck only for debugging.",
    )

    if mode == "Manual Links":
        st.header("📋 Manual Link Entry")
        st.markdown("### Paste product links (one per line)")
        manual_links_text = st.text_area(
            "Product Links",
            height=200,
            placeholder="https://www.ebay.com/itm/123456789\nhttps://www.ebay.com/itm/987654321\n...",
            label_visibility="collapsed",
        )
        
        if manual_links_text.strip():
            manual_links = [url.strip() for url in manual_links_text.split('\n') if url.strip()]
            manual_links = [url for url in manual_links if 'ebay.com' in url.lower() and '/itm/' in url.lower()]
            st.success(f"✅ Parsed {len(manual_links)} links")
            
            if st.button("🚀 Start Scraping", type="primary", disabled=not manual_links):
                with st.spinner("🔄 Scraping products... This may take a few minutes"):
                    http_session = create_http_session()
                    scrape_progress = st.progress(0, text="Starting product scraping...")
                    scraped_data = _scrape_all_products_ui(
                        manual_links,
                        scrape_progress,
                        selenium_driver=None,
                        use_headless=headless,
                        session=http_session,
                    )
                    st.success(f"📦 Products Scraped: {len(scraped_data):,}")

                st.header("📥 Step 2: Download Results")
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
                    st.error("❌ No products were scraped. Please check the links and try again.")
        return

    store_url = st.sidebar.text_input(
        "eBay Store URL",
        placeholder="https://www.ebay.com/str/STORENAME",
        help="Enter the eBay store URL (works best locally with Selenium)",
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
        http_session = None

        if on_cloud:
            st.info(
                "☁️ **Streamlit Cloud Mode**: Using HTTP-only scraping. "
                "eBay may block some requests. If scraping fails, switch to 'Manual Links' mode."
            )
            http_session = create_http_session()
        elif mode != "Manual Links":
            with st.spinner("🔄 Initializing scraper..."):
                try:
                    driver = get_driver(use_headless=headless)
                    st.success("✅ Scraper initialized successfully!")
                except Exception as e:
                    if mode == "Local Only":
                        st.error(f"❌ Browser initialization failed: {str(e)[:100]}")
                        return
                    st.warning(f"⚠️ Browser automation unavailable, using HTTP-only mode: {str(e)[:100]}")
                    http_session = create_http_session()

        st.header("📊 Step 1: Detecting Total Products")
        total_progress = st.progress(0, text="Analyzing store...")
        total_products = None

        if driver:
            total_products = get_total_products(store_url, driver)
        elif http_session or not driver:
            if http_session is None:
                http_session = create_http_session()
            total_products = get_total_products_http(store_url, session=http_session)

        if total_products:
            total_progress.progress(1.0, text=f"✅ Found {total_products:,} total products")
            st.success(f"🎯 Total Products Detected: {total_products:,}")
        else:
            total_progress.progress(1.0, text="ℹ️ Will scrape all available products")
            st.info("ℹ️ Product count detection failed. Will scrape all available products from the store.")

        st.header("🔗 Step 2: Collecting Product Links")
        links_progress = st.progress(0, text="Starting link collection...")
        all_links = []
        
        if driver:
            all_links = _get_all_product_links_ui(store_url, driver, links_progress, total_products)
            links_progress.progress(1.0, text=f"✅ Found {len(all_links):,} product links")
            if all_links:
                st.success(f"🔗 Product Links Collected: {len(all_links):,}")
            else:
                st.warning("⚠️ No links found via browser. This store might be blocked or empty.")
        else:
            st.info("ℹ️ Collecting links via HTTP (no browser)...")
            if http_session is None:
                http_session = create_http_session()
            def on_http_progress(page, max_pages, found, new_items, total_links):
                progress_ratio = min(page / max(max_pages, 1), 1.0)
                links_progress.progress(
                    progress_ratio,
                    text=f"Page {page}: Collected {total_links} unique links"
                )
            all_links = get_all_product_links_http(
                store_url,
                on_progress=on_http_progress,
                max_pages=100,
                session=http_session,
            )
            links_progress.progress(1.0, text=f"Found {len(all_links):,} product links")
            if all_links:
                st.success(f"🔗 Product Links Collected: {len(all_links):,}")
            else:
                st.error(
                    "❌ **eBay blocked link collection via HTTP.** This is an eBay anti-bot protection. "
                    "\n\n**Solutions:**\n"
                    "1. **Run locally** with a real browser (Selenium)\n"
                    "2. **Use Manual Mode** - paste product links directly\n"
                    "3. **Try a different store URL**"
                )
                if driver:
                    driver.quit()
                if http_session:
                    try:
                        http_session.close()
                    except Exception:
                        pass
                return

        st.header("📦 Step 3: Scraping Product Details")

        if not all_links:
            st.error("❌ No product links found. Please check the store URL and try again.")
            if driver:
                driver.quit()
            if http_session:
                try:
                    http_session.close()
                except Exception:
                    pass
            return

        with st.spinner("🔄 Scraping products... This may take a few minutes"):
            scrape_progress = st.progress(0, text="Starting product scraping...")
            scraped_data = _scrape_all_products_ui(
                all_links,
                scrape_progress,
                selenium_driver=driver,
                use_headless=headless,
                session=http_session,
            )
            st.success(f"📦 Products Scraped: {len(scraped_data):,}")

        if driver:
            driver.quit()
        if http_session is not None:
            try:
                http_session.close()
            except Exception:
                pass

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
        mode_label = "Auto (HTTP Only)" if on_cloud else "Auto (Browser + HTTP)"
        st.markdown(f"""
        ### Modes Available:
        
        **1. {mode_label}** (Best for stores)
        - {'HTTP-only scraping (no browser required)' if on_cloud else 'Automatically tries browser + HTTP scraping'}
        - {'Best for Streamlit Cloud deployment' if on_cloud else 'Works best when run locally'}
        
        **2. Manual Links** (Always works)
        - Paste or upload product links directly
        - Use when automatic scraping is blocked
        
        **3. Local Only** {'(Selenium required)' if not on_cloud else '(not available on cloud)'}
        - {'Forces browser-based scraping' if not on_cloud else 'Unavailable in cloud mode'}
        - {'Most reliable for anti-bot protection' if not on_cloud else 'Use Auto or Manual mode instead'}
        """)

    with st.sidebar.expander("🔗 Sample URLs"):
        st.code("""
https://www.ebay.com/str/prolinefix
https://www.ebay.com/str/mystore
https://www.ebay.com/itm/123456789
        """)


if __name__ == "__main__":
    main()

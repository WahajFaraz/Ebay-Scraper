#!/usr/bin/env python3
"""
Diagnostic script to understand the eBay store page structure.
Helps identify why extra products are being scraped.
"""

import sys
import os
import re
import time
import random
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from ebay_scraper import (
    find_chromedriver, clean_url, extract_item_id, is_valid_product_url,
    STEALTH_JS, USER_AGENTS, STORE_URL, PAGE_TIMEOUT
)


def init_driver():
    """Initialize Chrome driver with stealth settings."""
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
        driver = webdriver.Chrome(service=service, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    driver.implicitly_wait(2)
    return driver


def analyze_page_structure(driver, url):
    """Analyze the page structure to understand what elements are present."""
    print(f"\n{'='*70}")
    print(f"Analyzing: {url}")
    print(f"{'='*70}")

    try:
        driver.get(url)
        time.sleep(3)

        # Scroll to load lazy content
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)

        # Get page title
        print(f"\nPage title: {driver.title}")

        # Check for security page
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        security_keywords = ["captcha", "recaptcha", "verify you are human", "robot check"]
        for kw in security_keywords:
            if kw in body_text:
                print(f"WARNING: Security page detected! Keyword: {kw}")
                return

        # Analyze store card selectors
        print(f"\n--- Store Card Selectors ---")
        store_selectors = [
            "article.str-item-card",
            ".str-item-card",
            ".StoreFrontItemCard",
            ".store-item-card",
            "[data-testid='store-item-card']",
            ".str-card",
            "li.s-item",
        ]

        for sel in store_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                if elements:
                    print(f"  {sel}: {len(elements)} elements")
                    # Check first few elements
                    for i, el in enumerate(elements[:3]):
                        try:
                            text = el.text.strip()[:100]
                            print(f"    [{i}] {text}")
                        except:
                            pass
            except Exception as e:
                print(f"  {sel}: ERROR - {e}")

        # Analyze search selectors
        print(f"\n--- Search Selectors ---")
        search_selectors = [
            ".s-item",
            ".brwrvr-item",
            "li[data-view*='mi:']",
            ".b-list__items_nolist li",
            "[data-testid='product-card']",
            ".rst-scroll-items li",
            ".srp-results li",
        ]

        for sel in search_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                if elements:
                    print(f"  {sel}: {len(elements)} elements")
            except Exception as e:
                print(f"  {sel}: ERROR - {e}")

        # Check for main content containers
        print(f"\n--- Main Content Containers ---")
        containers = [
            "#mainContent",
            ".str-container",
            ".str-storefront",
            "[data-testid='storefront']",
            ".str-items",
            ".str-item-list",
            ".srp-results",
            "#Content",
            ".content-wrapper",
        ]

        for sel in containers:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                if elements:
                    print(f"  {sel}: {len(elements)} elements")
            except Exception as e:
                print(f"  {sel}: ERROR - {e}")

        # Check for recommendation sections
        print(f"\n--- Recommendation Sections ---")
        rec_selectors = [
            ".str-recommendations",
            ".recommendations",
            "[data-testid='recommendations']",
            ".related-items",
            ".similar-items",
            ".you-may-also-like",
            ".str-related-items",
        ]

        for sel in rec_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                if elements:
                    print(f"  {sel}: {len(elements)} elements")
            except Exception as e:
                print(f"  {sel}: ERROR - {e}")

        # Check pagination
        print(f"\n--- Pagination ---")
        try:
            page_items = driver.find_elements(By.CSS_SELECTOR, "a.pagination__item[href*='_pgn=']")
            if page_items:
                print(f"  Pagination links found: {len(page_items)}")
                for p in page_items:
                    try:
                        href = p.get_attribute("href") or ""
                        m = re.search(r'_pgn=(\d+)', href)
                        if m:
                            print(f"    Page {m.group(1)}")
                    except:
                        pass
            else:
                print("  No pagination links found")
        except Exception as e:
            print(f"  Pagination error: {e}")

        # Check for next button
        try:
            next_btn = driver.find_elements(By.CSS_SELECTOR, "a.pagination__next[href]")
            if next_btn:
                print(f"  Next button found")
            else:
                print("  No next button found")
        except Exception as e:
            print(f"  Next button error: {e}")

        # Count all product links on page
        print(f"\n--- All Product Links ---")
        all_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='itm/']")
        print(f"  Total a[href*='itm/'] links: {len(all_links)}")

        # Check which links are in main content
        in_main = 0
        not_in_main = 0
        for link in all_links:
            try:
                href = link.get_attribute("href") or ""
                if "ebay.com" not in href.lower():
                    continue
                url_clean = clean_url(href)
                if not is_valid_product_url(url_clean):
                    continue

                # Check if in main content
                result = driver.execute_script("""
                    function isDescendant(parent, child) {
                        var node = child.parentNode;
                        while (node != null) {
                            if (node == parent) return true;
                            node = node.parentNode;
                        }
                        return false;
                    }
                    var containers = document.querySelectorAll(
                        '#mainContent, .str-container, .str-storefront, ' +
                        '[data-testid="storefront"], .str-items, .str-item-list, ' +
                        '.srp-results, #Content, .content-wrapper'
                    );
                    for (var i = 0; i < containers.length; i++) {
                        if (isDescendant(containers[i], arguments[0])) return true;
                    }
                    return false;
                """, link)

                if result:
                    in_main += 1
                else:
                    not_in_main += 1
            except:
                pass

        print(f"  In main content: {in_main}")
        print(f"  NOT in main content: {not_in_main}")

        # Get page source for further analysis
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, "lxml")

        # Check for JSON-LD structured data
        print(f"\n--- JSON-LD Structured Data ---")
        scripts = soup.find_all("script", type="application/ld+json")
        print(f"  JSON-LD scripts found: {len(scripts)}")
        for i, script in enumerate(scripts[:3]):
            try:
                import json
                data = json.loads(script.string)
                if isinstance(data, dict):
                    print(f"  Script {i}: @type = {data.get('@type', 'unknown')}")
            except:
                pass

        # Check for store name in page
        print(f"\n--- Store Name Detection ---")
        store_name_selectors = [
            ".str-store-name",
            ".store-name",
            "[data-testid='store-name']",
            ".str-header__store-name",
            "h1",
        ]
        for sel in store_name_selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el:
                    print(f"  {sel}: {el.text.strip()[:50]}")
            except:
                pass

    except Exception as e:
        print(f"Error analyzing page: {e}")
        import traceback
        traceback.print_exc()


def main():
    store_url = "https://www.ebay.com/str/humanit"

    print("eBay Store Page Diagnostic")
    print(f"Store URL: {store_url}")

    driver = None
    try:
        driver = init_driver()

        # Analyze first page
        analyze_page_structure(driver, store_url)

        # Analyze second page if exists
        print(f"\n{'='*70}")
        print("Checking page 2...")
        print(f"{'='*70}")
        analyze_page_structure(driver, store_url + "?_pgn=2")

    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()

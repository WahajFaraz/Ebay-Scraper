import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st

from ebay_scraper import (
    ListingScraper, DetailScraper, export_csv, log, MAX_WORKERS,
    SCRAPE_STATE as EB_SCRAPE_STATE,
)

st.set_page_config(page_title="eBay Store Scraper", page_icon="🛒", layout="centered")
st.title("eBay Store Scraper")
st.markdown("Enter an eBay store URL and download all products as CSV")

if "started" not in st.session_state:
    st.session_state.started = False


def _output_path(url):
    m = re.search(r"/str/([^/?]+)", url)
    name = m.group(1) if m else "store"
    return os.path.abspath(f"{name}_products.csv")


def _run_scrape(url):
    start = time.time()
    out_path = _output_path(url)
    try:
        listing = ListingScraper(url)
        listing._init_driver()
        listing.scrape_listings()
        listing._quit_driver()

        products = listing.products
        EB_SCRAPE_STATE["total"] = len(products)

        if not products:
            export_csv([], out_path)
            EB_SCRAPE_STATE["output_file"] = out_path
            EB_SCRAPE_STATE["done"] = True
            EB_SCRAPE_STATE["running"] = False
            return

        detail = DetailScraper()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(detail.scrape, p): p for p in products}
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    log.error(f"Detail worker failed: {e}")

        export_csv(products, out_path)
        EB_SCRAPE_STATE["output_file"] = out_path
        EB_SCRAPE_STATE["done"] = True
    except Exception as e:
        log.exception("Scrape failed")
        EB_SCRAPE_STATE["error"] = str(e)
    finally:
        EB_SCRAPE_STATE["running"] = False
        log.info(f"Scrape finished in {time.time()-start:.1f}s")


store_url = st.text_input(
    "eBay Store URL",
    placeholder="https://www.ebay.com/str/your-store-name",
    key="store_url_input",
    disabled=EB_SCRAPE_STATE.get("running", False),
)

if st.button("Start Scraping", disabled=EB_SCRAPE_STATE.get("running", False), use_container_width=True):
    if not store_url:
        st.error("Please enter a store URL")
        st.stop()
    st.session_state.started = True
    EB_SCRAPE_STATE["running"] = True
    EB_SCRAPE_STATE["done"] = False
    EB_SCRAPE_STATE["error"] = None
    EB_SCRAPE_STATE["output_file"] = None
    EB_SCRAPE_STATE["phase"] = ""
    EB_SCRAPE_STATE["page"] = 0
    EB_SCRAPE_STATE["products_found"] = 0
    EB_SCRAPE_STATE["detail_progress"] = 0
    EB_SCRAPE_STATE["detail_total"] = 0
    EB_SCRAPE_STATE["total"] = 0
    threading.Thread(target=_run_scrape, args=(store_url,), daemon=True).start()
    st.rerun()

out_path = EB_SCRAPE_STATE.get("output_file")
if EB_SCRAPE_STATE.get("done") and out_path and os.path.exists(out_path):
    with open(out_path, "rb") as f:
        st.download_button(
            "Download CSV",
            f,
            file_name=os.path.basename(out_path),
            mime="text/csv",
            use_container_width=True,
        )


status_placeholder = st.empty()
progress_placeholder = st.empty()

error = EB_SCRAPE_STATE.get("error")
running = EB_SCRAPE_STATE.get("running", False)
done = EB_SCRAPE_STATE.get("done", False)

if error:
    status_placeholder.error(f"Error: {error}")

elif running:
    phase = EB_SCRAPE_STATE.get("phase", "")
    if phase == "listing":
        page = EB_SCRAPE_STATE.get("page", 0)
        pf = EB_SCRAPE_STATE.get("products_found", 0)
        status_placeholder.info(f"Scraping listings... Page {page} \u00b7 {pf} products found")
    elif phase == "details":
        dp = EB_SCRAPE_STATE.get("detail_progress", 0)
        dt = EB_SCRAPE_STATE.get("detail_total", 0)
        total = EB_SCRAPE_STATE.get("total", 0)
        if dt > 0:
            progress_placeholder.progress(dp / dt, text=f"Scraping details... {dp}/{dt}")
            status_placeholder.info(f"Scraping details... ({dp}/{dt}) \u00b7 {total} products total")
        else:
            status_placeholder.info("Preparing detail scraping...")
    else:
        status_placeholder.info("Starting...")
    time.sleep(1)
    st.rerun()

elif done:
    total = EB_SCRAPE_STATE.get("total", 0)
    status_placeholder.success(f"Complete! {total} products scraped")
    out_path = EB_SCRAPE_STATE.get("output_file")
    if out_path and os.path.exists(out_path):
        st.info(f"Output file: `{out_path}`")

elif not st.session_state.started:
    status_placeholder.info("Enter a store URL and click Start Scraping")

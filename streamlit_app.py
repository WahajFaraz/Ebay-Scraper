import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st

from ebay_scraper import (
    ListingScraper, DetailScraper, export_csv, log, MAX_WORKERS,
    SCRAPE_STATE as EB_SCRAPE_STATE,
)

# Persist scrape state across page refreshes (same session)
if "scrape_ref" not in st.session_state:
    st.session_state.scrape_ref = EB_SCRAPE_STATE

st.set_page_config(page_title="eBay Store Scraper", page_icon="🛒", layout="centered")

# ── Global Styles ──────────────────────────────────────────────
LIGHT_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.stApp { background: #1a1a1a; }
.main > div { padding: 1rem 2rem; }
h1 { font-weight: 700; font-size: 1.75rem !important; letter-spacing: -0.02em; color: #ffffff !important; }
p, span, label, [data-testid="stMarkdownContainer"] { color: #e0e0e0 !important; }
.card { background: #2a2a2a; border-radius: 12px; padding: 1.5rem; box-shadow: 0 2px 8px rgba(0,0,0,0.3); margin-bottom: 1rem; border: 1px solid #3a3a3a; }
.stButton button { border-radius: 8px; font-weight: 600; font-size: 0.9rem; height: 44px; transition: all 0.15s; background-color: #3a3a3a !important; color: #ffffff !important; border: 1px solid #4a4a4a !important; }
.stButton button:hover { border-color: #e53935 !important; }
.stButton button:active { transform: scale(0.97); }
.stTextInput input { border-radius: 8px; border: 1px solid #4a4a4a !important; font-size: 0.9rem; background-color: #3a3a3a !important; color: #ffffff !important; }
.stTextInput input:focus { border-color: #e53935 !important; box-shadow: 0 0 0 3px rgba(229,57,53,0.2) !important; }
.stProgress > div > div > div { background-color: #e53935 !important; border-radius: 4px; }
.stProgress > div > div { background-color: #3a3a3a !important; }
.metric { text-align: center; padding: 0.75rem; background: #3a3a3a !important; border-radius: 8px; border: 1px solid #4a4a4a !important; }
.metric-val { font-size: 1.5rem; font-weight: 700; color: #e53935; line-height: 1.2; }
.metric-label { font-size: 0.75rem; color: #9e9e9e; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
.stAlert, .stInfo, .stSuccess, .stError { background-color: #2a2a2a !important; border: 1px solid #3a3a3a !important; color: #e0e0e0 !important; }
[data-testid="stNotification"] { background-color: #2a2a2a !important; }
div[data-testid="stDownloadButton"] button { background-color: #e53935 !important; color: white !important; border: none !important; }
div[data-testid="stDownloadButton"] button:hover { background-color: #b71c1c !important; }
</style>
"""

DARK_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.stApp { background: #000000 !important; color: #e0e0e0 !important; }
.main > div { padding: 1rem 2rem; }
h1 { font-weight: 700; font-size: 1.75rem !important; letter-spacing: -0.02em; color: #ffffff !important; }
h2, h3, p, span, label, [data-testid="stMarkdownContainer"] { color: #e0e0e0 !important; }
.card { background: #0d0d0d !important; border-radius: 12px; padding: 1.5rem; box-shadow: 0 2px 8px rgba(0,0,0,0.5); margin-bottom: 1rem; border: 1px solid #2a2a2a !important; }
.stButton button { border-radius: 8px; font-weight: 600; font-size: 0.9rem; height: 44px; transition: all 0.15s; background-color: #1a1a1a !important; color: #ffffff !important; border: 1px solid #3a3a3a !important; }
.stButton button:hover { border-color: #e53935 !important; }
.stButton button:active { transform: scale(0.97); }
.stTextInput input { border-radius: 8px; border: 1px solid #3a3a3a !important; font-size: 0.9rem; background-color: #1a1a1a !important; color: #ffffff !important; }
.stTextInput input:focus { border-color: #e53935 !important; box-shadow: 0 0 0 3px rgba(229,57,53,0.25) !important; }
.stProgress > div > div > div { background-color: #e53935 !important; border-radius: 4px; }
.stProgress > div > div { background-color: #1a1a1a !important; }
.metric { text-align: center; padding: 0.75rem; background: #1a1a1a !important; border-radius: 8px; border: 1px solid #2a2a2a !important; }
.metric-val { font-size: 1.5rem; font-weight: 700; color: #e53935; line-height: 1.2; }
.metric-label { font-size: 0.75rem; color: #757575; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
.stAlert, .stInfo, .stSuccess, .stError { background-color: #0d0d0d !important; border: 1px solid #2a2a2a !important; color: #e0e0e0 !important; }
[data-testid="stNotification"] { background-color: #0d0d0d !important; }
div[data-testid="stDownloadButton"] button { background-color: #e53935 !important; color: white !important; border: none !important; }
div[data-testid="stDownloadButton"] button:hover { background-color: #b71c1c !important; }
</style>
"""

# ── App ────────────────────────────────────────────────────────
dark_mode = st.toggle("Dark Mode", value=False)
st.markdown(DARK_CSS if dark_mode else LIGHT_CSS, unsafe_allow_html=True)

# Header
col_title, _ = st.columns([3, 1])
with col_title:
    st.title("🛒 eBay Store Scraper")
    st.markdown("Extract all products from any eBay store in seconds")

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

        if EB_SCRAPE_STATE.get("stop"):
            return

        products = listing.products
        EB_SCRAPE_STATE["total"] = len(products)

        if not products:
            export_csv([], out_path)
            EB_SCRAPE_STATE["output_file"] = out_path
            EB_SCRAPE_STATE["done"] = True
            return

        detail = DetailScraper()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(detail.scrape, p): p for p in products}
            for f in as_completed(futures):
                if EB_SCRAPE_STATE.get("stop"):
                    for ff in futures:
                        ff.cancel()
                    break
                try:
                    f.result()
                except Exception:
                    pass

        if EB_SCRAPE_STATE.get("stop"):
            return

        export_csv(products, out_path)
        EB_SCRAPE_STATE["output_file"] = out_path
        EB_SCRAPE_STATE["done"] = True
    except Exception as e:
        log.exception("Scrape failed")
        EB_SCRAPE_STATE["error"] = str(e)
    finally:
        EB_SCRAPE_STATE["running"] = False
        EB_SCRAPE_STATE["stop"] = False
        log.info(f"Scrape finished in {time.time()-start:.1f}s")


# ── Input Section ──────────────────────────────────────────────
with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    store_url = st.text_input(
        "eBay Store URL",
        placeholder="https://www.ebay.com/str/your-store-name",
        key="store_url_input",
        disabled=EB_SCRAPE_STATE.get("running", False),
    )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        start_disabled = EB_SCRAPE_STATE.get("running", False)
        if st.button("▶ Start Scraping", disabled=start_disabled, use_container_width=True):
            if not store_url:
                st.error("Please enter a store URL")
                st.stop()
            st.session_state.started = True
            EB_SCRAPE_STATE["running"] = True
            EB_SCRAPE_STATE["done"] = False
            EB_SCRAPE_STATE["stop"] = False
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

    with col_b:
        if EB_SCRAPE_STATE.get("running", False):
            if st.button("■ Stop", type="primary", use_container_width=True):
                EB_SCRAPE_STATE["stop"] = True
                st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


# ── Download Section ───────────────────────────────────────────
out_path = EB_SCRAPE_STATE.get("output_file")
if EB_SCRAPE_STATE.get("done") and out_path and os.path.exists(out_path):
    name = os.path.basename(out_path)
    size = os.path.getsize(out_path)
    size_str = f"{size/1024:.1f} KB" if size < 1024*1024 else f"{size/(1024*1024):.1f} MB"
    with st.container():
        st.markdown('<div class="card">', unsafe_allow_html=True)
        c1, c2 = st.columns([2, 1])
        with c1:
            st.markdown(f"**✅ {name}**  \n{size_str}")
        with c2:
            with open(out_path, "rb") as f:
                st.download_button(
                    "⬇ Download CSV",
                    f,
                    file_name=name,
                    mime="text/csv",
                    use_container_width=True,
                )
        st.markdown('</div>', unsafe_allow_html=True)


# ── Status & Progress ──────────────────────────────────────────
status_placeholder = st.empty()
progress_placeholder = st.empty()
metrics_placeholder = st.empty()

error = EB_SCRAPE_STATE.get("error")
running = EB_SCRAPE_STATE.get("running", False)
done = EB_SCRAPE_STATE.get("done", False)

if error:
    msg = f"❌ **Error** — {error}"
    status_placeholder.error(msg)

elif running:
    stopped = EB_SCRAPE_STATE.get("stop", False)
    phase = EB_SCRAPE_STATE.get("phase", "")

    if stopped:
        status_placeholder.warning("⏳ **Stopping…** Finishing current tasks")

    elif phase == "listing":
        page = EB_SCRAPE_STATE.get("page", 0)
        pf = EB_SCRAPE_STATE.get("products_found", 0)
        status_placeholder.info(f"📋 **Scraping listings** — Page {page} · {pf} products found")
        metrics_placeholder.markdown(
            f'<div style="display:flex;gap:1rem;margin-top:0.5rem">'
            f'<div class="metric"><div class="metric-val">{page}</div><div class="metric-label">Pages</div></div>'
            f'<div class="metric"><div class="metric-val">{pf}</div><div class="metric-label">Products</div></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    elif phase == "details":
        dp = EB_SCRAPE_STATE.get("detail_progress", 0)
        dt = EB_SCRAPE_STATE.get("detail_total", 0)
        total = EB_SCRAPE_STATE.get("total", 0)
        if dt > 0:
            pct = dp / dt
            progress_placeholder.progress(pct, text="")
            status_placeholder.info(f"🔍 **Scraping details** — {dp}/{dt} products · {total} total")
            metrics_placeholder.markdown(
                f'<div style="display:flex;gap:1rem;margin-top:0.5rem">'
                f'<div class="metric"><div class="metric-val">{dp}</div><div class="metric-label">Done</div></div>'
                f'<div class="metric"><div class="metric-val">{dt}</div><div class="metric-label">Total</div></div>'
                f'<div class="metric"><div class="metric-val">{pct:.0%}</div><div class="metric-label">Progress</div></div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            status_placeholder.info("🔍 Preparing detail scraping…")

    else:
        status_placeholder.info("🚀 **Starting…** Initializing browser")

    time.sleep(1)
    st.rerun()

elif done:
    total = EB_SCRAPE_STATE.get("total", 0)
    status_placeholder.success(f"✅ **Complete!** Scraped **{total}** products")

elif not st.session_state.started:
    status_placeholder.info("Enter a store URL above and click **Start Scraping**")

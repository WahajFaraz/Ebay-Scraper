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

st.set_page_config(page_title="eBay Store Scraper", page_icon="🛒", layout="centered")

# ── Global Styles ──────────────────────────────────────────────
LIGHT_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.stApp { background: #f8f9fa; }
.main > div { padding: 1rem 2rem; }
h1 { font-weight: 700; font-size: 1.75rem !important; letter-spacing: -0.02em; }
.card { background: #ffffff; border-radius: 12px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 1rem; border: 1px solid #e9ecef; }
.stButton button { border-radius: 8px; font-weight: 600; font-size: 0.9rem; height: 44px; transition: all 0.15s; }
.stButton button:active { transform: scale(0.97); }
.stTextInput input { border-radius: 8px; border: 1px solid #dee2e6; font-size: 0.9rem; }
.stTextInput input:focus { border-color: #1a73e8; box-shadow: 0 0 0 3px rgba(26,115,232,0.15); }
.stProgress > div > div > div { background-color: #1a73e8 !important; border-radius: 4px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em; }
.badge-listing { background: #e3f2fd; color: #1565c0; }
.badge-details { background: #fce4ec; color: #c62828; }
.badge-done { background: #e8f5e9; color: #2e7d32; }
.badge-stop { background: #fff3e0; color: #e65100; }
.metric { text-align: center; padding: 0.75rem; background: #f8f9fa; border-radius: 8px; border: 1px solid #e9ecef; }
.metric-val { font-size: 1.5rem; font-weight: 700; color: #1a73e8; line-height: 1.2; }
.metric-label { font-size: 0.75rem; color: #6c757d; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
</style>
"""

DARK_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.stApp { background: #0e1117 !important; color: #e8eaed !important; }
.main > div { padding: 1rem 2rem; }
h1 { font-weight: 700; font-size: 1.75rem !important; letter-spacing: -0.02em; color: #e8eaed !important; }
h2, h3, p, span, label, [data-testid="stMarkdownContainer"] { color: #e8eaed !important; }
.card { background: #1e2028 !important; border-radius: 12px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.3); margin-bottom: 1rem; border: 1px solid #2d2f3a !important; }
.stButton button { border-radius: 8px; font-weight: 600; font-size: 0.9rem; height: 44px; transition: all 0.15s; background-color: #2d2f3a !important; color: #e8eaed !important; border: 1px solid #3d3f4a !important; }
.stButton button:hover { border-color: #1a73e8 !important; }
.stButton button:active { transform: scale(0.97); }
.stTextInput input { border-radius: 8px; border: 1px solid #3d3f4a !important; font-size: 0.9rem; background-color: #262730 !important; color: #e8eaed !important; }
.stTextInput input:focus { border-color: #1a73e8 !important; box-shadow: 0 0 0 3px rgba(26,115,232,0.2) !important; }
.stProgress > div > div > div { background-color: #1a73e8 !important; border-radius: 4px; }
.stProgress > div > div { background-color: #2d2f3a !important; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em; }
.badge-listing { background: #1a3a5c; color: #64b5f6; }
.badge-details { background: #4a1c24; color: #ef9a9a; }
.badge-done { background: #1b3d2b; color: #81c784; }
.badge-stop { background: #4a2e1b; color: #ffb74d; }
.metric { text-align: center; padding: 0.75rem; background: #262730 !important; border-radius: 8px; border: 1px solid #2d2f3a !important; }
.metric-val { font-size: 1.5rem; font-weight: 700; color: #64b5f6; line-height: 1.2; }
.metric-label { font-size: 0.75rem; color: #9aa0a6; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
.stAlert, .stInfo, .stSuccess, .stError { background-color: #262730 !important; border: 1px solid #2d2f3a !important; color: #e8eaed !important; }
[data-testid="stNotification"] { background-color: #262730 !important; }
div[data-testid="stDownloadButton"] button { background-color: #1a73e8 !important; color: white !important; border: none !important; }
div[data-testid="stDownloadButton"] button:hover { background-color: #1557b0 !important; }
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
    status_placeholder.error(f"**Error** — {error}")

elif running:
    stopped = EB_SCRAPE_STATE.get("stop", False)
    phase = EB_SCRAPE_STATE.get("phase", "")

    if stopped:
        badge = '<span class="badge badge-stop">STOPPING</span>'
        status_placeholder.warning(f"{badge} Finishing current tasks…", unsafe_allow_html=True)

    elif phase == "listing":
        page = EB_SCRAPE_STATE.get("page", 0)
        pf = EB_SCRAPE_STATE.get("products_found", 0)
        badge = '<span class="badge badge-listing">LISTINGS</span>'
        status_placeholder.info(
            f"{badge} Page **{page}** · **{pf}** products found", unsafe_allow_html=True
        )
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
        badge = '<span class="badge badge-details">DETAILS</span>'
        if dt > 0:
            pct = dp / dt
            progress_placeholder.progress(pct, text=f"")
            status_placeholder.info(
                f"{badge} **{dp}/{dt}** products processed · {total} total",
                unsafe_allow_html=True,
            )
            metrics_placeholder.markdown(
                f'<div style="display:flex;gap:1rem;margin-top:0.5rem">'
                f'<div class="metric"><div class="metric-val">{dp}</div><div class="metric-label">Done</div></div>'
                f'<div class="metric"><div class="metric-val">{dt}</div><div class="metric-label">Total</div></div>'
                f'<div class="metric"><div class="metric-val">{pct:.0%}</div><div class="metric-label">Progress</div></div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            status_placeholder.info(f"{badge} Preparing…", unsafe_allow_html=True)

    else:
        badge = '<span class="badge badge-listing">STARTING</span>'
        status_placeholder.info(f"{badge} Initializing browser…", unsafe_allow_html=True)

    time.sleep(1)
    st.rerun()

elif done:
    total = EB_SCRAPE_STATE.get("total", 0)
    badge = '<span class="badge badge-done">COMPLETE</span>'
    status_placeholder.success(f"{badge} Successfully scraped **{total}** products", unsafe_allow_html=True)

elif not st.session_state.started:
    status_placeholder.info("Enter a store URL above and click **Start Scraping**")

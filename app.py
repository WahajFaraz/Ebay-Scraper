#!/usr/bin/env python3
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, request, send_file

from ebay_scraper import (ListingScraper, DetailScraper, export_csv, log,
                          MAX_WORKERS, SCRAPE_STATE as EB_SCRAPE_STATE)

app = Flask(__name__)

SCRAPE_STATE = {
    "running": False,
    "done": False,
    "error": None,
    "progress": 0,
    "total": 0,
    "started_at": None,
    "output_file": None,
}


def _output_path(url):
    m = re.search(r'/str/([^/?]+)', url)
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
        SCRAPE_STATE["total"] = len(products)
        SCRAPE_STATE["listing_total_products"] = len(products)

        if not products:
            export_csv([], out_path)
            SCRAPE_STATE["output_file"] = out_path
            SCRAPE_STATE["done"] = True
            SCRAPE_STATE["running"] = False
            return

        detail = DetailScraper()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(detail.scrape, p): p for p in products}
            done_count = 0
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    log.error(f"Detail worker failed: {e}")
                done_count += 1
                SCRAPE_STATE["progress"] = done_count

        export_csv(products, out_path)
        SCRAPE_STATE["output_file"] = out_path
        SCRAPE_STATE["done"] = True
    except Exception as e:
        log.exception("Scrape failed")
        SCRAPE_STATE["error"] = str(e)
    finally:
        SCRAPE_STATE["running"] = False
        log.info(f"Scrape finished in {time.time()-start:.1f}s")


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/scrape", methods=["POST"])
def start_scrape():
    if SCRAPE_STATE["running"]:
        return jsonify({"error": "Already running"}), 409
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Store URL is required"}), 400
    if "/str/" not in url:
        return jsonify({"error": "Invalid eBay store URL (must contain /str/)"}), 400

    SCRAPE_STATE["running"] = True
    SCRAPE_STATE["done"] = False
    SCRAPE_STATE["error"] = None
    SCRAPE_STATE["progress"] = 0
    SCRAPE_STATE["total"] = 0
    SCRAPE_STATE["listing_total_products"] = 0
    SCRAPE_STATE["started_at"] = time.time()
    SCRAPE_STATE["output_file"] = None
    # Reset ebay_scraper listing state
    EB_SCRAPE_STATE["phase"] = ""
    EB_SCRAPE_STATE["page"] = 0
    EB_SCRAPE_STATE["products_found"] = 0
    EB_SCRAPE_STATE["detail_progress"] = 0
    EB_SCRAPE_STATE["detail_total"] = 0
    threading.Thread(target=_run_scrape, args=(url,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/status")
def status():
    return jsonify({
        "running": SCRAPE_STATE["running"],
        "done": SCRAPE_STATE["done"],
        "error": SCRAPE_STATE["error"],
        "progress": SCRAPE_STATE["progress"],
        "total": SCRAPE_STATE["total"],
        "phase": EB_SCRAPE_STATE.get("phase", ""),
        "listing_page": EB_SCRAPE_STATE.get("page", 0),
        "listing_products": EB_SCRAPE_STATE.get("products_found", 0),
        "detail_progress": EB_SCRAPE_STATE.get("detail_progress", 0),
        "detail_total": EB_SCRAPE_STATE.get("detail_total", 0),
        "listing_total_products": SCRAPE_STATE.get("listing_total_products", 0),
    })


@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/download")
def download():
    path = SCRAPE_STATE.get("output_file")
    if not path or not os.path.exists(path):
        return jsonify({"error": "No output file yet"}), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>eBay Store Scraper</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .card { background: #fff; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,.08); padding: 40px 36px; width: 520px; max-width: 94vw; text-align: center; }
  h1 { font-size: 22px; font-weight: 600; margin-bottom: 4px; color: #1a1a1a; }
  p.sub { color: #666; font-size: 14px; margin-bottom: 24px; }
  label { display: block; text-align: left; font-size: 13px; font-weight: 500; color: #333; margin-bottom: 6px; }
  input[type=url] { width: 100%; padding: 12px 14px; border: 1px solid #dadce0; border-radius: 8px; font-size: 14px; outline: none; transition: .2s; margin-bottom: 20px; }
  input[type=url]:focus { border-color: #1a73e8; box-shadow: 0 0 0 2px rgba(26,115,232,.15); }
  .btn { border: none; border-radius: 10px; padding: 14px 40px; font-size: 16px; font-weight: 500; cursor: pointer; transition: .2s; width: 100%; }
  .btn-primary { background: #1a73e8; color: #fff; }
  .btn-primary:hover { background: #1557b0; }
  .btn-primary:disabled { background: #a0c4ff; cursor: not-allowed; }
  .btn-success { background: #188038; color: #fff; text-decoration: none; display: inline-block; }
  .btn-success:hover { background: #13622b; }
  .status { margin-top: 20px; font-size: 14px; color: #444; min-height: 48px; display: flex; flex-direction: column; justify-content: center; }
  .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #e0e0e0; border-top-color: #1a73e8; border-radius: 50%; animation: spin .7s linear infinite; margin-bottom: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .error { color: #d93025; }
  .success { color: #188038; }
</style>
</head>
<body>
<div class="card">
  <h1>eBay Store Scraper</h1>
  <p class="sub">Enter any eBay store URL & download products as CSV</p>
  <label for="storeUrl">eBay Store URL</label>
  <input type="url" id="storeUrl" value="" placeholder="https://www.ebay.com/str/..." autofocus>
  <button class="btn btn-primary" id="btnScrape" onclick="startScrape()">Start Scraping</button>
  <a id="btnDownload" style="display:none" class="btn btn-success" href="/download">Download CSV</a>
  <div class="status" id="status">Ready</div>
</div>
<script>
let pollTimer = null;
function startScrape() {
  const btn = document.getElementById('btnScrape');
  const status = document.getElementById('status');
  const url = document.getElementById('storeUrl').value.trim();
  if (!url) { status.innerHTML='<span class="error">Please enter a store URL</span>'; return; }
  btn.disabled = true;
  status.innerHTML = '<div class="spinner"></div>Starting...';
  fetch('/scrape', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:url}) })
    .then(r=>r.json()).then(d=>{
      if(d.error){ status.innerHTML='<span class="error">'+d.error+'</span>'; btn.disabled=false; return; }
      pollTimer = setInterval(pollStatus, 1000);
    }).catch(e=>{ status.innerHTML='<span class="error">Network error</span>'; btn.disabled=false; });
}
function pollStatus() {
  fetch('/status').then(r=>r.json()).then(s=>{
    const st = document.getElementById('status');
    const btn = document.getElementById('btnScrape');
    const dl = document.getElementById('btnDownload');
    if(s.error){ st.innerHTML='<span class="error">Error: '+s.error+'</span>'; btn.disabled=false; clearInterval(pollTimer); pollTimer=null; return; }
    if(s.running){
      if(s.phase==='listing'){
        st.innerHTML = '<div class="spinner"></div>Scraping listings... Page '+s.listing_page+' &middot; '+s.listing_products+' products found';
      } else if(s.phase==='details'){
        st.innerHTML = '<div class="spinner"></div>Scraping details... ('+s.detail_progress+'/'+s.detail_total+') &middot; '+s.listing_total_products+' products total';
      } else {
        st.innerHTML = '<div class="spinner"></div>Scraping...';
      }
    } else if(s.done){
      st.innerHTML = '<span class="success">Complete! '+s.listing_total_products+' products</span>';
      dl.style.display = 'block';
      btn.disabled = false;
      btn.style.display = 'none';
      clearInterval(pollTimer); pollTimer=null;
    }
  }).catch(()=>{});
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import webbrowser
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    webbrowser.open(f"http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)

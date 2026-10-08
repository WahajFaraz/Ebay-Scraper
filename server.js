const express = require("express");
const cors = require("cors");
const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer");
const XLSX = require("xlsx");

const app = express();

const PORT = 5002;
const HOST = "0.0.0.0";

const STORE_PAGE_SIZE = 60;

/*
Sort the listing pages so the order does not shift while we
paginate.

Measured on a real store: the store front's default order and
_sop=10 / _sop=12 all repeat items across page boundaries
(7-12 items per page) because new listings are inserted while
we crawl. _sop=15 (price, low to high) produced zero overlap
across adjacent pages, so no listing is skipped.
*/

const STORE_SORT = "15";

/*
eBay filters a store front by the visitor's delivery country, and
a logged-out visitor is geolocated from the IP address. A store can
therefore hand back a fraction of its inventory and still report
that fraction as the store total.

Measured on maxjm1 from a Pakistan-geolocated IP:

  /str/maxjm1                 "Search all 168 items"   4 pages,  172 unique
  /str/maxjm1?_fcid=1         "Search all 420 items"   9 pages,  425 unique

_fcid=1 is the unfiltered view. It was the only one of _fcid,
_mktpref, mkcid, LH_PrefLoc, _sacat and the dp1 delivery cookie
that changed the count, each measured in its own fresh browser so
no session state leaked between tests.

The dp1 cookie does carry the country (e.g. bbl/PKen-US6e86a7c8^)
but eBay re-signs it from the IP and ignores a hand-set value, so
it cannot be used to switch location.

With _fcid=1 the 168 location-filtered items were all still
present, so this only ever adds listings - it never hides any.
*/

const STORE_UNFILTERED_PARAM = "_fcid";
const STORE_UNFILTERED_VALUE = "1";
const STORE_UNFILTERED_ADDITIONAL = [
  { key: "LH_PrefLoc", value: "1" },
  { key: "LH_ShipToCountry", value: "US" },
  { key: "itemCondition", value: "0|1|2|3|4|5|6|7" },
];

/*
Extra pages to walk past the reported total, so a store whose
displayed count is slightly stale still gets fully collected.
*/

const COLLECTION_SLACK_PAGES = 15;

/*
Pure backstop against a runaway loop. The real stopping
conditions are "the store front stopped advancing" and "no new
items for a while", so this should never be the limiter.
*/

const COLLECTION_SAFETY_CAP = 5000;

const MAX_PAGE_RETRIES = 4;
const MAX_EMPTY_PAGES = 5;
const MAX_NO_NEW_PAGES = 8;
const MAX_LOAD_FAILURES = 5;

/*
How many pages to try before giving up on the store front and
falling back to seller search.
*/

const STRATEGY_FALLBACK_AFTER = 1;

const DETAIL_CONCURRENCY = 2;
const DETAIL_RETRIES = 3;

const NAVIGATION_TIMEOUT = 60000;
const PAGE_RENDER_WAIT = 1800;

const OUTPUT_DIR = path.join(__dirname, "downloads");

if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

app.use(cors());
app.use(express.json({ limit: "10mb" }));

// API PREFIX ALIAS

app.use((req, res, next) => {
  if (
    req.url === "/api" ||
    req.url.startsWith("/api/") ||
    req.url.startsWith("/api?")
  ) {
    req.url = req.url.slice(4) || "/";
  }

  next();
});

const scrapeState = {
  running: false,
  stopped: false,

  inputUrl: null,

  detectedTotal: 0,
  collectedProducts: 0,
  completedProducts: 0,
  failedPages: 0,

  currentPage: 0,
  totalPages: 0,

  currentProduct: 0,
  currentProductTitle: "",

  status: "idle",
  message: "Ready",

  fileName: null,
  filePath: null,

  startedAt: null,
  finishedAt: null,

  error: null,

  warning: null,
};

// ============================================================
// HELPERS
// ============================================================

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cleanText(value) {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeKey(value) {
  return cleanText(value)
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function normalizeUrl(url) {
  try {
    const u = new URL(url);
    u.hash = "";
    return u.toString();
  } catch {
    return String(url || "");
  }
}

function sanitizeFileName(value) {
  return String(value || "ebay")
    .replace(/[<>:"/\|?*]+/g, "")
    .replace(/\s+/g, "")
    .slice(0, 100);
}

function extractItemId(url) {
  if (!url) return null;

  try {
    const u = new URL(url);

    const match = u.pathname.match(
      /\/itm\/(?:[^/]+\/)?(\d{8,20})/i
    );

    if (match) return match[1];

    const item = u.searchParams.get("item");

    if (item && /^\d+$/.test(item)) {
      return item;
    }

  } catch { }

  const match = String(url).match(
    /\/itm\/(?:[^/]+\/)?(\d{8,20})/i
  );

  return match ? match[1] : null;
}

function getStoreName(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);

    const index = parts.findIndex(
      (x) => x.toLowerCase() === "str"
    );

    if (index >= 0 && parts[index + 1]) {
      return decodeURIComponent(parts[index + 1]);
    }

    return (
      u.searchParams.get("_ssn") ||
      "ebay_store"
    );

  } catch {
    return "ebay_store";
  }
}

// ============================================================
// EBAY URL HANDLING
// ============================================================

function getSellerName(url) {
  try {
    const u = new URL(url);

    const ssn = u.searchParams.get("_ssn");

    if (ssn) {
      return ssn;
    }

    const parts = u.pathname.split("/").filter(Boolean);

    const index = parts.findIndex(
      (x) => x.toLowerCase() === "str"
    );

    if (index >= 0 && parts[index + 1]) {
      return decodeURIComponent(parts[index + 1]);
    }

    return "";

  } catch {
    return "";
  }
}

function getEbayOrigin(url) {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}`;
  } catch {
    return "https://www.ebay.com";
  }
}

/*

IMPORTANT:



Never paginate /str/store/category directly.



We convert:



/str/STORE



into:



/sch/i.html?_ssn=STORE



This searches the seller's listings rather than one store

category.
*/
/*
Primary collection route: the store front itself.

Verified against live stores: /str/<seller>?_pgn=N returns a
fixed grid of real item cards, paginates well past page 200,
and is far less throttled than seller search.
*/

function buildStoreFrontUrl(url, pageNumber) {
  const seller = getSellerName(url);

  if (!seller) {
    throw new Error(
      "Could not detect eBay seller/store name from URL."
    );
  }

  const origin = getEbayOrigin(url);

  const u = new URL(
    `${origin}/str/${encodeURIComponent(seller)}`
  );

  u.searchParams.set(
    "_pgn",
    String(pageNumber)
  );

  u.searchParams.set(
    "_sop",
    STORE_SORT
  );

  if (
    STORE_UNFILTERED_PARAM &&
    STORE_UNFILTERED_VALUE
  ) {
    u.searchParams.set(
      STORE_UNFILTERED_PARAM,
      STORE_UNFILTERED_VALUE
    );
    for (const p of STORE_UNFILTERED_ADDITIONAL) {
      if (!u.searchParams.has(p.key)) {
        u.searchParams.set(p.key, p.value);
      }
    }
  }

  u.hash = "";

  return u.toString();
}

/*
Fallback for stores that do not expose a paginated front.
Seller search is capped at 100 pages by eBay and is throttled
more aggressively, so it is only used when the store front
produces nothing.
*/

function buildSellerSearchUrl(url, pageNumber) {
  const seller = getSellerName(url);

  if (!seller) {
    throw new Error(
      "Could not detect eBay seller/store name from URL."
    );
  }

  const origin = getEbayOrigin(url);

  const u = new URL(
    `${origin}/sch/i.html`
  );

  u.searchParams.set(
    "_ssn",
    seller
  );

  u.searchParams.set(
    "_sop",
    STORE_SORT
  );

  u.searchParams.set(
    "_ipg",
    String(STORE_PAGE_SIZE)
  );

  u.searchParams.set(
    "_pgn",
    String(pageNumber)
  );

  if (
    STORE_UNFILTERED_PARAM &&
    STORE_UNFILTERED_VALUE
  ) {
    u.searchParams.set(
      STORE_UNFILTERED_PARAM,
      STORE_UNFILTERED_VALUE
    );
    for (const p of STORE_UNFILTERED_ADDITIONAL) {
      if (!u.searchParams.has(p.key)) {
        u.searchParams.set(p.key, p.value);
      }
    }
  }

  u.hash = "";

  return u.toString();
}


// ============================================================
// BROWSER
// ============================================================

async function createBrowser() {
  return puppeteer.launch({
    headless: true,

    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--window-size=1920,1080",
    ],

    defaultViewport: {
      width: 1920,
      height: 1080,
      deviceScaleFactor: 1,
    },

  });
}

async function preparePage(page) {
  await page.setUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/146.0.0.0 Safari/537.36"
  );

  await page.setExtraHTTPHeaders({
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  });
  await page.setUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
  );

  page.setDefaultNavigationTimeout(
    NAVIGATION_TIMEOUT
  );

  page.setDefaultTimeout(
    NAVIGATION_TIMEOUT
  );
}

async function pageLooksBlocked(page, response) {

  // A real HTTP error status is a hard signal.
  if (
    response &&
    response.status() >= 400
  ) {
    return (
      `eBay returned HTTP ${response.status()}`
    );
  }

  try {
    const title = (
      await page.title()
    ).toLowerCase();

    const text = await page.evaluate(() => {
      return document.body
        ? document.body.innerText
          .toLowerCase()
          .slice(0, 30000)
        : "";
    });

    const blocked = [
      "verify you're human",
      "verify you are human",
      "verify you are not a robot",
      "security check",
      "access denied",
      "unusual traffic",
      "robot check",
      "please wait while we verify",
      "enable javascript and cookies to continue",
      "pardon our interruption",
      "something went wrong on our end",
      "we just need to make sure you're not a robot",
    ];

    if (
      title.includes("error page | ebay") ||
      title.includes("access denied")
    ) {
      return true;
    }

    return blocked.some((x) =>
      text.includes(x)
    );

  } catch {
    return false;
  }
}

/*
Distinguish a genuinely empty result page from a shell page
that loaded without content. Without this, a blocked/throttled
page is silently treated as "this store has no more products".
*/

async function pageShowsNoResults(page) {
  try {
    const text = await page.evaluate(() => {
      return (
        document.body?.innerText || ""
      ).toLowerCase();
    });

    const markers = [
      "0 results",
      "no results found",
      "we couldn't find any items",
      "we could not find any items",
      "0 items found",
      "try a different search",
      "no items match",
      "0 listings",
      "be the first to sell",
    ];

    return markers.some((m) =>
      text.includes(m)
    );
  } catch {
    return false;
  }
}


/*
Read the store front's own pagination widget.

eBay clamps out-of-range page requests to the last real page
instead of erroring, so the widget is the authoritative answer
to "am I still moving forward?". Verified on a live store:
page 368 returned 29 cards, and pages 370, 371, 375 and 740
all silently re-served page 368 with the same 29 items. This is
what stops the walk at the real end of the catalogue instead of
grinding through pages that repeat.
*/

async function detectStoreFrontPage(
  page
) {
  try {
    return await page.evaluate(() => {
      const widget = document.querySelector(
        ".str-pagination"
      );

      if (!widget) {
        return 0;
      }

      const text = (
        widget.innerText || ""
      ).replace(/\s+/g, " ")
        .trim();

      const match = text.match(
        /Page\s+(\d+)/
      );

      return match
        ? Number(match[1]) || 0
        : 0;
    });
  } catch {
    return 0;
  }
}

/*
eBay renders result lists client-side, so a fixed sleep is
not enough. Wait until real item links appear, or until we
hit the block page (which never grows item links).
*/

async function waitForItemLinks(
  page,
  timeout = 20000
) {
  const deadline =
    Date.now() + timeout;

  while (
    Date.now() < deadline
  ) {

    /*
    eBay can navigate while we are evaluating, which destroys
    the execution context. Treat that as "not ready yet"
    instead of failing the whole attempt.
    */

    let count = -1;

    try {
      count = await page.evaluate(() => {
        const scoped = [
          ".str-items-grid__container article.str-item-card a[href*='/itm/']",
          ".str-items-grid__container a[href*='/itm/']",
          ".srp-results li.s-item a[href*='/itm/']",
          "li.s-item a[href*='/itm/']",
        ];

        for (const selector of scoped) {
          const n =
            document.querySelectorAll(selector)
              .length;

          if (n) return n;
        }

        return 0;
      });
    } catch {
      await sleep(500);
      continue;
    }

    if (count > 0) {
      return count;
    }

    if (
      await pageLooksBlocked(page)
    ) {
      return 0;
    }

    await sleep(500);
  }

  return 0;
}

async function openPage(
  page,
  url,
  retries = MAX_PAGE_RETRIES,
  waitForItems = false
) {
  let lastError = null;

  for (
    let attempt = 1;
    attempt <= retries;
    attempt++
  ) {
    if (scrapeState.stopped) {
      throw new Error(
        "Scraping stopped by user."
      );
    }

    try {
      console.log(
        `[browser] ${url}`
      );

      const response =
        await page.goto(url, {
          waitUntil: "domcontentloaded",
          timeout: NAVIGATION_TIMEOUT,
        });

      if (
        await pageLooksBlocked(
          page,
          response
        )
      ) {
        throw new Error(
          "eBay blocked the request (verification/error page)."
        );
      }

      if (
        waitForItems
      ) {
        const found =
          await waitForItemLinks(page);

        if (
          found > 0
        ) {
          return;
        }

        if (
          await pageLooksBlocked(page)
        ) {
          throw new Error(
            "eBay blocked the request (verification/error page)."
          );
        }

        // No links and no block page: treat as an empty page.
        return;
      }

      await sleep(PAGE_RENDER_WAIT);

      if (
        await pageLooksBlocked(page)
      ) {
        throw new Error(
          "eBay blocked the request (verification/error page)."
        );
      }

      return;
    } catch (error) {
      lastError = error;

      console.log(
        `[browser] attempt ${attempt}/${retries}: ${error.message}`
      );

      if (
        attempt < retries
      ) {
        await sleep(
          2500 * attempt
        );
      }
    }

  }

  throw (
    lastError ||
    new Error(
      `Could not open ${url}`
    )
  );
}

// ============================================================
// STORE TOTAL
// ============================================================

async function detectStoreTotal(page) {
  try {
    const result =
      await page.evaluate(() => {
        const candidates = [];

        function add(
          value,
          score,
          source
        ) {
          const n = Number(
            String(value || "")
              .replace(/,/g, "")
          );

          if (
            !Number.isFinite(n) ||
            n <= 0 ||
            n > 10000000
          ) {
            return;
          }

          candidates.push({
            value: n,
            score,
            source,
          });
        }

        const text =
          document.body?.innerText ||
          "";

        const patterns = [
          {
            re: /search all\s+([\d,]+)\s+items?/i,
            score: 130,
            source: "search-all",
          },
          {
            re: /see all\s+([\d,]+)\s+items?/i,
            score: 130,
            source: "see-all",
          },
          {
            re: /([\d,]+)\s+items?\s+(?:in|from)\s+(?:this|the)\s+store/i,
            score: 130,
            source: "store-items",
          },
          {
            re: /\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)\s+(?:items?|results?|listings?)/i,
            score: 120,
            source: "range",
          },
          {
            re: /of\s+([\d,]+)\s+(?:items?|results?|listings?)/i,
            score: 100,
            source: "of-total",
          },
          {
            re: /(?:showing|displaying|found)\s+.*?([\d,]+)\s+(?:items?|results?|listings?)/i,
            score: 95,
            source: "showing",
          },
        ];

        for (
          const pattern of patterns
        ) {
          const match =
            text.match(
              pattern.re
            );

          if (match) {
            add(
              match[1],
              pattern.score,
              pattern.source
            );
          }
        }

        document
          .querySelectorAll(
            "a,button,input,[aria-label],[title]"
          )
          .forEach((el) => {
            const value = [
              el.innerText,
              el.textContent,
              el.value,
              el.getAttribute(
                "aria-label"
              ),
              el.getAttribute(
                "title"
              ),
            ]
              .filter(Boolean)
              .join(" ")
              .replace(/\s+/g, " ")
              .trim();

            for (
              const pattern of patterns
            ) {
              const match =
                value.match(
                  pattern.re
                );

              if (match) {
                add(
                  match[1],
                  pattern.score + 5,
                  `${pattern.source}-element`
                );
              }
            }
          });

        candidates.sort(
          (a, b) =>
            b.score - a.score ||
            b.value - a.value
        );

        return (
          candidates[0] ||
          null
        );
      });

    if (result?.value) {
      console.log(
        `[store] Reported total: ${result.value} (${result.source})`
      );

      return result.value;
    }

    return 0;

  } catch {
    return 0;
  }
}

// ============================================================
// PRODUCT EXTRACTION
// ============================================================

async function extractProductLinks(
  page
) {
  let raw = [];

  try {
    raw =
      await page.evaluate(() => {
        /*
        Scope to the real listing grid.
  
        A bare a[href*="/itm/"] also matches carousel,
        "similar items" and recommendation strips. Those change
        from page to page, which both pollutes the results with
        items that are not in the store and keeps producing
        "new" items so the crawler never stops.
        */

        const scoped = [
          ".str-items-grid__container article.str-item-card a[href*='/itm/']",
          ".str-items-grid__container a[href*='/itm/']",
          ".srp-results li.s-item a[href*='/itm/']",
          ".srp-results a[href*='/itm/']",
          "li.s-item a[href*='/itm/']",
        ];

        let nodes = [];

        for (const selector of scoped) {
          nodes = Array.from(
            document.querySelectorAll(selector)
          );

          if (nodes.length) break;
        }

        return nodes.map((a) => ({
          href: a.href,
          title:
            a.getAttribute("aria-label") ||
            a.getAttribute("title") ||
            a.innerText ||
            a.textContent ||
            "",
        }));
      });
  } catch (error) {

    /*
    The page navigated mid-evaluate. Report no links so the
    caller retries rather than crashing the whole scrape.
    */

    console.log(
      `[store] extractProductLinks failed: ${error.message}`
    );

    return [];
  }

  const products = [];
  const seen = new Set();

  for (
    const item of raw
  ) {
    const url =
      normalizeUrl(
        item.href
      );

    const itemId =
      extractItemId(url);

    if (!itemId) {
      continue;
    }

    if (seen.has(itemId)) {
      continue;
    }

    seen.add(itemId);

    products.push({
      itemId,
      url,
      title:
        cleanText(
          item.title
        ),
    });

  }

  return products;
}

function pageSignature(
  products
) {
  return products
    .map((p) => p.itemId)
    .sort()
    .join("|");
}

// ============================================================
// STORE COLLECTION
// ============================================================

async function collectAllStoreProducts(
  browser,
  storeUrl
) {
  const products =
    new Map();

  const visitedPages =
    new Set();

  const signatures =
    new Set();

  let reportedTotal = 0;

  let pageNumber = 1;

  let emptyPages = 0;
  let noNewPages = 0;
  let consecutiveLoadFailures = 0;
  let clampedPages = 0;

  /*
  
  
  
  FIRST: open original store page
  
  
  
  */

  const storePage =
    await browser.newPage();

  await preparePage(
    storePage
  );

  try {
    scrapeState.status =
      "detecting_total";

    scrapeState.message = "Opening eBay store...";

    await openPage(
      storePage,
      storeUrl
    );

    reportedTotal =
      await detectStoreTotal(
        storePage
      );

    if (
      reportedTotal > 0
    ) {
      scrapeState.detectedTotal =
        reportedTotal;
    }

  } catch (error) {

    /*
    The store landing page is only used for the approximate
    total. It can 410/redirect/403 even when the seller search
    works fine, so never fail the whole scrape because of it.
    */

    console.log(
      `[store] Store page unavailable (${error.message}); falling back to seller search.`
    );
  } finally {
    await storePage
      .close()
      .catch(() => { });
  }

  /*
  
  
  
  SECOND:
  
  
  
  Walk the store's own listing pages.
  
  
  
  The seller search endpoint is throttled far more aggressively
  
  and only covers the first 100 pages, so the store front is
  
  the primary route and seller search is the fallback.
  
  
  
  */

  let strategy = "store-front";

  let itemsPerPage = 0;

  let pageBudget = COLLECTION_SAFETY_CAP;

  console.log(
    "=================================================="
  );

  console.log(
    `[store] Original: ${storeUrl}`
  );

  console.log(
    `[store] Route: ${buildStoreFrontUrl(
      storeUrl,
      1
    )}`
  );

  console.log(
    `[store] Location filter: ${STORE_UNFILTERED_PARAM ? "OFF (applied)" : "not applied (skipped due to eBay challenge)"}`
  );

  console.log(
    `[store] Reported total: ${reportedTotal || "unknown"}`
  );

  console.log(
    "=================================================="
  );


  console.log(
    "===================="
  );

  const page =
    await browser.newPage();

  await preparePage(page);

  try {
    while (
      pageNumber <=
      pageBudget
    ) {
      if (
        scrapeState.stopped
      ) {
        throw new Error(
          "Scraping stopped by user."
        );
      }

      scrapeState.status =
        "collecting_products";

      scrapeState.currentPage =
        pageNumber;

      scrapeState.message =
        `Collecting products - page ${pageNumber}`;

      const currentUrl =
        strategy === "store-front"
          ? buildStoreFrontUrl(
            storeUrl,
            pageNumber
          )
          : buildSellerSearchUrl(
            storeUrl,
            pageNumber
          );

      const normalized =
        normalizeUrl(
          currentUrl
        );

      if (
        visitedPages.has(
          normalized
        )
      ) {
        break;
      }

      visitedPages.add(
        normalized
      );

      console.log(
        `[store] PAGE ${pageNumber}: ${currentUrl}`
      );

      let loaded = false;

      let lastLoadError = null;

      for (
        let attempt = 1;
        attempt <=
        MAX_PAGE_RETRIES;
        attempt++
      ) {
        try {
          await openPage(
            page,
            currentUrl,
            1,
            true
          );

          loaded = true;
          break;
        } catch (error) {
          lastLoadError = error;

          console.log(
            `[store] Page ${pageNumber}, attempt ${attempt}: ${error.message}`
          );

          if (
            attempt <
            MAX_PAGE_RETRIES
          ) {
            await sleep(
              5000 * attempt
            );
          }
        }
      }

      if (!loaded) {

        /*
        eBay blocks intermittently, so one bad page must not throw
        away everything already collected. Only give up once a run
        of consecutive pages has failed.
        */

        consecutiveLoadFailures++;

        scrapeState.failedPages++;

        console.log(
          `[store] Page ${pageNumber} failed to load: ${consecutiveLoadFailures}/${MAX_LOAD_FAILURES}`
        );

        if (
          consecutiveLoadFailures >=
          MAX_LOAD_FAILURES
        ) {
          throw new Error(
            `eBay stopped serving listing pages after ${consecutiveLoadFailures} consecutive failures. ${lastLoadError?.message || ""
            }`
          );
        }

        pageNumber++;
        continue;
      }

      consecutiveLoadFailures = 0;

      const pageProducts =
        await extractProductLinks(
          page
        );

      const pageTotal =
        await detectStoreTotal(
          page
        );

      if (
        pageTotal >
        reportedTotal
      ) {
        reportedTotal =
          pageTotal;
      }


      /*
       * Work out how many pages this store actually needs.
       *
       * Derived from the measured page size and eBay's own reported
       * count, so a store with 40 items walks 2 pages and a store
       * with 18,000 items walks ~400. Nothing is capped at a fixed
       * number.
       */

      if (
        pageProducts.length > 0 &&
        itemsPerPage === 0
      ) {
        itemsPerPage = pageProducts.length;

        if (
          reportedTotal > 0
        ) {
          pageBudget =
            Math.ceil(
              reportedTotal /
              itemsPerPage
            ) +
            COLLECTION_SLACK_PAGES;

          console.log(
            `[store] ${itemsPerPage} items/page, reported total ${reportedTotal} -> walking up to ${pageBudget} pages`
          );
        } else {
          console.log(
            `[store] ${itemsPerPage} items/page, total unknown -> walking until the store front stops advancing`
          );
        }
      }


      console.log(
        `[store] Page ${pageNumber}: ${pageProducts.length} links`
      );

      /*
       * Empty page handling.
       */

      if (
        pageProducts.length ===
        0
      ) {

        /*
        A page with no item links is either a genuinely empty
        result page or a throttled shell page that slipped past
        the earlier checks. Only trust it as "empty" when eBay
        actually says there are no results; otherwise treat it as
        a failed load so blocking is reported honestly.
        */

        const genuinelyEmpty =
          await pageShowsNoResults(page);

        if (
          !genuinelyEmpty ||
          await pageLooksBlocked(page)
        ) {
          consecutiveLoadFailures++;

          scrapeState.failedPages++;

          console.log(
            `[store] Page ${pageNumber} returned no items without a no-results message: ${consecutiveLoadFailures}/${MAX_LOAD_FAILURES}`
          );

          if (
            consecutiveLoadFailures >=
            MAX_LOAD_FAILURES
          ) {
            throw new Error(
              `eBay did not return listing results for ${MAX_LOAD_FAILURES} consecutive pages. This usually means eBay is throttling this IP - wait a while and try again.`
            );
          }

          await sleep(
            5000 *
            consecutiveLoadFailures
          );

          pageNumber++;
          continue;
        }


        /*
        The store front is returning genuinely empty pages while
        nothing has been collected. Some stores do not publish a
        paginated front, so redo the whole walk through seller
        search before giving up.
        */

        if (
          strategy === "store-front" &&
          products.size === 0 &&
          pageNumber >= STRATEGY_FALLBACK_AFTER
        ) {
          console.log(
            `[store] Store front returned no items after ${STRATEGY_FALLBACK_AFTER} pages; switching to seller search.`
          );

          strategy = "seller-search";

          pageNumber = 1;
          visitedPages.clear();
          emptyPages = 0;
          noNewPages = 0;
          clampedPages = 0;
          consecutiveLoadFailures = 0;

          continue;
        }

        emptyPages++;

        console.log(
          `[store] Empty page ${pageNumber}: ${emptyPages}/${MAX_EMPTY_PAGES}`
        );

        if (
          emptyPages >=
          MAX_EMPTY_PAGES
        ) {
          break;
        }

        pageNumber++;
        continue;
      }

      emptyPages = 0;
      clampedPages = 0;
      consecutiveLoadFailures = 0;

      /*
       * Detect exact repeated page.
       */

      const signature =
        pageSignature(
          pageProducts
        );

      if (
        signature &&
        signatures.has(
          signature
        )
      ) {
        noNewPages++;
      } else if (
        signature
      ) {
        signatures.add(
          signature
        );
      }

      /*
       * Add unique products.
       */

      let newProducts = 0;

      for (
        const product of pageProducts
      ) {
        if (
          !product.itemId
        ) {
          continue;
        }

        if (
          !products.has(
            product.itemId
          )
        ) {
          products.set(
            product.itemId,
            product
          );

          newProducts++;
        }
      }

      scrapeState.collectedProducts =
        products.size;

      console.log(
        `[store] Page ${pageNumber}: ${newProducts} NEW / ${products.size} UNIQUE`
      );
      /*
         * The store front tells us which page it actually served.
         *
         * eBay silently clamps an out-of-range _pgn to the last real page
         * and re-serves it, so once the served page stops matching the
         * requested page we are past the end of the catalogue. Verified
         * on a live store: page 370 returned 42 cards, and pages 372,
         * 375 and 740 all re-served page 370 with the same items.
         *
         * Two consecutive clamps are required before stopping, because a
         * single mis-served page can also be a soft redirect or a slow
         * render. Two in a row cannot happen while we are still making
         * progress.
         */

      if (
        strategy === "store-front"
      ) {
        const servedPage =
          await detectStoreFrontPage(page);

        if (
          servedPage > 0 &&
          servedPage < pageNumber
        ) {
          clampedPages++;
        } else {
          clampedPages = 0;
        }

        if (
          clampedPages >= 2
        ) {
          console.log(
            `[store] Reached the end of the catalogue: eBay served page ${servedPage} instead of ${pageNumber} twice in a row.`
          );

          scrapeState.detectedTotal =
            products.size;

          break;
        }
      }


      if (
        newProducts === 0
      ) {
        noNewPages++;
      } else {
        noNewPages = 0;
      }

      /*
       * IMPORTANT:
       *
       * We DO NOT do:
       *
       * if (products.size >= reportedTotal)
       *     break;
       *
       * because eBay's displayed count can be approximate.
       */

      if (
        noNewPages >=
        MAX_NO_NEW_PAGES
      ) {
        console.log(
          `[store] No new products for ${MAX_NO_NEW_PAGES} consecutive pages.`
        );

        break;
      }

      pageNumber++;
    }

  } finally {
    await page
      .close()
      .catch(() => { });
  }

  const finalProducts =
    Array.from(
      products.values()
    ).map(
      (product) => ({
        ...product,
        /*
        Remembered so the detail leg can fall back to the store
        name when eBay does not render a seller card.
        */

        storeUrl,
      })
    );

  const finalCount =
    finalProducts.length;

  scrapeState.collectedProducts =
    finalCount;

  /*
  
  If eBay's displayed total is smaller than the number we
  
  actually collected, use the actual unique collection count.
  
  
  
  Example:
  
  
  
  eBay displayed = 39
  
  actual unique  = 1228
  
  
  
  final = 1228
  */

  scrapeState.detectedTotal =
    Math.max(
      Number(reportedTotal) || 0,
      finalCount
    );

  scrapeState.totalPages =
    Math.max(
      0,
      pageNumber - 1
    );


  /*
  If we collected noticeably fewer products than eBay reported,
  say so instead of quietly producing a short file.
  */

  if (
    reportedTotal > 0 &&
    finalCount <
    reportedTotal * 0.95
  ) {
    const shortfall =
      reportedTotal - finalCount;

    console.log(
      `[store] WARNING: collected ${finalCount} of ${reportedTotal} reported items (${shortfall} short). eBay may be throttling, or listings may have ended during the crawl.`
    );

    scrapeState.warning =
      `Collected ${finalCount} of ${reportedTotal} reported items (${shortfall} missing).`;

    scrapeState.detectedTotal = finalCount;
  }

  console.log(
    "=================================================="
  );

  console.log(
    `[store] EBAY DISPLAYED: ${reportedTotal || "unknown"}`
  );

  console.log(
    `[store] ACTUAL UNIQUE: ${finalCount}`
  );

  console.log(
    `[store] FINAL TOTAL: ${scrapeState.detectedTotal}`
  );

  console.log(
    `[store] PAGES: ${scrapeState.totalPages}`
  );

  console.log(
    "=================================================="
  );

  return finalProducts;
}

// ============================================================
// ITEM SPECIFICS
// ============================================================

async function extractItemSpecifics(
  page
) {
  const result = {};

  /*
  Keep the human-readable label alongside the normalized key so
  every specific eBay exposes can be exported as its own column,
  not just Brand/Model/MPN.
  */

  const labels = {};

  function add(
    key,
    value
  ) {
    const k =
      normalizeKey(key);

    const v =
      cleanText(value);

    if (
      k &&
      v &&
      !result[k]
    ) {
      result[k] = v;

      const label =
        cleanText(key)
          .replace(
            /[:\uFF1A]\s*$/,
            ""
          );

      if (
        label &&
        !labels[label]
      ) {
        labels[label] = v;
      }
    }

  }

  const rows =
    await page.evaluate(() => {
      const data = [];

      document
        .querySelectorAll(
          "tr"
        )
        .forEach((row) => {
          const cells =
            Array.from(
              row.querySelectorAll(
                "th,td"
              )
            ).map(
              (x) =>
                x.innerText || ""
            );

          if (
            cells.length >= 2
          ) {
            data.push([
              cells[0],
              cells[1],
            ]);
          }
        });

      document
        .querySelectorAll(
          "dt"
        )
        .forEach((dt) => {
          const dd =
            dt.nextElementSibling;

          if (
            dd &&
            dd.tagName.toLowerCase() ===
            "dd"
          ) {
            data.push([
              dt.innerText || "",
              dd.innerText || "",
            ]);
          }
        });

      document
        .querySelectorAll(
          "[class*='ux-labels-values']"
        )
        .forEach((row) => {
          const label =
            row.querySelector(
              "dt,.ux-labels-values__labels"
            );

          const value =
            row.querySelector(
              "dd,.ux-labels-values__values"
            );

          if (
            label &&
            value
          ) {
            data.push([
              label.innerText ||
              "",
              value.innerText ||
              "",
            ]);
          }
        });

      return data;
    });

  for (
    const row of rows
  ) {
    add(
      row[0],
      row[1]
    );
  }

  return {
    values: result,
    labels,
  };
}

function findSpecific(
  specifics,
  names
) {
  const keys =
    names.map(
      normalizeKey
    );

  for (
    const key of keys
  ) {
    if (
      specifics[key]
    ) {
      return specifics[key];
    }
  }

  for (
    const [key, value] of
    Object.entries(
      specifics
    )
  ) {
    for (
      const target of keys
    ) {
      if (
        key.includes(target) ||
        target.includes(key)
      ) {
        return value;
      }
    }
  }

  return "";
}

// ============================================================
// LISTING FACTS
// ============================================================

/*
The standard facts eBay prints next to the price and in the seller
card. They are read only from the buy-box ("above the fold")
region on purpose: the recommendation strips further down the
page repeat the same words for other sellers, so a page-wide
search for "sold" happily returns 405 sold from somebody else's
listing.

Verified live on an eBay item page:

  [data-testid='x-sellercard-atf__data-item']  -> "99.9% positive"
  .x-sellercard-atf a[href*="/usr/"]          -> "maxjm1"
  "Quantity:" followed by "9 available"
  "1 sold"                                    -> listing sold count
  "Located in: Dubuque, IA, United States"
  [data-testid='x-watch-heart__watcher-counter'] -> "4"
  "or Best Offer"                             -> best offer enabled
*/

async function extractListingFacts(
  page
) {
  try {
    return await page.evaluate(() => {
      function text(
        el
      ) {
        return (
          el?.innerText ||
          el?.textContent ||
          ""
        )
          .replace(/\s+/g, " ")
          .trim();
      }

      const buyBox =
        document.querySelector(
          "[data-testid='x-evo-atf-right-river']"
        ) ||
        document.querySelector(
          ".x-actionable-container"
        ) ||
        document.body;

      const buyBoxText = text(buyBox) || "";

      /*
      Watchers sit in their own widget, outside the buy box.
      */

      const watcherEl =
        document.querySelector(
          "[data-testid='x-watch-heart__watcher-counter']"
        ) ||
        document.querySelector(
          ".x-watch-heart-btn-text"
        );

      let watchers = text(watcherEl);

      if (
        !watchers ||
        /watch/i.test(watchers)
      ) {
        watchers = "";
      }

      /*
      "1 has already sold." is a notice about the offer, not the
      listing's sold count, so only accept "<number> sold".
      */

      const sellerLink =
        document.querySelector(
          ".x-sellercard-atf a[href*='/usr/'], .x-sellercard-atf__info__container a[href*='/usr/']"
        );

      const feedbackEl =
        document.querySelector(
          ".x-sellercard-atf [data-testid='x-sellercard-atf__data-item']"
        );

      const feedbackText =
        text(feedbackEl) || "";

      const locationText = (
        document.body.innerText.match(
          /Located in:\s*([^\n]+?)(?:\n\s*\n|$)/
        ) || []
      )[1] || "";

      return {
        Seller:
          text(sellerLink) ||
          text(
            document.querySelector(
              ".x-sellercard-atf__info__about-seller"
            )
          ) ||
          "",

        "Seller Feedback":
          feedbackText.match(
            /([\d.]+%)\s*positive/i
          )?.[1] ||
          "",

        "Item Location":
          locationText.replace(
            /[.\s]+$/,
            ""
          ),

        Quantity:
          buyBoxText.match(
            /(\d+)\s*available/i
          )?.[1] || "",

        Sold:
          buyBoxText.match(
            /(?:^|\s)(\d[\d,]*)\s+sold\b/i
          )?.[1] || "",

        Watchers:
          watchers,

        "Best Offer":
          /or\s+best\s+offer/i.test(
            buyBoxText
          )
            ? "Yes"
            : "",
      };

    });
  } catch (error) {
    console.log(
      `[details] extractListingFacts failed: ${error.message}`
    );

    return {
      Seller: "",
      "Seller Feedback": "",
      "Item Location": "",
      Quantity: "",
      Sold: "",
      Watchers: "",
      "Best Offer": "",
    };
  }
}

// ============================================================
// PRODUCT DETAIL
// ============================================================

const LISTING_FACT_KEYS = [
  "Seller",
  "Seller Feedback",
  "Item Location",
  "Quantity",
  "Sold",
  "Watchers",
  "Best Offer",
];

async function extractProductDetail(
  page,
  product
) {
  await openPage(
    page,
    product.url,
    2
  );

  await sleep(800);

  const specifics =
    await extractItemSpecifics(
      page
    );

  const facts =
    await extractListingFacts(
      page
    );

  const data =
    await page.evaluate(() => {
      function text(
        selectors
      ) {
        for (
          const selector of
          selectors
        ) {
          const el =
            document.querySelector(
              selector
            );

          if (el) {
            const value =
              el.innerText ||
              el.textContent ||
              "";

            if (
              value.trim()
            ) {
              return value.trim();
            }
          }
        }

        return "";
      }

      function attr(
        selectors,
        name
      ) {
        for (
          const selector of
          selectors
        ) {
          const el =
            document.querySelector(
              selector
            );

          if (el) {
            const value =
              el.getAttribute(
                name
              );

            if (value) {
              return value;
            }
          }
        }

        return "";
      }

      return {
        title: text([
          "h1.x-item-title__mainTitle",
          "h1[data-testid='x-item-title']",
          "h1[itemprop='name']",
          "h1",
        ]),

        price: text([
          ".x-price-primary",
          "[data-testid='x-price-primary']",
          "[itemprop='price']",
        ]),

        description: text([
          "#desc_div",
          "#viTabs_0_is",
          "#desc_ifr",
          "[data-testid='x-item-description']",
          "#itemDescription",
        ]),

        image:
          attr(
            [
              "img[data-zoom-src]",
              "img[itemprop='image']",
              ".ux-image-carousel-item.active img",
              "#icImg",
            ],
            "src"
          ) ||
          attr(
            [
              "img[data-zoom-src]",
              "#icImg",
            ],
            "data-zoom-src"
          ),
      };
    });

  let jsonLd = {};

  try {
    jsonLd =
      await page.evaluate(() => {
        const scripts =
          Array.from(
            document.querySelectorAll(
              'script[type="application/ld+json"]'
            )
          );

        for (
          const script of
          scripts
        ) {
          try {
            const parsed =
              JSON.parse(
                script.textContent
              );

            const list =
              Array.isArray(
                parsed
              )
                ? parsed
                : [parsed];

            const product =
              list.find(
                (x) =>
                  x &&
                  typeof x ===
                  "object" &&
                  (
                    x["@type"] ===
                    "Product" ||
                    x.name
                  )
              );

            if (
              product
            ) {
              return {
                name:
                  product.name ||
                  "",

                description:
                  product.description ||
                  "",

                image:
                  Array.isArray(
                    product.image
                  )
                    ? product.image[0]
                    : product.image ||
                    "",

                price:
                  product.offers?.price ||
                  product.offers?.lowPrice ||
                  "",

                brand:
                  typeof product.brand ===
                    "object"
                    ? product.brand?.name ||
                    ""
                    : product.brand ||
                    "",

                model:
                  product.model ||
                  "",

                mpn:
                  product.mpn ||
                  "",
              };
            }
          } catch { }
        }

        return {};
      });

  } catch { }

  return {
    Title:
      cleanText(
        data.title ||
        jsonLd.name ||
        product.title
      ),

    Price:
      cleanText(
        data.price ||
        jsonLd.price
      ),

    Brand:
      cleanText(
        findSpecific(
          specifics.values,
          [
            "Brand",
            "Brand Name",
            "Manufacturer Brand",
            "Marke",
            "Hersteller",
            "Marque",
          ]
        ) ||
        jsonLd.brand
      ),

    Model:
      cleanText(
        findSpecific(
          specifics.values,
          [
            "Model",
            "Model Number",
            "Model Name",
            "Modell",
            "Modellnummer",
          ]
        ) ||
        jsonLd.model
      ),

    MPN:
      cleanText(
        findSpecific(
          specifics.values,
          [
            "MPN",
            "Manufacturer Part Number",
            "Manufacturer Part No",
            "Teilenummer",
            "Artikelnummer",
          ]
        ) ||
        jsonLd.mpn
      ),

    Description:
      cleanText(
        data.description ||
        jsonLd.description
      ),

    "Image URL":
      cleanText(
        data.image ||
        jsonLd.image
      ),

    URL:
      product.url,

    /*
    The standard listing facts eBay shows in the buy box and the
    seller card. The seller falls back to the store we are
    crawling, which is always known from the store URL.
    */

    Seller:
      cleanText(
        facts.Seller ||
        getSellerName(
          product.storeUrl || ""
        )
      ),

    "Seller Feedback":
      cleanText(
        facts["Seller Feedback"]
      ),

    "Item Location":
      cleanText(
        facts["Item Location"]
      ),

    Quantity:
      cleanText(
        facts.Quantity
      ),

    Sold:
      cleanText(
        facts.Sold
      ),

    Watchers:
      cleanText(
        facts.Watchers
      ),

    "Best Offer":
      cleanText(
        facts["Best Offer"]
      ),

    /*
    Every other item specific eBay exposes (ISBN, Condition,
    Autor, Material, ...) keyed by its original label.
    */

    Specifics:
      specifics.labels || {},

  };
}

// ============================================================
// DETAIL SCRAPER
// ============================================================

async function scrapeSingleProduct(
  page,
  product,
  index,
  total
) {
  for (
    let attempt = 1;
    attempt <=
    DETAIL_RETRIES;
    attempt++
  ) {
    if (
      scrapeState.stopped
    ) {
      throw new Error(
        "Scraping stopped by user."
      );
    }

    try {
      scrapeState.currentProduct =
        index;

      scrapeState.currentProductTitle =
        product.title || "";

      scrapeState.message =
        `Scraping product ${index} of ${total}`;

      const result =
        await extractProductDetail(
          page,
          product
        );

      return result;
    } catch (error) {
      console.log(
        `[details] ${index}/${total} attempt ${attempt}: ${error.message}`
      );

      if (
        attempt <
        DETAIL_RETRIES
      ) {
        await sleep(
          1500 * attempt
        );
      }
    }

  }

  /*
  
  One product failing must not stop the whole store.
  */

  return {
    Title:
      product.title || "",
    Price: "",
    Brand: "",
    Model: "",
    MPN: "",
    Description: "",
    "Image URL":
      "",
    URL:
      product.url,
    Seller:
      getSellerName(
        product.storeUrl || ""
      ),
    "Seller Feedback":
      "",
    "Item Location":
      "",
    Quantity: "",
    Sold: "",
    Watchers: "",
    "Best Offer": "",
    Specifics:
      {},
  };
}

async function scrapeAllDetails(
  browser,
  products
) {
  const results =
    new Array(
      products.length
    );

  let nextIndex = 0;

  /*
  Each worker owns one page for its whole run. Creating a page
  per product is slow and makes eBay block us far more often.
  */

  async function worker() {
    let page = null;

    try {
      page =
        await browser.newPage();

      await preparePage(
        page
      );
    } catch (error) {
      console.log(
        `[details] worker failed to start: ${error.message}`
      );

      return;
    }

    try {
      while (true) {
        if (
          scrapeState.stopped
        ) {
          return;
        }

        const index =
          nextIndex++;

        if (
          index >=
          products.length
        ) {
          return;
        }

        results[index] =
          await scrapeSingleProduct(
            page,
            products[index],
            index + 1,
            products.length
          );

        scrapeState.completedProducts =
          results.filter(Boolean)
            .length;
      }
    } finally {
      await page
        .close()
        .catch(() => { });
    }
  }

  const workers =
    Math.min(
      DETAIL_CONCURRENCY,
      products.length
    );

  await Promise.all(
    Array.from(
      {
        length: workers,
      },
      () => worker()
    )
  );

  return results.filter(
    Boolean
  );
}

// ============================================================
// EXCEL
// ============================================================

function createExcel(
  products,
  storeUrl
) {
  const FIXED_HEADERS = [
    "Product Title",
    "Price",
    "Description",
    "Part NUM MPN",
    "Brand",
    "Model",
    "Picture link",
    "Product Link",
  ];

  /*
  eBay exposes far more specifics than Brand/Model/MPN (ISBN,
  Condition, Material, Autor, ...). Build the union of every
  label seen so no scraped data is thrown away. Labels that map
  to an existing fixed column are skipped to avoid duplicates.
  */

  const header = [...FIXED_HEADERS];

  const rows =
    products.map(
      (p) => {
        const row = {};

        for (
          const key of FIXED_HEADERS
        ) {
          let value = p[key] || "";

          if (key === "Part NUM MPN") {
            value = p.MPN || p["Part NUM MPN"] || p.mpn || p["Part Number"] || p["Manufacturer Part Number"] || p["Manufacturer Part No"] || "";
          }

          if (key === "Part NUM MPN") {
            value = p["Part NUM MPN"] || p.MPN || p["MPN"] || (p.Specifics && (p.Specifics["MPN"] || p.Specifics["Manufacturer Part Number"] || p.Specifics["Manufacturer Part No"])) || "";
          }

          if (key === "Brand") {
            value = p.Brand || p["Brand"] || (p.Specifics && (p.Specifics["Brand"] || p.Specifics["Brand Name"])) || "";
          }

          if (key === "Model") {
            value = p.Model || p["Model"] || (p.Specifics && p.Specifics["Model"]) || "";
          }

          if (key === "Product Title") {
            value = p.Title || p["Product Title"] || p.title || "";
          }

          if (key === "Price") {
            value = p.Price || p["Price"] || "";
          }

          if (key === "Description") {
            value = p.Description || p["Description"] || "";
          }

          if (key === "Picture link") {
            value = p["Picture link"] || p["Picture Link"] || p["Image URL"] || p["image URL"] || p.image || "";
          }

          if (key === "Product Link") {
            value = p.URL || p.url || p["Product Link"] || "";
          }

          row[key] = value;
        }



        return row;
      }
    );

  const worksheet =
    XLSX.utils.json_to_sheet(
      rows,
      {
        header,
      }
    );

  worksheet["!cols"] =
    header.map((h) => ({
      wch:
        h === "Description"
          ? 70
          : h === "Picture link"
            ? 65
            : h === "Part NUM MPN" || h === "MPN"
              ? 25
              : h === "Product Link"
                ? 60
                : h === "Product Title"
                  ? 60
                  : 22,
    }));

  const workbook =
    XLSX.utils.book_new();

  XLSX.utils.book_append_sheet(
    workbook,
    worksheet,
    "Products"
  );

  const store =
    sanitizeFileName(
      getStoreName(
        storeUrl
      )
    );

  const fileName =
    `${store}_products.xlsx`;

  const filePath =
    path.join(
      OUTPUT_DIR,
      fileName
    );

  XLSX.writeFile(
    workbook,
    filePath
  );

  return {
    fileName,
    filePath,
  };
}

// ============================================================
// MAIN
// ============================================================

async function runScrape(
  storeUrl
) {
  let browser = null;

  try {
    scrapeState.running =
      true;

    scrapeState.stopped =
      false;

    scrapeState.inputUrl =
      storeUrl;

    scrapeState.detectedTotal =
      0;

    scrapeState.collectedProducts =
      0;

    scrapeState.completedProducts =
      0;

    scrapeState.failedPages =
      0;

    scrapeState.currentPage =
      0;

    scrapeState.totalPages =
      0;

    scrapeState.currentProduct =
      0;

    scrapeState.currentProductTitle =
      "";

    scrapeState.warning =
      null;

    scrapeState.fileName =
      null;

    scrapeState.filePath =
      null;

    scrapeState.startedAt =
      new Date().toISOString();

    scrapeState.finishedAt =
      null;

    scrapeState.error =
      null;

    scrapeState.status =
      "starting";

    scrapeState.message =
      "Starting scraper...";

    browser =
      await createBrowser();

    /*
     * COLLECTION
     */

    const products =
      await collectAllStoreProducts(
        browser,
        storeUrl
      );

    if (
      scrapeState.stopped
    ) {
      throw new Error(
        "Scraping stopped by user."
      );
    }

    const total =
      products.length;

    scrapeState.collectedProducts =
      total;

    if (
      !scrapeState.detectedTotal
    ) {
      scrapeState.detectedTotal =
        total;
    }

    if (
      total === 0
    ) {

      /*
      Distinguish "store is genuinely empty" from "eBay refused to
      serve the listing pages", which is by far the more common cause.
      */

      if (
        scrapeState.failedPages > 0 ||
        scrapeState.detectedTotal > 0
      ) {
        throw new Error(
          `eBay reported ${scrapeState.detectedTotal} items in this store but ${scrapeState.failedPages
          } listing page(s) were blocked, so no products could be collected. eBay is likely rate-limiting this IP - wait a while and try again.`
        );
      }

      throw new Error(
        "No products were found. Check that the store URL is correct and the store has active listings."
      );
    }

    /*
     * DETAILS
     */

    scrapeState.status =
      "scraping_details";

    scrapeState.message =
      `Scraping details for ${total} products`;

    scrapeState.completedProducts =
      0;

    const detailed =
      await scrapeAllDetails(
        browser,
        products
      );

    if (
      scrapeState.stopped
    ) {
      throw new Error(
        "Scraping stopped by user."
      );
    }

    /*
     * EXCEL
     */

    scrapeState.status =
      "creating_excel";

    scrapeState.message =
      "Creating Excel file...";

    const excel =
      createExcel(
        detailed,
        storeUrl
      );

    scrapeState.fileName =
      excel.fileName;

    scrapeState.filePath =
      excel.filePath;

    scrapeState.completedProducts =
      detailed.length;

    /*
     * DONE
     */

    scrapeState.status =
      "completed";

    scrapeState.message =
      `Completed successfully. ${detailed.length} products saved.`;

    scrapeState.finishedAt =
      new Date().toISOString();

    console.log(
      "=================================================="
    );

    console.log(
      `[main] STORE TOTAL: ${scrapeState.detectedTotal}`
    );

    console.log(
      `[main] PRODUCTS FOUND: ${scrapeState.collectedProducts}`
    );

    console.log(
      `[main] DETAILS DONE: ${detailed.length}`
    );

    console.log(
      `[main] FILE: ${excel.filePath}`
    );

    console.log(
      "=================================================="
    );

  } catch (error) {
    console.error(
      "[main]",
      error
    );

    scrapeState.status =
      "error";

    scrapeState.error =
      error.message ||
      "Scraping failed.";

    scrapeState.message =
      scrapeState.error;

    scrapeState.finishedAt =
      new Date().toISOString();

  } finally {
    if (browser) {
      await browser
        .close()
        .catch(() => { });
    }

    scrapeState.running =
      false;

    scrapeState.currentProduct =
      0;

    scrapeState.currentProductTitle =
      "";

  }
}

// ============================================================
// HEALTH
// ============================================================

app.get(
  "/health",
  (req, res) => {
    res.json({
      success: true,
      status: "ok",
      service:
        "ebay-scraper",
    });
  }
);

// ============================================================
// STATUS
// ============================================================

app.get(
  "/status",
  (req, res) => {
    res.set(
      "Cache-Control",
      "no-store"
    );

    res.json({
      success: true,

      running:
        scrapeState.running,

      stopped:
        scrapeState.stopped,

      inputUrl:
        scrapeState.inputUrl,

      detectedTotal:
        scrapeState.detectedTotal,

      collectedProducts:
        scrapeState.collectedProducts,

      completedProducts:
        scrapeState.completedProducts,

      failedPages:
        scrapeState.failedPages,

      warning:
        scrapeState.warning,

      currentPage:
        scrapeState.currentPage,

      totalPages:
        scrapeState.totalPages,

      currentProduct:
        scrapeState.currentProduct,

      currentProductTitle:
        scrapeState.currentProductTitle,

      status:
        scrapeState.status,

      message:
        scrapeState.message,

      fileName:
        scrapeState.fileName,

      startedAt:
        scrapeState.startedAt,

      finishedAt:
        scrapeState.finishedAt,

      error:
        scrapeState.error,
    });

  }
);

// ============================================================
// START
// ============================================================

app.post(
  "/scrape",
  (req, res) => {
    const url =
      cleanText(
        req.body?.url
      );

    if (
      scrapeState.running
    ) {
      return res.status(409).json({
        success: false,
        error:
          "A scrape is already running.",
      });
    }

    if (!url) {
      return res.status(400).json({
        success: false,
        error:
          "eBay store URL is required.",
      });
    }

    let parsed;

    try {
      parsed =
        new URL(url);
    } catch {
      return res.status(400).json({
        success: false,
        error:
          "Invalid URL.",
      });
    }

    if (
      !parsed.hostname
        .toLowerCase()
        .includes("ebay.")
    ) {
      return res.status(400).json({
        success: false,
        error:
          "Please enter an eBay URL.",
      });
    }

    runScrape(
      url
    ).catch(
      console.error
    );

    res.json({
      success: true,
      status: "started",
      message:
        "Scraping started.",
    });

  }
);

// ============================================================
// STOP
// ============================================================

app.post(
  "/stop",
  (req, res) => {
    if (
      !scrapeState.running
    ) {
      return res.json({
        success: true,
        message:
          "No scrape is running.",
      });
    }

    scrapeState.stopped =
      true;

    scrapeState.status =
      "stopping";

    scrapeState.message =
      "Stopping scraper...";

    res.json({
      success: true,
      message:
        "Stop requested.",
    });

  }
);

// ============================================================
// DOWNLOAD
// ============================================================

app.get(
  "/download",
  (req, res) => {
    if (
      !scrapeState.filePath ||
      !fs.existsSync(
        scrapeState.filePath
      )
    ) {
      return res.status(404).json({
        success: false,
        error:
          "Excel file is not available.",
      });
    }

    res.download(
      scrapeState.filePath,
      scrapeState.fileName ||
      "ebay_products.xlsx"
    );

  }
);

// ============================================================
// EXPRESS ERROR
// ============================================================

app.use(
  (error, req, res, next) => {
    console.error(
      "[express]",
      error
    );

    res.status(500).json({
      success: false,
      error:
        "Internal server error.",
    });

  }
);

// ============================================================
// SERVER
// ============================================================

/*
Only start listening when this file is run directly. Tests can
require() it to reuse the scraping functions without binding
a port.
*/

if (
  require.main === module
) {
  app.listen(
    PORT,
    HOST,
    () => {
      console.log(
        "=================================================="
      );

      console.log(
        `[server] Running on port ${PORT}`
      );

      console.log(
        `[server] http://localhost:${PORT}`
      );

      console.log(
        `[server] Store page size: ${STORE_PAGE_SIZE}`
      );

      console.log(
        "=================================================="
      );

    }
  );
}

module.exports = {
  app,
  runScrape,
  scrapeState,
  createBrowser,
  preparePage,
  extractProductDetail,
  extractItemSpecifics,
  extractProductLinks,
  detectStoreTotal,
  detectStoreFrontPage,
  collectAllStoreProducts,
  scrapeAllDetails,
  createExcel,
  openPage,
  pageLooksBlocked,
  pageShowsNoResults,
  getSellerName,
  getStoreName,
  buildStoreFrontUrl,
  buildSellerSearchUrl,
  extractItemId,
  normalizeUrl,
  findSpecific,
};
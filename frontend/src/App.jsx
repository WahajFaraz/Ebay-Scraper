import { useState, useEffect, useRef, useCallback } from "react";

function App() {
  const [darkMode, setDarkMode] = useState(true);
  const [url, setUrl] = useState("");

  const [status, setStatus] = useState({
    status: "idle",
    message: "Ready to scrape",
    running: false,
    stopped: false,
    detectedTotal: 0,
    collectedProducts: 0,
    completedProducts: 0,
    currentPage: 0,
    totalPages: 0,
    currentProduct: 0,
    fileName: null,
    error: null,
  });

  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);

  const pollRef = useRef(null);

  const API_BASE = "/api";

  // =========================================================
  // STATUS POLLING
  // =========================================================

  const fetchStatus = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/status`, {
        method: "GET",
        cache: "no-store",
      });

      if (!response.ok) {
        throw new Error(`Status request failed: ${response.status}`);
      }

      const data = await response.json();

      setStatus((prev) => ({
        ...prev,
        ...data,
      }));

      return data;
    } catch (err) {
      console.error("Status error:", err);
      return null;
    }
  }, []);

  const startPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
    }

    fetchStatus();

    pollRef.current = setInterval(async () => {
      const data = await fetchStatus();

      if (!data) return;

      const finishedStatuses = [
        "completed",
        "error",
        "stopped",
        "idle",
      ];

      if (
        !data.running &&
        finishedStatuses.includes(data.status)
      ) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }, 1000);
  }, [fetchStatus]);

  useEffect(() => {
    fetchStatus();

    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
      }
    };
  }, [fetchStatus]);

  // =========================================================
  // START SCRAPING
  // =========================================================

  const handleStart = async () => {
    const cleanUrl = url.trim();

    if (!cleanUrl) {
      setError("Please enter an eBay Store URL.");
      return;
    }

    if (!/^https?:\/\/(www\.)?ebay\./i.test(cleanUrl)) {
      setError("Please enter a valid eBay URL.");
      return;
    }

    setError("");
    setStarting(true);

    try {
      const response = await fetch(`${API_BASE}/scrape`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          url: cleanUrl,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data?.message || data?.error || "Failed to start scraping."
        );
      }

      setStatus((prev) => ({
        ...prev,
        running: true,
        stopped: false,
        status: data.status || "starting",
        message:
          data.message || "Scraping started successfully.",
        error: null,
      }));

      startPolling();
    } catch (err) {
      console.error("Start scraping error:", err);

      setError(
        err?.message ||
          "Unable to start scraping. Please try again."
      );
    } finally {
      setStarting(false);
    }
  };

  // =========================================================
  // STOP SCRAPING
  // =========================================================

  const handleStop = async () => {
    if (stopping) return;

    setStopping(true);
    setError("");

    try {
      const response = await fetch(`${API_BASE}/stop`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data?.message || data?.error || "Failed to stop scraping."
        );
      }

      setStatus((prev) => ({
        ...prev,
        stopped: true,
        status: "stopping",
        message:
          data.message ||
          "Stopping scraper... Please wait.",
      }));

      startPolling();
    } catch (err) {
      console.error("Stop scraping error:", err);

      setError(
        err?.message ||
          "Unable to stop scraping. Please try again."
      );
    } finally {
      setStopping(false);
    }
  };

  // =========================================================
  // NEW SCRAPE
  // =========================================================

  const handleNewScrape = () => {
    setUrl("");
    setError("");

    setStatus({
      status: "idle",
      message: "Ready to scrape",
      running: false,
      stopped: false,
      detectedTotal: 0,
      collectedProducts: 0,
      completedProducts: 0,
      currentPage: 0,
      totalPages: 0,
      currentProduct: 0,
      fileName: null,
      error: null,
    });

    fetchStatus();
  };

  // =========================================================
  // DOWNLOAD
  // =========================================================

  const handleDownload = () => {
    window.open(`${API_BASE}/download`, "_blank");
  };

  // =========================================================
  // DERIVED VALUES
  // =========================================================

  const backendStatus = status.status || "idle";

  const isRunning =
    status.running ||
    [
      "starting",
      "detecting_total",
      "collecting_products",
      "scraping_details",
      "creating_excel",
      "stopping",
    ].includes(backendStatus);

  const isCompleted = backendStatus === "completed";

  const isError =
    backendStatus === "error" ||
    Boolean(status.error) ||
    Boolean(error);

  const isStopped =
    backendStatus === "stopped" ||
    (status.stopped && !status.running);

  const productsFound = Number(
    status.collectedProducts || 0
  );

  const storeTotal = Number(
    status.detectedTotal || 0
  );

  const detailsDone = Number(
    status.completedProducts || 0
  );

  const currentProduct = Number(
    status.currentProduct || 0
  );

  const currentPage = Number(
    status.currentPage || 0
  );

  const totalPages = Number(
    status.totalPages || 0
  );

  const detailProgress =
    productsFound > 0
      ? Math.min(
          100,
          Math.round(
            (detailsDone / productsFound) * 100
          )
        )
      : 0;

  const collectionProgress =
    storeTotal > 0
      ? Math.min(
          100,
          Math.round(
            (productsFound / storeTotal) * 100
          )
        )
      : 0;

  const overallProgress = isCompleted
    ? 100
    : backendStatus === "scraping_details" ||
      backendStatus === "creating_excel"
    ? detailProgress
    : collectionProgress;

  // =========================================================
  // STATUS UI
  // =========================================================

  const getStatusInfo = () => {
    switch (backendStatus) {
      case "starting":
        return {
          label: "Starting",
          description:
            "Preparing the browser and scraper...",
          icon: "🚀",
          type: "running",
        };

      case "detecting_total":
        return {
          label: "Finding Store",
          description:
            "Checking the eBay store and finding its product listing...",
          icon: "🔎",
          type: "running",
        };

      case "collecting_products":
        return {
          label: "Collecting Products",
          description:
            "Going through store pages and collecting unique products...",
          icon: "📦",
          type: "running",
        };

      case "scraping_details":
        return {
          label: "Scraping Details",
          description:
            "Opening product pages and collecting product information...",
          icon: "⚙️",
          type: "running",
        };

      case "creating_excel":
        return {
          label: "Creating Excel",
          description:
            "All product details are collected. Preparing your Excel file...",
          icon: "📊",
          type: "running",
        };

      case "completed":
        return {
          label: "Completed",
          description:
            "Scraping completed successfully. Your Excel file is ready.",
          icon: "✓",
          type: "success",
        };

      case "stopping":
        return {
          label: "Stopping",
          description:
            "Stopping the scraper safely. Please wait...",
          icon: "⏹",
          type: "warning",
        };

      case "stopped":
        return {
          label: "Stopped",
          description:
            "The scraping process was stopped.",
          icon: "■",
          type: "warning",
        };

      case "error":
        return {
          label: "Error",
          description:
            status.error ||
            "Something went wrong during scraping.",
          icon: "!",
          type: "error",
        };

      default:
        return {
          label: "Ready",
          description:
            "Enter an eBay Store URL and start scraping.",
          icon: "◉",
          type: "idle",
        };
    }
  };

  const statusInfo = getStatusInfo();

  // =========================================================
  // HELPERS
  // =========================================================

  const formatNumber = (value) => {
    return Number(value || 0).toLocaleString();
  };

  const getPhaseClass = (phase) => {
    if (
      backendStatus === phase ||
      (phase === "collecting_products" &&
        backendStatus === "detecting_total")
    ) {
      return "active";
    }

    return "";
  };

  // =========================================================
  // UI
  // =========================================================

  return (
    <div className={`app ${darkMode ? "dark" : ""}`}>
      <div className="container">

        {/* ================================================= */}
        {/* HEADER */}
        {/* ================================================= */}

        <header className="header">
          <div className="brand">
            <div className="brand-icon">
              EB
            </div>

            <div>
              <h1>eBay Product Scraper</h1>
              <p>
                Collect complete product data into Excel
              </p>
            </div>
          </div>

          <button
            type="button"
            className="theme-btn"
            onClick={() =>
              setDarkMode((prev) => !prev)
            }
            title={
              darkMode
                ? "Switch to light mode"
                : "Switch to dark mode"
            }
          >
            {darkMode ? "☀️" : "🌙"}
          </button>
        </header>

        {/* ================================================= */}
        {/* URL INPUT */}
        {/* ================================================= */}

        <section className="card input-card">
          <div className="section-heading">
            <div>
              <h2>Start a Scrape</h2>
              <p>
                Enter an eBay Store URL to collect all
                available products.
              </p>
            </div>

            <span className="live-badge">
              ● LIVE
            </span>
          </div>

          <div className="url-row">
            <div className="url-input-wrapper">
              <span className="url-icon">
                🔗
              </span>

              <input
                type="url"
                value={url}
                onChange={(e) =>
                  setUrl(e.target.value)
                }
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !isRunning
                  ) {
                    handleStart();
                  }
                }}
                placeholder="https://www.ebay.com/str/example-store"
                disabled={isRunning || starting}
              />
            </div>

            {!isRunning ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleStart}
                disabled={starting}
              >
                {starting ? (
                  <>
                    <span className="spinner" />
                    Starting...
                  </>
                ) : (
                  <>
                    ▶ Start Scraping
                  </>
                )}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-danger"
                onClick={handleStop}
                disabled={
                  stopping ||
                  backendStatus === "stopping"
                }
              >
                {stopping ||
                backendStatus === "stopping" ? (
                  <>
                    <span className="spinner" />
                    Stopping...
                  </>
                ) : (
                  <>■ Stop Scraping</>
                )}
              </button>
            )}
          </div>

          {error && (
            <div className="alert alert-error">
              <span>!</span>
              <div>
                <strong>Something went wrong</strong>
                <p>{error}</p>
              </div>
            </div>
          )}

          {status.message && !error && (
            <div className="current-message">
              <span className="message-dot" />
              <span>{status.message}</span>
            </div>
          )}
        </section>

        {/* ================================================= */}
        {/* CURRENT STATUS */}
        {/* ================================================= */}

        <section className="card status-card">
          <div className="status-top">
            <div className="status-title">
              <div
                className={`status-icon ${statusInfo.type}`}
              >
                {statusInfo.icon}
              </div>

              <div>
                <div className="eyebrow">
                  CURRENT STATUS
                </div>

                <h2>{statusInfo.label}</h2>

                <p>
                  {statusInfo.description}
                </p>
              </div>
            </div>

            <div
              className={`status-pill ${statusInfo.type}`}
            >
              <span className="status-dot" />
              {isRunning
                ? "PROCESSING"
                : isCompleted
                ? "READY"
                : isError
                ? "ERROR"
                : isStopped
                ? "STOPPED"
                : "IDLE"}
            </div>
          </div>

          {/* ================================================= */}
          {/* PROGRESS */}
          {/* ================================================= */}

          <div className="progress-section">
            <div className="progress-header">
              <span>Overall Progress</span>

              <strong>
                {overallProgress}%
              </strong>
            </div>

            <div className="progress-container">
              <div
                className={`progress-bar ${
                  isCompleted
                    ? "complete"
                    : isError
                    ? "failed"
                    : ""
                }`}
                style={{
                  width: `${overallProgress}%`,
                }}
              />
            </div>

            <div className="progress-footer">
              <span>
                {formatNumber(productsFound)} products
                collected
              </span>

              <span>
                {formatNumber(detailsDone)} details
                scraped
              </span>
            </div>
          </div>

          {/* ================================================= */}
          {/* SCRAPING PIPELINE */}
          {/* ================================================= */}

          <div className="pipeline">
            <div
              className={`pipeline-step ${
                backendStatus === "starting" ||
                backendStatus ===
                  "detecting_total"
                  ? "active"
                  : productsFound > 0
                  ? "done"
                  : ""
              }`}
            >
              <div className="pipeline-number">
                {productsFound > 0 ? "✓" : "1"}
              </div>

              <div>
                <strong>Find Store</strong>
                <span>
                  Detect store & product count
                </span>
              </div>
            </div>

            <div
              className={`pipeline-line ${
                productsFound > 0 ? "done" : ""
              }`}
            />

            <div
              className={`pipeline-step ${
                backendStatus ===
                  "collecting_products" ||
                backendStatus ===
                  "detecting_total"
                  ? "active"
                  : detailsDone > 0
                  ? "done"
                  : ""
              }`}
            >
              <div className="pipeline-number">
                {detailsDone > 0
                  ? "✓"
                  : "2"}
              </div>

              <div>
                <strong>Collect Products</strong>
                <span>
                  Find all unique product URLs
                </span>
              </div>
            </div>

            <div
              className={`pipeline-line ${
                detailsDone > 0 ? "done" : ""
              }`}
            />

            <div
              className={`pipeline-step ${
                backendStatus ===
                "scraping_details"
                  ? "active"
                  : isCompleted ||
                    backendStatus ===
                      "creating_excel"
                  ? "done"
                  : ""
              }`}
            >
              <div className="pipeline-number">
                {isCompleted ||
                backendStatus ===
                  "creating_excel"
                  ? "✓"
                  : "3"}
              </div>

              <div>
                <strong>Scrape Details</strong>
                <span>
                  Extract product information
                </span>
              </div>
            </div>

            <div
              className={`pipeline-line ${
                isCompleted ? "done" : ""
              }`}
            />

            <div
              className={`pipeline-step ${
                backendStatus ===
                "creating_excel"
                  ? "active"
                  : isCompleted
                  ? "done"
                  : ""
              }`}
            >
              <div className="pipeline-number">
                {isCompleted ? "✓" : "4"}
              </div>

              <div>
                <strong>Create Excel</strong>
                <span>
                  Prepare final downloadable file
                </span>
              </div>
            </div>
          </div>
        </section>

        {/* ================================================= */}
        {/* LIVE METRICS */}
        {/* ================================================= */}

        <section className="metrics">

          <div className="metric">
            <div className="metric-icon blue">
              🏪
            </div>

            <div>
              <span>Store Total</span>
              <strong>
                {formatNumber(storeTotal)}
              </strong>

              <small>
                {storeTotal > 0
                  ? "Products detected"
                  : "Waiting for store scan"}
              </small>
            </div>
          </div>

          <div className="metric">
            <div className="metric-icon purple">
              📦
            </div>

            <div>
              <span>Products Found</span>
              <strong>
                {formatNumber(productsFound)}
              </strong>

              <small>
                {storeTotal > 0
                  ? `${collectionProgress}% collected`
                  : "Collecting..."}
              </small>
            </div>
          </div>

          <div className="metric">
            <div className="metric-icon green">
              ✓
            </div>

            <div>
              <span>Details Scraped</span>
              <strong>
                {formatNumber(detailsDone)}
              </strong>

              <small>
                {productsFound > 0
                  ? `${detailProgress}% complete`
                  : "Waiting for products"}
              </small>
            </div>
          </div>

          <div className="metric">
            <div className="metric-icon orange">
              📄
            </div>

            <div>
              <span>Details Remaining</span>
              <strong>
                {formatNumber(
                  Math.max(
                    productsFound -
                      detailsDone,
                    0
                  )
                )}
              </strong>

              <small>
                {isCompleted
                  ? "All completed"
                  : "Still processing"}
              </small>
            </div>
          </div>
        </section>

        {/* ================================================= */}
        {/* LIVE SCRAPING DETAILS */}
        {/* ================================================= */}

        <section className="card details-card">
          <div className="section-heading">
            <div>
              <div className="eyebrow">
                LIVE SCRAPER MONITOR
              </div>

              <h2>
                Scraping Progress
              </h2>

              <p>
                Everything happening in the
                background is shown here in real
                time.
              </p>
            </div>

            {isRunning && (
              <div className="live-indicator">
                <span />
                Live
              </div>
            )}
          </div>

          <div className="live-grid">

            <div className="live-item">
              <span className="live-label">
                Current Page
              </span>

              <strong>
                {formatNumber(currentPage)}
              </strong>

              <small>
                {totalPages > 0
                  ? `of ${formatNumber(
                      totalPages
                    )} pages`
                  : "Pages being discovered"}
              </small>
            </div>

            <div className="live-item">
              <span className="live-label">
                Pages Checked
              </span>

              <strong>
                {formatNumber(currentPage)}
              </strong>

              <small>
                Store pagination
              </small>
            </div>

            <div className="live-item">
              <span className="live-label">
                Current Product
              </span>

              <strong>
                {currentProduct > 0
                  ? formatNumber(
                      currentProduct
                    )
                  : "—"}
              </strong>

              <small>
                {productsFound > 0
                  ? `of ${formatNumber(
                      productsFound
                    )}`
                  : "Waiting..."}
              </small>
            </div>

            <div className="live-item">
              <span className="live-label">
                Detail Progress
              </span>

              <strong>
                {detailProgress}%
              </strong>

              <div className="mini-progress">
                <div
                  style={{
                    width: `${detailProgress}%`,
                  }}
                />
              </div>
            </div>
          </div>

          {/* DETAILS PROGRESS BAR */}

          <div className="detail-progress-block">
            <div className="detail-progress-header">
              <div>
                <strong>
                  Product Details
                </strong>

                <span>
                  {formatNumber(
                    detailsDone
                  )}{" "}
                  of{" "}
                  {formatNumber(
                    productsFound
                  )} completed
                </span>
              </div>

              <strong>
                {detailProgress}%
              </strong>
            </div>

            <div className="progress-container">
              <div
                className="progress-bar detail"
                style={{
                  width: `${detailProgress}%`,
                }}
              />
            </div>
          </div>
        </section>

        {/* ================================================= */}
        {/* EXCEL RESULT */}
        {/* ================================================= */}

        <section
          className={`card excel-card ${
            isCompleted ? "ready" : ""
          }`}
        >
          <div className="excel-icon">
            📊
          </div>

          <div className="excel-content">
            <div className="eyebrow">
              EXPORT
            </div>

            <h2>
              {isCompleted
                ? "Excel File Ready"
                : backendStatus ===
                  "creating_excel"
                ? "Creating Excel File..."
                : "Excel Export"}
            </h2>

            <p>
              {isCompleted
                ? "Your scraped product data has been successfully saved and is ready to download."
                : backendStatus ===
                  "creating_excel"
                ? "The product details are complete. We are now preparing the Excel file."
                : "The Excel file will be generated automatically after all product details are scraped."}
            </p>

            {status.fileName && (
              <div className="file-name">
                <span>📄</span>
                <span>
                  {status.fileName}
                </span>
              </div>
            )}
          </div>

          {isCompleted && (
            <button
              type="button"
              className="btn btn-success download-btn"
              onClick={handleDownload}
            >
              ↓ Download Excel
            </button>
          )}
        </section>

        {/* ================================================= */}
        {/* FINAL SUMMARY */}
        {/* ================================================= */}

        {isCompleted && (
          <section className="completion-card">
            <div className="completion-check">
              ✓
            </div>

            <div className="completion-content">
              <h2>
                Scraping Completed Successfully
              </h2>

              <p>
                All collected products have been
                processed and the Excel file is
                ready.
              </p>

              <div className="completion-stats">
                <div>
                  <strong>
                    {formatNumber(
                      productsFound
                    )}
                  </strong>
                  <span>
                    Products
                  </span>
                </div>

                <div>
                  <strong>
                    {formatNumber(
                      detailsDone
                    )}
                  </strong>
                  <span>
                    Details
                  </span>
                </div>

                <div>
                  <strong>
                    {formatNumber(
                      currentPage
                    )}
                  </strong>
                  <span>
                    Pages
                  </span>
                </div>

                <div>
                  <strong>
                    8
                  </strong>
                  <span>
                    Excel Columns
                  </span>
                </div>
              </div>
            </div>

            <button
              type="button"
              className="btn btn-primary"
              onClick={handleNewScrape}
            >
              + New Scrape
            </button>
          </section>
        )}

        {/* ================================================= */}
        {/* STOPPED STATE */}
        {/* ================================================= */}

        {isStopped && !isCompleted && (
          <section className="stopped-card">
            <div className="stopped-icon">
              ■
            </div>

            <div>
              <h3>
                Scraping Stopped
              </h3>

              <p>
                The scraper stopped safely.
                You can start a new scrape
                whenever you are ready.
              </p>
            </div>

            <button
              type="button"
              className="btn btn-primary"
              onClick={handleNewScrape}
            >
              Start New Scrape
            </button>
          </section>
        )}

        {/* ================================================= */}
        {/* ERROR STATE */}
        {/* ================================================= */}

        {isError && (
          <section className="error-card">
            <div className="error-big-icon">
              !
            </div>

            <div>
              <h3>
                Scraping Error
              </h3>

              <p>
                {status.error ||
                  error ||
                  "An unexpected error occurred."}
              </p>
            </div>

            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleNewScrape}
            >
              Try Again
            </button>
          </section>
        )}

        {/* ================================================= */}
        {/* INFO */}
        {/* ================================================= */}

        <section className="info-grid">

          <div className="info-card">
            <div className="info-icon">
              🔍
            </div>

            <div>
              <h3>
                Complete Product Data
              </h3>

              <p>
                Title, price, brand, model,
                MPN, description, image URL
                and product URL.
              </p>
            </div>
          </div>

          <div className="info-card">
            <div className="info-icon">
              📦
            </div>

            <div>
              <h3>
                Unique Products
              </h3>

              <p>
                Products are deduplicated while
                collecting store pages so repeated
                listings are not counted twice.
              </p>
            </div>
          </div>

          <div className="info-card">
            <div className="info-icon">
              📊
            </div>

            <div>
              <h3>
                Excel Export
              </h3>

              <p>
                The final data is automatically
                converted into an Excel file
                after scraping finishes.
              </p>
            </div>
          </div>

        </section>

        {/* ================================================= */}
        {/* FOOTER */}
        {/* ================================================= */}

        <footer className="footer">
          <span>
            eBay Product Scraper
          </span>

          <span>
            Live scraping dashboard
          </span>
        </footer>
      </div>

      {/* =================================================== */}
      {/* PAGE STYLES */}
      {/* =================================================== */}

      <style>{`
        * {
          box-sizing: border-box;
        }

        body {
          margin: 0;
          font-family:
            Inter,
            ui-sans-serif,
            system-ui,
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            sans-serif;
          background: #f5f7fb;
        }

        button,
        input {
          font: inherit;
        }

        .app {
          min-height: 100vh;
          background:
            radial-gradient(
              circle at top left,
              rgba(37, 99, 235, 0.08),
              transparent 32%
            ),
            #f5f7fb;
          color: #111827;
          padding: 34px 20px;
          transition: 0.25s ease;
        }

        .app.dark {
          background:
            radial-gradient(
              circle at top left,
              rgba(59, 130, 246, 0.13),
              transparent 30%
            ),
            #080b12;
          color: #f3f4f6;
        }

        .container {
          width: min(1180px, 100%);
          margin: 0 auto;
        }

        /* HEADER */

        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 26px;
        }

        .brand {
          display: flex;
          align-items: center;
          gap: 15px;
        }

        .brand-icon {
          width: 52px;
          height: 52px;
          border-radius: 15px;
          display: grid;
          place-items: center;
          background:
            linear-gradient(
              135deg,
              #2563eb,
              #7c3aed
            );
          color: white;
          font-size: 16px;
          font-weight: 900;
          box-shadow:
            0 12px 30px
            rgba(37, 99, 235, 0.28);
        }

        .brand h1 {
          margin: 0;
          font-size: 25px;
          letter-spacing: -0.5px;
        }

        .brand p {
          margin: 5px 0 0;
          color: #6b7280;
          font-size: 13px;
        }

        .dark .brand p {
          color: #9ca3af;
        }

        .theme-btn {
          border: 1px solid #dbe1ea;
          background: white;
          width: 43px;
          height: 43px;
          border-radius: 12px;
          cursor: pointer;
          font-size: 18px;
          transition: 0.2s ease;
        }

        .dark .theme-btn {
          background: #121722;
          border-color: #242b39;
        }

        .theme-btn:hover {
          transform: translateY(-2px);
        }

        /* CARDS */

        .card {
          background: rgba(255, 255, 255, 0.94);
          border: 1px solid #e4e8ef;
          border-radius: 20px;
          padding: 25px;
          margin-bottom: 18px;
          box-shadow:
            0 10px 35px
            rgba(15, 23, 42, 0.045);
        }

        .dark .card {
          background: rgba(15, 19, 29, 0.92);
          border-color: #202735;
          box-shadow:
            0 16px 40px
            rgba(0, 0, 0, 0.18);
        }

        .section-heading {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 20px;
          margin-bottom: 22px;
        }

        .section-heading h2 {
          margin: 3px 0 6px;
          font-size: 20px;
        }

        .section-heading p {
          margin: 0;
          color: #6b7280;
          font-size: 13px;
          line-height: 1.6;
        }

        .dark .section-heading p {
          color: #9ca3af;
        }

        .eyebrow {
          color: #6b7280;
          font-size: 10px;
          font-weight: 800;
          letter-spacing: 1.3px;
        }

        .dark .eyebrow {
          color: #7f8ba3;
        }

        .live-badge {
          padding: 7px 10px;
          border-radius: 999px;
          background: #ecfdf5;
          color: #059669;
          font-size: 10px;
          font-weight: 800;
          letter-spacing: 0.8px;
        }

        .dark .live-badge {
          background: rgba(16, 185, 129, 0.1);
          color: #34d399;
        }

        /* INPUT */

        .url-row {
          display: flex;
          gap: 12px;
        }

        .url-input-wrapper {
          flex: 1;
          position: relative;
          display: flex;
          align-items: center;
        }

        .url-icon {
          position: absolute;
          left: 16px;
          font-size: 16px;
          opacity: 0.65;
          pointer-events: none;
        }

        .url-input-wrapper input {
          width: 100%;
          height: 50px;
          border: 1px solid #dce2ea;
          border-radius: 12px;
          background: #f9fafb;
          color: #111827;
          outline: none;
          padding: 0 15px 0 45px;
          transition: 0.2s ease;
        }

        .url-input-wrapper input:focus {
          border-color: #2563eb;
          background: white;
          box-shadow:
            0 0 0 4px
            rgba(37, 99, 235, 0.1);
        }

        .dark .url-input-wrapper input {
          background: #0c1018;
          border-color: #293142;
          color: #f3f4f6;
        }

        .dark
          .url-input-wrapper
          input:focus {
          background: #0c1018;
          border-color: #3b82f6;
        }

        .btn {
          min-height: 50px;
          border: 0;
          border-radius: 12px;
          padding: 0 19px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          font-weight: 750;
          font-size: 13px;
          cursor: pointer;
          transition:
            transform 0.18s ease,
            box-shadow 0.18s ease,
            opacity 0.18s ease;
          white-space: nowrap;
        }

        .btn:hover:not(:disabled) {
          transform: translateY(-2px);
        }

        .btn:disabled {
          opacity: 0.55;
          cursor: not-allowed;
        }

        .btn-primary {
          color: white;
          background:
            linear-gradient(
              135deg,
              #2563eb,
              #4f46e5
            );
          box-shadow:
            0 10px 25px
            rgba(37, 99, 235, 0.2);
        }

        .btn-danger {
          color: white;
          background:
            linear-gradient(
              135deg,
              #dc2626,
              #b91c1c
            );
        }

        .btn-success {
          color: white;
          background:
            linear-gradient(
              135deg,
              #059669,
              #047857
            );
          box-shadow:
            0 10px 25px
            rgba(5, 150, 105, 0.2);
        }

        .btn-secondary {
          color: #111827;
          background: #e5e7eb;
        }

        .dark .btn-secondary {
          color: white;
          background: #252c3a;
        }

        .spinner {
          width: 15px;
          height: 15px;
          border: 2px solid rgba(255,255,255,0.35);
          border-top-color: white;
          border-radius: 50%;
          animation: spin 0.7s linear infinite;
        }

        @keyframes spin {
          to {
            transform: rotate(360deg);
          }
        }

        .current-message {
          margin-top: 14px;
          display: flex;
          align-items: center;
          gap: 8px;
          color: #6b7280;
          font-size: 12px;
        }

        .dark .current-message {
          color: #9ca3af;
        }

        .message-dot {
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: #22c55e;
          box-shadow:
            0 0 0 4px
            rgba(34, 197, 94, 0.1);
        }

        /* ALERT */

        .alert {
          margin-top: 15px;
          padding: 13px 15px;
          border-radius: 12px;
          display: flex;
          gap: 12px;
          align-items: flex-start;
          font-size: 12px;
        }

        .alert-error {
          background: #fef2f2;
          border: 1px solid #fecaca;
          color: #991b1b;
        }

        .dark .alert-error {
          background: rgba(127, 29, 29, 0.16);
          border-color: rgba(239, 68, 68, 0.25);
          color: #fca5a5;
        }

        .alert strong {
          display: block;
          margin-bottom: 3px;
        }

        .alert p {
          margin: 0;
        }

        /* STATUS */

        .status-top {
          display: flex;
          justify-content: space-between;
          gap: 20px;
          align-items: center;
        }

        .status-title {
          display: flex;
          align-items: center;
          gap: 15px;
        }

        .status-icon {
          width: 52px;
          height: 52px;
          flex: 0 0 52px;
          border-radius: 15px;
          display: grid;
          place-items: center;
          font-size: 19px;
          font-weight: 900;
        }

        .status-icon.running {
          background: #eff6ff;
          color: #2563eb;
        }

        .dark .status-icon.running {
          background: rgba(37, 99, 235, 0.13);
        }

        .status-icon.success {
          background: #ecfdf5;
          color: #059669;
        }

        .dark .status-icon.success {
          background: rgba(16, 185, 129, 0.12);
        }

        .status-icon.warning {
          background: #fffbeb;
          color: #d97706;
        }

        .dark .status-icon.warning {
          background: rgba(217, 119, 6, 0.12);
        }

        .status-icon.error {
          background: #fef2f2;
          color: #dc2626;
        }

        .dark .status-icon.error {
          background: rgba(220, 38, 38, 0.12);
        }

        .status-icon.idle {
          background: #f3f4f6;
          color: #6b7280;
        }

        .dark .status-icon.idle {
          background: #1b212c;
          color: #9ca3af;
        }

        .status-title h2 {
          margin: 3px 0 5px;
          font-size: 20px;
        }

        .status-title p {
          margin: 0;
          color: #6b7280;
          font-size: 12px;
        }

        .dark .status-title p {
          color: #9ca3af;
        }

        .status-pill {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 8px 11px;
          border-radius: 999px;
          font-size: 10px;
          font-weight: 800;
          letter-spacing: 0.7px;
        }

        .status-pill.running {
          color: #2563eb;
          background: #eff6ff;
        }

        .dark .status-pill.running {
          background: rgba(37, 99, 235, 0.1);
        }

        .status-pill.success {
          color: #059669;
          background: #ecfdf5;
        }

        .dark .status-pill.success {
          background: rgba(16, 185, 129, 0.1);
        }

        .status-pill.warning {
          color: #b45309;
          background: #fffbeb;
        }

        .dark .status-pill.warning {
          background: rgba(217, 119, 6, 0.1);
        }

        .status-pill.error {
          color: #dc2626;
          background: #fef2f2;
        }

        .dark .status-pill.error {
          background: rgba(220, 38, 38, 0.1);
        }

        .status-pill.idle {
          color: #6b7280;
          background: #f3f4f6;
        }

        .dark .status-pill.idle {
          background: #1b212c;
        }

        .status-dot {
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: currentColor;
        }

        /* PROGRESS */

        .progress-section {
          margin-top: 27px;
        }

        .progress-header {
          display: flex;
          justify-content: space-between;
          margin-bottom: 9px;
          font-size: 12px;
          color: #6b7280;
        }

        .dark .progress-header {
          color: #9ca3af;
        }

        .progress-header strong {
          color: #111827;
        }

        .dark .progress-header strong {
          color: #f3f4f6;
        }

        .progress-container {
          width: 100%;
          height: 10px;
          background: #e9edf3;
          border-radius: 999px;
          overflow: hidden;
        }

        .dark .progress-container {
          background: #202633;
        }

        .progress-bar {
          height: 100%;
          border-radius: inherit;
          background:
            linear-gradient(
              90deg,
              #2563eb,
              #7c3aed
            );
          transition: width 0.45s ease;
          position: relative;
          overflow: hidden;
        }

        .progress-bar::after {
          content: "";
          position: absolute;
          inset: 0;
          background:
            linear-gradient(
              90deg,
              transparent,
              rgba(255,255,255,0.22),
              transparent
            );
          animation: shine 1.8s infinite;
        }

        @keyframes shine {
          from {
            transform: translateX(-100%);
          }

          to {
            transform: translateX(100%);
          }
        }

        .progress-bar.complete {
          background:
            linear-gradient(
              90deg,
              #059669,
              #10b981
            );
        }

        .progress-bar.failed {
          background: #dc2626;
        }

        .progress-footer {
          display: flex;
          justify-content: space-between;
          margin-top: 8px;
          color: #9ca3af;
          font-size: 10px;
        }

        /* PIPELINE */

        .pipeline {
          display: grid;
          grid-template-columns:
            1fr
            40px
            1fr
            40px
            1fr
            40px
            1fr;
          align-items: center;
          margin-top: 30px;
        }

        .pipeline-step {
          display: flex;
          align-items: center;
          gap: 9px;
          opacity: 0.55;
        }

        .pipeline-step.active,
        .pipeline-step.done {
          opacity: 1;
        }

        .pipeline-number {
          width: 31px;
          height: 31px;
          flex: 0 0 31px;
          border-radius: 50%;
          display: grid;
          place-items: center;
          background: #edf0f4;
          color: #6b7280;
          font-size: 11px;
          font-weight: 800;
        }

        .dark .pipeline-number {
          background: #202633;
          color: #9ca3af;
        }

        .pipeline-step.active
          .pipeline-number {
          background: #2563eb;
          color: white;
          box-shadow:
            0 0 0 5px
            rgba(37, 99, 235, 0.1);
        }

        .pipeline-step.done
          .pipeline-number {
          background: #059669;
          color: white;
        }

        .pipeline-step strong {
          display: block;
          font-size: 10px;
        }

        .pipeline-step span {
          display: block;
          margin-top: 2px;
          color: #9ca3af;
          font-size: 9px;
        }

        .pipeline-line {
          height: 2px;
          background: #e5e7eb;
        }

        .dark .pipeline-line {
          background: #252c38;
        }

        .pipeline-line.done {
          background: #10b981;
        }

        /* METRICS */

        .metrics {
          display: grid;
          grid-template-columns:
            repeat(4, 1fr);
          gap: 14px;
          margin-bottom: 18px;
        }

        .metric {
          display: flex;
          align-items: center;
          gap: 13px;
          min-width: 0;
          background: white;
          border: 1px solid #e4e8ef;
          border-radius: 17px;
          padding: 17px;
          box-shadow:
            0 8px 25px
            rgba(15, 23, 42, 0.035);
        }

        .dark .metric {
          background: #0f131d;
          border-color: #202735;
        }

        .metric-icon {
          width: 43px;
          height: 43px;
          flex: 0 0 43px;
          display: grid;
          place-items: center;
          border-radius: 12px;
          font-size: 17px;
        }

        .metric-icon.blue {
          background: #eff6ff;
        }

        .metric-icon.purple {
          background: #f5f3ff;
        }

        .metric-icon.green {
          background: #ecfdf5;
        }

        .metric-icon.orange {
          background: #fff7ed;
        }

        .dark .metric-icon.blue {
          background: rgba(37, 99, 235, 0.1);
        }

        .dark .metric-icon.purple {
          background: rgba(124, 58, 237, 0.1);
        }

        .dark .metric-icon.green {
          background: rgba(16, 185, 129, 0.1);
        }

        .dark .metric-icon.orange {
          background: rgba(249, 115, 22, 0.1);
        }

        .metric span {
          display: block;
          color: #6b7280;
          font-size: 10px;
          font-weight: 700;
        }

        .dark .metric span {
          color: #9ca3af;
        }

        .metric strong {
          display: block;
          margin-top: 2px;
          font-size: 22px;
          letter-spacing: -0.5px;
        }

        .metric small {
          display: block;
          margin-top: 2px;
          color: #9ca3af;
          font-size: 9px;
        }

        /* LIVE DETAILS */

        .live-indicator {
          display: flex;
          align-items: center;
          gap: 7px;
          color: #059669;
          font-size: 11px;
          font-weight: 800;
        }

        .live-indicator span {
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: #10b981;
          box-shadow:
            0 0 0 5px
            rgba(16, 185, 129, 0.09);
          animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
          50% {
            opacity: 0.35;
            transform: scale(0.85);
          }
        }

        .live-grid {
          display: grid;
          grid-template-columns:
            repeat(4, 1fr);
          gap: 12px;
        }

        .live-item {
          background: #f8fafc;
          border: 1px solid #edf0f4;
          border-radius: 14px;
          padding: 15px;
        }

        .dark .live-item {
          background: #0b0f17;
          border-color: #202735;
        }

        .live-label {
          display: block;
          color: #6b7280;
          font-size: 10px;
          font-weight: 700;
        }

        .dark .live-label {
          color: #8d98ab;
        }

        .live-item strong {
          display: block;
          margin-top: 6px;
          font-size: 22px;
        }

        .live-item small {
          display: block;
          margin-top: 3px;
          color: #9ca3af;
          font-size: 9px;
        }

        .mini-progress {
          height: 5px;
          background: #e5e7eb;
          border-radius: 999px;
          overflow: hidden;
          margin-top: 9px;
        }

        .dark .mini-progress {
          background: #252c38;
        }

        .mini-progress div {
          height: 100%;
          border-radius: inherit;
          background: #2563eb;
          transition: width 0.4s ease;
        }

        .detail-progress-block {
          margin-top: 20px;
          padding-top: 20px;
          border-top: 1px solid #edf0f4;
        }

        .dark .detail-progress-block {
          border-top-color: #202735;
        }

        .detail-progress-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-end;
          margin-bottom: 9px;
        }

        .detail-progress-header strong {
          display: block;
          font-size: 12px;
        }

        .detail-progress-header span {
          display: block;
          margin-top: 3px;
          color: #9ca3af;
          font-size: 10px;
        }

        .progress-bar.detail {
          background:
            linear-gradient(
              90deg,
              #059669,
              #10b981
            );
        }

        /* EXCEL */

        .excel-card {
          display: flex;
          align-items: center;
          gap: 18px;
        }

        .excel-icon {
          width: 58px;
          height: 58px;
          flex: 0 0 58px;
          border-radius: 16px;
          display: grid;
          place-items: center;
          background: #ecfdf5;
          font-size: 25px;
        }

        .dark .excel-icon {
          background: rgba(16, 185, 129, 0.1);
        }

        .excel-content {
          flex: 1;
          min-width: 0;
        }

        .excel-content h2 {
          margin: 3px 0 5px;
          font-size: 18px;
        }

        .excel-content p {
          margin: 0;
          color: #6b7280;
          font-size: 12px;
          line-height: 1.6;
        }

        .dark .excel-content p {
          color: #9ca3af;
        }

        .file-name {
          margin-top: 10px;
          display: inline-flex;
          align-items: center;
          gap: 7px;
          max-width: 100%;
          padding: 7px 10px;
          border-radius: 8px;
          background: #f3f4f6;
          color: #4b5563;
          font-size: 10px;
          font-weight: 700;
        }

        .dark .file-name {
          background: #1a202b;
          color: #aeb7c7;
        }

        .download-btn {
          flex: 0 0 auto;
        }

        /* COMPLETION */

        .completion-card {
          display: flex;
          align-items: center;
          gap: 18px;
          padding: 22px;
          margin-bottom: 18px;
          border-radius: 20px;
          background:
            linear-gradient(
              135deg,
              #ecfdf5,
              #f0fdf4
            );
          border: 1px solid #bbf7d0;
        }

        .dark .completion-card {
          background:
            linear-gradient(
              135deg,
              rgba(5, 150, 105, 0.11),
              rgba(16, 185, 129, 0.05)
            );
          border-color: rgba(16, 185, 129, 0.22);
        }

        .completion-check {
          width: 52px;
          height: 52px;
          flex: 0 0 52px;
          display: grid;
          place-items: center;
          border-radius: 50%;
          background: #059669;
          color: white;
          font-size: 23px;
          font-weight: 900;
          box-shadow:
            0 8px 20px
            rgba(5, 150, 105, 0.2);
        }

        .completion-content {
          flex: 1;
        }

        .completion-content h2 {
          margin: 0 0 4px;
          font-size: 18px;
        }

        .completion-content p {
          margin: 0;
          color: #64748b;
          font-size: 12px;
        }

        .dark .completion-content p {
          color: #9ca3af;
        }

        .completion-stats {
          display: flex;
          gap: 22px;
          margin-top: 12px;
        }

        .completion-stats strong {
          display: block;
          font-size: 16px;
        }

        .completion-stats span {
          display: block;
          color: #64748b;
          font-size: 9px;
          margin-top: 2px;
        }

        .dark .completion-stats span {
          color: #9ca3af;
        }

        /* STOPPED / ERROR */

        .stopped-card,
        .error-card {
          display: flex;
          align-items: center;
          gap: 16px;
          margin-bottom: 18px;
          padding: 20px;
          border-radius: 18px;
        }

        .stopped-card {
          background: #fffbeb;
          border: 1px solid #fde68a;
        }

        .dark .stopped-card {
          background: rgba(217, 119, 6, 0.08);
          border-color: rgba(217, 119, 6, 0.22);
        }

        .error-card {
          background: #fef2f2;
          border: 1px solid #fecaca;
        }

        .dark .error-card {
          background: rgba(220, 38, 38, 0.08);
          border-color: rgba(220, 38, 38, 0.2);
        }

        .stopped-icon,
        .error-big-icon {
          width: 44px;
          height: 44px;
          flex: 0 0 44px;
          display: grid;
          place-items: center;
          border-radius: 12px;
          font-weight: 900;
        }

        .stopped-icon {
          background: #fef3c7;
          color: #b45309;
        }

        .error-big-icon {
          background: #fee2e2;
          color: #dc2626;
        }

        .stopped-card > div:nth-child(2),
        .error-card > div:nth-child(2) {
          flex: 1;
        }

        .stopped-card h3,
        .error-card h3 {
          margin: 0 0 4px;
          font-size: 15px;
        }

        .stopped-card p,
        .error-card p {
          margin: 0;
          color: #6b7280;
          font-size: 11px;
          line-height: 1.5;
        }

        .dark .stopped-card p,
        .dark .error-card p {
          color: #9ca3af;
        }

        /* INFO */

        .info-grid {
          display: grid;
          grid-template-columns:
            repeat(3, 1fr);
          gap: 14px;
          margin-top: 18px;
        }

        .info-card {
          display: flex;
          gap: 12px;
          padding: 17px;
          border-radius: 16px;
          background: white;
          border: 1px solid #e4e8ef;
        }

        .dark .info-card {
          background: #0f131d;
          border-color: #202735;
        }

        .info-icon {
          width: 38px;
          height: 38px;
          flex: 0 0 38px;
          display: grid;
          place-items: center;
          border-radius: 10px;
          background: #f3f4f6;
          font-size: 16px;
        }

        .dark .info-icon {
          background: #1a202b;
        }

        .info-card h3 {
          margin: 1px 0 4px;
          font-size: 12px;
        }

        .info-card p {
          margin: 0;
          color: #6b7280;
          font-size: 10px;
          line-height: 1.55;
        }

        .dark .info-card p {
          color: #8d98ab;
        }

        /* FOOTER */

        .footer {
          display: flex;
          justify-content: space-between;
          padding: 20px 3px 5px;
          color: #9ca3af;
          font-size: 9px;
        }

        /* RESPONSIVE */

        @media (max-width: 950px) {
          .metrics {
            grid-template-columns:
              repeat(2, 1fr);
          }

          .live-grid {
            grid-template-columns:
              repeat(2, 1fr);
          }

          .pipeline {
            grid-template-columns:
              1fr;
          }

          .pipeline-line {
            display: none;
          }

          .pipeline-step {
            padding: 8px 0;
          }

          .info-grid {
            grid-template-columns:
              1fr;
          }
        }

        @media (max-width: 700px) {
          .app {
            padding: 20px 12px;
          }

          .header {
            align-items: flex-start;
          }

          .brand h1 {
            font-size: 20px;
          }

          .url-row {
            flex-direction: column;
          }

          .url-row .btn {
            width: 100%;
          }

          .status-top {
            align-items: flex-start;
            flex-direction: column;
          }

          .metrics {
            grid-template-columns:
              1fr 1fr;
          }

          .live-grid {
            grid-template-columns:
              1fr 1fr;
          }

          .excel-card,
          .completion-card,
          .stopped-card,
          .error-card {
            align-items: flex-start;
            flex-direction: column;
          }

          .download-btn,
          .completion-card .btn,
          .stopped-card .btn,
          .error-card .btn {
            width: 100%;
          }

          .completion-stats {
            flex-wrap: wrap;
            gap: 14px 25px;
          }
        }

        @media (max-width: 450px) {
          .metrics,
          .live-grid {
            grid-template-columns:
              1fr;
          }

          .card {
            padding: 18px;
          }
        }
      `}</style>
    </div>
  );
}

export default App;
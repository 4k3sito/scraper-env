"""Shared utilities for scraper reliability: retry, checkpoint, atomic writes, graceful shutdown.

Designed as lightweight helpers — no external dependencies beyond stdlib + json.
Each function is self-contained; import what you need.
"""
import os, sys, json, time, signal, logging, functools
from pathlib import Path


# ── Retry ──────────────────────────────────────────────────────────────────

def retry_extract(extract_fn, url, retries=3, base_delay=2, backoff=2, url_key="url"):
    """Call extract_fn(url), retrying on any exception with exponential backoff.

    Returns the dict from extract_fn on success, or an error dict on final failure.
    extract_fn receives the url as its first argument.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return extract_fn(url) if url_key == "url" else extract_fn({"url": url})
        except Exception as e:
            last_error = str(e)
            if attempt < retries:
                delay = base_delay * (backoff ** (attempt - 1))
                print(f"   [retry] {url} — attempt {attempt}/{retries} failed: {last_error[:80]}, waiting {delay:.0f}s")
                time.sleep(delay)
    return {"url": url, "error": f"failed after {retries} retries: {last_error}"}


def retry_fetch(fetch_fn, url, retries=3, base_delay=2, backoff=2):
    """Call fetch_fn(url) with retry on exception. Returns (result, None) or (None, error_str)."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fetch_fn(url), None
        except Exception as e:
            last = str(e)
            if attempt < retries:
                delay = base_delay * (backoff ** (attempt - 1))
                print(f"   [retry] {url[:80]} — attempt {attempt}/{retries}: {last[:60]}, waiting {delay:.0f}s")
                time.sleep(delay)
    return None, f"failed after {retries} retries: {last}"


# ── Checkpoint / Progress ─────────────────────────────────────────────────

class Checkpoint:
    """Saves and loads progress for Phase 2 detail extraction.

    Usage:
        cp = Checkpoint("output/lamudi_checkpoint.json")
        # Before Phase 2:
        remaining = cp.resume(all_listings, key=lambda x: x["url"])
        # After each success:
        cp.done(url)
        # On completion:
        cp.clear()
    """
    def __init__(self, path):
        self.path = Path(path)
        self.completed = set()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.completed = set(data.get("completed", []))
                print(f"[checkpoint] loaded {len(self.completed)} completed URLs from {self.path}")
            except (json.JSONDecodeError, KeyError):
                self.completed = set()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"completed": list(self.completed)}, ensure_ascii=False))
        tmp.replace(self.path)

    def resume(self, items, key=lambda x: x):
        """Filter items to only those not yet completed. Returns remaining list."""
        if not self.completed:
            return items
        remaining = [i for i in items if key(i) not in self.completed]
        skipped = len(items) - len(remaining)
        if skipped:
            print(f"[checkpoint] skipping {skipped} already-completed items")
        return remaining

    def done(self, identifier):
        """Mark identifier as completed and persist checkpoint."""
        self.completed.add(identifier)
        self._save()

    def clear(self):
        """Remove checkpoint file (call after Phase 2 completes)."""
        if self.path.exists():
            self.path.unlink()
        tmp = self.path.with_suffix(".tmp")
        if tmp.exists():
            tmp.unlink()


# ── Atomic write ───────────────────────────────────────────────────────────

def atomic_write_json(data, path, **json_kw):
    """Write JSON atomically: write to .tmp, then rename.

    Prevents half-written files on crash. Also creates parent directories.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, **json_kw), encoding="utf-8")
    tmp.replace(path)


# ── Graceful shutdown ──────────────────────────────────────────────────────

_shutdown_requested = False

def _signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        print("\n[shutdown] Second signal received — forcing exit.")
        sys.exit(1)
    _shutdown_requested = True
    print("\n[shutdown] Ctrl+C caught — finishing current item, then saving partial results...")

def setup_graceful_shutdown():
    """Install signal handlers for SIGINT and SIGTERM.

    Call before starting Phase 2. Check should_stop() between items.
    """
    global _shutdown_requested
    _shutdown_requested = False
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

def should_stop():
    """Returns True if a shutdown signal was received."""
    return _shutdown_requested


# ── Dual logging ───────────────────────────────────────────────────────────

def setup_log(name, log_dir="output"):
    """Return a logger that writes to both file and terminal.

    Usage:
        log = setup_log("lamudi")
        log.info("Phase 2 starting...")
        log.warning("rate limit hit")
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"scraper.{name}")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(f"{log_dir}/{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log

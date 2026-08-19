"""Shared scraper plumbing: file logging and a Ctrl-C-safe run guard.

Progress bars are plain `tqdm` at the call sites — it already does determinate
(n/total + ETA) and indeterminate (counter + rate) and stays quiet off a TTY.

Both scrapers flush every row to the JSONL as they parse it, so data is already
durable — an interrupt never loses what was scraped. These helpers only make the
exit graceful, the timeline legible, and errors persistent on disk.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path


def setup_logging(out_path) -> logging.Logger:
    """Per-run logger writing the full timeline + errors to <out>.log. Live
    stdout stays clean for the progress bar; the file keeps the audit trail."""
    log_path = Path(out_path).with_name(Path(out_path).name + ".log")
    logger = logging.getLogger(f"scrape:{out_path}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # idempotent: re-runs in one process don't stack handlers
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    logger.info("run start → %s", out_path)
    return logger


@contextmanager
def graceful(logger: logging.Logger, summary):
    """Run a crawl body; on Ctrl-C (or crash) log it and print the completeness
    summary instead of a bare traceback. Data is already on disk — this only
    reports what was saved. `summary` is a zero-arg callable returning a string."""
    try:
        yield
    except KeyboardInterrupt:
        logger.warning("interrupted by user (SIGINT) — data already flushed to disk")
        sys.stderr.write("\n\n⏹  Interrupted. Everything scraped so far is saved.\n")
        sys.stderr.write(summary() + "\n")
        sys.exit(130)
    except Exception:
        logger.exception("crawl aborted with an unhandled error")
        sys.stderr.write("\n\n✖  Aborted (see log). Data scraped so far is saved.\n")
        sys.stderr.write(summary() + "\n")
        raise
    else:
        logger.info("run complete")
        sys.stderr.write("\n" + summary() + "\n")


if __name__ == "__main__":  # self-check: python scrape_utils.py
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "x.jsonl"
        log = setup_logging(out)

        # Ctrl-C: exits 130, and the summary is reported rather than a traceback.
        try:
            with graceful(log, lambda: "SUMMARY-INT"):
                raise KeyboardInterrupt
        except SystemExit as e:
            assert e.code == 130, e.code
        else:
            raise AssertionError("KeyboardInterrupt must exit 130")

        # Crashes still propagate (fail fast) after reporting.
        try:
            with graceful(log, lambda: "SUMMARY-ERR"):
                raise ValueError("boom")
        except ValueError:
            pass
        else:
            raise AssertionError("unhandled errors must re-raise")

        with graceful(log, lambda: "SUMMARY-OK"):
            pass

        text = Path(str(out) + ".log").read_text(encoding="utf-8")
        assert "SIGINT" in text and "boom" in text and "run complete" in text, text
    print("OK scrape_utils selfcheck")

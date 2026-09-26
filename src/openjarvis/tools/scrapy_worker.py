"""Isolated Scrapy subprocess entry point for :mod:`scrapy_crawl`."""

from __future__ import annotations

from openjarvis.tools.scrapy_crawl import _worker_main

if __name__ == "__main__":
    raise SystemExit(_worker_main())

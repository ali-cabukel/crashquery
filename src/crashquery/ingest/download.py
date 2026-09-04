"""Fetch STATS19 CSVs to data/raw/.

Downloads are cached and resumable-ish (a partial file is deleted rather than
left to poison the load). Not every year exists for every table name — DfT
renamed `accident` to `collision` in the 2022 release — so a 404 is logged and
skipped rather than fatal.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from crashquery.ingest.sources import TABLES, candidate_csv_urls, csv_filename
from crashquery.settings import get_settings

log = logging.getLogger(__name__)

USER_AGENT = "crashquery/0.1 (portfolio project)"


def download_one(table: str, year: int, dest_dir: Path | None = None) -> Path | None:
    dest_dir = dest_dir if dest_dir is not None else get_settings().raw_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / csv_filename(table, year)

    if dest.exists() and dest.stat().st_size > 0:
        log.info("cached  %s", dest.name)
        return dest

    last_error: httpx.HTTPError | None = None
    for url in candidate_csv_urls(table, year):
        log.info("fetching %s", url)
        try:
            with httpx.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=120.0,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                if response.status_code == 404:
                    log.warning("not published: %s", url.rsplit("/", 1)[-1])
                    continue
                response.raise_for_status()

                with dest.open("wb") as fh:
                    for block in response.iter_bytes(chunk_size=1 << 20):
                        fh.write(block)
        except httpx.HTTPError as exc:
            dest.unlink(missing_ok=True)
            last_error = exc
            log.error("failed %s — %s", dest.name, exc)
            continue

        log.info("saved   %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest

    if last_error:
        dest.unlink(missing_ok=True)
    return None


def download_years(years: list[int], dest_dir: Path | None = None) -> list[Path]:
    dest_dir = dest_dir if dest_dir is not None else get_settings().raw_dir
    paths: list[Path] = []
    for year in years:
        for table in TABLES:
            path = download_one(table, year, dest_dir)
            if path:
                paths.append(path)
    return paths

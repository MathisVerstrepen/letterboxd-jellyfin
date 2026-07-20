from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import bs4
from bs4 import BeautifulSoup

from src.logger import get_logger
from src.proxies import ProxyManager, make_request
from src.results import LetterboxdDetailResult, WatchlistEntry, WatchlistResult

URL = "https://letterboxd.com/"
logger = get_logger("letterboxd")


def make_letterboxd_request(
    endpoint: str, proxy_manager: ProxyManager, retries: int = 3
):
    """Make a retried request to a Letterboxd endpoint."""
    url = URL + endpoint
    for attempt in range(retries):
        proxy = proxy_manager.get_proxy()
        try:
            return make_request(url, proxy, allow_fallback=proxy_manager.allow_fallback)
        except Exception:
            logger.warning(
                "Letterboxd request failed; retrying",
                extra={"event": "letterboxd_request_retry", "attempt": attempt + 1},
            )
    logger.error(
        "Letterboxd request exhausted retries",
        extra={"event": "letterboxd_scrape_failed", "stage": "letterboxd"},
    )
    return None


def extract_tmdb_id_from_endpoint(
    endpoint: str, proxy_manager: ProxyManager
) -> LetterboxdDetailResult:
    """Resolve a Letterboxd endpoint to a movie, series, or retry result."""
    movie_page = make_letterboxd_request(endpoint, proxy_manager)
    if movie_page is None:
        return LetterboxdDetailResult(outcome="retry")
    try:
        movie_soup = BeautifulSoup(movie_page.content, "html.parser")
        tmdb_link_tag = movie_soup.find("a", attrs={"data-track-action": "TMDB"})
        if not isinstance(tmdb_link_tag, bs4.element.Tag):
            raise ValueError
        href = tmdb_link_tag.attrs.get("href")
        if not isinstance(href, str) or not href:
            raise ValueError
        if "/tv/" in href:
            media_type = "series"
        elif "/movie/" in href:
            media_type = "movie"
        else:
            raise ValueError
        parts = href.rstrip("/").split("/")
        tmdb_id = parts[-1]
        if not tmdb_id:
            raise ValueError
        return LetterboxdDetailResult(
            outcome="resolved", media_type=media_type, tmdb_id=tmdb_id
        )
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "Letterboxd film detail could not be resolved",
            extra={"event": "letterboxd_detail_failed", "stage": "letterboxd"},
        )
        return LetterboxdDetailResult(outcome="retry")


def _canonical_endpoint(frame: object) -> str | None:
    if not isinstance(frame, bs4.element.Tag):
        return None
    target = frame.attrs.get("data-target-link")
    if not isinstance(target, str):
        return None
    endpoint = target.lstrip("/")
    return endpoint or None


def get_new_watchlist_entries(
    username: str,
    proxy_manager: ProxyManager,
    max_workers: int,
    cursor: dict | None,
    *,
    include_series: bool = False,
    include_movies: bool = True,
) -> WatchlistResult:
    """Scrape ordered watchlist entries with explicit completeness semantics."""
    logger.info(
        "Starting incremental Letterboxd watchlist scrape",
        extra={"event": "letterboxd_scrape_started"},
    )
    watchlist_page = make_letterboxd_request(f"{username}/watchlist/", proxy_manager)
    if watchlist_page is None:
        logger.error(
            "Initial Letterboxd watchlist page could not be fetched",
            extra={"event": "letterboxd_scrape_failed", "stage": "letterboxd"},
        )
        return WatchlistResult(
            entries=[], outcome="failed", scan_complete=False, failed_items=1
        )

    cursor_kind = cursor.get("kind") if cursor else None
    cursor_value = cursor.get("value") if cursor else None
    entries: list[WatchlistEntry] = []
    failed_items = 0
    skipped_items = 0
    cursor_uri = None
    boundary_uri = None
    scan_complete = True
    stopped = False
    soup = BeautifulSoup(watchlist_page.content, "html.parser")

    while not stopped:
        frames = soup.find_all("div", {"data-component-class": "LazyPoster"})
        endpoints = [_canonical_endpoint(frame) for frame in frames]
        traversal_blocked = False
        page_endpoints = []
        for endpoint in endpoints:
            if endpoint is None:
                failed_items += 1
                scan_complete = False
                traversal_blocked = True
                break
            page_endpoints.append(endpoint)
            if cursor_kind == "letterboxd" and endpoint == cursor_value:
                break
        detail_endpoints = []
        for endpoint in page_endpoints:
            if cursor_uri is None:
                cursor_uri = endpoint
            if cursor_kind == "letterboxd" and endpoint == cursor_value:
                boundary_uri = endpoint
                stopped = True
                break
            detail_endpoints.append(endpoint)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    copy_context().run,
                    extract_tmdb_id_from_endpoint,
                    endpoint,
                    proxy_manager,
                )
                for endpoint in detail_endpoints
            ]
            for endpoint, future in zip(detail_endpoints, futures, strict=True):
                try:
                    detail = future.result()
                except Exception:
                    logger.error(
                        "Unexpected Letterboxd detail failure",
                        extra={"event": "letterboxd_detail_failed", "stage": "letterboxd"},
                        exc_info=True,
                    )
                    detail = LetterboxdDetailResult(outcome="retry")
                if detail.outcome == "retry":
                    failed_items += 1
                    entries.append(WatchlistEntry(endpoint=endpoint, detail=detail))
                    if cursor_kind == "legacy_tmdb":
                        scan_complete = False
                        stopped = True
                        break
                elif detail.media_type == "series" and not include_series:
                    skipped_items += 1
                elif detail.media_type == "movie" and not include_movies:
                    skipped_items += 1
                elif (
                    cursor_kind == "legacy_tmdb"
                    and detail.media_type == "movie"
                    and detail.tmdb_id == cursor_value
                ):
                    boundary_uri = endpoint
                    stopped = True
                    break
                else:
                    entries.append(WatchlistEntry(endpoint=endpoint, detail=detail))

        if stopped:
            break
        if traversal_blocked:
            break
        next_link = soup.find("a", {"class": "next"})
        if next_link is None:
            break
        if not isinstance(next_link, bs4.element.Tag):
            failed_items += 1
            scan_complete = False
            break
        next_href = next_link.attrs.get("href")
        if not isinstance(next_href, str) or not next_href:
            failed_items += 1
            scan_complete = False
            break
        next_page = make_letterboxd_request(next_href, proxy_manager)
        if next_page is None:
            failed_items += 1
            scan_complete = False
            break
        soup = BeautifulSoup(next_page.content, "html.parser")

    outcome = "success" if failed_items == 0 else "partial"
    return WatchlistResult(
        entries=entries,
        outcome=outcome,
        scan_complete=scan_complete,
        cursor_uri=cursor_uri,
        boundary_uri=boundary_uri,
        failed_items=failed_items,
        skipped_items=skipped_items,
    )

from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
import bs4
import logging
from dataclasses import dataclass

from src.proxies import ProxyManager, make_request
from src.results import WatchlistResult

URL = "https://letterboxd.com/"
logger = logging.getLogger("letterboxd-sync")


@dataclass(frozen=True)
class _DetailResult:
    tmdb_id: str | None = None
    failed_items: int = 0


def make_letterboxd_request(
    endpoint: str, proxy_manager: ProxyManager, retries: int = 3
):
    """Make a request to the Letterboxd API

    Args:
        endpoint (str): The Letterboxd API endpoint
        proxy_manager (ProxyManager): The proxy manager instance
        retries (int): The number of times to retry the request if it fails

    Returns:
        requests.Response: The response from the API
    """
    url = URL + endpoint

    for attempt in range(retries):
        proxy = proxy_manager.get_proxy()
        try:
            # Pass the selected proxy to the generic make_request function with fallback setting from proxy manager
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
    return None  # Return None on persistent failure


def extract_tmdb_id_from_endpoint(
    endpoint: str, proxy_manager: ProxyManager
) -> _DetailResult:
    """From a Letterboxd film endpoint, extract the TMDB ID."""
    movie_page = make_letterboxd_request(endpoint, proxy_manager)
    if movie_page is None:
        return _DetailResult(failed_items=1)

    movie_soup = BeautifulSoup(movie_page.content, "html.parser")
    tmdb_link_tag = movie_soup.find("a", attrs={"data-track-action": "TMDB"})
    if (
        not isinstance(tmdb_link_tag, bs4.element.Tag)
        or "href" not in tmdb_link_tag.attrs
    ):
        logger.warning(
            "Letterboxd film detail has no TMDB link",
            extra={"event": "letterboxd_detail_failed", "stage": "letterboxd"},
        )
        return _DetailResult(failed_items=1)
    try:
        if "/tv/" in tmdb_link_tag["href"]:
            logger.info(
                "Skipping identified TV item",
                extra={"event": "letterboxd_tv_skipped"},
            )
            return _DetailResult()
        tmdb_id = str(tmdb_link_tag["href"]).split("/")[-2]
        if not tmdb_id:
            raise IndexError
        return _DetailResult(tmdb_id=tmdb_id)
    except (IndexError, TypeError):
        logger.warning(
            "Letterboxd TMDB identifier could not be extracted",
            extra={"event": "letterboxd_detail_failed", "stage": "letterboxd"},
        )
        return _DetailResult(failed_items=1)


def get_new_watchlist_tmdb_ids(
    username: str,
    proxy_manager: ProxyManager,
    max_workers: int,
    latest_synced_tmdb_id: str | None,
) -> WatchlistResult:
    """
    Get TMDB IDs of new films in a user's watchlist since the last sync, using parallel workers.
    Stops when it encounters `latest_synced_tmdb_id`.

    Args:
        username (str): The Letterboxd username.
        proxy_manager (ProxyManager): The proxy manager instance.
        max_workers (int): The number of parallel requests for scraping.
        latest_synced_tmdb_id (str | None): The TMDB ID of the last movie synced.

    Returns:
        list: A list of new TMDB IDs, with the most recently added film first.
    """
    page_idx = 1
    logger.info(
        "Starting incremental Letterboxd watchlist scrape",
        extra={"event": "letterboxd_scrape_started", "letterboxd_username": username},
    )
    if latest_synced_tmdb_id:
        logger.info(
            "Incremental scrape will stop at the saved item",
            extra={"event": "letterboxd_scrape_incremental", "letterboxd_username": username},
        )

    watchlist_page = make_letterboxd_request(f"{username}/watchlist/", proxy_manager)
    if not watchlist_page:
        logger.error(
            "Initial Letterboxd watchlist page could not be fetched",
            extra={"event": "letterboxd_scrape_failed", "stage": "letterboxd", "letterboxd_username": username},
        )
        return WatchlistResult(tmdb_ids=[], failed_items=1)

    watchlist_soup: BeautifulSoup | None = BeautifulSoup(
        watchlist_page.content, "html.parser"
    )
    new_tmdb_ids = []
    failed_items = 0
    sync_stopped = False

    while watchlist_soup is not None and not sync_stopped:
        film_frames = watchlist_soup.find_all(
            "div", {"data-component-class": "LazyPoster"}
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all movie detail scrapes on the current page to the thread pool
            futures = [
                executor.submit(
                    extract_tmdb_id_from_endpoint,
                    str(frame["data-target-link"][1:]),
                    proxy_manager,
                )
                for frame in film_frames
                if isinstance(frame, bs4.element.Tag)
                and "data-target-link" in frame.attrs
            ]
            failed_items += len(film_frames) - len(futures)

            # Process results in order to respect the watchlist sequence
            for future in futures:
                try:
                    detail_result = future.result()
                    failed_items += detail_result.failed_items
                    tmdb_id = detail_result.tmdb_id
                    if tmdb_id:
                        if tmdb_id == latest_synced_tmdb_id:
                            logger.info(
                                "Found saved item; stopping incremental scrape",
                                extra={"event": "letterboxd_scrape_boundary_found", "letterboxd_username": username},
                            )
                            sync_stopped = True
                            break  # Stop processing movies on this page
                        new_tmdb_ids.append(tmdb_id)
                except Exception:
                    failed_items += 1
                    logger.error(
                        "Unexpected Letterboxd detail failure",
                        extra={"event": "letterboxd_detail_failed", "stage": "letterboxd"},
                        exc_info=True,
                    )

        if sync_stopped:
            break  # Stop processing further pages

        next_page_link = watchlist_soup.find("a", {"class": "next"})
        if next_page_link is not None and isinstance(next_page_link, bs4.element.Tag):
            page_idx += 1
            logger.info(
                "Fetching another Letterboxd watchlist page",
                extra={"event": "letterboxd_page_started", "letterboxd_username": username, "count": page_idx},
            )
            watchlist_page = make_letterboxd_request(
                str(next_page_link["href"]), proxy_manager
            )
            if watchlist_page:
                watchlist_soup = BeautifulSoup(watchlist_page.content, "html.parser")
            else:
                failed_items += 1
                watchlist_soup = None
        else:
            watchlist_soup = None

    return WatchlistResult(tmdb_ids=new_tmdb_ids, failed_items=failed_items)

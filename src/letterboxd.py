from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
import bs4
import logging

from src.proxies import ProxyManager, make_request

URL = "https://letterboxd.com/"
logger = logging.getLogger("letterboxd-sync")


def make_letterboxd_request(
    endpoint: str, proxy_manager: ProxyManager, retries: int = 3
):
    """Make a request to the Letterboxd API

    Args:
        endpoint (str): The Letterboxd API endpoint
        retries (int): The number of times to retry the request if it fails

    Returns:
        requests.Response: The response from the API
    """
    url = URL + endpoint

    for attempt in range(retries):
        proxy = proxy_manager.get_proxy()
        try:
            # Pass the selected proxy to the generic make_request function
            return make_request(url, proxy)
        except Exception as e:
            logger.warning(
                f"Request to {url} failed (attempt {attempt + 1}/{retries}). Error: {e}"
            )

    logger.error(f"Failed to make request to {url} after {retries} retries.")
    return None  # Return None on persistent failure


def extract_tmdb_id_from_endpoint(
    endpoint: str, proxy_manager: ProxyManager
) -> str | None:
    """From a Letterboxd film endpoint, extract the TMDB ID."""
    movie_page = make_letterboxd_request(endpoint, proxy_manager)
    if movie_page is None:
        return None

    movie_soup = BeautifulSoup(movie_page.content, "html.parser")
    tmdb_link_tag = movie_soup.find("a", attrs={"data-track-action": "TMDB"})
    if (
        not isinstance(tmdb_link_tag, bs4.element.Tag)
        or "href" not in tmdb_link_tag.attrs
    ):
        logger.warning(f"Could not find TMDB link for movie at endpoint: {endpoint}")
        return None
    try:
        tmdb_id = str(tmdb_link_tag["href"]).split("/")[-2]
        return tmdb_id
    except IndexError:
        logger.warning(f"Could not parse TMDB ID from href: {tmdb_link_tag['href']}")
        return None


from typing import Optional


def get_new_watchlist_tmdb_ids(
    username: str,
    proxy_manager: ProxyManager,
    max_workers: int,
    latest_synced_tmdb_id: str | None,
) -> list[str]:
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
        f"[{username}] Starting incremental watchlist scrape with {max_workers} workers..."
    )
    if latest_synced_tmdb_id:
        logger.info(
            f"[{username}] Will stop when TMDB ID '{latest_synced_tmdb_id}' is found."
        )

    watchlist_page = make_letterboxd_request(f"{username}/watchlist/", proxy_manager)
    if not watchlist_page:
        logger.error(f"[{username}] Could not fetch initial watchlist page. Aborting.")
        return []

    watchlist_soup: BeautifulSoup | None = BeautifulSoup(
        watchlist_page.content, "html.parser"
    )
    new_tmdb_ids = []
    sync_stopped = False

    while watchlist_soup is not None and not sync_stopped:
        film_frames = watchlist_soup.find_all(
            "div", {"data-component-class": "LazyPoster"}
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all movie detail scrapes on the current page to the thread pool
            future_to_endpoint = {
                executor.submit(
                    extract_tmdb_id_from_endpoint,
                    str(frame["data-target-link"][1:]),
                    proxy_manager,
                ): frame
                for frame in film_frames
                if isinstance(frame, bs4.element.Tag)
                and "data-target-link" in frame.attrs
            }

            # Create a list of futures in the order they appear on the page
            ordered_futures = [
                future
                for future, frame in future_to_endpoint.items()
                if frame in film_frames
            ]

            # Process results in order to respect the watchlist sequence
            for future in ordered_futures:
                try:
                    tmdb_id = future.result()
                    if tmdb_id:
                        if tmdb_id == latest_synced_tmdb_id:
                            logger.info(
                                f"[{username}] Found last synced movie (TMDB ID: {tmdb_id}). Stopping scrape."
                            )
                            sync_stopped = True
                            break  # Stop processing movies on this page
                        new_tmdb_ids.append(tmdb_id)
                except Exception as exc:
                    logger.error(
                        f"An exception occurred while fetching a TMDB ID: {exc}"
                    )

        if sync_stopped:
            break  # Stop processing further pages

        next_page_link = watchlist_soup.find("a", {"class": "next"})
        if next_page_link is not None and isinstance(next_page_link, bs4.element.Tag):
            page_idx += 1
            logger.info(f"[{username}] Getting watchlist page {page_idx}")
            watchlist_page = make_letterboxd_request(
                str(next_page_link["href"]), proxy_manager
            )
            if watchlist_page:
                watchlist_soup = BeautifulSoup(watchlist_page.content, "html.parser")
            else:
                watchlist_soup = None
        else:
            watchlist_soup = None

    return new_tmdb_ids

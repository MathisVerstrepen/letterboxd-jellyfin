# pylint: disable=missing-module-docstring
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def get_watchlist_tmdb_ids(
    username: str, proxy_manager: ProxyManager, max_workers: int
) -> set:
    watchlist_soup: Optional[BeautifulSoup] = None
    """Get the TMDB IDs of all films in a user's watchlist

    Args:
        username (str): The Letterboxd username

    Returns:
        set: The TMDB IDs of the films in the watchlist
    """
    page_idx = 1
    logger.info(
        f"[{username}] Getting watchlist page {page_idx}, using up to {max_workers} parallel workers."
    )

    watchlist_page = make_letterboxd_request(f"{username}/watchlist/", proxy_manager)
    if not watchlist_page:
        logger.error(
            f"[{username}] Could not fetch initial watchlist page. Aborting scrape for this user."
        )
        return set()

    watchlist_soup = BeautifulSoup(watchlist_page.content, "html.parser")
    tmdb_ids = set()

    while watchlist_soup is not None:
        film_frames = watchlist_soup.find_all(
            "div", {"data-component-class": "LazyPoster"}
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for frame in film_frames:
                try:
                    if (
                        isinstance(frame, bs4.element.Tag)
                        and "data-target-link" in frame.attrs
                    ):
                        film_endpoint = str(frame["data-target-link"][1:])
                    else:
                        logger.warning(
                            "Found a non-Tag frame or frame without 'data-target-link', skipping."
                        )
                        continue
                except KeyError:
                    logger.warning(
                        "Found a film frame without a 'data-target-link', skipping."
                    )
                    continue

                future = executor.submit(
                    extract_tmdb_id_from_endpoint, film_endpoint, proxy_manager
                )
                futures.append(future)

            for future in as_completed(futures):
                tmdb_id = future.result()
                if tmdb_id:
                    tmdb_ids.add(tmdb_id)

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
                logger.warning(
                    f"[{username}] Failed to fetch page {page_idx}, stopping pagination."
                )
                watchlist_soup = None
        else:
            watchlist_soup = None

    return tmdb_ids

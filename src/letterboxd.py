# pylint: disable=missing-module-docstring
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

from src.proxies import make_request

URL = "https://letterboxd.com/"


def make_letterboxd_request(endpoint: str, retries: int = 3):
    """Make a request to the Letterboxd API

    Args:
        endpoint (str): The Letterboxd API endpoint
        retries (int): The number of times to retry the request if it fails

    Returns:
        requests.Response: The response from the API
    """
    url = URL + endpoint

    for _ in range(retries):
        try:
            response = make_request(url)
            return response
        except Exception as e:
            print("Fail to make request to " + url)
            print("Error: " + str(e))


def extract_tmdb_id_from_endpoint(endpoint: str) -> str:
    """From a Letterboxd film endpoint, extract the TMDB ID

    Args:
        endpoint (str): The Letterboxd film endpoint

    Returns:
        str: The TMDB ID
    """
    movie_page = make_letterboxd_request(endpoint)
    movie_soup = BeautifulSoup(movie_page.content, "html.parser")

    tmdb_id = movie_soup.find("a", attrs={"data-track-action": "TMDb"})["href"].split(
        "/"
    )[-2]

    if tmdb_id is None:
        print("Error: No TMDB ID found for film at " + endpoint)

    return tmdb_id


def get_watchlist_tmdb_ids(username: str) -> set:
    """Get the TMDB IDs of all films in a user's watchlist

    Args:
        username (str): The Letterboxd username

    Returns:
        set: The TMDB IDs of the films in the watchlist
    """
    page_idx = 1
    print("Getting watchlist page " + str(page_idx))

    # Get the watchlist page
    watchlist_page = make_letterboxd_request(username + "/watchlist/")
    watchlist_soup = BeautifulSoup(watchlist_page.content, "html.parser")

    # Find all the film links
    film_frames = watchlist_soup.find_all("div", {"class": "poster"})

    # Get the TMDB ID from each link
    tmdb_ids = set()

    while watchlist_soup is not None:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for frame in film_frames:
                film_endpoint = frame["data-target-link"][1:]

                future = executor.submit(extract_tmdb_id_from_endpoint, film_endpoint)
                futures.append(future)

            for future in as_completed(futures):
                tmdb_id = future.result()
                tmdb_ids.add(tmdb_id)

        # Get the next page
        next_page_link = watchlist_soup.find("a", {"class": "next"})
        if next_page_link is not None:
            page_idx += 1
            print("Getting watchlist page " + str(page_idx))

            watchlist_page = make_letterboxd_request(next_page_link["href"])
            watchlist_soup = BeautifulSoup(watchlist_page.content, "html.parser")
            film_frames = watchlist_soup.find_all("div", {"class": "poster"})
        else:
            watchlist_soup = None

    return tmdb_ids

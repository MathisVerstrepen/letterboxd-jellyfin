# pylint: disable=missing-module-docstring
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

from src.proxies import make_request

URL = "https://letterboxd.com/"

def make_letterboxd_request(endpoint: str, retries: int = 3):
    """ Make a request to the Letterboxd API
    
    Args:
        endpoint (str): The Letterboxd API endpoint
        retries (int): The number of times to retry the request if it fails
        
    Returns:
        requests.Response: The response from the API
    """
    url = URL + endpoint
    print("Making request to " + url)

    for _ in range(retries):
        return make_request(url)
    
def extract_tmdb_id_from_endpoint(endpoint: str) -> str:
    """ From a Letterboxd film endpoint, extract the TMDB ID

    Args:
        endpoint (str): The Letterboxd film endpoint

    Returns:
        str: The TMDB ID
    """
    movie_page = make_letterboxd_request(endpoint)
    movie_soup = BeautifulSoup(movie_page.content, 'html.parser')
    
    tmdb_id = movie_soup.find('a', attrs={'data-track-action': 'TMDb'})['href'].split('/')[-2]
    
    if tmdb_id is None:
        print("Error: No TMDB ID found for film at " + endpoint)
        
    return tmdb_id
        
def get_watchlist_tmdb_ids(username: str) -> set:
    """ Get the TMDB IDs of all films in a user's watchlist
    
    Args:
        username (str): The Letterboxd username
        
    Returns:
        set: The TMDB IDs of the films in the watchlist
    """
    
    # Get the watchlist page
    watchlist_page = make_letterboxd_request(username + "/watchlist/")
    watchlist_soup = BeautifulSoup(watchlist_page.content, 'html.parser')
    
    # Find all the film links
    film_frames = watchlist_soup.find_all('div', {'class': 'poster'})

    # Get the TMDB ID from each link
    tmdb_ids = set()
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        for frame in film_frames:
            film_endpoint = frame["data-target-link"][1:]
            
            future = executor.submit(extract_tmdb_id_from_endpoint, film_endpoint)
            futures.append(future)

        for future in as_completed(futures):
            tmdb_id = future.result()
            tmdb_ids.add(tmdb_id)

    return tmdb_ids
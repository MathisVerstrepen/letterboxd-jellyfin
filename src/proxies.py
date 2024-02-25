# pylint: disable=missing-module-docstring
import requests

from src.exceptions import RequestException

PROXIES = [
    {
        "http": "socks5://192.168.2.51:1080",
        "https": "socks5://192.168.2.51:1080",
    },
    {
        "http": "socks5://192.168.2.51:1081",
        "https": "socks5://192.168.2.51:1081",
    },
    {
        "http": "socks5://192.168.2.51:1082",
        "https": "socks5://192.168.2.51:1082",
    },
]

i = 0

def make_request(url: str):
    """
    Make a request to the Letterboxd API
    """
    global i # pylint: disable=global-statement
    
    proxy = PROXIES[i%3]
    i += 1

    response = requests.get(url, timeout=20, proxies=proxy)
    if response.status_code == 200:
        return response
    
    raise RequestException("Unable to make request to " + url)
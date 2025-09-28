import logging
import random
import time
import threading
import requests
from typing import Any
import socket
from urllib.parse import urlparse

from src.exceptions import RequestException

logger = logging.getLogger("letterboxd-sync")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/118.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]


def get_browser_headers() -> dict[str, str]:
    """Returns a set of common browser headers with some randomization."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": random.choice(
            [
                "en-US,en;q=0.9",
                "en-GB,en;q=0.9",
                "en-US,en;q=0.8,fr;q=0.6",
                "en-US,en;q=0.9,es;q=0.8",
            ]
        ),
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


class ProxyManager:
    """
    Manages loading, formatting, and rotating proxies from configuration.
    """

    def __init__(self, config: dict[str, Any]):
        self.proxies: list[dict[str, str]] = []
        self.current_index = 0
        self.lock = threading.Lock()
        self.validate_on_startup = config.get("validate_proxies_on_startup", True)
        self.allow_fallback = config.get("allow_direct_fallback", True)
        self._load_proxies(config)

    def _load_proxies(self, config: dict[str, Any]):
        """
        Loads proxies from a file or a list based on the provided config.
        Prioritizes the proxy_file if it exists.
        """
        proxy_file = config.get("proxy_file")
        if proxy_file:
            proxy_type = config.get("proxy_type", "socks5")
            logger.info(f"Loading proxies from file: {proxy_file}")
            self._load_from_file(proxy_file, proxy_type)
        elif config.get("proxies"):
            logger.info("Loading proxies from config list.")
            self._load_from_list(config["proxies"])

        if self.proxies:
            logger.info(f"Successfully loaded {len(self.proxies)} proxies.")
            # Test proxy connectivity and filter out unreachable ones if enabled
            if self.validate_on_startup:
                self._validate_proxies()
            else:
                logger.info("Proxy validation is disabled. Using all loaded proxies.")
        else:
            logger.info("No proxy configuration found. Requests will be made directly.")

    def _load_from_file(self, file_path: str, proxy_type: str):
        """Loads proxies from a text file (IP:PORT:USER:PASS)."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    parts = line.split(":")
                    if len(parts) != 4:
                        logger.warning(f"Skipping malformed proxy line: {line}")
                        continue

                    ip, port, user, password = parts
                    proxy_url = f"{proxy_type}://{user}:{password}@{ip}:{port}"
                    self.proxies.append({"http": proxy_url, "https": proxy_url})
        except FileNotFoundError:
            logger.error(
                f"Proxy file not found at '{file_path}'. Please check the path in your config.yaml."
            )
        except Exception as e:
            logger.error(f"Failed to read or parse proxy file: {e}")

    def _load_from_list(self, proxy_list: list[str]):
        """Loads proxies from a list of full proxy URLs."""
        for proxy_url in proxy_list:
            self.proxies.append({"http": proxy_url, "https": proxy_url})

    def _test_proxy_connectivity(self, proxy_dict: dict[str, str]) -> bool:
        """
        Test if a proxy is reachable by making a simple connection test.
        Returns True if the proxy is working, False otherwise.
        """
        try:
            # Extract proxy details from the proxy URL
            proxy_url = proxy_dict.get("https", proxy_dict.get("http"))
            if not proxy_url:
                return False
                
            # Parse the proxy URL to get host and port
            parsed = urlparse(proxy_url)
            host = parsed.hostname
            port = parsed.port
            
            if not host or not port:
                return False
            
            # Test basic connectivity with a short timeout
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)  # 5 seconds timeout
            result = sock.connect_ex((host, port))
            sock.close()
            
            return result == 0
        except Exception as e:
            logger.debug(f"Proxy connectivity test failed for {proxy_dict}: {e}")
            return False

    def _validate_proxies(self):
        """
        Test all loaded proxies and remove unreachable ones.
        """
        if not self.proxies:
            return
            
        logger.info("Testing proxy connectivity...")
        working_proxies = []
        
        for proxy in self.proxies:
            if self._test_proxy_connectivity(proxy):
                working_proxies.append(proxy)
            else:
                proxy_url = proxy.get("https", proxy.get("http", "unknown"))
                logger.warning(f"Proxy {proxy_url} is not reachable, removing from list")
        
        original_count = len(self.proxies)
        self.proxies = working_proxies
        working_count = len(self.proxies)
        
        if working_count == 0:
            logger.error("No working proxies found! Requests will be made without proxies.")
        elif working_count < original_count:
            logger.warning(f"Only {working_count}/{original_count} proxies are working.")
        else:
            logger.info(f"All {working_count} proxies are working.")

    def get_proxy(self) -> dict[str, str] | None:
        """
        Returns the next proxy in the list in a thread-safe, round-robin fashion.
        Returns None if no proxies are loaded.
        """
        if not self.proxies:
            return None
        
        with self.lock:
            proxy = self.proxies[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.proxies)
            return proxy


# Global session for connection reuse and cookie persistence
_session = None


def get_session() -> requests.Session:
    """
    Returns a persistent session with browser-like configuration.
    """
    global _session
    if _session is None:
        _session = requests.Session()
        # Set some default headers that will be used for all requests
        _session.headers.update(
            {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    return _session


def make_request(
    url: str, proxy: dict | None = None, delay_range: tuple[float, float] = (0.5, 2.0), allow_fallback: bool = True
) -> requests.Response:
    """
    Makes a GET request with anti-detection measures, optionally using a provided proxy.

    Args:
        url: The URL to request
        proxy: Optional proxy configuration
        delay_range: Tuple of (min_delay, max_delay) in seconds for random delays
        allow_fallback: If True, fallback to direct connection if proxy fails
    """
    # Add random delay to avoid appearing too automated
    delay = random.uniform(delay_range[0], delay_range[1])
    time.sleep(delay)

    session = get_session()

    # Get fresh browser headers for each request
    headers = get_browser_headers()

    # First try with proxy if provided
    if proxy:
        try:
            response = session.get(
                url, timeout=20, proxies=proxy, headers=headers, allow_redirects=True
            )
            response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
            return response
        except requests.exceptions.RequestException as e:
            proxy_url = proxy.get("https", proxy.get("http", "unknown"))
            logger.warning(f"Request via proxy {proxy_url} failed: {e}")
            
            # If fallback is allowed, try direct connection
            if allow_fallback:
                logger.info(f"Attempting direct connection to {url}")
                try:
                    response = session.get(
                        url, timeout=20, proxies=None, headers=headers, allow_redirects=True
                    )
                    response.raise_for_status()
                    logger.info(f"Direct connection to {url} successful")
                    return response
                except requests.exceptions.RequestException as fallback_e:
                    logger.error(f"Direct connection also failed: {fallback_e}")
                    raise RequestException(f"Unable to make request to {url} via proxy or direct connection: {e}") from e
            else:
                raise RequestException(f"Unable to make request to {url}: {e}") from e
    else:
        # No proxy provided, make direct request
        try:
            response = session.get(
                url, timeout=20, proxies=None, headers=headers, allow_redirects=True
            )
            response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
            return response
        except requests.exceptions.RequestException as e:
            raise RequestException(f"Unable to make request to {url}: {e}") from e

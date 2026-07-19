import json
import urllib.request

from src.config import load_config


def main() -> int:
    try:
        config = load_config()
        observability = config.get("observability", {})
        host = observability.get("host", "127.0.0.1")
        port = observability.get("port", 8000)
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host in ("::", "[::]"):
            host = "::1"
        url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        request = urllib.request.Request(
            f"http://{url_host}:{port}/health", method="GET"
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                return 1
            payload = json.loads(response.read())
        return 0 if isinstance(payload, dict) and payload.get("live") is True else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

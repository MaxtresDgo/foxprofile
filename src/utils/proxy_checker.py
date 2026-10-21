import requests

from ..core.strings import get_string


def check_proxy_sync(proxy_str: str, timeout: int = 10) -> tuple[bool, str]:
    """Fetch the exit IP through the proxy. requests (with PySocks) speaks HTTP and
    SOCKS proxies alike, and resolves the target through the proxy as the browser does."""
    from ..services.proxy.geo_check import requests_proxy_url  # imports this package

    url = requests_proxy_url(proxy_str)
    if not url:
        return False, "Invalid proxy format"
    try:
        response = requests.get(
            "https://httpbin.org/ip", proxies={"http": url, "https": url}, timeout=timeout
        )
    except requests.exceptions.Timeout:
        return False, "Proxy connection timed out"
    except requests.exceptions.ProxyError:
        return False, "Failed to connect to proxy"
    except requests.exceptions.RequestException as e:
        return False, f"Proxy error: {e!s}"
    if response.status_code != 200:
        return False, f"Proxy returned status {response.status_code}"
    try:
        ip = response.json().get("origin", "unknown")
    except ValueError:
        ip = "unknown"
    return True, get_string("proxy_check_success", ip=ip)

"""Read-only readiness check; no credentials and no platform requests."""
import os
import socket
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    for port in (18761, 5900, 6080):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    origin = os.environ.get("PROMOTION_PANEL_ORIGIN", "http://127.0.0.1:18762")
    request = Request("http://127.0.0.1:18762/", headers={"Host": urlsplit(origin).netloc})
    try:
        with urlopen(request, timeout=2):
            raise SystemExit("authorization gateway unexpectedly allowed an anonymous request")
    except HTTPError as error:
        if error.code != 401:
            raise SystemExit("authorization gateway is not ready") from None


if __name__ == "__main__":
    main()

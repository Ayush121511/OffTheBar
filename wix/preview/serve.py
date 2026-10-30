"""
Serve wix/ for the local preview with browser caching off, so a reload always
runs the current page code and embeds. Under `python3 -m http.server` the
browser kept reusing an old offthebar-page.js against a newer backend, which
showed "HALFTIME!!! Generating" as a heading in the answer.

    cd wix && python3 preview/serve.py        # then open http://localhost:8000/preview/
    cd wix && python3 preview/serve.py 8001   # another port
"""
import contextlib
import http.server
import socket
import sys


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class DualStackServer(http.server.ThreadingHTTPServer):
    """Listens on IPv6 and IPv4, like `python3 -m http.server` (browsers try ::1 for localhost first)."""

    def server_bind(self):
        with contextlib.suppress(Exception):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        return super().server_bind()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    http.server.test(HandlerClass=NoCacheHandler, ServerClass=DualStackServer, port=port, bind="::")

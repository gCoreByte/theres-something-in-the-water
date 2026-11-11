import atexit
import queue
import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .spectrum import CHANNELS

SERVICE_TYPE = "_tsitw-upload._tcp"
UPLOAD_PATH = "/upload"
MODE_NAME = re.compile(r"^\w+$")


def reading_line(params):
    def value(key):
        values = params.get(key)
        return values[0] if values else None

    fields = [value(key) for key in ("frame", "timestamp", "combination", "t_nir", "t_vis", "t_uv")]
    channels = [value(f"{c}val") for c in CHANNELS]
    if None in fields or None in channels or not MODE_NAME.match(fields[2]):
        return None
    frame, timestamp, mode, t_nir, t_vis, t_uv = fields
    return (
        f"Frame {frame} Time {timestamp} [{mode}] Temps (NIR:{t_nir}, VIS:{t_vis}, UV:{t_uv})°C: {','.join(channels)}"
    )


def advertise(port):
    name = f"tsitw upload on {socket.gethostname().split('.')[0]}"
    if shutil.which("dns-sd"):
        command = ["dns-sd", "-R", name, SERVICE_TYPE, "local", str(port), f"path={UPLOAD_PATH}"]
    elif shutil.which("avahi-publish-service"):
        command = ["avahi-publish-service", name, SERVICE_TYPE, str(port), f"path={UPLOAD_PATH}"]
    else:
        return None
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class UploadServer:

    def __init__(self, port):
        self.description = f"WiFi, upload server on port {port}"
        self.port = port
        self._lines = queue.Queue()
        self._clients = set()
        self._http = HTTPServer(("", port), self._handler())
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self.advertiser = advertise(port)
        atexit.register(self.close)

    def lines(self):
        while True:
            yield self._lines.get()

    def receive(self, client, line):
        if client not in self._clients:
            self._clients.add(client)
            self._lines.put(f"Connected to WiFi with IP Address: {client}")
        self._lines.put(line)

    def close(self):
        if self.advertiser and self.advertiser.poll() is None:
            self.advertiser.terminate()
        self._http.shutdown()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):

            def do_GET(self):
                url = urlparse(self.path)
                if url.path != UPLOAD_PATH:
                    self.send_error(404)
                    return
                line = reading_line(parse_qs(url.query))
                if line is None:
                    self.send_error(400, "Missing reading fields")
                    return
                server.receive(self.client_address[0], line)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, format, *args):
                pass

        return Handler

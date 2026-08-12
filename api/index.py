"""Vercel HTTP entrypoint for Salee's public surface.

The always-on Docker worker remains the durable poller. This function gives
Stripe and customers a public HTTPS endpoint for landing, intake, and webhooks.
"""

from http.server import BaseHTTPRequestHandler

from config import load_settings
from store import build_store
from worker import RevenueWorker
from main import Handler as RuntimeHandler


class handler(BaseHTTPRequestHandler):
    _settings = load_settings(".")
    worker = RevenueWorker(_settings, build_store(_settings))
    settings = worker.settings

    def _delegate(self):
        RuntimeHandler.worker = self.worker
        RuntimeHandler.settings = self.settings
        self.__class__ = RuntimeHandler

    def do_GET(self):
        self._delegate()
        RuntimeHandler.do_GET(self)

    def do_POST(self):
        self._delegate()
        RuntimeHandler.do_POST(self)

    def log_message(self, fmt, *args):
        return

"""Part 4 student implementation."""

import json

import logging

import os

from concurrent.futures import ThreadPoolExecutor

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from tempfile import TemporaryDirectory

from app.integration import process_request

from app.webex_client import WEBEX_API_BASE, WebexClient

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000


class WebexEventHandler:
    def __init__(self, client, bot_person_id, processor=None):
        self.client = client
        self.bot_person_id = bot_person_id
        self.processor = processor if processor is not None else process_request

    def handle_event(self, event):
        """
        Handle one Webex webhook event.
        Return the response dictionary that was sent to the room,
        or None when the event is ignored.
        """
        # only "a message was created" events with a message id
        if not isinstance(event, dict):
            return None
        if event.get("resource") != "messages" or event.get("event") != "created":
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        message_id = data.get("id")
        if not isinstance(message_id, str) or not message_id:
            return None

        # ignore our own messages before spending an API call
        if data.get("personId") == self.bot_person_id:
            return None

        # the event only carries ids: fetch the real message
        message = self.client.get_message(message_id)

        # check the author again to prevent reply loops
        if message.get("personId") == self.bot_person_id:
            return None

        # always answer in the room the request came from
        room_id = message.get("roomId") or data.get("roomId")
        if not room_id:
            logger.warning("Message %s has no roomId; cannot reply", message_id)
            return None

        mentioned_people = message.get("mentionedPeople")
        if not isinstance(mentioned_people, list):
            mentioned_people = []

        files = message.get("files")
        if not isinstance(files, list):
            files = []

        if len(files) > 1:
            # stop before downloading anything or touching a router
            response = {"status": "error", "result": "multiple_attachments"}
        elif not files:
            response = self.processor(mentioned_people, None, self.bot_person_id)
        else:
            content = self.client.download_file(files[0])
            # the temporary directory (and the file) is removed even if
            # processing raises
            with TemporaryDirectory() as tmp_dir:
                attachment_path = Path(tmp_dir) / "request.yaml"
                attachment_path.write_bytes(content)
                response = self.processor(
                    mentioned_people, attachment_path, self.bot_person_id
                )

        self.client.send_message(room_id, json.dumps(response))
        return response


def make_http_handler(submit_event):
    """
    Build the HTTP request handler class.
    submit_event(event) must return quickly; the real work happens later.
    """

    class WebhookRequestHandler(BaseHTTPRequestHandler):
        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            return self.path.split("?", 1)[0]

        def do_GET(self):
            if self._route() == "/health":
                self._send_json(200, {"status": "ok", "result": "healthy"})
            else:
                self._send_json(404, {"status": "error", "result": "not_found"})

        def do_POST(self):
            if self._route() != "/webhook":
                self._send_json(404, {"status": "error", "result": "not_found"})
                return

            invalid = {"status": "error", "result": "invalid_event"}

            # body length must be 1 .. 1,000,000 bytes
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length)
            except (TypeError, ValueError):
                self._send_json(400, invalid)
                return
            if length < 1 or length > MAX_BODY_BYTES:
                self._send_json(400, invalid)
                return

            body = self.rfile.read(length)
            if len(body) != length:
                self._send_json(400, invalid)
                return

            # body must be a JSON object
            try:
                event = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, invalid)
                return
            if not isinstance(event, dict):
                self._send_json(400, invalid)
                return

            # acknowledge now; the real work runs in the background
            try:
                submit_event(event)
            except Exception:
                logger.exception("Could not queue the event")
                self._send_json(500, {"status": "error", "result": "internal_error"})
                return
            self._send_json(200, {"status": "ok", "result": "accepted"})

        def log_message(self, format, *args):
            logger.info("%s - %s", self.address_string(), format % args)

    return WebhookRequestHandler


class _EventRunner:
    """
    Runs events in the background. The Webex client and the bot's person id
    are prepared on first use, so the server (and /health) can start even
    before Webex is reachable.
    """

    def __init__(self):
        self._handler = None

    def _get_handler(self):
        if self._handler is None:
            client = WebexClient()
            response = client.session.get(
                f"{WEBEX_API_BASE}/people/me",
                headers=client.headers,
                timeout=10,
            )
            response.raise_for_status()
            bot_person_id = response.json()["id"]
            self._handler = WebexEventHandler(client, bot_person_id)
        return self._handler

    def __call__(self, event):
        try:
            self._get_handler().handle_event(event)
        except Exception:
            logger.exception("Failed to handle Webex event")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBHOOK_PORT", "8000"))

    # one worker => router requests are processed one at a time
    executor = ThreadPoolExecutor(max_workers=1)
    runner = _EventRunner()

    def submit_event(event):
        executor.submit(runner, event)

    server = ThreadingHTTPServer((host, port), make_http_handler(submit_event))
    logger.info("Webhook server listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
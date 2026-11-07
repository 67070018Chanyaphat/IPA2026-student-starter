"""Part 4 student implementation."""

import logging

import yaml

from app.core import validate_request
from app.dispatcher import dispatch_request

from app.request_handler import validate_webex_envelope

logger = logging.getLogger(__name__)

ACCEPTED = {"status": "ok", "result": "accepted"}


def process_request(mentioned_people, attachment_path, bot_person_id):
    """
    Full pipeline for one Webex request:
    envelope check -> load YAML once -> validate -> dispatch to a backend.
    Return the response dictionary.
    """
    # 1) real mention of this bot + a YAML attachment
    error = validate_webex_envelope(
        mentioned_people, attachment_path, bot_person_id
    )
    if error is not None:
        return error

    # 2) load the YAML exactly once
    try:
        with open(attachment_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, UnicodeDecodeError):
        return {"status": "error", "result": "invalid_yaml"}

    # 3) Part 1 validation; stop here for anything that is not accepted
    validation = validate_request(data)
    if validation != ACCEPTED:
        return validation

    # 4) dispatch only accepted requests; never let a backend crash the bot
    try:
        return dispatch_request(data)
    except Exception:
        logger.exception("Backend raised an unexpected exception")
        return {"status": "error", "result": "backend_failed"}
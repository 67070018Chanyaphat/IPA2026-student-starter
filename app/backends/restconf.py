import json as _json
import os
import socket
from ipaddress import IPv4Interface
from urllib.parse import quote

import requests
import urllib3

from app.backends import planning

# ---------------------------------------------------------------- helpers

AUTHENTICATION_FAILED = "authentication_failed"
CONNECTION_FAILED = "connection_failed"
BACKEND_FAILED = "backend_failed"

# Exception class names used by the real libraries (matched on the whole
# class hierarchy, so ncclient / paramiko / netmiko need not be imported).
_AUTH_NAMES = {
    "AuthenticationError",  # ncclient
    "AuthenticationException",  # paramiko
    "BadAuthenticationType",  # paramiko
    "NetmikoAuthenticationException",  # netmiko
}
_CONNECTION_NAMES = {
    "NetmikoTimeoutException",  # netmiko
    "NoValidConnectionsError",  # paramiko
    "SSHException",  # paramiko
    "SSHError",  # ncclient
    "SessionCloseError",  # ncclient
    "TransportError",  # ncclient
}


def error_response(code):
    return {"status": "error", "result": code}


def not_found_response():
    return {"status": "ok", "result": "not_found", "interface": None}


def classify_exception(exc):
    """Map an exception to one of the three backend error codes."""
    if isinstance(exc, PermissionError):
        return AUTHENTICATION_FAILED

    if isinstance(
        exc,
        (ConnectionError, TimeoutError, socket.timeout, socket.gaierror),
    ):
        return CONNECTION_FAILED

    # requests.exceptions.ConnectionError is NOT the built-in ConnectionError
    if isinstance(
        exc,
        (requests.exceptions.Timeout, requests.exceptions.ConnectionError),
    ):
        return CONNECTION_FAILED

    for cls in type(exc).__mro__:
        if cls.__name__ in _AUTH_NAMES:
            return AUTHENTICATION_FAILED
        if cls.__name__ in _CONNECTION_NAMES:
            return CONNECTION_FAILED

    return BACKEND_FAILED


def router_credentials():
    """Router login from the environment (never from source code)."""
    user = os.environ.get("ROUTER_USER")
    password = os.environ.get("ROUTER_PASS")
    if not user or not password:
        raise PermissionError("ROUTER_USER / ROUTER_PASS are not set")
    return user, password

REQUEST_TIMEOUT = 10  # seconds
_HEADERS = {
    "Accept": "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

# The lab routers use self-signed certificates.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _http_get(url):
    """Real transport: HTTPS GET with the router credentials."""
    return requests.get(
        url,
        auth=router_credentials(),
        headers=_HEADERS,
        verify=False,
        timeout=REQUEST_TIMEOUT,
    )


def _http_put(url, json=None):
    """Real transport: HTTPS PUT with the router credentials."""
    return requests.put(
        url,
        data=_json.dumps(json),
        auth=router_credentials(),
        headers=_HEADERS,
        verify=False,
        timeout=REQUEST_TIMEOUT,
    )


def _normalize_interface(raw, interface_name):
    """ietf-interfaces JSON -> normalized interface dictionary."""
    item = raw["ietf-interfaces:interface"]
    if isinstance(item, list):  # RFC 8040 may wrap a list entry in an array
        item = item[0]

    ipv4 = None
    ip_container = item.get("ietf-ip:ipv4") or item.get("ipv4") or {}
    addresses = ip_container.get("address") or []
    if addresses:
        first = addresses[0]
        if "netmask" in first:
            ipv4 = str(IPv4Interface(f"{first['ip']}/{first['netmask']}"))
        else:
            ipv4 = str(IPv4Interface(f"{first['ip']}/{first['prefix-length']}"))

    return {
        "name": item.get("name", interface_name),
        "ipv4": ipv4,
        "description": item.get("description"),
        "admin_state": "up" if item.get("enabled", True) else "down",
    }


def get_interface_state(
    router,
    interface_name,
    http_get=None,
):
    """Return normalized interface status using RESTCONF."""
    if http_get is None:
        http_get = _http_get

    url = (
        f"https://{router}/restconf/data/"
        f"ietf-interfaces:interfaces/interface={quote(interface_name, safe='')}"
    )

    try:
        response = http_get(url)
        status_code = response.status_code

        if status_code == 404:
            return not_found_response()
        if status_code in (401, 403):
            return error_response(AUTHENTICATION_FAILED)
        if status_code != 200:
            return error_response(BACKEND_FAILED)

        interface = _normalize_interface(response.json(), interface_name)
    except Exception as exc:
        return error_response(classify_exception(exc))

    return {"status": "ok", "result": "found", "interface": interface}


def plan_interface(
    router,
    desired_interface,
    http_get=None,
):
    """Return create/update/no_change plan using RESTCONF status."""
    return planning.plan_interface(
        router,
        desired_interface,
        lambda r, name: get_interface_state(r, name, http_get),
    )


def apply_interface(
    router,
    desired_interface,
    http_put=None,
):
    """Apply complete desired interface state using RESTCONF."""
    if http_put is None:
        http_put = _http_put

    try:
        name = desired_interface["name"]
        if not name.startswith("Loopback"):
            raise ValueError(f"unsupported interface name: {name}")
        number = int(name[len("Loopback"):])

        ipv4 = IPv4Interface(desired_interface["ipv4"])

        payload = {
            "Cisco-IOS-XE-native:Loopback": {
                "name": number,
                "description": desired_interface["description"],
                "ip": {
                    "address": {
                        "primary": {
                            "address": str(ipv4.ip),
                            "mask": str(ipv4.network.netmask),
                        }
                    }
                },
            }
        }
        if desired_interface["admin_state"] == "down":
            payload["Cisco-IOS-XE-native:Loopback"]["shutdown"] = [None]

        url = (
            f"https://{router}/restconf/data/"
            f"Cisco-IOS-XE-native:native/interface/Loopback={number}"
        )

        # PUT replaces the whole managed Loopback resource
        response = http_put(url, json=payload)
        status_code = response.status_code

        if status_code in (200, 201, 204):
            return {"status": "ok", "result": "applied"}
        if status_code in (401, 403):
            return error_response(AUTHENTICATION_FAILED)
        return error_response(BACKEND_FAILED)
    except Exception as exc:
        return error_response(classify_exception(exc))
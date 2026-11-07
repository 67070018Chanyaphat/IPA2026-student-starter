import os
import re
import socket
from ipaddress import IPv4Interface

import requests


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

CONNECT_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------- parsing

def _canonical(name):
    """Loopback123 and Lo123 compare equal (IOS abbreviates the name)."""
    return re.sub(r"^loopback", "lo", str(name).strip().lower())


def _field(record, candidates):
    """Look a value up by any of several key names (case-insensitive)."""
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in candidates:
        if key in lowered:
            return lowered[key]
    return None


def _first(value):
    """TextFSM list values: take the first element."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


_IP_NAME = ("intf", "interface", "name", "port")
_IP_ADDRESS = ("ip_address", "ipaddr", "ip", "address")
_IP_PREFIX = ("prefix_length", "prefixlen", "prefix", "mask")
_IP_LINK = ("link_status", "admin_state", "status")
_DESC_NAME = ("port", "interface", "intf", "name")
_DESC_TEXT = ("description", "descrip", "desc")


def _normalize(interface_name, ip_info, descriptions):
    """Parsed CLI records -> normalized interface dict, or None if absent."""
    if not isinstance(ip_info, list) or not isinstance(descriptions, list):
        raise ValueError("TextFSM output is not a list of records")

    wanted = _canonical(interface_name)

    record = None
    for item in ip_info:
        if _canonical(_field(item, _IP_NAME) or "") == wanted:
            record = item
            break
    if record is None:
        return None

    ipv4 = None
    address = _first(_field(record, _IP_ADDRESS))
    prefix = _first(_field(record, _IP_PREFIX))
    if address:
        ipv4 = str(IPv4Interface(f"{address}/{prefix}"))

    link = str(_field(record, _IP_LINK) or "").lower()
    admin_state = "down" if "administratively down" in link else "up"

    description = None
    for item in descriptions:
        if _canonical(_field(item, _DESC_NAME) or "") == wanted:
            text = _field(item, _DESC_TEXT)
            description = text.strip() if text and text.strip() else None
            break

    return {
        "name": interface_name,
        "ipv4": ipv4,
        "description": description,
        "admin_state": admin_state,
    }


# -------------------------------------------------------------- transport

def _connect(router):
    user, password = router_credentials()

    from netmiko import ConnectHandler

    return ConnectHandler(
        device_type="cisco_ios",
        host=router,
        username=user,
        password=password,
        conn_timeout=CONNECT_TIMEOUT,
        auth_timeout=CONNECT_TIMEOUT,
        banner_timeout=CONNECT_TIMEOUT,
    )


def _read_status(connection, interface_name):
    """Run the two read-only commands on an open connection."""
    ip_info = connection.send_command(
        f"show ip interface {interface_name}",
        use_textfsm=True,
    )
    if isinstance(ip_info, str):
        # IOS answers "% Invalid input ..." for an interface that is absent;
        # any other text that TextFSM cannot parse is a real failure.
        if "%" in ip_info or "invalid" in ip_info.lower():
            return [], []
        raise ValueError("TextFSM could not parse 'show ip interface'")

    if not ip_info:
        return [], []

    descriptions = connection.send_command(
        "show interfaces description",
        use_textfsm=True,
    )
    if isinstance(descriptions, str):
        raise ValueError("TextFSM could not parse 'show interfaces description'")

    return ip_info, descriptions


def _netmiko_get(router, interface_name):
    """Real transport: returns (ip_info, descriptions)."""
    with _connect(router) as connection:
        return _read_status(connection, interface_name)


def _netmiko_delete(router, interface_name):
    """Real transport: returns "deleted" or "not_found"."""
    with _connect(router) as connection:
        ip_info, descriptions = _read_status(connection, interface_name)
        if _normalize(interface_name, ip_info, descriptions) is None:
            return "not_found"

        connection.send_config_set([f"no interface {interface_name}"])

        ip_info, descriptions = _read_status(connection, interface_name)
        if _normalize(interface_name, ip_info, descriptions) is not None:
            raise RuntimeError("interface is still present after delete")
        return "deleted"


# ------------------------------------------------------------- public API

def get_interface_state(
    router,
    interface_name,
    netmiko_get=None,
):
    """Return normalized interface status using Netmiko + TextFSM."""
    if netmiko_get is None:
        netmiko_get = _netmiko_get

    try:
        ip_info, descriptions = netmiko_get(router, interface_name)
        interface = _normalize(interface_name, ip_info, descriptions)
    except Exception as exc:
        return error_response(classify_exception(exc))

    if interface is None:
        return not_found_response()
    return {"status": "ok", "result": "found", "interface": interface}


def delete_interface(
    router,
    interface_name,
    netmiko_delete=None,
):
    """Delete an interface using Netmiko."""
    if netmiko_delete is None:
        netmiko_delete = _netmiko_delete

    try:
        outcome = netmiko_delete(router, interface_name)
    except Exception as exc:
        return error_response(classify_exception(exc))

    if outcome == "deleted":
        return {"status": "ok", "result": "deleted"}
    if outcome == "not_found":
        return not_found_response()
    return error_response("backend_failed")
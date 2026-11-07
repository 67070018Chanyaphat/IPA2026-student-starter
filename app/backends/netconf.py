import os
import socket
import xml.etree.ElementTree as ET
from ipaddress import IPv4Interface
from xml.sax.saxutils import escape

import requests

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

NETCONF_PORT = 830
TIMEOUT = 10  # seconds


def _netconf_get_config(router, filter_xml):
    """Real transport: NETCONF get-config (running) with a subtree filter."""
    user, password = router_credentials()

    from ncclient import manager

    with manager.connect(
        host=router,
        port=NETCONF_PORT,
        username=user,
        password=password,
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
        timeout=TIMEOUT,
    ) as session:
        reply = session.get_config(
            source="running",
            filter=("subtree", filter_xml),
        )
        return reply.data_xml


def _netconf_edit_config(router, config_xml):
    """Real transport: NETCONF edit-config on the running datastore."""
    user, password = router_credentials()

    from ncclient import manager

    with manager.connect(
        host=router,
        port=NETCONF_PORT,
        username=user,
        password=password,
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
        timeout=TIMEOUT,
    ) as session:
        session.edit_config(target="running", config=config_xml)


def _local(tag):
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element, name):
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element):
    if element is None or element.text is None:
        return None
    return element.text.strip()


def _normalize_interface(xml_text, interface_name):
    """ietf-interfaces XML -> normalized interface dict, or None if absent."""
    root = ET.fromstring(xml_text.strip())

    for element in root.iter():
        if _local(element.tag) != "interface":
            continue
        if _text(_child(element, "name")) != interface_name:
            continue

        ipv4 = None
        ip_container = _child(element, "ipv4")
        if ip_container is not None:
            address = _child(ip_container, "address")
            if address is not None:
                ip = _text(_child(address, "ip"))
                netmask = _text(_child(address, "netmask"))
                prefix = _text(_child(address, "prefix-length"))
                if netmask:
                    ipv4 = str(IPv4Interface(f"{ip}/{netmask}"))
                else:
                    ipv4 = str(IPv4Interface(f"{ip}/{prefix}"))

        enabled = _text(_child(element, "enabled"))
        description = _text(_child(element, "description"))

        return {
            "name": interface_name,
            "ipv4": ipv4,
            "description": description if description else None,
            "admin_state": "down" if enabled == "false" else "up",
        }

    return None


def get_interface_state(
    router,
    interface_name,
    netconf_get_config=None,
):
    """Return normalized interface status using NETCONF."""
    if netconf_get_config is None:
        netconf_get_config = _netconf_get_config

    filter_xml = (
        '<interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">'
        "<interface>"
        f"<name>{escape(interface_name)}</name>"
        "</interface>"
        "</interfaces>"
    )

    try:
        xml_text = netconf_get_config(router, filter_xml)
        interface = _normalize_interface(xml_text, interface_name)
    except Exception as exc:
        return error_response(classify_exception(exc))

    if interface is None:
        return not_found_response()
    return {"status": "ok", "result": "found", "interface": interface}


def plan_interface(
    router,
    desired_interface,
    netconf_get_config=None,
):
    """Return create/update/no_change plan using NETCONF status."""
    return planning.plan_interface(
        router,
        desired_interface,
        lambda r, name: get_interface_state(r, name, netconf_get_config),
    )


def _build_config_xml(desired_interface):
    name = desired_interface["name"]
    if not name.startswith("Loopback"):
        raise ValueError(f"unsupported interface name: {name}")
    number = int(name[len("Loopback"):])

    ipv4 = IPv4Interface(desired_interface["ipv4"])
    shutdown = "<shutdown/>" if desired_interface["admin_state"] == "down" else ""

    # operation="replace" swaps the whole Loopback entry, so no stale
    # secondary addresses are left behind
    return (
        '<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" '
        'xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0">'
        '<native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">'
        "<interface>"
        '<Loopback nc:operation="replace">'
        f"<name>{number}</name>"
        f"<description>{escape(desired_interface['description'])}</description>"
        "<ip><address><primary>"
        f"<address>{ipv4.ip}</address>"
        f"<mask>{ipv4.network.netmask}</mask>"
        "</primary></address></ip>"
        f"{shutdown}"
        "</Loopback>"
        "</interface>"
        "</native>"
        "</config>"
    )


def apply_interface(
    router,
    desired_interface,
    netconf_edit_config=None,
):
    """Apply complete desired interface state using NETCONF."""
    if netconf_edit_config is None:
        netconf_edit_config = _netconf_edit_config

    try:
        config_xml = _build_config_xml(desired_interface)
        netconf_edit_config(router, config_xml)
    except Exception as exc:
        return error_response(classify_exception(exc))

    return {"status": "ok", "result": "applied"}
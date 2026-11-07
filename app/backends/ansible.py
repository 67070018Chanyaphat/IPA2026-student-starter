import json
import socket
import subprocess
from ipaddress import IPv4Interface
from pathlib import Path




def error_response(code):
    return {"status": "error", "result": code}


def classify_exception(exc):
    """Map an exception to one of the three backend error codes."""
    if isinstance(exc, PermissionError):
        return "authentication_failed"
    if isinstance(
        exc,
        (ConnectionError, TimeoutError, socket.timeout, socket.gaierror),
    ):
        return "connection_failed"
    return "backend_failed"


def _run_ansible_playbook(variables):
    """Run the Ansible apply playbook."""

    project_root = Path(__file__).resolve().parents[2]

    playbook_path = (
        project_root / "ansible" / "apply_interface.yml"
    )

    command = [
        "ansible-playbook",
        str(playbook_path),
        "-i",
        f"{variables['router']},",
        "--extra-vars",
        json.dumps(variables),
    ]

    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectionError("ansible-playbook timed out") from exc
    except subprocess.CalledProcessError as exc:
        # turn the playbook failure into the three backend error meanings
        output = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
        if (
            "authentication failed" in output
            or "permission denied" in output
            or "incorrect username or password" in output
        ):
            raise PermissionError("router rejected the credentials") from exc
        if (
            exc.returncode == 4
            or "unreachable" in output
            or "connection refused" in output
            or "timed out" in output
        ):
            raise ConnectionError("router is unreachable") from exc
        raise RuntimeError("playbook failed") from exc


def apply_interface(
    router,
    desired_interface,
    run_playbook=None,
):
    """Apply complete desired interface state using Ansible."""

    if run_playbook is None:
        run_playbook = _run_ansible_playbook

    try:
        ipv4 = IPv4Interface(desired_interface["ipv4"])

        variables = {
            "router": router,
            "interface_name": desired_interface["name"],
            "description": desired_interface["description"],
            "ipv4_address": str(ipv4.ip),
            "netmask": str(ipv4.network.netmask),
            "shutdown": desired_interface["admin_state"] == "down",
        }

        run_playbook(variables)

        return {
            "status": "ok",
            "result": "applied",
        }

    except Exception as exc:
        return error_response(classify_exception(exc))
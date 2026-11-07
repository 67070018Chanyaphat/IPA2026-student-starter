
import json
import subprocess
from pathlib import Path


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

    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )


def apply_interface(
    router,
    desired_interface,
    run_playbook=None,
):
    """Apply complete desired interface state using Ansible."""

    if run_playbook is None:
        run_playbook = _run_ansible_playbook

    try:
        variables = {
            "router": router,
            "interface_name": desired_interface["name"],
            "description": desired_interface["description"],
            "ipv4_address": desired_interface["ipv4"],
            "shutdown": desired_interface["admin_state"] == "down",
        }

        run_playbook(variables)

        return {
            "status": "ok",
            "result": "applied",
        }

    except PermissionError:
        return {
            "status": "error",
            "result": "authentication_failed",
        }

    except ConnectionError:
        return {
            "status": "error",
            "result": "connection_failed",
        }

    except Exception:
        return {
            "status": "error",
            "result": "backend_failed",
        }

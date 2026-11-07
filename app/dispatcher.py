"""Part 4 student implementation."""

from app.backends import ansible, netconf, netmiko_textfsm, restconf

# method -> (backend module, {action: function name in that module})
# The function is looked up by name at call time so each backend can be
# replaced (for example by a test double) without changing this table.
BACKENDS = {
    "restconf": (
        restconf,
        {
            "status": "get_interface_state",
            "plan": "plan_interface",
            "apply": "apply_interface",
        },
    ),
    "netconf": (
        netconf,
        {
            "status": "get_interface_state",
            "plan": "plan_interface",
            "apply": "apply_interface",
        },
    ),
    "ansible": (
        ansible,
        {"apply": "apply_interface"},
    ),
    "netmiko-textfsm": (
        netmiko_textfsm,
        {
            "status": "get_interface_state",
            "delete": "delete_interface",
        },
    ),
}


def dispatch_request(data):
    """
    Send an already validated request to the backend that matches its
    method/action and return the backend response unchanged.
    """
    method = data["method"]
    action = data["action"]
    router = data["router"]
    interface = data["desired"]["interface"]

    if method not in BACKENDS:
        return {"status": "error", "result": "invalid_method"}

    module, actions = BACKENDS[method]
    if action not in actions:
        return {"status": "error", "result": "invalid_action"}

    function = getattr(module, actions[action])

    if action in ("status", "delete"):
        # these only need the interface name
        return function(router, interface["name"])

    # plan / apply need the complete desired interface
    return function(router, interface)
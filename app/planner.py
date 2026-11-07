MANAGED_FIELDS = ("ipv4", "description", "admin_state")


def build_plan(desired_interface, current_interface):
    """
    Compare a desired interface with the normalized current interface and
    return a deterministic plan (create / update / no_change).

    desired_interface: complete interface dict from Part 1
        {"name", "ipv4", "description", "admin_state"}
    current_interface: normalized interface dict, or None if it does not exist

    This function never talks to a router and never modifies its inputs.
    """
    # create: the interface does not exist yet -> list every managed field
    if current_interface is None:
        changes = {
            field: {"to": desired_interface.get(field)}
            for field in MANAGED_FIELDS
        }
        return _planned("create", changes)

    # update / no_change: keep only the managed fields whose value differs.
    # "name" identifies the interface, so it is never part of "changes".
    changes = {}
    for field in MANAGED_FIELDS:
        current_value = current_interface.get(field)
        desired_value = desired_interface.get(field)
        if current_value != desired_value:
            changes[field] = {"from": current_value, "to": desired_value}

    if changes:
        return _planned("update", changes)
    return _planned("no_change", {})


def _planned(operation, changes):
    return {
        "status": "ok",
        "result": "planned",
        "operation": operation,
        "changes": changes,
    }
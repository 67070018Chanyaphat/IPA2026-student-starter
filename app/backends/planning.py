from app.planner import build_plan


def plan_interface(
    router,
    desired_interface,
    get_interface_state,
):
    """Build a Part 2 plan from backend status."""
    status = get_interface_state(router, desired_interface["name"])

    # a backend error is returned unchanged
    if status.get("status") == "error":
        return status

    result = status.get("result")
    if result == "found":
        current_interface = status["interface"]
    elif result == "not_found":
        current_interface = None
    else:
        return {"status": "error", "result": "backend_failed"}

    # all comparison logic stays in Part 2
    return build_plan(desired_interface, current_interface)
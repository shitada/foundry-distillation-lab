"""Bind the six frozen function schemas to one private RetailSession."""


def bind_tools(session, audit=None):
    from agent_framework import tool

    def call(name, arguments):
        if audit is None:
            return session.call(name, arguments)
        return audit.call_tool(name, arguments, lambda: session.call(name, arguments))

    def get_order_details(order_id: str):
        return call("get_order_details", {"order_id": order_id})

    def get_fulfillment_status(order_id: str):
        return call("get_fulfillment_status", {"order_id": order_id})

    def check_resolution_policy(order_id: str, item_id: str, reason: str):
        return call("check_resolution_policy",
                            {"order_id": order_id, "item_id": item_id, "reason": reason})

    def check_inventory(sku: str):
        return call("check_inventory", {"sku": sku})

    def calculate_resolution(order_id: str, items: list[dict]):
        return call("calculate_resolution", {"order_id": order_id, "items": items})

    def submit_resolution(order_id: str, calculation_id: str, resolution_summary: str):
        return call("submit_resolution", {"order_id": order_id,
                            "calculation_id": calculation_id, "resolution_summary": resolution_summary})

    functions = {f.__name__: f for f in (get_order_details, get_fulfillment_status,
                 check_resolution_policy, check_inventory, calculate_resolution, submit_resolution)}
    return [tool(name=d["function"]["name"], description=d["function"]["description"],
                 schema=d["function"]["parameters"], approval_mode="never_require")(
                     functions[d["function"]["name"]]) for d in session.tools]

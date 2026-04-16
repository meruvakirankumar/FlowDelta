"""
FlowDelta – AI-powered flow tracing and delta-based test generation.

Drop-in integration for any Python application::

    from flowdelta import FlowDelta, observe, track

    # Option 1 – instance
    fd = FlowDelta()
    result = fd.observe(my_pipeline, *args, flow_id="my-pipeline")

    # Option 2 – decorator on the entry point
    @fd.track(flow_id="checkout", golden=True)
    def checkout(user_id: str, cart: dict) -> dict:
        ...

    # Option 3 – one-shot, no instance
    from flowdelta import observe
    observe(my_pipeline, *args)

    # Option 4 – standalone decorator
    from flowdelta import track

    @track(flow_id="checkout")
    def checkout(user_id: str, cart: dict) -> dict:
        ...
"""

from .sdk import FlowDelta, observe, track

__all__ = ["FlowDelta", "observe", "track"]

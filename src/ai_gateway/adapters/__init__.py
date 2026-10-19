"""Pure, request-local protocol mapping. See README.md for transport wiring."""
from .protocol import AdapterError, ProtocolAdapter

__all__ = ["AdapterError", "ProtocolAdapter"]

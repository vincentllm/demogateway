"""Non-buffering HTTP transport; see README.md for the adapter/lifecycle contract."""

from .streaming import StreamingTransport, TransportError, parse_sse

__all__ = ["StreamingTransport", "TransportError", "parse_sse"]

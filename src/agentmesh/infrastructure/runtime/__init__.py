"""Outward runtime adapters.

Framework and provider dependencies are allowed here; application services
depend only on the framework-neutral runtime ports.
"""

from .subprocess_adapter import SubprocessAgentRuntime, reference_subprocess_descriptor

__all__ = ["SubprocessAgentRuntime", "reference_subprocess_descriptor"]

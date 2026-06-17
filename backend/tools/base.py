"""
Tool base class and ToolResult schema.

Every tool in the registry must subclass Tool and implement execute().
ToolResult is the uniform return type from all tool invocations.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel


class ToolResult(BaseModel):
    """Uniform return type for every tool execution."""

    tool_name: str
    params_used: dict[str, Any]
    output: Optional[str] = None
    error: Optional[str] = None
    latency_ms: int = 0


class Tool(ABC):
    """Abstract base class for all registered tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical name that must match the config.yaml entry."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description surfaced to the LLM as part of the tool schema."""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict:
        """
        JSON Schema for this tool's parameters.

        Used to build the OpenAI-compatible function definition sent to Ollama.
        """
        ...

    @abstractmethod
    async def execute(self, params: dict[str, Any]) -> ToolResult:
        """Execute the tool with the given params. Always returns a ToolResult."""
        ...

    def to_openai_schema(self) -> dict:
        """Build the OpenAI-compatible function definition for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

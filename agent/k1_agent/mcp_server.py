"""
MCP server exposing the agent's tools (tools.ArmToolbox), so a coding agent (Claude Code, Codex, ...) can be
the agent, the way ASPIRE's System 2 drives its robot. The tools are the very ones the agent's own loop uses,
and the server's instructions are the shared operating rules (agent/AGENTS.md).

    claude mcp add k1 -- k1-agent mcp --allow-motion

The same interlocks as `k1-agent run` apply: without --allow-motion every motion tool refuses, and
--profile operate leaves out free joint motion.
"""
import inspect
import json

from k1_agent.prompts import operating_rules

try:                                    # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer as Server
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server, Image


def text(result: dict) -> str:
    return json.dumps({k: v for k, v in result.items() if k != "image"}, default=str)


def mcp_function(toolbox, name: str):
    """A plain function for the MCP SDK: the tool's signature (so the SDK builds the same schema), returning the
    result as JSON text plus the camera frame taken after the call."""
    method = getattr(toolbox, name)

    def call(**arguments):
        result, frame = toolbox.call(name, arguments)
        return [text(result)] + ([Image(data=frame, format="jpeg")] if frame else [])

    call.__name__ = name
    call.__doc__ = inspect.getdoc(method)
    call.__signature__ = inspect.signature(method).replace(return_annotation=list)
    return call


def build_server(toolbox) -> "Server":
    """Every tool available in this session (profile, operator, --allow-enable) becomes an MCP tool."""
    server = Server("k1-arm", instructions=operating_rules())
    for name in toolbox.available():
        server.add_tool(mcp_function(toolbox, name), name=name, description=inspect.getdoc(getattr(toolbox, name)))
    return server

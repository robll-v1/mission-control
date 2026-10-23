import asyncio
import importlib
import json


def test_mcp_entrypoint_registers_expected_tools(monkeypatch, tmp_path):
    """Import the real MCP entrypoint without touching the user's config/data."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    server_module = importlib.import_module("app.mcp_server")
    tools = asyncio.run(server_module.mcp.list_tools())

    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {
        "review_code",
        "get_review_status",
        "get_review_findings",
        "abort_review",
    }
    for name, tool in by_name.items():
        schema = tool.inputSchema
        assert schema["type"] == "object", name
        json.dumps(schema)
    assert "repo_path" in by_name["review_code"].inputSchema["required"]
    assert "task_id" in by_name["get_review_status"].inputSchema["required"]

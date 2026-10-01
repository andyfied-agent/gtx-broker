"""Tests for Hermes tools integration - vault_tools and repo_tools.

These tests verify that the actual Hermes tools can be imported, instantiated,
and called through their handler functions.
"""

import json
import tempfile
from pathlib import Path

from gtx_broker.tools.vault_tools import VaultTools, ToolResult as VaultToolResult
from gtx_broker.tools.repo_tools import RepositoryTools, ToolResult as RepoToolResult


def test_vault_tools_integration():
    """Test that VaultTools Hermes tools work end-to-end."""
    with tempfile.TemporaryDirectory() as tmpdir:
        vault_path = Path(tmpdir) / "vault"
        vault_path.mkdir()

        (vault_path / "test.md").write_text("Test content")
        (vault_path / "notes").mkdir()
        (vault_path / "notes" / "nested.md").write_text("Nested content")

        broker_dir = vault_path / "AI" / "GTX-Broker"
        broker_dir.mkdir(parents=True)

        tools = VaultTools(str(vault_path), "AI/GTX-Broker")

        definitions = tools.get_definitions()
        assert len(definitions) == 4
        tool_names = [definition.name for definition in definitions]
        assert "read_vault_note" in tool_names
        assert "search_vault" in tool_names
        assert "list_vault_directory" in tool_names
        assert "write_broker_note" in tool_names

        result = tools.read_note("test.md")
        assert isinstance(result, VaultToolResult)
        assert result.success, result.error
        assert result.content == "Test content"

        result = tools.search_vault("*.md")
        assert isinstance(result, VaultToolResult)
        assert result.success, result.error
        data = json.loads(result.content)
        assert len(data) >= 2

        result = tools.write_note("AI/GTX-Broker/test.md", "Wrote content")
        assert isinstance(result, VaultToolResult)
        assert result.success, result.error
        assert "Successfully wrote" in result.content

        result = tools.read_note("AI/GTX-Broker/test.md")
        assert result.success
        assert result.content == "Wrote content"

        result = tools.read_note("/etc/passwd")
        assert isinstance(result, VaultToolResult)
        assert not result.success
        assert "Absolute paths are not allowed" in result.error


def test_repo_tools_integration(tmp_path, monkeypatch):
    """Test that RepositoryTools Hermes tools can be instantiated."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "README.md").write_text("checkout README")
    monkeypatch.setenv("GTX_BROKER_SOURCE_ROOT", str(tmp_path))
    tools = RepositoryTools(github_token=None)

    definitions = tools.get_definitions()
    assert len(definitions) == 3
    tool_names = [definition.name for definition in definitions]
    assert "discover_repository_readmes" in tool_names
    assert "read_repository_readme" in tool_names
    assert "get_repository_summary" in tool_names

    result = tools.discover_readmes()
    assert isinstance(result, RepoToolResult)
    assert result.success, result.error
    data = json.loads(result.content)
    assert isinstance(data, list)

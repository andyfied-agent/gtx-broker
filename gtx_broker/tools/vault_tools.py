"""Hermes tools for vault and repository access.

Provides tool definitions for the GTX broker to access documentation
and Obsidian vault with proper security boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Callable
import json
import os


@dataclass
class ToolDefinition:
    """Definition of a Hermes tool."""
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable


@dataclass
class ToolResult:
    """Result from a tool execution."""
    success: bool
    content: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class VaultTools:
    """Hermes tools for Obsidian vault access."""

    def __init__(self, vault_path: str, write_dir: str = "AI/GTX-Broker"):
        """Initialize vault tools.

        Args:
            vault_path: Path to Obsidian vault
            write_dir: Broker write directory within vault
        """
        self.vault_path = vault_path
        self.write_dir = write_dir

    def get_definitions(self) -> List[ToolDefinition]:
        """Get tool definitions for vault access."""
        return [
            ToolDefinition(
                name="read_vault_note",
                description="Read a note from the Obsidian vault. "
                           "Read-only access to any note in the vault. "
                           "Path should be relative to vault root (e.g., 'AI/Broker/note.md').",
                parameters={
                    "type": "object",
                    "properties": {
                        "note_path": {
                            "type": "string",
                            "description": "Path to note relative to vault root",
                        },
                    },
                    "required": ["note_path"],
                },
                handler=self.read_note,
            ),
            ToolDefinition(
                name="search_vault",
                description="Search the Obsidian vault for notes matching a pattern. "
                           "Returns list of matching note paths. "
                           "Supports glob patterns (e.g., 'notes/*', '*.md').",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Glob pattern to search for",
                        },
                        "extension": {
                            "type": "string",
                            "description": "File extension filter (e.g., '.md')",
                            "default": ".md",
                        },
                    },
                    "required": ["query"],
                },
                handler=self.search_vault,
            ),
            ToolDefinition(
                name="list_vault_directory",
                description="List all files and directories in a vault directory. "
                           "Returns relative paths. Does not follow symlinks.",
                parameters={
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Directory path relative to vault root (default: '')",
                            "default": "",
                        },
                    },
                },
                handler=self.list_directory,
            ),
            ToolDefinition(
                name="write_broker_note",
                description="Write a note to the broker's dedicated directory in the vault. "
                           "This is the ONLY directory where notes can be written. "
                           "All other vault content is read-only.",
                parameters={
                    "type": "object",
                    "properties": {
                        "note_path": {
                            "type": "string",
                            "description": "Path within broker directory (e.g., 'AI/Broker/note.md')",
                        },
                        "content": {
                            "type": "string",
                            "description": "Note content to write",
                        },
                    },
                    "required": ["note_path", "content"],
                },
                handler=self.write_note,
            ),
        ]

    def read_note(self, note_path: str) -> ToolResult:
        """Read a vault note.

        Args:
            note_path: Path to note relative to vault root

        Returns:
            ToolResult with content or error
        """
        from gtx_broker.vault_readers import ObsidianVault, VaultOperationResult

        try:
            vault = ObsidianVault(self.vault_path, self.write_dir)
            result: VaultOperationResult = vault.read_note(note_path)

            if result.success:
                return ToolResult(
                    success=True,
                    content=result.content,
                    metadata={
                        "path": result.path,
                        "bytes_read": result.bytes_read,
                    },
                )
            else:
                return ToolResult(
                    success=False,
                    error=result.error,
                )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Read error: {e}",
            )

    def search_vault(self, query: str, extension: str = ".md") -> ToolResult:
        """Search vault for files.

        Args:
            query: Search pattern
            extension: File extension filter

        Returns:
            ToolResult with list of paths
        """
        from gtx_broker.vault_readers import ObsidianVault

        try:
            vault = ObsidianVault(self.vault_path, self.write_dir)
            results = vault.search_vault(query, extension)

            return ToolResult(
                success=True,
                content=json.dumps(results, indent=2),
                metadata={
                    "count": len(results),
                    "query": query,
                    "extension": extension,
                },
            )
        except PermissionError as e:
            return ToolResult(
                success=False,
                error=f"Permission denied searching vault: {e}",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Search error: {e}",
            )

    def list_directory(self, directory: str = "") -> ToolResult:
        """List directory contents.

        Args:
            directory: Directory path relative to vault root

        Returns:
            ToolResult with list of paths
        """
        from gtx_broker.vault_readers import ObsidianVault

        try:
            vault = ObsidianVault(self.vault_path, self.write_dir)
            entries = vault.list_directory(directory)

            return ToolResult(
                success=True,
                content=json.dumps(entries, indent=2),
                metadata={
                    "count": len(entries),
                    "directory": directory,
                },
            )
        except PermissionError as e:
            return ToolResult(
                success=False,
                error=f"Permission denied listing vault directory: {e}",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"List error: {e}",
            )

    def write_note(self, note_path: str, content: str) -> ToolResult:
        """Write note to broker directory.

        Args:
            note_path: Path within broker directory
            content: Content to write

        Returns:
            ToolResult indicating success or failure
        """
        from gtx_broker.vault_readers import ObsidianVault, VaultOperationResult

        try:
            vault = ObsidianVault(self.vault_path, self.write_dir)
            result: VaultOperationResult = vault.write_note(note_path, content)

            if result.success:
                return ToolResult(
                    success=True,
                    content=f"Successfully wrote {note_path}",
                    metadata={
                        "path": result.path,
                        "bytes_written": result.bytes_read,
                    },
                )
            else:
                return ToolResult(
                    success=False,
                    error=result.error,
                )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Write error: {e}",
            )

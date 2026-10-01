"""Hermes tools for repository README access.

Provides tools for the GTX broker to discover and read README files
from all accessible repositories with proper security boundaries.
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


class RepositoryTools:
    """Hermes tools for repository README access."""

    def __init__(self, github_token: Optional[str] = None, *, registry_path: Optional[str] = None):
        """Initialize repository tools.

        Args:
            registry_path: JSON registry of configured repositories
            github_token: GitHub token for remote access
        """
        self.registry_path = registry_path or os.getenv("GTX_BROKER_REPOSITORY_REGISTRY")
        self.github_token = github_token or os.getenv("GITHUB_TOKEN")

    def _registry(self):
        """Load the configured repository registry for this invocation."""
        from ..repo_readers import RepositoryRegistry

        if self.registry_path:
            return RepositoryRegistry.from_file(self.registry_path)
        source_root = os.getenv("GTX_BROKER_SOURCE_ROOT", "/home/andyfied/src")
        return RepositoryRegistry.discover(source_root, self.github_token)

    def get_definitions(self) -> List[ToolDefinition]:
        """Get tool definitions for repo access."""
        return [
            ToolDefinition(
                name="discover_repository_readmes",
                description="Discover README files in all accessible repositories. "
                           "Reads from local checkouts and optionally from GitHub API.",
                parameters={
                    "type": "object",
                    "properties": {},
                },
                handler=self.discover_readmes,
            ),
            ToolDefinition(
                name="read_repository_readme",
                description="Read README from a specific repository. "
                           "Available repos are listed in the registry.",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo_name": {
                            "type": "string",
                            "description": "Repository name from registry",
                        },
                        "readme_path": {
                            "type": "string",
                            "description": "Exact README* path returned by discovery (optional)",
                        },
                        "refresh": {
                            "type": "boolean",
                            "description": "Force re-read even if cached",
                            "default": False,
                        },
                    },
                    "required": ["repo_name"],
                },
                handler=self.read_readme,
            ),
            ToolDefinition(
                name="get_repository_summary",
                description="Get summary of all accessible repositories and their READMEs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "include_previews": {
                            "type": "boolean",
                            "description": "Include content previews",
                            "default": True,
                        },
                    },
                },
                handler=self.get_summary,
            ),
        ]

    def discover_readmes(self) -> ToolResult:
        """Discover all READMEs."""
        from ..repo_readers import RepositoryReadmeReader

        try:
            registry = self._registry()
            if registry.discovery_error:
                return ToolResult(success=False, error=registry.discovery_error)
            reader = RepositoryReadmeReader(registry, self.github_token)
            readmes = reader.discover_readmes()

            data = [
                {
                    "repo": r.repo_name,
                    "status": "ok" if not r.error else "error",
                    "path": r.repo_path,
                    "readme_file": r.readme_path,
                    "size": len(r.content) if r.content else 0,
                    "error": r.error,
                }
                for r in readmes
            ]
            errors = [item["error"] for item in data if item["error"]]

            return ToolResult(
                success=not errors,
                content=json.dumps(data, indent=2),
                error=("; ".join(errors) if errors else None),
                metadata={"count": len(data)},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Discovery error: {e}",
            )

    def read_readme(
        self,
        repo_name: str,
        readme_path: Optional[str] = None,
        refresh: bool = False,
    ) -> ToolResult:
        """Read a root or exact nested README from a specific repository."""
        from ..repo_readers import RepositoryReadmeReader

        try:
            # Preserve the original read_readme(repo_name, refresh) positional form.
            if isinstance(readme_path, bool):
                refresh = readme_path
                readme_path = None
            registry = self._registry()
            if registry.discovery_error:
                return ToolResult(success=False, error=registry.discovery_error)
            reader = RepositoryReadmeReader(registry, self.github_token)
            readme = reader.get_readme(
                repo_name, force_refresh=refresh, readme_path=readme_path
            )

            if readme is not None and not readme.error:
                return ToolResult(
                    success=True,
                    content=readme.content,
                    metadata={
                        "repo": readme.repo_name,
                        "path": readme.repo_path,
                        "readme_file": readme.readme_path,
                        "size": len(readme.content),
                    },
                )
            else:
                error_msg = readme.error if readme else "Repository not found"
                return ToolResult(
                    success=False,
                    error=error_msg,
                )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Read error: {e}",
            )

    def get_summary(self, include_previews: bool = True) -> ToolResult:
        """Get repository summary."""
        from ..repo_readers import RepositoryReadmeReader

        try:
            registry = self._registry()
            if registry.discovery_error:
                return ToolResult(success=False, error=registry.discovery_error)
            reader = RepositoryReadmeReader(registry, self.github_token)
            summary = reader.get_summary(include_previews=include_previews)
            summary_errors = [
                item["error"]
                for item in summary["readmes"]
                if item["status"] == "error"
            ]

            return ToolResult(
                success=not summary_errors,
                content=json.dumps(summary, indent=2),
                error=("; ".join(summary_errors) if summary_errors else None),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Summary error: {e}",
            )

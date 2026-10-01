"""Obsidian vault reader and scoped writer for GTX broker.

Implements read-only access to entire vault with scoped write access to a
dedicated broker directory. Enforces path boundaries at filesystem level
and prevents traversal attacks.

Security boundaries:
- Read: entire vault, but never modifies
- Write: only to broker-owned subdirectory
- Path canonicalization prevents traversal attacks
- Symlink rejection prevents escape via links
"""

from __future__ import annotations

import hashlib
import fnmatch
import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


class VaultSecurityError(ValueError):
    """Raised when a vault operation violates security boundaries."""
    pass


class VaultNotFoundError(FileNotFoundError):
    """Raised when a vault path does not exist."""
    pass


@dataclass(frozen=True)
class VaultOperationResult:
    """Result of a vault operation."""
    success: bool
    path: str
    content: Optional[str] = None
    error: Optional[str] = None
    bytes_read: int = 0
    is_cached: bool = False


@dataclass(frozen=True)
class VaultFileMetadata:
    """Metadata about a vault file."""
    path: str
    relative_path: str
    size_bytes: int
    modified_at: str  # ISO format
    is_markdown: bool
    content_hash: str


class ObsidianVaultReader:
    """Read-only access to Obsidian vault with security boundaries.

    Provides safe traversal and reading of vault files while preventing
    path traversal attacks and enforcing read-only semantics.
    """

    def __init__(self, vault_path: str, write_dir: str = "AI/GTX-Broker"):
        """Initialize vault reader.

        Args:
            vault_path: Absolute path to Obsidian vault root
            write_dir: Relative path within vault for broker writes (e.g., "AI/GTX-Broker")
        """
        # Resolve vault root first (before any filesystem writes)
        vault_root = Path(vault_path).resolve(strict=False)
        self.vault_root = vault_root
        self.write_dir = write_dir  # Keep as relative
        write_dir_path = Path(write_dir)
        if not write_dir.strip() or write_dir_path in {Path("."), Path("")}:
            raise ValueError("broker write directory must be non-empty")
        self.write_dir_parts = tuple(
            part for part in write_dir_path.parts if part not in {"", "."}
        )

        # Validate vault exists BEFORE any filesystem writes
        if not vault_root.exists():
            raise VaultNotFoundError(f"Vault does not exist: {vault_path}")

        # Validate write directory is within vault BEFORE creating it
        # Resolve the write directory path and verify containment FIRST
        write_dir_resolved = vault_root / write_dir
        # Resolve to get canonical path (follows symlinks)
        write_dir_resolved = write_dir_resolved.resolve()
        # Verify containment BEFORE any mkdir
        try:
            write_dir_resolved.relative_to(vault_root)
        except ValueError:
            raise ValueError(
                f"Write directory {write_dir} resolves outside vault: {write_dir_resolved}"
            )
        # Only now create the directory, without following symlinked components.
        directory_fd = self._open_directory_chain(self.write_dir_parts, create=True)
        os.close(directory_fd)
        self.write_dir_resolved = write_dir_resolved

        logger.info("Initialized ObsidianVaultReader for %s (write dir: %s)",
                   vault_path, self.write_dir)

    def _open_directory_chain(self, parts: tuple[str, ...], *, create: bool) -> int:
        """Open a vault-relative directory without following symlinks."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(self.vault_root), flags | nofollow)
        try:
            for part in parts:
                if part in {"", ".", ".."}:
                    raise VaultSecurityError(f"Unsafe vault path component: {part!r}")
                if create:
                    try:
                        os.mkdir(part, mode=0o770, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, flags | nofollow, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except Exception:
            os.close(fd)
            raise

    def _open_relative_file(self, path: str) -> tuple[int, tuple[str, ...]]:
        """Open a vault-relative regular file without following symlinks."""
        path_parts = Path(path).parts
        if (
            Path(path).is_absolute()
            or any(part in {"", ".", ".."} for part in path_parts)
            or not path_parts
        ):
            raise VaultSecurityError(f"Unsafe vault file path: {path}")
        parent_fd = self._open_directory_chain(path_parts[:-1], create=False)
        try:
            file_fd = os.open(
                path_parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        return file_fd, path_parts

    def _canonicalize_path(self, path: str) -> Path:
        """Canonicalize path and verify it's within vault.

        Args:
            path: Path string (can be relative or absolute)

        Returns:
            Absolute canonical path

        Raises:
            VaultSecurityError: If path escapes vault boundaries
        """
        # Handle absolute paths - must be within vault, reject all absolute paths
        p = Path(path)
        if p.is_absolute():
            raise VaultSecurityError(
                f"Absolute paths are not allowed: {path}"
            )

        # Handle relative paths - resolve from vault root
        candidate = (self.vault_root / p).resolve()

        # Verify candidate is within vault
        try:
            candidate.relative_to(self.vault_root)
            return candidate
        except ValueError:
            raise VaultSecurityError(
                f"Path {path} resolves outside vault: {candidate}"
            )

    def _validate_no_symlink_escape(self, path: Path) -> None:
        """Validate that path and all components are not symlinks escaping vault.

        Args:
            path: Path to validate

        Raises:
            VaultSecurityError: If path contains symlinks that escape vault
        """
        # Check the path itself
        if path.is_symlink():
            real_path = path.resolve()
            try:
                real_path.relative_to(self.vault_root)
            except ValueError:
                raise VaultSecurityError(
                    f"Symlink {path} resolves outside vault: {real_path}"
                )

        # Check all parent components
        for parent in path.parents:
            if parent.is_symlink():
                real_parent = parent.resolve()
                try:
                    real_parent.relative_to(self.vault_root)
                except ValueError:
                    raise VaultSecurityError(
                        f"Symlink parent {parent} resolves outside vault: {real_parent}"
                    )

    def _get_relative_path(self, path: Path) -> str:
        """Get relative path from vault root.

        Args:
            path: Absolute path

        Returns:
            Relative path string
        """
        return str(path.relative_to(self.vault_root))

    def read_file(self, path: str) -> VaultOperationResult:
        """Read file content from vault.

        Args:
            path: Path relative to vault root (e.g., "notes/example.md")

        Returns:
            VaultOperationResult with content or error
        """
        try:
            # Canonicalize and validate
            abs_path = self._canonicalize_path(path)

            # Check for symlink escape
            self._validate_no_symlink_escape(abs_path)

            file_fd, _path_parts = self._open_relative_file(path)
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                return VaultOperationResult(
                    success=False,
                    path=self._get_relative_path(abs_path),
                    error=f"Path is a directory: {path}"
                )

            # Check file size (limit to 1MB for safety)
            file_size = file_stat.st_size
            if file_size > 1024 * 1024:
                os.close(file_fd)
                return VaultOperationResult(
                    success=False,
                    path=self._get_relative_path(abs_path),
                    error=f"File too large: {file_size} bytes (max 1MB)"
                )

            try:
                content = os.read(file_fd, file_size + 1).decode("utf-8")
            finally:
                os.close(file_fd)

            return VaultOperationResult(
                success=True,
                path=self._get_relative_path(abs_path),
                content=content,
                bytes_read=file_size,
            )

        except VaultSecurityError as e:
            return VaultOperationResult(
                success=False,
                path=path,
                error=str(e)
            )
        except Exception as e:
            return VaultOperationResult(
                success=False,
                path=path,
                error=f"Read error: {e}"
            )

    def get_file_metadata(self, path: str) -> Optional[VaultFileMetadata]:
        """Get metadata about a vault file.

        Args:
            path: Path relative to vault root

        Returns:
            VaultFileMetadata if found, None otherwise
        """
        try:
            abs_path = self._canonicalize_path(path)

            file_fd, _path_parts = self._open_relative_file(path)
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                return None

            rel_path = self._get_relative_path(abs_path)

            # Check if markdown
            is_markdown = abs_path.suffix.lower() in {'.md', '.markdown', '.mdown'}

            # Calculate content hash
            digest = hashlib.sha256()
            try:
                while chunk := os.read(file_fd, 1024 * 1024):
                    digest.update(chunk)
            finally:
                os.close(file_fd)
            content_hash = digest.hexdigest()

            mtime = file_stat.st_mtime
            modified_at = datetime.fromtimestamp(mtime, timezone.utc).isoformat()

            return VaultFileMetadata(
                path=str(abs_path),
                relative_path=rel_path,
                size_bytes=file_stat.st_size,
                modified_at=modified_at,
                is_markdown=is_markdown,
                content_hash=content_hash,
            )

        except Exception:
            return None

    def list_directory(self, path: str = "") -> List[str]:
        """List contents of a directory in the vault.

        Args:
            path: Path relative to vault root (default: root)

        Returns:
            List of relative paths to files and directories
        """
        directory_fd = None
        try:
            path_parts = Path(path).parts
            if Path(path).is_absolute() or ".." in path_parts:
                return []
            directory_fd = self._open_directory_chain(path_parts, create=False)
            entries = []
            for name in os.listdir(directory_fd):
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                except PermissionError:
                    raise
                except OSError:
                    try:
                        file_fd = os.open(
                            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
                        )
                        os.close(file_fd)
                    except PermissionError:
                        raise
                    except OSError:
                        continue
                else:
                    os.close(child_fd)
                entries.append(str(Path(*path_parts, name)))
            return sorted(entries)
        except PermissionError as exc:
            raise PermissionError(f"Permission denied listing {path or '.'}: {exc}") from exc
        except Exception:
            return []
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def search_files(self, pattern: str, extension: Optional[str] = None) -> List[str]:
        """Search for files matching a pattern.

        Args:
            pattern: Glob pattern (e.g., "*.md", "notes/*")
            extension: Optional file extension filter

        Returns:
            List of matching relative paths
        """
        root_fd = None
        try:
            pattern_path = Path(pattern)
            if pattern_path.is_absolute() or ".." in pattern_path.parts:
                return []
            results = []
            root_fd = self._open_directory_chain((), create=False)

            def walk(directory_fd: int, prefix: tuple[str, ...]) -> None:
                for name in os.listdir(directory_fd):
                    try:
                        child_fd = os.open(
                            name,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=directory_fd,
                        )
                    except PermissionError:
                        raise
                    except OSError:
                        try:
                            file_fd = os.open(
                                name,
                                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=directory_fd,
                            )
                            file_stat = os.fstat(file_fd)
                            os.close(file_fd)
                        except PermissionError:
                            raise
                        except OSError:
                            continue
                        relative = str(Path(*prefix, name))
                        if stat.S_ISREG(file_stat.st_mode) and (
                            fnmatch.fnmatch(relative, pattern)
                            or fnmatch.fnmatch(name, pattern)
                        ) and (not extension or Path(name).suffix.lower() == extension):
                            results.append(relative)
                    else:
                        try:
                            walk(child_fd, (*prefix, name))
                        finally:
                            os.close(child_fd)

            walk(root_fd, ())
            return sorted(results)
        except PermissionError as exc:
            raise PermissionError(f"Permission denied searching {pattern}: {exc}") from exc
        except Exception:
            return []
        finally:
            if root_fd is not None:
                os.close(root_fd)

    def write_file(self, path: str, content: str, encoding: str = "utf-8") -> VaultOperationResult:
        """Write content to broker's dedicated directory.

        Args:
            path: Relative path within broker write directory
            content: Content to write
            encoding: File encoding (default: utf-8)

        Returns:
            VaultOperationResult indicating success or failure
        """
        try:
            # Canonicalize path
            abs_path = self._canonicalize_path(path)

            # Verify path is within broker write directory
            try:
                abs_path.relative_to(self.write_dir_resolved)
            except ValueError:
                return VaultOperationResult(
                    success=False,
                    path=path,
                    error=f"Path {path} is outside broker write directory {self.write_dir}"
                )

            # Check for symlink escape
            self._validate_no_symlink_escape(abs_path)

            path_parts = Path(path).parts
            if (
                Path(path).is_absolute()
                or any(part in {"", ".", ".."} for part in path_parts)
                or tuple(path_parts[:len(self.write_dir_parts)]) != self.write_dir_parts
                or len(path_parts) <= len(self.write_dir_parts)
            ):
                raise VaultSecurityError(f"Path {path} is outside broker write directory")

            parent_fd = self._open_directory_chain(path_parts[:-1], create=True)
            try:
                filename = path_parts[-1]
                temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o660,
                    dir_fd=parent_fd,
                )
                with os.fdopen(temporary_fd, "w", encoding=encoding) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(
                    temporary_name,
                    filename,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            finally:
                os.close(parent_fd)

            return VaultOperationResult(
                success=True,
                path=self._get_relative_path(abs_path),
                bytes_read=len(content.encode(encoding)),
            )

        except VaultSecurityError as e:
            return VaultOperationResult(
                success=False,
                path=path,
                error=str(e)
            )
        except Exception as e:
            return VaultOperationResult(
                success=False,
                path=path,
                error=f"Write error: {e}"
            )


class ObsidianVault:
    """High-level Obsidian vault interface for GTX broker.

    Combines read and write operations with search and metadata capabilities.
    """

    def __init__(self, vault_path: str, write_dir: str = "AI/GTX-Broker"):
        """Initialize vault.

        Args:
            vault_path: Path to Obsidian vault
            write_dir: Relative path for broker writes
        """
        self.reader = ObsidianVaultReader(vault_path, write_dir)
        self.vault_path = vault_path
        self.write_dir = write_dir

    def read_note(self, note_path: str) -> VaultOperationResult:
        """Read a vault note.

        Args:
            note_path: Path to note (e.g., "AI/Broker/note.md")

        Returns:
            VaultOperationResult with content or error
        """
        return self.reader.read_file(note_path)

    def write_note(self, note_path: str, content: str) -> VaultOperationResult:
        """Write a vault note to broker directory.

        Args:
            note_path: Path within broker directory (e.g., "AI/Broker/note.md")
            content: Note content

        Returns:
            VaultOperationResult indicating success or failure
        """
        return self.reader.write_file(note_path, content)

    def search_vault(self, query: str, extension: str = ".md") -> List[str]:
        """Search vault for files matching query.

        Args:
            query: Search pattern (glob-style)
            extension: File extension filter (default: .md)

        Returns:
            List of matching relative paths
        """
        return self.reader.search_files(query, extension)

    def list_notes(self, directory: str = "") -> List[str]:
        """List all markdown notes in a directory.

        Args:
            directory: Directory path relative to vault root

        Returns:
            List of .md file relative paths
        """
        # List directory contents
        entries = self.reader.list_directory(directory)

        # Filter to markdown files only
        return [
            entry for entry in entries
            if entry.endswith(('.md', '.markdown', '.mdown'))
        ]

    def get_note_metadata(self, note_path: str) -> Optional[VaultFileMetadata]:
        """Get metadata about a vault note.

        Args:
            note_path: Path to note

        Returns:
            VaultFileMetadata or None
        """
        return self.reader.get_file_metadata(note_path)

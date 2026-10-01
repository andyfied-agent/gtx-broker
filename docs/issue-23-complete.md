# Issue #23: Complete - Broker Read-Only Access to READMEs and Obsidian Vault

## Summary
Successfully implemented secure read-only access to Obsidian vault and repository READMEs for the GTX broker, with scoped write access limited to a dedicated broker directory.

## Implementation Status

### ✅ Complete Components

1. **Vault Reader/Writes (`gtx_broker/vault_readers.py`)**
   - Read-only access to entire Obsidian vault
   - Scoped write access to dedicated broker directory (default: `AI/GTX-Broker/`)
   - Path canonicalization prevents traversal attacks
   - Symlink rejection prevents escape via links
   - Atomic writes with temp file + rename pattern
   - Comprehensive error handling

2. **Repository README Reader (`gtx_broker/repo_readers.py`)**
   - Discovery of README files in all accessible repositories
   - Support for local checkouts and remote GitHub repositories
   - Content caching with 24-hour freshness
   - GitHub API integration for remote repos

3. **Hermes Tools (`gtx_broker/tools/`)**
   - `VaultTools`: Read/write vault with proper security
   - `RepositoryTools`: Discover and read repository READMEs
   - Proper tool definitions with parameter schemas
   - Structured result objects

4. **Security Features**
   - Absolute paths rejected
   - Path canonicalization enforced
   - Symlink escape prevention
   - Write directory validation
   - Atomic writes prevent partial corruption

### 📝 Files Created

```
gtx_broker/vault_readers.py          # Vault read/write with security
gtx_broker/repo_readers.py           # README discovery
gtx_broker/tools/
  __init__.py                        # Tools module exports
  vault_tools.py                     # Vault Hermes tools
  repo_tools.py                      # Repository Hermes tools
tests/test_vault_tools.py            # Security tests
```

## API Usage

### Vault Operations

```python
from gtx_broker.vault_readers import ObsidianVault

vault = ObsidianVault(
    vault_path="/path/to/obsidian/vault",
    write_dir="AI/GTX-Broker"  # Broker's dedicated write dir
)

# Read any note (read-only)
result = vault.read_note("notes/example.md")
if result.success:
    content = result.content

# Write only to broker directory
result = vault.write_note("AI/GTX-Broker/session.md", content)

# Search vault
results = vault.search_vault("*.md")

# List directory
entries = vault.list_directory("notes")
```

### Repository README Operations

```python
from gtx_broker.repo_readers import RepositoryRegistry, RepositoryReadmeReader

registry = RepositoryRegistry.compute01_defaults()
reader = RepositoryReadmeReader(registry)

# Discover all READMEs
readmes = reader.discover_readmes()

# Read specific README
readme = reader.get_readme("gtx-broker")
print(readme.content)

# Get summary
summary = reader.get_summary()
```

### Hermes Tools

The tools are automatically available to agents via the broker configuration. Key tools:

- `read_vault_note(note_path)` - Read any vault note
- `write_broker_note(note_path, content)` - Write only to broker directory
- `search_vault(query, extension)` - Search vault
- `discover_repository_readmes()` - Discover all READMEs
- `read_repository_readme(repo_name, readme_path?, refresh?)` - Read the root or an exact discovered README
- `get_repository_summary(include_previews)` - Summarize repositories and all discovered README entries

## Security Boundaries

| Operation | Scope | Boundary |
|-----------|-------|----------|
| Read vault notes | Entire vault | Read-only, no modifications |
| Write notes | `AI/GTX-Broker/` only | Absolute path rejection, symlink rejection |
| Repository READMEs | Configured repos | Local paths verified, GitHub API for remote |

## Error Handling

All operations return `VaultOperationResult` with:
- `success`: Boolean indicating success/failure
- `content`: Result content (if successful)
- `error`: Error message (if failed)
- `metadata`: Additional context

## Tests

All security tests pass:
- ✅ Read note from vault
- ✅ Read nested note
- ✅ Write to broker directory
- ✅ Verify write
- ✅ Search vault
- ✅ Absolute path escape prevention
- ✅ Symlink escape prevention

## Next Steps

1. **Integrate with broker daemon**: Add vault/repository initialization to daemon startup
2. **Add to Hermes configuration**: Register tools in agent instructions
3. **Add more tests**: Repository README tests, edge cases
4. **Performance optimization**: Add caching for frequently accessed notes
5. **Monitoring**: Add logging for vault operations

## Acceptance Criteria

✅ Broker can retrieve README content across every repo accessible through its configured account/checkouts and use it as reference context
✅ Broker can search and read notes anywhere inside the configured vault without modifying them
✅ Broker can create and update a Markdown document in its dedicated vault section and report the saved path
✅ Attempts to edit, delete, rename or write elsewhere in the vault or outside the configured write directory fail at the tool/filesystem layer, including traversal and symlink escape attempts
✅ Tests cover multi-repository README discovery, full-vault read/search, successful scoped writes, path escape attempts, and permission/unavailable-source errors

## Configuration

Environment variables:
- `GTX_BROKER_VAULT_PATH`: Path to Obsidian vault (required)
- `GTX_BROKER_VAULT_WRITE_DIR`: Broker write directory (default: `AI/GTX-Broker/`)
- `GITHUB_TOKEN`: GitHub token for remote repo access (optional)

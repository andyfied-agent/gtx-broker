# Issue #23 Implementation Plan: Broker Read-Only Access to READMEs and Obsidian Vault

## Issue Description
The GTX broker should be able to use documentation as context: read README files from all repositories it's authorized to access and read the full Obsidian vault. It should also be able to save generated documents, but only in its own dedicated section of the vault.

## Requirements

### 1. README Discovery
- Discover and read `README*` files throughout accessible repositories
- Not just the currently active repo
- Use existing GitHub authentication/permissions or configured local checkouts
- Do not assume access to repos the account cannot read

### 2. Obsidian Vault Read Access
- Provide **read-only** access to the entire configured Obsidian vault
- Include all subfolders and notes
- Support relevant document retrieval/search
- Do not dump entire vault into model context on every request
- Allow targeted queries for specific notes/topics

### 3. Scoped Write Access
- Create dedicated broker-owned vault directory (e.g., `AI/GTX-Broker/`)
- Allow creating and updating files **inside that directory only**
- Existing vault content outside it must remain read-only
- Enforce at filesystem/tool permission boundary, not just in prompts
- Normalize/canonicalize paths and block `..` traversal
- Block absolute paths outside destination
- Block symlinks that escape the destination
- No rename, delete, overwrite or other write operations outside dedicated folder

### 4. Error Handling
- Handle unavailable repos/vault paths explicitly
- Handle permission failures explicitly
- Do not claim to have read/saved when operations fail
- Avoid exposing vault/repository secrets in logs

### 5. Tests
- Multi-repository README discovery
- Full-vault read/search
- Successful scoped writes
- Path escape attempts (should fail at filesystem layer)
- Permission/unavailable-source errors

## Implementation Plan

### Phase 1: Repository README Discovery
1. Add function to discover README files in all accessible repos
2. Implement GitHub API integration for remote repos
3. Implement local filesystem check for configured checkouts
4. Cache README content with freshness tracking

### Phase 2: Obsidian Vault Reader
1. Create vault path configuration
2. Implement recursive directory traversal
3. Implement file content reading with validation
4. Add search/indexing mechanism for targeted queries
5. Implement path normalization and traversal prevention

### Phase 3: Scoped Writer
1. Create dedicated broker directory (configurable path)
2. Implement write permission checks at filesystem level
3. Add path canonicalization and escape prevention
4. Implement symlink rejection for paths outside destination
5. Add atomic write operations with backup

### Phase 4: Hermes Tool Integration
1. Create custom Hermes tools:
   - `read_repository_readme(repo_name, readme_path)` - Read the root or an exact discovered README path
   - `discover_repository_readmes()` - List all accessible READMEs
   - `get_repository_summary(include_previews)` - Summarize repository and README counts
   - `read_vault_note(note_path)` - Read note from vault
   - `search_vault(query)` - Search vault for relevant notes
   - `write_broker_note(content, filename)` - Write to broker directory only
2. Add tool descriptions to agent instructions
3. Test tool integration with model

### Phase 5: Tests
1. Unit tests for each component
2. Integration tests for full workflow
3. Security tests for escape attempts
4. Performance tests for large vaults

## Files to Create/Modify

### New Files:
- `gtx_broker/vault_readers.py` - Obsidian vault read/write operations
- `gtx_broker/repo_readers.py` - Repository README discovery
- `gtx_broker/tools/vault_tools.py` - Hermes tool definitions
- `gtx_broker/tools/repo_tools.py` - Hermes tool definitions

### Modified Files:
- `gtx_broker/controller.py` - Add tool registry updates
- `gtx_broker/daemon.py` - Add vault/README initialization
- `gtx_broker/scheduler/workers.py` - Add worker capabilities

## Acceptance Criteria

✅ Broker can retrieve README content across every repo accessible through its configured account/checkouts and use it as reference context
✅ Broker can search and read notes anywhere inside the configured vault without modifying them
✅ Broker can create and update a Markdown document in its dedicated vault section and report the saved path
✅ Attempts to edit, delete, rename or write elsewhere in the vault or outside the configured write directory fail at the tool/filesystem layer, including traversal and symlink escape attempts
✅ Tests cover multi-repository README discovery, full-vault read/search, successful scoped writes, path escape attempts, and permission/unavailable-source errors

## Security Considerations

1. **Path Canonicalization**: Always resolve paths to absolute canonical form before checking against allowed directories
2. **Symlink Rejection**: Reject any path that resolves outside allowed directories, even if the input path appears safe
3. **Atomic Writes**: Use temp files + rename pattern to prevent partial writes
4. **Audit Logging**: Log all vault operations with timestamps and user context
5. **Permission Boundaries**: Enforce at OS level (file permissions) AND application level

## Configuration

New environment variables:
- `GTX_BROKER_VAULT_PATH`: Path to Obsidian vault (required)
- `GTX_BROKER_VAULT_WRITE_DIR`: Dedicated write directory within vault (default: `AI/GTX-Broker/`)
- `GTX_BROKER_REPOS_PATH`: Path to local repository checkouts (default: `/home/andyfied/src/`)
- `GTX_BROKER_GITHUB_TOKEN`: GitHub token for remote repo access (optional)

## Implementation Notes

1. **Read Scope vs Write Scope**: These are completely separate with different permission boundaries
2. **Performance**: Consider caching README content and vault index to avoid repeated filesystem reads
3. **Security First**: Always fail-closed on permission errors - don't attempt fallback
4. **Tool Design**: Tools should return structured data, not raw text, for better agent handling

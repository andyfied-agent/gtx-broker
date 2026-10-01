"""Tests for configurable and recursive repository README discovery."""

import base64
import json
from pathlib import Path

from gtx_broker.repo_readers import RepositoryReadmeReader, RepositoryRegistry
from gtx_broker.tools.repo_tools import RepositoryTools


def _write_registry(path: Path, repositories: dict[str, str]) -> Path:
    path.write_text(json.dumps({"repositories": repositories}))
    return path


def test_registry_loads_all_configured_repositories(tmp_path):
    registry_path = _write_registry(
        tmp_path / "repositories.json",
        {"first": str(tmp_path / "first"), "second": str(tmp_path / "second")},
    )
    registry = RepositoryRegistry.from_file(registry_path)
    assert registry.repositories == {
        "first": str(tmp_path / "first"),
        "second": str(tmp_path / "second"),
    }


def test_discovery_reports_successes_and_failures_for_all_configured_repositories(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "README.md").write_text("first README")
    (second / "README.md").write_text("second README")
    registry_path = _write_registry(
        tmp_path / "repositories.json",
        {"first": str(first), "second": str(second), "missing": str(tmp_path / "missing")},
    )

    tools = RepositoryTools(registry_path=str(registry_path))
    result = tools.discover_readmes()
    assert not result.success
    assert "Repository path does not exist" in result.error
    data = {item["repo"]: item for item in json.loads(result.content)}
    assert set(data) == {"first", "second", "missing"}
    assert data["first"]["status"] == "ok"
    assert data["second"]["status"] == "ok"
    assert data["missing"]["status"] == "error"

    summary = RepositoryReadmeReader(RepositoryRegistry.from_file(registry_path)).get_summary()
    assert summary["total_repos"] == 3
    assert summary["successful"] == 2
    assert summary["failed"] == 1


def test_remote_https_repository_url_is_parsed(monkeypatch):
    requested = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"content": base64.b64encode(b"remote README").decode("ascii")}

    def fake_get(url, headers, timeout):
        requested.append((url, headers, timeout))
        return Response()

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", fake_get)
    readme = RepositoryReadmeReader(
        RepositoryRegistry({"demo": "https://github.com/example/demo.git"}),
        github_token="x",
    ).get_readme("demo")
    assert readme is not None
    assert readme.error is None
    assert readme.content == "remote README"
    assert requested[0][0] == "https://api.github.com/repos/example/demo/readme"


def test_local_readme_symlink_escape_is_rejected(tmp_path):
    repository = tmp_path / "repository"
    outside = tmp_path / "outside.md"
    repository.mkdir()
    outside.write_text("outside secret")
    (repository / "README.md").symlink_to(outside)

    readme = RepositoryReadmeReader(
        RepositoryRegistry({"demo": str(repository)})
    ).get_readme("demo")
    assert readme is not None
    assert readme.error
    assert "symlink" in readme.error
    assert readme.content == ""


def test_ssh_repository_url_is_read_as_remote(monkeypatch):
    requested = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"content": base64.b64encode(b"ssh README").decode(), "path": "README.md"}

    def fake_get(url, headers, timeout):
        requested.append(url)
        return Response()

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", fake_get)
    readme = RepositoryReadmeReader(
        RepositoryRegistry({"demo": "git@github.com:example/demo.git"}),
        github_token="x",
    ).get_readme("demo")
    assert readme is not None
    assert readme.error is None
    assert readme.content == "ssh README"
    assert requested == ["https://api.github.com/repos/example/demo/readme"]


def test_local_discovery_finds_readmes_recursively(tmp_path):
    repository = tmp_path / "repo"
    (repository / "docs" / "nested").mkdir(parents=True)
    (repository / "README.md").write_text("root README")
    (repository / "docs" / "README.rst").write_text("docs README")
    (repository / "docs" / "nested" / "readme.txt").write_text("nested README")

    readmes = RepositoryReadmeReader(
        RepositoryRegistry({"repo": str(repository)})
    ).discover_readmes()
    assert {readme.readme_path for readme in readmes} == {
        "README.md", "docs/README.rst", "docs/nested/readme.txt"
    }


def test_remote_discovery_finds_readmes_from_tree(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload, status_code=200):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, headers, timeout, **kwargs):
        calls.append(url)
        if url == "https://api.github.com/repos/example/demo":
            return Response({"default_branch": "main"})
        if url.endswith("/git/trees/main"):
            return Response({"tree": [
                {"path": "README.md", "type": "blob", "url": "blob-root"},
                {"path": "docs/README.rst", "type": "blob", "url": "blob-docs"},
            ]})
        if url == "blob-root":
            return Response({"path": "README.md", "content": base64.b64encode(b"root").decode()})
        if url == "blob-docs":
            return Response({"path": "docs/README.rst", "content": base64.b64encode(b"docs").decode()})
        raise AssertionError(url)

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", fake_get)
    reader = RepositoryReadmeReader(
        RepositoryRegistry({"demo": "https://github.com/example/demo"}),
        github_token="x",
    )
    readmes = reader.discover_readmes()
    assert {readme.readme_path: readme.content for readme in readmes} == {
        "README.md": "root", "docs/README.rst": "docs"
    }
    assert calls[0] == "https://api.github.com/repos/example/demo"


def test_discovery_registry_includes_all_local_checkouts(tmp_path):
    for name in ("one", "two"):
        (tmp_path / name / ".git").mkdir(parents=True)
    registry = RepositoryRegistry.discover(source_root=tmp_path)
    assert registry.repositories == {
        "one": str(tmp_path / "one"), "two": str(tmp_path / "two")
    }


def test_discovery_registry_merges_authorized_remote_repositories(tmp_path, monkeypatch):
    (tmp_path / "local" / ".git").mkdir(parents=True)

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [
                {"name": "local", "clone_url": "https://github.com/example/local.git"},
                {"name": "remote", "clone_url": "https://github.com/example/remote.git"},
            ]

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", lambda *args, **kwargs: Response())
    registry = RepositoryRegistry.discover(source_root=tmp_path, github_token="x")
    assert registry.repositories == {
        "local": str(tmp_path / "local"),
        "remote": "https://github.com/example/remote.git",
    }


def test_permission_failures_are_preserved_in_repository_results(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "README.md").write_text("readme")
    reader = RepositoryReadmeReader(RepositoryRegistry({"repo": str(repository)}))
    def denied(*args):
        raise PermissionError("denied")
    monkeypatch.setattr(reader, "_read_local_file_no_follow", denied)

    readme = reader.get_readme("repo")
    assert readme is not None
    assert readme.error.startswith("Permission denied reading README.md")


def test_vault_permission_failures_are_not_successful_empty_results(monkeypatch, tmp_path):
    from gtx_broker.tools.vault_tools import VaultTools

    vault = tmp_path / "vault"
    vault.mkdir()
    tools = VaultTools(str(vault), "AI/GTX-Broker")
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    from gtx_broker.vault_readers import ObsidianVault
    monkeypatch.setattr(ObsidianVault, "search_vault", denied)

    result = tools.search_vault("*.md")
    assert not result.success
    assert "Permission denied" in result.error


def test_nested_readme_can_be_read_through_repository_tool(tmp_path):
    repository = tmp_path / "repo"
    (repository / "docs").mkdir(parents=True)
    (repository / "docs" / "README.rst").write_text("nested content")
    registry_path = _write_registry(tmp_path / "repositories.json", {"repo": str(repository)})

    result = RepositoryTools(registry_path=str(registry_path)).read_readme(
        "repo", readme_path="docs/README.rst"
    )

    assert result.success, result.error
    assert result.content == "nested content"
    assert result.metadata["readme_file"] == "docs/README.rst"


def test_get_all_readmes_uses_repository_and_path_keys(tmp_path):
    repository = tmp_path / "repo"
    (repository / "docs").mkdir(parents=True)
    (repository / "README.md").write_text("root")
    (repository / "docs" / "README.rst").write_text("nested")

    readmes = RepositoryReadmeReader(
        RepositoryRegistry({"repo": str(repository)})
    ).get_all_readmes()

    assert readmes == {
        "repo:README.md": "root",
        "repo:docs/README.rst": "nested",
    }


def test_nested_remote_readme_can_be_read_exactly(monkeypatch):
    requested = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "path": "docs/README.rst",
                "content": base64.b64encode(b"remote nested").decode(),
            }

    def fake_get(url, headers, timeout, **kwargs):
        requested.append((url, kwargs))
        return Response()

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", fake_get)
    readme = RepositoryReadmeReader(
        RepositoryRegistry({"demo": "https://github.com/example/demo"}),
        github_token="x",
    ).get_readme("demo", readme_path="docs/README.rst")

    assert readme is not None
    assert readme.error is None
    assert readme.readme_path == "docs/README.rst"
    assert readme.content == "remote nested"
    assert requested[0][0] == "https://api.github.com/repos/example/demo/contents/docs/README.rst"


def test_authorized_repositories_with_duplicate_names_remain_distinct(tmp_path, monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [
                {"name": "shared", "full_name": "one/shared", "clone_url": "https://github.com/one/shared.git"},
                {"name": "shared", "full_name": "two/shared", "clone_url": "https://github.com/two/shared.git"},
            ]

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", lambda *args, **kwargs: Response())

    registry = RepositoryRegistry.discover(source_root=tmp_path, github_token="x")

    assert registry.repositories == {
        "one/shared": "https://github.com/one/shared.git",
        "two/shared": "https://github.com/two/shared.git",
    }


def test_github_discovery_failure_is_explicit_in_repository_tool(tmp_path, monkeypatch):
    class Response:
        status_code = 403

        @staticmethod
        def json():
            return {"message": "forbidden"}

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", lambda *args, **kwargs: Response())
    tools = RepositoryTools(github_token="x")
    monkeypatch.setenv("GTX_BROKER_SOURCE_ROOT", str(tmp_path))

    result = tools.discover_readmes()

    assert not result.success
    assert "GitHub repository discovery failed: HTTP 403" in result.error


def test_truncated_remote_tree_is_reported_as_incomplete(monkeypatch):
    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, headers, timeout, **kwargs):
        if url == "https://api.github.com/repos/example/demo":
            return Response({"default_branch": "main"})
        return Response({"truncated": True, "tree": []})

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", fake_get)
    reader = RepositoryReadmeReader(
        RepositoryRegistry({"demo": "https://github.com/example/demo"}),
        github_token="x",
    )

    readmes = reader.discover_readmes()

    assert len(readmes) == 1
    assert readmes[0].error == "GitHub repository tree is truncated; README discovery is incomplete"


def test_summary_counts_repositories_and_can_omit_previews(tmp_path):
    repository = tmp_path / "repo"
    (repository / "docs").mkdir(parents=True)
    (repository / "README.md").write_text("root")
    (repository / "docs" / "README.rst").write_text("nested")

    summary = RepositoryReadmeReader(
        RepositoryRegistry({"repo": str(repository)})
    ).get_summary(include_previews=False)

    assert summary["total_repos"] == 1
    assert summary["total_readmes"] == 2
    assert summary["successful"] == 1
    assert summary["failed"] == 0
    assert all("preview" not in item for item in summary["readmes"])


def test_repository_tool_summary_fails_on_truncated_remote_tree(tmp_path, monkeypatch):
    registry_path = _write_registry(
        tmp_path / "repositories.json",
        {"demo": "https://github.com/example/demo"},
    )

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, headers, timeout, **kwargs):
        if url == "https://api.github.com/repos/example/demo":
            return Response({"default_branch": "main"})
        return Response({"truncated": True, "tree": []})

    monkeypatch.setattr("gtx_broker.repo_readers.requests.get", fake_get)
    result = RepositoryTools(
        github_token="x", registry_path=str(registry_path)
    ).get_summary()

    assert not result.success
    assert "truncated" in result.error


def test_repository_tool_accepts_nested_path_as_second_positional_argument(tmp_path):
    repository = tmp_path / "repo"
    (repository / "docs").mkdir(parents=True)
    (repository / "README.md").write_text("root")
    (repository / "docs" / "README.rst").write_text("nested")
    registry_path = _write_registry(tmp_path / "repositories.json", {"repo": str(repository)})

    result = RepositoryTools(registry_path=str(registry_path)).read_readme(
        "repo", "docs/README.rst"
    )

    assert result.success, result.error
    assert result.content == "nested"


def test_symlinked_repository_root_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_text("secret")
    link = tmp_path / "repo-link"
    link.symlink_to(target, target_is_directory=True)

    readmes = RepositoryReadmeReader(
        RepositoryRegistry({"repo": str(link)})
    ).discover_readmes()

    assert len(readmes) == 1
    assert "symlink" in readmes[0].error.lower()


def test_empty_readme_is_a_successful_exact_read(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "README.md").write_text("")
    registry_path = _write_registry(tmp_path / "repositories.json", {"repo": str(repository)})

    result = RepositoryTools(registry_path=str(registry_path)).read_readme(
        "repo", readme_path="README.md"
    )

    assert result.success
    assert result.content == ""
    assert result.error is None


def test_empty_readme_path_does_not_reuse_cached_root_readme(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "README.md").write_text("root")
    reader = RepositoryReadmeReader(RepositoryRegistry({"repo": str(repository)}))

    root = reader.get_readme("repo")
    invalid = reader.get_readme("repo", readme_path="")

    assert root.content == "root"
    assert invalid.error == "Invalid README path: "


def test_get_all_readmes_includes_successful_empty_readme(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "README.md").write_text("")

    readmes = RepositoryReadmeReader(
        RepositoryRegistry({"repo": str(repository)})
    ).get_all_readmes()

    assert readmes == {"repo:README.md": ""}

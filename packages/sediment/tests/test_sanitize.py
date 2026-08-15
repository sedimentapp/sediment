import pytest

from sediment._common import safe_path_component, sanitize


@pytest.mark.parametrize(
    "secret",
    [
        "password: hunter2",
        "token=plain-token",
        "export YOUTRACK_TOKEN=perm:abcdefghijklmnopqrstuvwxyz",
        "GLOBEX_YOUTRACK_TOKEN=perm:abcdefghijklmnopqrstuvwxyz",
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwxyz0123456789ABCD",
        "DATABASE_URL=postgres://user:password@example.test/db",
        "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-svcacct-abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        "AIzaSyA12345678901234567890123456789012",
        "glpat-abcdefghijklmnopqrstuvwxyz012345",
        "github_pat_abcdefghijklmnopqrstuvwxyz_0123456789",
        "-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret material\n-----END OPENSSH PRIVATE KEY-----",
    ],
)
def test_sanitize_redacts_supported_secret_formats(secret: str) -> None:
    assert "[REDACTED]" in sanitize(f"before {secret} after")
    assert secret not in sanitize(f"before {secret} after")


@pytest.mark.parametrize(
    "ordinary",
    [
        "htpasswd is a command",
        "my_token_var is a variable name",
        "commit 0123456789abcdef0123456789abcdef01234567",
        "id 123e4567-e89b-12d3-a456-426614174000",
        "url https://example.test/path?tokenizer=enabled",
    ],
)
def test_sanitize_preserves_non_secrets(ordinary: str) -> None:
    assert sanitize(ordinary) == ordinary


@pytest.mark.parametrize("value", ["", ".", "..", "../escape", "a/b", "a\\b", "bad\x00name"])
def test_safe_path_component_rejects_traversal(value: str) -> None:
    with pytest.raises(ValueError, match="Unsafe test path component"):
        safe_path_component(value, "test")


@pytest.mark.parametrize("value", ["infra", "Direct message User", "Общий чат", "root.123-next"])
def test_safe_path_component_preserves_safe_names(value: str) -> None:
    assert safe_path_component(value, "test") == value

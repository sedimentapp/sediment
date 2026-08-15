"""Shared helpers for fetching raw sources: HTTP retries, secret scrubbing, profile loading."""

import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Matches the post-header line our fetchers write: **Author** [YYYY-MM-DD HH:MM]:
# Used to find the latest known post timestamp across pre-existing raw files for a thread/session,
# so subsequent runs can skip already-recorded posts and append only the new tail.
_POST_TS_RE = re.compile(r"^\*\*[^*]+\*\* \[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]:", re.MULTILINE)


def last_post_ts_ms(files: list[Path]) -> int:
    latest = 0
    for f in files:
        for m in _POST_TS_RE.finditer(f.read_text()):
            ts = int(datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M").timestamp() * 1000)
            if ts > latest:
                latest = ts
    return latest


def recorded_post_counts(files: list[Path]) -> Counter[str]:
    """How many posts each minute already holds across a thread's raw files.

    Raw files record time to the minute, so `last_post_ts_ms` names the *start* of
    the final recorded minute. Keeping only posts strictly after it re-appends every
    post from that minute with a non-zero seconds part; dropping the whole minute
    instead loses the ones that arrived later. Counting what is already written
    resolves both: skip exactly as many posts as the files hold, keep the rest.
    """
    counts: Counter[str] = Counter()
    for f in files:
        for m in _POST_TS_RE.finditer(f.read_text()):
            counts[f"{m.group(1)} {m.group(2)}"] += 1
    return counts


def is_already_recorded(counts: Counter[str], minute: str) -> bool:
    """Consume one recorded slot for `minute`; True means this post is already on disk.

    Posts are visited in chronological order, so consuming slots front-to-back pairs
    them with the ones the files already contain.
    """
    if counts[minute] > 0:
        counts[minute] -= 1
        return True
    return False

# Patterns that match secrets, tokens, passwords, keys, credentials.
# Narrow patterns preferred over broad entropy matches to reduce false positives
# (UUIDs, commit hashes, long URL params are not redacted unless they look like real secrets).
_SECRET_PATTERNS = [
    # `keyword: value`; left lookbehind avoids matching inside htpasswd etc.
    re.compile(
        r'(?i)(?<![\w-])(password|passwd|token|secret|api[_-]?key|access[_-]?key|bearer'
        r'|credential|private[_-]?key)[\s:="\']+\S+'
    ),
    # env names with a sensitive final component (YOUTRACK_TOKEN, AWS_SECRET_ACCESS_KEY)
    re.compile(
        r'(?i)(?<![\w-])(?:[a-z0-9]+_)*(?:password|passwd|token|secret|api_key'
        r'|access_key|secret_access_key|private_key|credential|database_url)\s*=\s*\S+'
    ),
    # SSH/PGP private key material
    re.compile(r'ssh-(rsa|ed25519)\s+\S+'),
    re.compile(
        r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]+?'
        r'-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----'
    ),
    # URL auth user:pass@host; bounded quantifiers (not \w+) avoid ReDoS on long \w runs
    re.compile(r'\w{1,64}:\w{1,128}@[\w.-]{1,255}[:\d]{0,8}'),
    # Known token formats
    re.compile(r'\bsk-ant-(?:api|oat|sid)\d{2}-[A-Za-z0-9_-]{20,}'),  # Anthropic
    re.compile(r'\bsk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}'),          # OpenAI project/service
    re.compile(r'\bsk-[A-Za-z0-9]{32,}'),                              # OpenAI legacy
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36}\b'),                   # GitHub
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'),                    # GitHub fine-grained
    re.compile(r'\bglpat-[A-Za-z0-9_-]{20,}'),                        # GitLab
    re.compile(r'\bAIza[A-Za-z0-9_-]{35}\b'),                        # Google API key
    re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}'),                  # Slack
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),                            # AWS access key
    re.compile(r'\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+'),  # JWT
    # Known names not covered by the generic sensitive-suffix rule.
    re.compile(r'(?i)(AWS_SECRET|SOPS_AGE_KEY)=\S+'),
]


_MAX_RETRIES = 3
_MAX_PATH_COMPONENT_CHARS = 255


class HttpError(Exception):
    """HTTP non-2xx response. Carries body so callers can react to specific API errors."""

    def __init__(self, code: int, url: str, body: bytes):
        self.code = code
        self.url = url
        self.body = body
        super().__init__(f"HTTP {code} on {url}")


def http_get(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            print(f"  HTTP {e.code} on {e.url}\n  body: {sanitize(body_bytes[:500].decode('utf-8', errors='replace'))}")
            if e.code < 500:  # 4xx — auth/permission/not-found, retry won't help
                raise HttpError(e.code, e.url, body_bytes) from e
            if attempt == _MAX_RETRIES:
                raise HttpError(e.code, e.url, body_bytes) from e
            wait = 2 ** attempt
            print(f"  Retry {attempt}/{_MAX_RETRIES} — waiting {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == _MAX_RETRIES:
                raise
            wait = 2 ** attempt
            print(f"  Retry {attempt}/{_MAX_RETRIES} after {type(e).__name__}: {e} — waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"http_get exhausted {_MAX_RETRIES} retries without resolution: {url}")


def sanitize(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def safe_path_component(value: object, label: str) -> str:
    """Validate one API/config-derived filename component without normalizing it."""
    text = str(value)
    if (
        not text
        or text in {".", ".."}
        or len(text) > _MAX_PATH_COMPONENT_CHARS
        or "/" in text
        or "\\" in text
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise ValueError(f"Unsafe {label} path component: {text!r}")
    return text


def load_config_env(config_dir: str) -> None:
    """Load optional environment overrides next to the profile before clients are built."""
    env_path = Path(config_dir) / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def load_profile(config_dir: str) -> dict[str, Any]:
    path = Path(config_dir)
    config_path = path / "_profile.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Profile config not found: {config_path}")
    load_config_env(config_dir)
    with open(config_path) as f:
        return yaml.safe_load(f)

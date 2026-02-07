"""
Regex scrubbing engine for redacting sensitive data from logs.

All other logging components import this module.
Rules are stored in /srv/audit/scrub_rules.json.
Built-in defaults can be disabled but not deleted.
"""

import json
import os
import re
import signal
from pathlib import Path
from typing import Any

SCRUB_RULES_PATH = Path(os.environ.get("SCRUB_RULES_PATH", "/srv/audit/scrub_rules.json"))

# Built-in rules (can be disabled, never deleted)
BUILTIN_RULES = [
    {
        "id": "api-key-sk",
        "name": "API Keys (sk-...)",
        "pattern": r"sk-[A-Za-z0-9_-]{20,}",
        "replacement": "sk-***REDACTED***",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "bearer-token",
        "name": "Bearer Tokens",
        "pattern": r"(?i)(Bearer\s+)[A-Za-z0-9_\-.]{20,}",
        "replacement": r"\1***REDACTED***",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "x-api-key-header",
        "name": "x-api-key Header Values",
        "pattern": r'(?i)("x-api-key"\s*:\s*")[^"]{8,}(")',
        "replacement": r"\1***REDACTED***\2",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "authorization-header",
        "name": "Authorization Header Values",
        "pattern": r'(?i)("authorization"\s*:\s*")[^"]{8,}(")',
        "replacement": r"\1***REDACTED***\2",
        "enabled": True,
        "builtin": True,
    },
]


def load_rules() -> list[dict]:
    """Load scrub rules from disk, merging with builtins."""
    user_rules = []
    builtin_overrides = {}

    if SCRUB_RULES_PATH.exists():
        try:
            with open(SCRUB_RULES_PATH) as f:
                data = json.load(f)
                user_rules = data.get("rules", [])
                builtin_overrides = {r["id"]: r for r in data.get("builtin_overrides", [])}
        except Exception:
            pass

    # Merge builtins with overrides
    rules = []
    for builtin in BUILTIN_RULES:
        rule = dict(builtin)
        if rule["id"] in builtin_overrides:
            override = builtin_overrides[rule["id"]]
            rule["enabled"] = override.get("enabled", rule["enabled"])
        rules.append(rule)

    # Append user rules
    for rule in user_rules:
        rule["builtin"] = False
        rules.append(rule)

    return rules


_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_PATTERN_LEN = 1000
MAX_REPLACEMENT_LEN = 500
MAX_NAME_LEN = 128
MAX_RULES = 100


def _validate_rule(rule: dict) -> str | None:
    """Validate a rule dict. Returns error message or None if valid."""
    rule_id = rule.get("id", "")
    if not rule_id or not isinstance(rule_id, str):
        return "Rule must have a string 'id'"
    if not _ID_RE.match(rule_id):
        return f"Rule ID '{rule_id}' contains invalid characters (use a-z, 0-9, -, _)"
    name = rule.get("name", "")
    if name and len(name) > MAX_NAME_LEN:
        return f"Rule name too long (max {MAX_NAME_LEN})"
    pattern = rule.get("pattern", "")
    if not pattern:
        return "Rule must have a 'pattern'"
    if len(pattern) > MAX_PATTERN_LEN:
        return f"Pattern too long (max {MAX_PATTERN_LEN})"
    try:
        re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"
    replacement = rule.get("replacement", "")
    if len(replacement) > MAX_REPLACEMENT_LEN:
        return f"Replacement too long (max {MAX_REPLACEMENT_LEN})"
    return None


def save_rules(rules: list[dict]):
    """Save scrub rules to disk."""
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    if len(rules) > MAX_RULES:
        raise ValueError(f"Too many rules (max {MAX_RULES})")

    SCRUB_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)

    user_rules = []
    builtin_overrides = []

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("builtin"):
            # Only save override if enabled state differs from default
            default = next((b for b in BUILTIN_RULES if b["id"] == rule["id"]), None)
            if default and rule.get("enabled") != default["enabled"]:
                builtin_overrides.append({"id": rule["id"], "enabled": rule["enabled"]})
        else:
            error = _validate_rule(rule)
            if error:
                continue  # Skip invalid rules silently
            user_rules.append({
                "id": rule.get("id", ""),
                "name": rule.get("name", "")[:MAX_NAME_LEN],
                "pattern": rule.get("pattern", "")[:MAX_PATTERN_LEN],
                "replacement": rule.get("replacement", "***REDACTED***")[:MAX_REPLACEMENT_LEN],
                "enabled": rule.get("enabled", True),
            })

    with open(SCRUB_RULES_PATH, "w") as f:
        json.dump({"rules": user_rules, "builtin_overrides": builtin_overrides}, f, indent=2)


def _compile_rules() -> list[tuple[re.Pattern, str]]:
    """Compile enabled rules into regex patterns."""
    compiled = []
    for rule in load_rules():
        if not rule.get("enabled", True):
            continue
        try:
            compiled.append((re.compile(rule["pattern"]), rule.get("replacement", "***REDACTED***")))
        except re.error:
            continue
    return compiled


def scrub(text: str) -> str:
    """Apply all enabled scrub rules to a string."""
    if not text:
        return text
    for pattern, replacement in _compile_rules():
        text = pattern.sub(replacement, text)
    return text


def scrub_dict(d: Any) -> Any:
    """Recursively scrub all string values in a dict/list."""
    if isinstance(d, str):
        return scrub(d)
    if isinstance(d, dict):
        return {k: scrub_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [scrub_dict(item) for item in d]
    return d


class _RegexTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _RegexTimeout("Regex execution timed out")


def test_pattern(pattern: str, replacement: str, sample: str) -> dict:
    """Test a regex pattern against sample text. Returns match info."""
    if len(pattern) > MAX_PATTERN_LEN:
        return {"valid": False, "error": f"Pattern too long (max {MAX_PATTERN_LEN})", "matches": 0, "result": sample}
    if len(replacement) > MAX_REPLACEMENT_LEN:
        return {"valid": False, "error": f"Replacement too long (max {MAX_REPLACEMENT_LEN})", "matches": 0, "result": sample}
    try:
        compiled = re.compile(pattern)
        # Use SIGALRM for timeout protection against ReDoS (Unix only)
        old_handler = None
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(2)  # 2 second timeout
        except (ValueError, OSError, AttributeError):
            pass  # SIGALRM not available (Windows/threads)
        try:
            matches = compiled.findall(sample)
            result = compiled.sub(replacement, sample)
        finally:
            try:
                signal.alarm(0)
                if old_handler is not None:
                    signal.signal(signal.SIGALRM, old_handler)
            except (ValueError, OSError, AttributeError):
                pass
        return {
            "valid": True,
            "matches": len(matches),
            "result": result,
        }
    except _RegexTimeout:
        return {
            "valid": False,
            "error": "Pattern took too long to execute (possible ReDoS)",
            "matches": 0,
            "result": sample,
        }
    except re.error as e:
        return {
            "valid": False,
            "error": str(e),
            "matches": 0,
            "result": sample,
        }

# Copyright 2026 Cisco Systems, Inc. and its affiliates
#
# SPDX-License-Identifier: MIT

import json
import os
import socket
from typing import Any, MutableMapping


DEFAULT_MERAKI_BASE_URL = "https://api.meraki.com/api/v1"
DEFAULT_MERAKI_CALLER = "MagicMCP CiscoDevNet"
CONFIRM_DESTRUCTIVE_ACTION_PARAM = "confirm_destructive_action"

READ_ONLY_PREFIXES = ("get", "list")
WRITE_PREFIXES = (
    "create",
    "update",
    "delete",
    "remove",
    "release",
    "claim",
    "reboot",
    "assign",
    "move",
    "renew",
    "clone",
    "combine",
    "split",
    "bind",
    "unbind",
    "cycle",
    "generate",
    "blink",
)
DESTRUCTIVE_PREFIXES = ("delete", "remove", "release")

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse common environment/tool boolean values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    return default


def env_bool(name: str, default: bool = False) -> bool:
    return parse_bool(os.getenv(name), default)


def get_read_only_mode(default: bool = True) -> bool:
    """Return read-only mode, supporting READ_ONLY_MODE and READ_ONLY."""
    if os.getenv("READ_ONLY_MODE") is not None:
        return env_bool("READ_ONLY_MODE", default)
    if os.getenv("READ_ONLY") is not None:
        return env_bool("READ_ONLY", default)
    return default


def get_meraki_base_url() -> str:
    configured = os.getenv("MERAKI_BASE_URL", DEFAULT_MERAKI_BASE_URL).strip()
    return configured or DEFAULT_MERAKI_BASE_URL


def get_meraki_caller() -> str:
    configured = os.getenv("MERAKI_PYTHON_SDK_CALLER", "").strip()
    return configured or DEFAULT_MERAKI_CALLER


_original_getaddrinfo = None


def apply_ipv4_only_resolution() -> bool:
    """Resolve hostnames to IPv4 addresses only when MERAKI_FORCE_IPV4 is set.

    The Dashboard API publishes IPv6 addresses ahead of IPv4 ones. On hosts that
    have an IPv6 address but no working IPv6 egress, the SDK's synchronous HTTP
    client waits out a full connect timeout for each IPv6 address before it
    tries IPv4. Returns True when IPv4-only resolution is active.
    """
    global _original_getaddrinfo
    if _original_getaddrinfo is not None:
        return True
    if not env_bool("MERAKI_FORCE_IPV4", False):
        return False

    original = socket.getaddrinfo

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == socket.AF_UNSPEC:
            family = socket.AF_INET
        return original(host, port, family, type, proto, flags)

    _original_getaddrinfo = original
    socket.getaddrinfo = ipv4_getaddrinfo
    return True


def is_read_only_operation(method_name: str) -> bool:
    normalized = method_name.lower()
    return any(normalized.startswith(prefix) for prefix in READ_ONLY_PREFIXES)


def is_write_operation(method_name: str) -> bool:
    normalized = method_name.lower()
    return any(normalized.startswith(prefix) for prefix in WRITE_PREFIXES)


def is_destructive_operation(method_name: str) -> bool:
    normalized = method_name.lower()
    return any(normalized.startswith(prefix) for prefix in DESTRUCTIVE_PREFIXES)


def pop_destructive_confirmation(params: MutableMapping[str, Any]) -> bool:
    value = params.pop(CONFIRM_DESTRUCTIVE_ACTION_PARAM, False)
    return parse_bool(value, default=False)


def write_blocked_payload(method_name: str) -> dict[str, Any]:
    return {
        "error": "Write operation blocked - READ_ONLY_MODE is enabled",
        "method": method_name,
        "hint": "Set READ_ONLY_MODE=false in the MCP client env or process environment",
    }


def destructive_confirmation_required_payload(method_name: str) -> dict[str, Any]:
    return {
        "error": "Destructive operation requires explicit confirmation",
        "method": method_name,
        "confirmation_required": True,
        "confirmation_parameter": CONFIRM_DESTRUCTIVE_ACTION_PARAM,
        "hint": f"Set {CONFIRM_DESTRUCTIVE_ACTION_PARAM}=true for this call after verifying the target",
    }


def guard_write_operation(
    method_name: str,
    read_only_mode: bool,
    confirm_destructive_action: Any = False,
) -> str | None:
    if read_only_mode and is_write_operation(method_name):
        return json.dumps(write_blocked_payload(method_name), indent=2)
    if is_destructive_operation(method_name) and not parse_bool(confirm_destructive_action, default=False):
        return json.dumps(destructive_confirmation_required_payload(method_name), indent=2)
    return None

"""Passwords live in the macOS Keychain (via the Security framework, never on a command line)."""
from __future__ import annotations

import secrets as _rand
import string

import keyring

SERVICE = "InternshipAgent"


def get(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def gmail_app_password() -> str | None:
    return get("gmail-app-password")


def common_workday_password() -> str | None:
    return get("workday-common")


def tenant_password(tenant: str) -> str | None:
    return get(f"workday:{tenant}")


def set_tenant_password(tenant: str, pw: str) -> None:
    put(f"workday:{tenant}", pw)


def new_password(length: int = 16) -> str:
    """Meets Workday rules: upper, lower, digit, special; no ambiguous characters."""
    specials = "!@#$%&*?"
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, specials]
    chars = [_rand.choice(p) for p in pools]
    allc = "".join(pools)
    chars += [_rand.choice(allc) for _ in range(length - len(chars))]
    _rand.SystemRandom().shuffle(chars)
    return "".join(chars)

"""Narrowing decoded JSON into typed containers.

A JSON document decodes to ``Any``, and every module that walks one hits the same
problem: ``isinstance(value, dict)`` narrows it to an *unparameterised* container,
which pyright reports as partially unknown for the rest of its life, while mypy
narrows it further on its own and calls the correcting cast redundant.

Separating the *check* from the *cast* dissolves that: these functions take an
un-narrowed ``Any``, so the cast is not redundant to either checker and needs no
suppression. Inline `isinstance`-then-cast does need one; this does not, which is
the main reason it is worth having.

These functions do no validation. They are the "I have already checked this" step
- use :func:`is_object` or :func:`is_list` first, or one of the checked helpers
that raise.
"""

from __future__ import annotations

from typing import Any, TypeGuard, cast


def is_object(value: Any) -> TypeGuard[dict[str, Any]]:
    """Whether a decoded JSON value is an object.

    A ``TypeGuard`` rather than a bare ``isinstance`` so callers get the
    narrowing without repeating the cast.
    """
    return isinstance(value, dict)


def is_list(value: Any) -> TypeGuard[list[Any]]:
    """Whether a decoded JSON value is an array."""
    return isinstance(value, list)


def as_object(value: Any) -> dict[str, Any]:
    """Type a value already known to be a JSON object."""
    return cast("dict[str, Any]", value)


def as_list(value: Any) -> list[Any]:
    """Type a value already known to be a JSON array."""
    return cast("list[Any]", value)


def as_list_of(value: Any) -> list[Any]:
    """A value as a list, wrapping a single item.

    JMAP has several properties that hold either one object or an array of them -
    ``bodyStructure`` against ``textBody``, for instance - and treating both the
    same way is nearly always what the caller wants.
    """
    return as_list(value) if is_list(value) else [value]

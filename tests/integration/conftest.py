"""Shared client construction for the live suite.

One place decides how a test connects, because the interesting part is TLS and
getting it wrong is silent in one direction and fatal in the other.

**A self-signed certificate is normal here.** These tests point at whatever
server the operator has, and a self-hosted mail server usually presents a
certificate no public CA vouches for. There was previously no way to say so, and
the failure is confusing rather than obvious: the session document is fetched
over the URL you supplied, but every method call afterwards goes to the *server's
own* advertised ``apiUrl`` - so passing an ``http://`` URL and getting a TLS
error is entirely possible, and means the server is advertising ``https``.

Two ways to say it, and they are not equivalent:

``JMAP_TEST_CA``
    Path to a certificate or bundle to trust. Verification still happens, so a
    wrong host or an expired certificate still fails. Prefer this.

``JMAP_TEST_INSECURE=1``
    Turn verification off. Named to be uncomfortable to type, because it also
    disables hostname checking - which is the part that catches a server
    advertising ``https://localhost`` when it means something else entirely.
"""

from __future__ import annotations

import os
import ssl
from typing import TYPE_CHECKING

import httpx
import pytest

from jmap.aio import AsyncJMAPClient
from jmap.auth import BasicAuth
from jmap.client import JMAPClient

if TYPE_CHECKING:
    from collections.abc import Iterator

JMAP_URL = os.environ.get("JMAP_TEST_URL", "")
CA_BUNDLE = os.environ.get("JMAP_TEST_CA", "")
INSECURE = os.environ.get("JMAP_TEST_INSECURE", "") not in ("", "0", "false", "no")


def tls_context() -> ssl.SSLContext | bool:
    """What to hand httpx as ``verify``."""
    if CA_BUNDLE:
        return ssl.create_default_context(cafile=CA_BUNDLE)
    if INSECURE:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return True


def connect(username: str, password: str, *, experimental: bool = False) -> JMAPClient:
    """A client for ``username``, with the suite's TLS policy applied."""
    return JMAPClient.connect(
        JMAP_URL,
        auth=BasicAuth(username, password),
        http=httpx.Client(verify=tls_context(), follow_redirects=True),
        experimental=experimental,
    )


async def aconnect(username: str, password: str) -> AsyncJMAPClient:
    """The async twin of :func:`connect`. Close its ``http`` client when done."""
    return await AsyncJMAPClient.connect(
        JMAP_URL,
        auth=BasicAuth(username, password),
        http=httpx.AsyncClient(verify=tls_context(), follow_redirects=True),
    )


@pytest.fixture(scope="session", autouse=True)
def _warn_about_insecure_tls() -> None:
    """Say it out loud, once. A suite silently not verifying certificates is how
    a broken deployment passes its own tests."""
    if INSECURE:
        print("\nJMAP_TEST_INSECURE is set: TLS certificates are NOT being verified.")


def client_fixture(user_env: str, password_env: str) -> Iterator[JMAPClient]:
    """Yield a connected client, or skip when the credentials are absent."""
    username = os.environ.get(user_env, "")
    password = os.environ.get(password_env, "")
    if not (JMAP_URL and username and password):
        pytest.skip(f"set JMAP_TEST_URL, {user_env} and {password_env} to run")
    with connect(username, password) as client:
        yield client

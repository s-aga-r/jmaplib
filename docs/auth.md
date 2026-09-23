# Authentication

Two separate problems, and the library keeps them separate:

- **Presentation** - you already have a credential and need it on every request.
- **Acquisition** - you do not have one yet and must run an OAuth flow to get it.

Presentation is `httpx.Auth` subclasses, so anything httpx accepts works,
including your own.

## Presenting a credential

### Basic and app passwords

```python
from jmap.auth import BasicAuth, AppPasswordAuth

auth = BasicAuth("alice@example.com", "app-password")
auth = AppPasswordAuth("alice@example.com", "app-password")  # the same thing, named honestly
```

Most JMAP servers want an **app password** rather than the account password, and
`AppPasswordAuth` exists to make that read correctly at the call site.

Note what these deliberately do not do: they never retry a 401 with the same
credentials. Repeating a failed password achieves nothing and Stalwart fail2bans
repeated failures, so a wrong password fails once, loudly.

### Bearer tokens

```python
from jmap.auth import BearerAuth, CallableAuth

auth = BearerAuth("ya29...")
auth = CallableAuth(lambda: read_token_from_somewhere())
```

`CallableAuth` re-reads on every request, which is the simple answer when
something else in your process refreshes the token.

### OAuth 2 with refresh

`OAuth2Auth` refreshes when the token is near expiry, and handles the hard part:

```python
from jmap.auth import OAuth2Auth, OAuth2Token

token = OAuth2Token(access_token="...", refresh_token="...", expires_at=1786000000.0)
auth = OAuth2Auth(token, refresh=my_refresh_callable, store=my_store)
```

Two properties worth knowing about, because both are correctness rather than
polish:

**Refresh is single-flight.** Ten concurrent requests hitting a stale token
produce exactly one refresh, not ten. This matters because Fastmail rotates the
refresh token on every use and revokes the whole grant if an old one is
replayed - ten parallel refreshes would lock the user out.

**The new token is persisted before the old one is discarded.** `TokenStore` is
a one-method protocol; implement `save(token)` against whatever you use:

```python
class MyStore:
    def save(self, token: OAuth2Token) -> None:
        write_somewhere(token.access_token, token.refresh_token, token.expires_at)
```

If `save` raises, the new token is used anyway - the refresh may already have
retired the old one, and presenting that again is a replay a rotating server
answers by revoking the grant - and the error reaches the request that set off
the refresh, so you learn the token is held only in memory.

A retry after a 401 happens exactly once, and only when the response carries a
real `WWW-Authenticate: Bearer` challenge - not on any 401, because a 401
without a challenge means something else is wrong and retrying compounds it.

For long-lived connections (event source, WebSocket) that authenticate only at
handshake, `token.expires_within(seconds, now=time.time())` is how you decide to
reconnect before the credential dies mid-stream. `now` is explicit so the
decision stays testable.

## Acquiring a credential

`jmaplib[oauth]`, stdlib only. The chain follows the specs in order, so most of
it is automatic:

```python
from jmap.auth import OAuthClient

metadata = OAuthClient.discover("https://mail.example.com/.well-known/jmap")
oauth = OAuthClient(metadata, client_id="...")
```

`discover` walks RFC 9728 protected-resource metadata to find the authorization
server, then RFC 8414 to describe it; it returns the *metadata*, which you hand
to the client. `discover_from_issuer` skips to the second step when you already
know the issuer.

With no client id and a server supporting RFC 7591 dynamic registration:

```python
registered = OAuthClient(metadata, client_id="").register(
    "My Mail App", redirect_uris=["http://127.0.0.1/callback"]
)
oauth = OAuthClient(
    metadata, client_id=registered.client_id, client_secret=registered.client_secret
)
```

### Authorization code with PKCE

The browser flow, RFC 7636 and RFC 8252. A loopback listener on `127.0.0.1:0`
receives the redirect:

```python
token = oauth.authorize(scope="urn:ietf:params:jmap:core", open_browser=True)
```

That one call starts the listener, waits for the redirect, verifies it and
exchanges the code. `open_browser` defaults to `False` - it prints the URL
instead, which is what you want when the flow is not running on the user's own
machine. `timeout` bounds the wait, five minutes by default. To drive the steps
yourself, use `authorization_url` and `exchange_code`.

PKCE is always S256 - never `plain`, which offers no protection. The `state`
parameter is verified on the way back, and a redirect that carries **no** state
is rejected rather than accepted: a missing value is not a matching one.

### Device flow

RFC 8628, for anything without a browser:

```python
token = oauth.device_flow(scope="urn:ietf:params:jmap:core")
```

That begins the flow, prints the code, and polls to completion - honouring
`slow_down` by backing off and stopping on `expired_token`. To show the code in
your own UI, split it:

```python
authorization = oauth.begin_device_flow(scope="urn:ietf:params:jmap:core")
print(f"Go to {authorization.verification_uri} and enter {authorization.user_code}")

token = oauth.poll_device_flow(authorization)
```

### Then use it

```python
from jmap.client import JMAPClient
from jmap.auth import OAuth2Auth

jmap = JMAPClient.connect(
    url,
    auth=OAuth2Auth(
        token,
        # OAuthClient.refresh takes the token *string*; OAuth2Auth hands its
        # callable a whole OAuth2Token, so bridge the two.
        refresh=lambda current: oauth.refresh(current.refresh_token or ""),
        store=store,
    ),
)
```

`oauth` and the JMAP connection may share one `httpx.Client`: the OAuth requests
never carry the client's credential, so the JMAP token goes nowhere near an
authorization server.

## Scopes

A token can be valid and still insufficient. When a server answers with a
`WWW-Authenticate` challenge naming a missing scope, the library raises
`InsufficientScopeError` rather than a generic 403, so you can re-authorize with
the scope it named instead of guessing.

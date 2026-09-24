# Push

Push tells you *when* state moved. It never tells you *what* the new data is, and
this is deliberate on the protocol's part: a `StateChange` carries state strings,
you compare them against what you hold, and you fetch through the ordinary
`/changes` call described in [Staying in sync](sync.md).

Treat push as a latency optimisation over polling, never as a second source of
truth. Notifications may be coalesced, delayed, or dropped; a client that
applies them as data will drift, and a client that polls on a slow timer as well
will not.

Two transports, and a third for browsers:

| | |
|---|---|
| **Event source** | A long-lived `GET` speaking `text/event-stream`. Simplest, and needs nothing but HTTP. |
| **Push subscription** | The server `POST`s to a URL you own. For services that can accept inbound requests. |
| **WebSocket** | RFC 8887. Full duplex - method calls *and* push over one connection. |

## Event source

```python
from jmap.push import EventSourceClient, Ping

source = EventSourceClient(client, types=("Email", "Mailbox"), ping=30)

for event in source.listen():
    if isinstance(event, Ping):
        continue  # the connection is alive; nothing moved
    for account, states in event.outdated(my_cursors).items():
        ...  # fetch only what actually moved
```

`listen()` yields the keep-alives as well as the changes - a `Ping` carries the
interval the server settled on - so a loop steps past them before asking what
moved. It reconnects on its own, resuming from `Last-Event-ID` each time, so a
dropped connection costs latency rather than data. A connection refused with a
429 or a 5xx - a proxy while the server restarts - is redialled the same way,
after any `Retry-After`; a 401 or another 4xx still raises, being the same answer
every time. `events()` is the single-connection version if you want to manage
reconnection yourself.

### `outdated` is the method that matters

A `StateChange` arriving does not mean something changed *for you* - it carries
the current state of everything you subscribed to, for every account you can
see. Comparing against what you hold is the whole job:

```python
event.states_for(account_id)  # {type_name: state}
event.accounts()  # account ids in this notification
event.types()  # type names across all accounts
event.outdated(cursors)  # only what differs from the states you pass
event.matches(account_id, type_name, state)  # True if it announces that state
```

`matches` exists because RFC 8620 §7.1 notes a notification can arrive while
your own `/set` is still in flight. Pass the `newState` your `/set` answered
with: if that is the state announced, the change is your own write. Without it
you re-fetch what you just wrote.

### `ping`, and why it decides your timeout

An event source is idle by design. Ask for pings and the server promises traffic
on a schedule; ask for none and a healthy connection may legitimately say
nothing for hours.

The client's read deadline follows from that. With `ping=0` there is no deadline
at all - waiting indefinitely is what "notify me when something changes" means.
With a ping requested, the deadline is the interval plus slack.

**Do not ask for a short interval expecting a short deadline.** RFC 8620 §7.3
lets a server set a minimum of up to 30 seconds and round your request up to it -
Stalwart clamps to exactly that - so the client allows for the clamp. Anything
from 30 to 300 seconds is honoured verbatim by every conformant server.

Also: a server sends nothing on connect. On a quiet account the first traffic is
the first ping, up to 30 seconds later. If you are testing this, do not read the
silence as a broken connection.

### `closeafter=state`

```python
source = EventSourceClient(client, close_after_state=True, ping=30)
```

The server ends the response after one state event. This exists because some
proxies buffer a stream until it completes and would otherwise hold every
notification back indefinitely. `listen()` treats the end as success and
reconnects.

## Push subscriptions

The server `POST`s to a URL you control. Three steps, and the middle one is the
security property:

```python
from jmap.push import new_subscription

with client.batch() as batch:
    created = batch.core.push_subscription.set(
        create={
            "s1": new_subscription(
                device_client_id="my-app-on-this-device",
                url="https://push.example.com/hook/abc",
                types=["Email", "EmailDelivery"],
            )
        }
    )
```

`new_subscription` checks what it is given before the server has to: `types` is
a list of names - one name on its own is refused rather than sent as a string -
`keys` may be a mapping or a `PushKeys` model, and `expires` must be a UTCDate,
`Z` and all (`2026-10-01T00:00:00Z`). `renewal_update` checks its `expires` the
same way.

1. You create the subscription.
2. The server immediately `POST`s a `PushVerification` to that URL and sends
   **nothing else** until you echo its code back. This is what stops anyone
   pointing a firehose at a URL they do not own.
3. You update the subscription with the code:

```python
from jmap.push import verification_update

with client.batch() as batch:
    batch.core.push_subscription.set(update={subscription_id: verification_update(code)})
```

The code can arrive *before* the `/set` response that created the subscription,
as RFC 8620 §7.2.3 warns, so match codes by subscription id, not by order.
`PendingVerification` records each code as it lands and hands it over once you
know the id, whichever of the two comes first.

Note the two id-like values. `device_client_id` is *yours* - stable for this
installation - and `mine(subscriptions, device_client_id)` finds your own
subscriptions among those the credentials can see. `PushSubscription/get` never
returns `url` or `keys`, so those properties cannot be requested at all; the
library rejects asking for them rather than letting the whole call earn
`forbidden`.

Subscriptions expire. `expires` is a hint the server may shorten, and
`needs_recreating` / `renewal_update` handle the two cases.

### Encrypted payloads (VAPID)

RFC 9749. If the server advertises `urn:ietf:params:jmap:webpush-vapid`, it
publishes a public key you can pin, and payloads can be encrypted so the push
service in the middle learns nothing:

```python
from jmap.push import application_server_key

key = application_server_key(client.session)
```

Reading the key needs no extra. Decrypting the payloads (RFC 8291) is your push
endpoint's job rather than this library's; `jmaplib[push]` installs
`cryptography`, which that needs. Check `vapid_key_rotated` before trusting a
cached key - a rotation means re-subscribing.

## WebSocket

RFC 8887, one connection carrying both method calls and push. The URL is
advertised in the session:

```python
from jmap.capabilities.push import WEBSOCKET_URN, WebSocketCapability

capability = WebSocketCapability.of(client.session.capability_value(WEBSOCKET_URN))
print(capability.url, capability.supports_push, capability.is_secure)
```

The library's part is the protocol rather than the socket.
`jmap.push.WebSocketProtocol` frames requests and the push enable and disable
messages, and classifies whatever comes back: a response, a request-level error
or a state change. Responses may arrive out of order (RFC 8887 §4.3.2), so it
matches them to requests by id rather than by arrival, and it keeps the latest
`pushState`, which lets a reconnect catch up in one exchange instead of a
`/changes` call per type. Carry the frames over any WebSocket client that
negotiates the `jmap` subprotocol (`jmap.push.SUBPROTOCOL`); `jmaplib[ws]`
installs `httpx-ws` for that.

`is_secure` is worth checking before you send credentials over it. RFC 8887 §4.2
requires TLS; a `ws://` URL from an `https://` session is a downgrade and worth
refusing.

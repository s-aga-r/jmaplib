"""Uploading and downloading blobs (RFC 8620 §6).

Blobs are the one part of JMAP that is not JMAP: they move over plain HTTP to
URLs the Session advertises as RFC 6570 templates, not as method calls. So there
is no ``using``, no batching and no back-references here - just a POST and a GET.

Two things are worth getting right.

**The size check belongs before the upload, not after.** ``maxSizeUpload`` is
advertised, so a 60 MB attachment against a 50 MB limit can fail in microseconds
instead of after streaming 60 MB and being refused.

**Download is a template, not a URL.** ``downloadUrl`` carries ``accountId``,
``blobId``, ``type`` and ``name``, and RFC 6570 level 1 percent-encodes *all*
reserved characters - including ``/``. That matters: a blob id or filename
containing a slash must not escape its path segment.

``name`` is a suggestion to the browser, not an identifier: the server puts it in
``Content-Disposition``. Nothing looks it up by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmap.core.errors import CapabilityFieldError
from jmap.core.uritemplate import expand
from jmap.models.base import JMAPModel

if TYPE_CHECKING:
    from jmap.core.ids import Id
    from jmap.core.limits import Limits
    from jmap.core.session import Session

#: RFC 8620 §6.1. What the server returns for a successful upload.
DEFAULT_UPLOAD_TYPE = "application/octet-stream"


class UploadResult(JMAPModel):
    """The server's answer to a blob upload (RFC 8620 §6.1).

    ``type`` is what the server *decided*, which need not be what was offered:
    servers may sniff the content and correct it, and the corrected value is the
    one to quote when referencing the blob later.
    """

    account_id: str | None = None
    blob_id: str | None = None
    type: str | None = None
    size: int | None = None


def check_upload_size(size: int, limits: Limits) -> None:
    """Refuse an upload the server has already said it will not accept.

    Checked before sending because the alternative is discovering it after the
    whole body has gone over the wire.
    """
    if size > limits.max_size_upload:
        raise CapabilityFieldError(
            "urn:ietf:params:jmap:core", "maxSizeUpload", limits.max_size_upload, size
        )


def upload_url(session: Session, account_id: Id) -> str:
    """The endpoint to POST a blob to, for one account."""
    return expand(session.upload_url, accountId=account_id)


def download_url(
    session: Session,
    account_id: Id,
    blob_id: str,
    *,
    name: str = "download",
    content_type: str = DEFAULT_UPLOAD_TYPE,
) -> str:
    """The URL to GET a blob from.

    ``name`` becomes the ``Content-Disposition`` filename and ``content_type``
    the ``Content-Type``; neither identifies the blob, which is addressed solely
    by ``blob_id``.
    """
    return expand(
        session.download_url,
        accountId=account_id,
        blobId=blob_id,
        name=name,
        type=content_type,
    )


def upload_headers(content_type: str | None = None) -> dict[str, str]:
    """Headers for a blob upload.

    The type is a hint. RFC 8620 §6.1 lets the server override it, and
    ``application/octet-stream`` is the honest default when the caller does not
    know - it says "bytes" rather than guessing wrong.
    """
    return {"Content-Type": content_type or DEFAULT_UPLOAD_TYPE}


def parse_upload(payload: Any) -> UploadResult:
    """Turn an upload response body into an :class:`UploadResult`."""
    return UploadResult.model_validate(payload)

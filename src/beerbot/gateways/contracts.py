"""Provider-independent contracts for inbound messaging adapters.

The first-party app will call the same application core directly. Third-party
gateways normalize provider events into these contracts, but do not own users,
workspace state, agent sessions, or activity semantics.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GatewayCapabilities:
    """Transport features an adapter can faithfully preserve."""

    group_conversations: bool = False
    direct_conversations: bool = True
    mentions: bool = False
    replies: bool = False
    images: bool = False
    video: bool = False
    reactions: bool = False
    streaming: bool = False


@dataclass(frozen=True)
class ExternalActorRef:
    """Provider assertion about a sender, prior to global-person resolution."""

    gateway_type: str
    issuer_key: str
    subject_key: str
    display_name: str
    avatar_url: str | None = None


@dataclass(frozen=True)
class CanonicalMention:
    """Provider-native mention represented as an external identity reference."""

    actor: ExternalActorRef
    start: int | None = None
    length: int | None = None


@dataclass(frozen=True)
class CanonicalAttachment:
    """Media or structured content referenced by a gateway event."""

    kind: str
    url: str | None = None
    preview_url: str | None = None
    content_type: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundEnvelope:
    """Normalized transport event before workspace and identity resolution."""

    gateway_type: str
    route_key: str
    external_message_id: str
    external_conversation_id: str
    actor: ExternalActorRef
    occurred_at: datetime
    text: str | None = None
    mentions: tuple[CanonicalMention, ...] = ()
    attachments: tuple[CanonicalAttachment, ...] = ()
    reply_to_external_message_id: str | None = None
    raw_metadata: dict[str, object] = field(default_factory=dict)

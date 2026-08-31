"""GroupMe event normalization without changing the legacy callback path."""

from datetime import UTC, datetime

from ..models import GroupMeMessage
from ..routing import GROUPME_GATEWAY_TYPE, groupme_route_key
from .contracts import (
    CanonicalAttachment,
    CanonicalMention,
    ExternalActorRef,
    GatewayCapabilities,
    InboundEnvelope,
)


class GroupMeInboundAdapter:
    """Convert GroupMe callback models into provider-independent envelopes."""

    gateway_type = GROUPME_GATEWAY_TYPE
    capabilities = GatewayCapabilities(
        group_conversations=True,
        direct_conversations=False,
        mentions=True,
        replies=True,
        images=True,
        video=True,
    )

    @staticmethod
    def _actor(user_id: str, name: str, avatar_url: str | None = None) -> ExternalActorRef:
        return ExternalActorRef(
            gateway_type=GROUPME_GATEWAY_TYPE,
            issuer_key="groupme",
            subject_key=user_id,
            display_name=name,
            avatar_url=avatar_url,
        )

    def normalize(self, message: GroupMeMessage) -> InboundEnvelope:
        mentions: list[CanonicalMention] = []
        attachments: list[CanonicalAttachment] = []
        reply_to: str | None = None

        for attachment in message.attachments:
            if attachment.type == "mentions":
                for index, user_id in enumerate(attachment.user_ids):
                    start = length = None
                    display_name = f"User {user_id[-4:]}"
                    if index < len(attachment.loci):
                        start, length = attachment.loci[index]
                        if message.text and 0 <= start < len(message.text):
                            display_name = message.text[start : start + length].lstrip("@").strip()
                    mentions.append(
                        CanonicalMention(
                            actor=self._actor(user_id, display_name),
                            start=start,
                            length=length,
                        )
                    )
            elif attachment.type == "reply" and attachment.reply_id:
                reply_to = attachment.reply_id
            else:
                attachments.append(
                    CanonicalAttachment(
                        kind=attachment.type,
                        url=attachment.url,
                        preview_url=attachment.preview_url,
                    )
                )

        return InboundEnvelope(
            gateway_type=GROUPME_GATEWAY_TYPE,
            route_key=groupme_route_key(message.group_id),
            external_message_id=message.id,
            external_conversation_id=message.group_id,
            actor=self._actor(message.user_id, message.name, message.avatar_url),
            occurred_at=datetime.fromtimestamp(message.created_at, tz=UTC),
            text=message.text,
            mentions=tuple(mentions),
            attachments=tuple(attachments),
            reply_to_external_message_id=reply_to,
            raw_metadata={"sender_type": message.sender_type},
        )

"""Tests for provider-independent gateway contracts."""

from datetime import UTC, datetime

from src.beerbot.gateways.groupme import GroupMeInboundAdapter
from src.beerbot.models import GroupMeAttachment, GroupMeMessage
from src.beerbot.routing import (
    groupme_connection_id,
    groupme_route_id,
    groupme_route_key,
    groupme_workspace_id,
)


def test_groupme_route_identifiers_are_stable():
    assert groupme_workspace_id("123") == "groupme:123"
    assert groupme_connection_id("123") == "groupme-bot:123"
    assert groupme_route_id("123") == "groupme-route:123"
    assert groupme_route_key("123") == "123"


def test_groupme_adapter_normalizes_mentions_replies_and_media():
    text = "+1 beer @Bob"
    message = GroupMeMessage(
        attachments=[
            GroupMeAttachment(type="mentions", user_ids=["user-2"], loci=[[8, 4]]),
            GroupMeAttachment(type="reply", reply_id="message-0"),
            GroupMeAttachment(
                type="image",
                url="https://example.com/photo.jpg",
                preview_url="https://example.com/preview.jpg",
            ),
        ],
        avatar_url="https://example.com/alice.jpg",
        created_at=1_703_700_000,
        group_id="group-1",
        id="message-1",
        name="Alice",
        sender_id="user-1",
        sender_type="user",
        text=text,
        user_id="user-1",
    )

    envelope = GroupMeInboundAdapter().normalize(message)

    assert envelope.gateway_type == "groupme"
    assert envelope.route_key == "group-1"
    assert envelope.external_conversation_id == "group-1"
    assert envelope.external_message_id == "message-1"
    assert envelope.actor.subject_key == "user-1"
    assert envelope.actor.issuer_key == "groupme"
    assert envelope.occurred_at == datetime.fromtimestamp(1_703_700_000, tz=UTC)
    assert envelope.reply_to_external_message_id == "message-0"
    assert envelope.mentions[0].actor.subject_key == "user-2"
    assert envelope.mentions[0].actor.display_name == "Bob"
    assert envelope.attachments[0].kind == "image"


def test_groupme_adapter_declares_transport_capabilities():
    capabilities = GroupMeInboundAdapter.capabilities

    assert capabilities.group_conversations is True
    assert capabilities.mentions is True
    assert capabilities.replies is True
    assert capabilities.images is True
    assert capabilities.video is True
    assert capabilities.streaming is False

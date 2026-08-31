"""Pydantic models for GroupMe messages and internal data."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DrinkType(str, Enum):
    """Types of drinks we track."""

    BEER = "beer"
    WINE = "wine"
    COCKTAIL = "cocktail"
    CLAW = "claw"  # White Claw, Truly, alcoholic seltzers

    @classmethod
    def from_string(cls, s: str) -> "DrinkType":
        """Parse drink type from string, defaulting to BEER."""
        mapping = {
            "beer": cls.BEER,
            "beers": cls.BEER,
            "wine": cls.WINE,
            "wines": cls.WINE,
            "cocktail": cls.COCKTAIL,
            "cocktails": cls.COCKTAIL,
            "claw": cls.CLAW,
            "claws": cls.CLAW,
            "seltzer": cls.CLAW,
            "seltzers": cls.CLAW,
        }
        return mapping.get(s.lower(), cls.BEER)


class GroupMeAttachment(BaseModel):
    """Attachment in a GroupMe message."""

    type: str
    url: Optional[str] = None
    preview_url: Optional[str] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    name: Optional[str] = None
    user_ids: list[str] = Field(default_factory=list)  # For mentions
    loci: list[list[int]] = Field(default_factory=list)  # For mentions: [[start, length], ...]
    reply_id: Optional[str] = None  # For reply attachments: direct parent message ID
    base_reply_id: Optional[str] = None  # For reply attachments: root of thread


class GroupMeMessage(BaseModel):
    """Incoming message from GroupMe callback."""

    attachments: list[GroupMeAttachment] = Field(default_factory=list)
    avatar_url: Optional[str] = None
    created_at: int  # Unix timestamp
    group_id: str
    id: str  # message_id
    name: str  # sender's display name
    sender_id: str
    sender_type: str  # "user", "bot", "system"
    text: Optional[str] = None
    user_id: str


class User(BaseModel):
    """User who has logged beers."""

    id: int
    groupme_user_id: str
    name: str
    avatar_url: Optional[str] = None
    person_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class Drink(BaseModel):
    """Individual drink log entry."""

    id: int
    user_id: int
    group_id: str
    quantity: int
    logged_at: datetime
    message_id: Optional[str] = None
    split_the_g: int = 0
    drink_type: DrinkType = DrinkType.BEER


# Backward compatibility alias
Beer = Drink


class UserStats(BaseModel):
    """Statistics for a single user."""

    name: str
    total_beers: int
    last_beer_at: Optional[datetime] = None


class GroupStats(BaseModel):
    """Statistics for a group."""

    total_beers: int
    unique_drinkers: int
    user_stats: list[UserStats]
    period_description: str  # "all time", "today", "this week"
    drink_type_counts: dict[str, int] = {}  # {"beer": 10, "wine": 5, ...}


class SplitGUserStats(BaseModel):
    """Split-the-G statistics for a user."""

    name: str
    split_the_g_count: int
    last_split_at: Optional[datetime] = None


class SplitGGroupStats(BaseModel):
    """Split-the-G statistics for a group."""

    total_splits: int
    unique_splitters: int
    user_stats: list[SplitGUserStats]


class UserDebt(BaseModel):
    """Simple debt tracking for a user (how many beers they owe the group)."""

    id: int
    user_id: int
    group_id: str
    amount: int
    updated_at: datetime


class DebtLeaderboardEntry(BaseModel):
    """Entry in the debt leaderboard."""

    name: str
    amount: int


class Group(BaseModel):
    """Registered GroupMe group with bot mapping."""

    group_id: str
    bot_id: str
    name: Optional[str] = None
    workspace_id: Optional[str] = None
    created_at: datetime


class Workspace(BaseModel):
    """Provider-independent tenant and data boundary."""

    id: str
    name: str
    timezone: str = "America/New_York"
    settings: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class GatewayConnection(BaseModel):
    """Credentialed messaging-provider installation."""

    id: str
    gateway_type: str
    name: Optional[str] = None
    credential_ref: Optional[str] = None
    config: dict[str, object] = Field(default_factory=dict)
    status: str = "active"
    created_at: datetime
    updated_at: datetime


class GatewayRoute(BaseModel):
    """Opaque provider route mapped to a workspace and connection."""

    id: str
    gateway_type: str
    route_key: str
    workspace_id: str
    gateway_connection_id: str
    external_conversation_id: Optional[str] = None
    name: Optional[str] = None
    config: dict[str, object] = Field(default_factory=dict)
    status: str = "active"
    created_at: datetime
    updated_at: datetime


class WorkspaceContext(BaseModel):
    """Resolved tenant and gateway context for one inbound conversation."""

    workspace: Workspace
    connection: GatewayConnection
    route: GatewayRoute


class Person(BaseModel):
    """Global activity subject, optionally claimed by a first-party account."""

    id: str
    display_name: str
    avatar_url: Optional[str] = None
    status: str = "provisional"
    canonical_person_id: Optional[str] = None
    settings: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Account(BaseModel):
    """Optional first-party login attached to one global person."""

    id: str
    person_id: str
    status: str = "active"
    settings: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ExternalIdentity(BaseModel):
    """Provider identity linked to a global person."""

    id: str
    gateway_type: str
    issuer_key: str
    subject_key: str
    person_id: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    assurance: str = "gateway_asserted"
    status: str = "active"
    metadata: dict[str, object] = Field(default_factory=dict)
    first_seen_at: datetime
    last_seen_at: datetime


class WorkspaceMembership(BaseModel):
    """A global person's contextual identity and permissions in a workspace."""

    id: str
    workspace_id: str
    person_id: str
    display_name: str
    role: str = "member"
    status: str = "active"
    settings: dict[str, object] = Field(default_factory=dict)
    joined_at: datetime
    updated_at: datetime


class IdentityContext(BaseModel):
    """Read-only shadow resolution of a gateway identity in one workspace."""

    person: Person
    external_identity: ExternalIdentity
    membership: WorkspaceMembership

"""Pydantic models for GroupMe messages and internal data."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


@dataclass
class VisionResult:
    """Result from image analysis."""

    beer_count: int = 0
    split_the_g_count: int = 0


class GroupMeAttachment(BaseModel):
    """Attachment in a GroupMe message."""

    type: str
    url: Optional[str] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    name: Optional[str] = None
    user_ids: list[str] = Field(default_factory=list)  # For mentions
    loci: list[list[int]] = Field(default_factory=list)  # For mentions: [[start, length], ...]


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
    created_at: datetime
    updated_at: datetime


class Beer(BaseModel):
    """Individual beer log entry."""

    id: int
    user_id: int
    group_id: str
    quantity: int
    logged_at: datetime
    message_id: Optional[str] = None
    split_the_g: int = 0


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

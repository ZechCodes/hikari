#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Nekoka.tt 2019
#
# This file is part of Hikari.
#
# Hikari is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Hikari is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Hikari. If not, see <https://www.gnu.org/licenses/>.
"""
Channel models.
"""
from __future__ import annotations

__all__ = ()

import abc
import dataclasses
import enum
import typing

from hikari.model import base
from hikari.model import overwrite
from hikari.model import user
from hikari import utils


class ChannelType(enum.IntEnum):
    """
    Type of a channel.
    """

    GUILD_TEXT = 0
    DM = 1
    GUILD_VOICE = 2
    GROUP_DM = 3
    GUILD_CATEGORY = 4
    GUILD_NEWS = 5
    GUILD_STORE = 6


@dataclasses.dataclass()
class Channel(base.Snowflake, abc.ABC):
    """
    A generic type of channel.
    """

    __slots__ = ()

    @property
    @abc.abstractmethod
    def type(self) -> ChannelType:
        """The type of channel."""


@dataclasses.dataclass()
class GuildChannel(Channel, abc.ABC):
    """
    A channel that belongs to a guild.
    """

    __slots__ = ("guild_id", "position", "permission_overwrites", "name", "nsfw", "parent_id")

    #: ID of the guild that owns this channel.
    guild_id: int
    #: The position of the channel in the channel list.
    position: int
    #: A list of permission overwrites for this channel.
    permission_overwrites: typing.List[overwrite.Overwrite]
    #: The name of the channel.
    name: str
    #: Whether the channel is flagged as being NSFW or not.
    nsfw: bool
    #: The ID of the parent category, if there is one.
    parent_id: typing.Optional[int]


@dataclasses.dataclass()
class GuildTextChannel(GuildChannel):
    """
    A text channel.
    """

    __slots__ = ("topic", "rate_limit_per_user", "last_message_id")

    #: The channel topic.
    topic: typing.Optional[str]
    #: How many seconds a user has to wait before sending consecutive messages.
    rate_limit_per_user: int
    #: The optional ID of the last message to be sent.
    last_message_id: typing.Optional[int]

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.GUILD_TEXT

    @classmethod
    def from_dict(cls: GuildTextChannel, payload: utils.DiscordObject, state) -> Channel:
        return cls(
            state,
            id=payload["id"],
            guild_id=payload["guild_id"],
            position=payload["position"],
            permission_overwrites=overwrite.Overwrite.from_dict(payload["permission_overwrites"], state),
            name=payload["name"],
            nsfw=payload["nsfw"],
            parent_id=payload.get("parent_id"),
            topic=payload.get("topic"),
            rate_limit_per_user=payload.get("rate_limit_per_user"),
            last_message_id=payload["last_message_id"],
        )


@dataclasses.dataclass()
class DMChannel(Channel):
    """
    A DM channel between users.
    """

    __slots__ = ("last_message_id", "recipients")

    #: The optional ID of the last message to be sent.
    last_message_id: typing.Optional[int]
    #: List of recipients in the DM chat.
    recipients: typing.List[user.User]

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.DM

    @classmethod
    def from_dict(cls: DMChannel, payload: utils.DiscordObject, state) -> Channel:
        return cls(
            state,
            id=payload["id"],
            last_message_id=payload["last_message_id"],
            recipients=[user.User.from_dict(recipient, state) for recipient in payload["recipients"]],
        )


@dataclasses.dataclass()
class GuildVoiceChannel(GuildChannel):
    """
    A voice channel within a guild.
    """

    __slots__ = ("bitrate", "user_limit")

    #: Bit-rate of the voice channel.
    bitrate: int
    #: The max number of users in the voice channel, or `0` if there is no limit.
    user_limit: int

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.GUILD_VOICE

    @classmethod
    def from_dict(cls: GuildVoiceChannel, payload: utils.DiscordObject, state) -> Channel:
        return cls(state, id=payload["id"], bitrate=payload["bitrate"], user_limit=payload["user_limit"])


@dataclasses.dataclass()
class GroupDMChannel(DMChannel):
    """
    A DM group chat.
    """

    __slots__ = ("icon", "name", "owner_id", "owner_application_id")

    #: Hash of the icon for the chat, if there is one.
    icon: typing.Optional[bytes]
    #: Name for the chat, if there is one.
    name: typing.Optional[str]
    #: ID of the owner of the chat.
    owner_id: int
    #: If the chat was made by a bot, this will be the application ID of the bot that made it. For all other cases it
    #: will be `None`.
    owner_application_id: typing.Optional[int]

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.GROUP_DM

    @classmethod
    def from_dict(cls: GroupDMChannel, payload: utils.DiscordObject, state) -> Channel:
        return cls(
            state,
            id=payload["id"],
            last_message_id=payload["last_message_id"],
            recipients=[user.User.from_dict(recipient, state) for recipient in payload["recipients"]],
            icon=payload.get("icon").encode() if payload["icon"] else None,
            name=payload.get("name"),
            owner_application_id=payload.get("application_id"),
            owner_id=payload["owner_id"],
        )


@dataclasses.dataclass()
class GuildCategory(GuildChannel):
    """
    A category within a guild.
    """

    __slots__ = ()

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.GUILD_CATEGORY


@dataclasses.dataclass()
class GuildNewsChannel(GuildChannel):
    """
    A channel for news topics within a guild.
    """

    __slots__ = ("topic", "last_message_id")

    #: The channel topic.
    topic: typing.Optional[str]
    #: The optional ID of the last message to be sent.
    last_message_id: typing.Optional[int]

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.GUILD_NEWS


@dataclasses.dataclass()
class GuildStoreChannel(GuildChannel):
    """
    A store channel for selling of games within a guild.
    """

    __slots__ = ()

    @property
    def type(self) -> ChannelType:
        """The type of the channel."""
        return ChannelType.GUILD_STORE


def channel_from_dict(payload: utils.DiscordObject, state) -> Channel:
    raw_channel_type = payload["type"]
    channel_type = getattr(ChannelType, raw_channel_type, raw_channel_type)

    if channel_type == ChannelType.GUILD_TEXT:
        return GuildTextChannel.from_dict(payload, state)
    if channel_type == ChannelType.DM:
        return DMChannel.from_dict(payload, state)
    if channel_type == ChannelType.GUILD_VOICE:
        return GuildVoiceChannel.from_dict(payload, state)
    if channel_type == ChannelType.GROUP_DM:
        return GroupDMChannel.from_dict(payload, state)
    if channel_type == ChannelType.GUILD_CATEGORY:
        return GuildCategory.from_dict(payload, state)
    if channel_type == ChannelType.GUILD_NEWS:
        return GuildNewsChannel.from_dict(payload, state)
    if channel_type == ChannelType.GUILD_STORE:
        return GuildStoreChannel.from_dict(payload, state)

    raise TypeError(f"Invalid channel type {channel_type}")

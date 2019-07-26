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
A role within a guild.
"""
__all__ = ("Role",)

import dataclasses

from hikari.model import base
from hikari.model import color as _color
from hikari.model import permission as _permission
from hikari.utils import maps


@dataclasses.dataclass()
class Role(base.SnowflakeMixin):
    """
    Representation of a role within a guild.
    """

    __slots__ = ("id", "name", "color", "hoist", "position", "permissions", "managed", "mentionable")

    #: The ID of the role.
    id: int
    #: The name of the role.
    name: str
    #: The color of the role.
    color: _color.Color
    #: Whether the role will be hoisted (show as a separate list in the member list)
    hoist: bool
    #: The position of the role.
    position: int
    #: The permissions for the role.
    permissions: _permission.Permission
    #: True if the role is created by an integration or by adding a bot to the server, or False otherwise.
    managed: bool
    #: True if you can mention this role and thus ping all members in that role at once, False if you can not.
    mentionable: bool

    @staticmethod
    def from_dict(payload):
        return Role(
            id=maps.get_from_map_as(payload, "id", int),
            name=maps.get_from_map_as(payload, "name", str),
            color=maps.get_from_map_as(payload, "color", _color.Color),
            hoist=maps.get_from_map_as(payload, "hoist", bool),
            position=maps.get_from_map_as(payload, "position", int),
            permissions=maps.get_from_map_as(payload, "permissions", _permission.Permission),
            managed=maps.get_from_map_as(payload, "managed", bool),
            mentionable=maps.get_from_map_as(payload, "mentionable", bool),
        )
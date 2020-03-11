#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Nekokatt 2019-2020
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
Auto-requesting chunking options.
"""
__all__ = ["ChunkMode"]

import enum


class ChunkMode(enum.IntEnum):
    """
    Options for automatically retrieving all guild members in a guild.
    """

    #: Never autochunk guilds.
    NEVER = 0
    #: Autochunk guild members only.
    MEMBERS = 1
    #: Autochunk guild members and their presences.
    MEMBERS_AND_PRESENCES = 2
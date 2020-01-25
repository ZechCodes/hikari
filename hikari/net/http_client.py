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
Implementation of a basic HTTP client that uses aiohttp to interact with the
V6 Discord API.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import email.utils
import json
import typing
import uuid

import aiohttp.typedefs

from hikari.internal_utilities import assertions
from hikari.internal_utilities import containers
from hikari.internal_utilities import conversions
from hikari.internal_utilities import storage
from hikari.internal_utilities import transformations
from hikari.internal_utilities import unspecified
from hikari.net import errors
from hikari.net import base_http_client
from hikari.net import ratelimits
from hikari.net import routes

if typing.TYPE_CHECKING:
    import ssl

    from hikari.internal_utilities import type_hints


class HTTPClient(base_http_client.BaseHTTPClient):
    """
    A RESTful client to allow you to interact with the Discord API.
    """

    _AUTHENTICATION_SCHEMES = ("Bearer", "Bot")

    def __init__(
        self,
        *,
        base_url="https://discordapp.com/api/v6",
        allow_redirects: bool = False,
        connector: aiohttp.BaseConnector = None,
        proxy_headers: aiohttp.typedefs.LooseHeaders = None,
        proxy_auth: aiohttp.BasicAuth = None,
        proxy_url: str = None,
        ssl_context: ssl.SSLContext = None,
        verify_ssl: bool = True,
        timeout: float = None,
        json_deserialize=json.loads,
        json_serialize=json.dumps,
        token,
    ):
        super().__init__(
            allow_redirects=allow_redirects,
            connector=connector,
            proxy_headers=proxy_headers,
            proxy_auth=proxy_auth,
            proxy_url=proxy_url,
            ssl_context=ssl_context,
            verify_ssl=verify_ssl,
            timeout=timeout,
            json_serialize=json_serialize,
        )
        self.base_url = base_url
        self.global_ratelimiter = ratelimits.ManualRateLimiter()
        self.json_serialize = json_serialize
        self.json_deserialize = json_deserialize
        self.ratelimiter = ratelimits.HTTPBucketRateLimiterManager()
        self.ratelimiter.start()

        if token is not None and not token.startswith(self._AUTHENTICATION_SCHEMES):
            this_type = type(self).__name__
            auth_schemes = " or ".join(self._AUTHENTICATION_SCHEMES)
            raise RuntimeError(f"Any token passed to {this_type} should begin with {auth_schemes}")
        self.token = token

    async def close(self):
        with contextlib.suppress(Exception):
            self.ratelimiter.close()
        with contextlib.suppress(Exception):
            await self.client_session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, ex_t, ex, ex_tb):
        await self.close()

    async def _request(
        self,
        compiled_route,
        *,
        headers=None,
        query=None,
        form_body=None,
        json_body: type_hints.Nullable[typing.Union[containers.JSONObject, containers.JSONArray]] = None,
        reason=None,
        re_seekable_resources=containers.EMPTY_COLLECTION,
        **kwargs,
    ) -> typing.Union[containers.JSONObject, containers.JSONArray]:
        future, real_hash = self.ratelimiter.acquire(compiled_route)
        request_headers = {"X-RateLimit-Precision": "millisecond"}

        if self.token is not None:
            request_headers["Authorization"] = self.token

        if reason is not None:
            request_headers["X-Audit-Log-Reason"] = reason

        if headers is not None:
            request_headers.update(headers)

        backoff = ratelimits.ExponentialBackOff()

        while True:
            # If we are uploading files with io objects in a form body, we need to reset the seeks to 0 to ensure
            # we can re-read the buffer.
            for resource in re_seekable_resources:
                resource.seek(0)

            # Aids logging when lots of entries are being logged at once by matching a unique UUID
            # between the request and response
            request_uuid = uuid.uuid4()

            await asyncio.gather(future, self.global_ratelimiter.acquire())

            if json_body is not None:
                body_type = "json"
            elif form_body is not None:
                body_type = "form"
            else:
                body_type = "None"

            self.logger.debug(
                "%s send to %s headers=%s query=%s body_type=%s body=%s",
                request_uuid,
                compiled_route,
                request_headers,
                query,
                body_type,
                json_body if json_body is not None else form_body,
            )

            async with super()._request(
                compiled_route.method,
                compiled_route.create_url(self.base_url),
                headers=request_headers,
                json=json_body,
                params=query,
                data=form_body,
                **kwargs,
            ) as resp:
                raw_body = await resp.read()
                headers = resp.headers

                self.logger.debug(
                    "%s recv from %s status=%s reason=%s headers=%s body=%s",
                    request_uuid,
                    compiled_route,
                    resp.status,
                    resp.reason,
                    headers,
                    raw_body,
                )

                limit = int(headers.get("X-RateLimit-Limit", "1"))
                remaining = int(headers.get("X-RateLimit-Remaining", "1"))
                bucket = headers.get("X-RateLimit-Bucket", "None")
                reset = float(headers.get("X-RateLimit-Reset", "0"))
                reset_date = datetime.datetime.fromtimestamp(reset, tz=datetime.timezone.utc)
                now_date = email.utils.parsedate_to_datetime(headers["Date"])
                content_type = resp.headers["Content-Type"]

                status = resp.status

                if status == 204:
                    body = None
                if content_type == "application/json":
                    body = self.json_deserialize(raw_body)
                elif content_type == "text/plain" or content_type == "text/html":
                    await self._handle_bad_response(
                        backoff,
                        f"Received unexpected response of type {content_type}",
                        compiled_route,
                        raw_body.decode(),
                        status,
                    )
                    continue
                else:
                    body = None

            self.ratelimiter.update_rate_limits(
                compiled_route, real_hash, bucket, remaining, limit, now_date, reset_date,
            )

            if status == 429:
                # We are being rate limited.
                if body["global"]:
                    retry_after = float(body["retry_after"]) / 1_000
                    self.global_ratelimiter.throttle(retry_after)
                continue

            if status >= 400:
                try:
                    message = body["message"]
                    code = int(body["code"])
                except (ValueError, KeyError):
                    message, code = "", -1

                if status == 400:
                    raise errors.BadRequestHTTPError(compiled_route, message, code)
                elif status == 401:
                    raise errors.UnauthorizedHTTPError(compiled_route, message, code)
                elif status == 403:
                    raise errors.ForbiddenHTTPError(compiled_route, message, code)
                elif status == 404:
                    raise errors.NotFoundHTTPError(compiled_route, message, code)
                elif status < 500:
                    raise errors.ClientHTTPError(f"{status}: {resp.reason}", compiled_route, message, code)

                await self._handle_bad_response(
                    backoff, "Received a server error response", compiled_route, message, status
                )
                continue

            return body

    async def _handle_bad_response(self, backoff, reason, route, message, status):
        try:
            next_sleep = next(backoff)
            self.logger.warning("received a server error response, backing off for %ss and trying again", next_sleep)
            await asyncio.sleep(next_sleep)
        except asyncio.TimeoutError:
            raise errors.ServerHTTPError(reason, route, message, status)

    async def get_gateway(self) -> str:
        """
        Returns:
            A static URL to use to connect to the gateway with.

        Note:
            Users are expected to attempt to cache this result.
        """
        result = await self._request(routes.GATEWAY.compile(self.GET))
        return result["url"]

    async def get_gateway_bot(self) -> containers.JSONObject:
        """
        Returns:
            An object containing a `url` to connect to, an :class:`int` number of shards recommended to use
            for connecting, and a `session_start_limit` object.

        Note:
            Unlike `get_gateway`, this requires a valid token to work.
        """
        return await self._request(routes.GATEWAY_BOT.compile(self.GET))

    async def get_guild_audit_log(
        self,
        guild_id: str,
        *,
        user_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        action_type: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Get an audit log object for the given guild.

        Args:
            guild_id:
                The guild ID to look up.
            user_id:
                Optional user ID to filter by.
            action_type:
                Optional action type to look up.
            limit:
                Optional limit to apply to the number of records. Defaults to 50. Must be between 1 and 100 inclusive.

        Returns:
            An audit log object.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack the given permissions to view an audit log.
            hikari.net.errors.NotFoundError:
                If the guild does not exist.
        """
        query = {}
        transformations.put_if_specified(query, "user_id", user_id)
        transformations.put_if_specified(query, "action_type", action_type)
        transformations.put_if_specified(query, "limit", limit)
        route = routes.GUILD_AUDIT_LOGS.compile(self.GET, guild_id=guild_id)
        return await self._request(route, query=query)

    async def get_channel(self, channel_id: str) -> containers.JSONObject:
        """
        Get a channel object from a given channel ID.

        Args:
            channel_id:
                The channel ID to look up.

        Returns:
            The channel object that has been found.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel does not exist.
        """
        route = routes.CHANNEL.compile(self.GET, channel_id=channel_id)
        return await self._request(route)

    async def modify_channel(  # lgtm [py/similar-function]
        self,
        channel_id: str,
        *,
        position: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        topic: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        nsfw: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        rate_limit_per_user: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        bitrate: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        user_limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        permission_overwrites: type_hints.NotRequired[typing.Sequence[containers.JSONObject]] = unspecified.UNSPECIFIED,
        parent_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Update one or more aspects of a given channel ID.

        Args:
            channel_id:
                The channel ID to update. This must be between 2 and 100 characters in length.
            position:
                An optional position to change to.
            topic:
                An optional topic to set. This is only applicable to text channels. This must be between 0 and 1024
                characters in length.
            nsfw:
                An optional flag to set the channel as NSFW or not. Only applicable to text channels.
            rate_limit_per_user:
                An optional number of seconds the user has to wait before sending another message. This will
                not apply to bots, or to members with `manage_messages` or `manage_channel` permissions. This must be
                between 0 and 21600 seconds. This only applies to text channels.
            bitrate:
                The optional bitrate in bits per second allowable for the channel. This only applies to voice channels
                and must be between 8000 and 96000 or 128000 for VIP servers.
            user_limit:
                The optional max number of users to allow in a voice channel. This must be between 0 and 99 inclusive,
                where 0 implies no limit.
            permission_overwrites:
                An optional list of permission overwrites that are category specific to replace the existing overwrites
                with.
            parent_id:
                The optional parent category ID to set for the channel.
            reason:
                An optional audit log reason explaining why the change was made.
        
        Returns:
            The channel object that has been modified.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel does not exist.
            hikari.net.errors.ForbiddenError:
                If you lack the permission to make the change.
            hikari.net.errors.BadRequestError:
                If you provide incorrect options for the corresponding channel type (e.g. a `bitrate` for a text
                channel).
        """
        payload = {}
        transformations.put_if_specified(payload, "position", position)
        transformations.put_if_specified(payload, "topic", topic)
        transformations.put_if_specified(payload, "nsfw", nsfw)
        transformations.put_if_specified(payload, "rate_limit_per_user", rate_limit_per_user)
        transformations.put_if_specified(payload, "bitrate", bitrate)
        transformations.put_if_specified(payload, "user_limit", user_limit)
        transformations.put_if_specified(payload, "permission_overwrites", permission_overwrites)
        transformations.put_if_specified(payload, "parent_id", parent_id)
        route = routes.CHANNEL.compile(self.PATCH, channel_id=channel_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def delete_close_channel(self, channel_id: str) -> None:
        """
        Delete the given channel ID, or if it is a DM, close it.
        Args:
            channel_id:
                The channel ID to delete, or the user ID of the direct message to close.

        Returns:
            Nothing, unlike what the API specifies. This is done to maintain consistency with other calls of a similar
            nature in this API wrapper.

        Warning:
            Deleted channels cannot be un-deleted. Deletion of DMs is able to be undone by reopening the DM.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel does not exist
            hikari.net.errors.ForbiddenError:
                If you do not have permission to delete the channel.
        """
        route = routes.CHANNEL.compile(self.DELETE, channel_id=channel_id)
        await self._request(route)

    async def get_channel_messages(
        self,
        channel_id: str,
        *,
        limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        after: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        before: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        around: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> typing.Sequence[containers.JSONObject]:
        """
        Retrieve message history for a given channel. If a user is provided, retrieve the DM history.

        Args:
            channel_id:
                The channel ID to retrieve messages from.
            limit:
                Optional number of messages to return. Must be between 1 and 100 inclusive, and defaults to 50 if
                unspecified.
            after:
                A message ID. If provided, only return messages sent AFTER this message.
            before:
                A message ID. If provided, only return messages sent BEFORE this message.
            around:
                A message ID. If provided, only return messages sent AROUND this message.

        Warning:
            You can only specify a maximum of one from `before`, `after`, and `around`. Specifying more than one will
            cause a :class:`hikari.net.errors.BadRequestError` to be raised.

        Note:
            If you are missing the `VIEW_CHANNEL` permission, you will receive a :class:`hikari.net.errors.ForbiddenError`.
            If you are instead missing the `READ_MESSAGE_HISTORY` permission, you will always receive zero results, and
            thus an empty list will be returned instead.

        Returns:
            A list of message objects.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack permission to read the channel.
            hikari.net.errors.BadRequestError:
                If your query is malformed, has an invalid value for `limit`, or contains more than one of `after`,
                `before` and `around`.
            hikari.net.errors.NotFoundError:
                If the given `channel_id` was not found, or the message ID provided for one of the filter arguments
                is not found.
        """
        query = {}
        transformations.put_if_specified(query, "limit", limit)
        transformations.put_if_specified(query, "before", before)
        transformations.put_if_specified(query, "after", after)
        transformations.put_if_specified(query, "around", around)
        route = routes.CHANNEL_MESSAGES.compile(self.GET, channel_id=channel_id)
        return await self._request(route, query=query)

    async def get_channel_message(self, channel_id: str, message_id: str) -> containers.JSONObject:
        """
        Get the message with the given message ID from the channel with the given channel ID.

        Args:
            channel_id:
                The channel to look in.
            message_id:
                The message to retrieve.

        Returns:
            A message object.

        Note:
            This requires the `READ_MESSAGE_HISTORY` permission to be set.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack permission to see the message.
            hikari.net.errors.NotFoundError:
                If the message ID or channel ID is not found.
        """
        route = routes.CHANNEL_MESSAGE.compile(self.GET, channel_id=channel_id, message_id=message_id)
        return await self._request(route)

    async def create_message(
        self,
        channel_id: str,
        *,
        content: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        nonce: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        tts: type_hints.NotRequired[bool] = False,
        files: type_hints.NotRequired[typing.Sequence[typing.Tuple[str, storage.FileLikeT]]] = unspecified.UNSPECIFIED,
        embed: type_hints.NotRequired[containers.JSONObject] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Create a message in the given channel or DM.

        Args:
            channel_id:
                The channel or user ID to send to.
            content:
                The message content to send.
            nonce:
                An optional ID to send for opportunistic message creation. This doesn't serve any real purpose for
                general use, and can usually be ignored.
            tts:
                If specified and `True`, then the message will be sent as a TTS message.
            files:
                If specified, this should be a list of between 1 and 5 tuples. Each tuple should consist of the
                file name, and either raw :class:`bytes` or an :class:`io.IOBase` derived object with a seek that
                points to a buffer containing said file.
            embed:
                if specified, this embed will be sent with the message.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel ID is not found.
            hikari.net.errors.BadRequestError:
                If the file is too large, the embed exceeds the defined limits, if the message content is specified and
                empty or greater than 2000 characters, or if neither of content, file or embed are specified.
            hikari.net.errors.ForbiddenError:
                If you lack permissions to send to this channel.

        Returns:
            The created message object.
        """
        form = aiohttp.FormData()

        json_payload = {"tts": tts}
        transformations.put_if_specified(json_payload, "content", content)
        transformations.put_if_specified(json_payload, "nonce", nonce)
        transformations.put_if_specified(json_payload, "embed", embed)

        form.add_field("payload_json", json.dumps(json_payload), content_type="application/json")

        re_seekable_resources = []
        if files is not unspecified.UNSPECIFIED:
            for i, (file_name, file) in enumerate(files):
                file = storage.make_resource_seekable(file)
                re_seekable_resources.append(file)
                form.add_field(f"file{i}", file, filename=file_name, content_type="application/octet-stream")

        route = routes.CHANNEL_MESSAGES.compile(self.POST, channel_id=channel_id)
        return await self._request(route, form_body=form, re_seekable_resources=re_seekable_resources)

    async def create_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """
        Add a reaction to the given message in the given channel or user DM.

        Args:
            channel_id:
                The ID of the channel to add the reaction in.
            message_id:
                The ID of the message to add the reaction in.
            emoji:
                The emoji to add. This can either be a series of unicode characters making up a valid Discord
                emoji, or it can be in the form of name:id for a custom emoji.

        Raises:
            hikari.net.errors.ForbiddenError:
                If this is the first reaction using this specific emoji on this message and you lack the `ADD_REACTIONS`
                permission. If you lack `READ_MESSAGE_HISTORY`, this may also raise this error.
            hikari.net.errors.NotFoundError:
                If the channel or message is not found, or if the emoji is not found.
            hikari.net.errors.BadRequestError:
                If the emoji is not valid, unknown, or formatted incorrectly
        """
        route = routes.OWN_REACTION.compile(self.PUT, channel_id=channel_id, message_id=message_id, emoji=emoji)
        await self._request(route)

    async def delete_own_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """
        Remove a reaction you made using a given emoji from a given message in a given channel or user DM.

        Args:
            channel_id:
                The ID of the channel to delete the reaction from.
            message_id:
                The ID of the message to delete the reaction from.
            emoji:
                The emoji to delete. This can either be a series of unicode characters making up a valid Discord
                emoji, or it can be a snowflake ID for a custom emoji.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel or message or emoji is not found.
        """
        route = routes.OWN_REACTION.compile(self.DELETE, channel_id=channel_id, message_id=message_id, emoji=emoji)
        await self._request(route)

    async def delete_user_reaction(self, channel_id: str, message_id: str, emoji: str, user_id: str) -> None:
        """
        Remove a reaction made by a given user using a given emoji on a given message in a given channel or user DM.

        Args:
            channel_id:
                The channel ID to remove from.
            message_id:
                The message ID to remove from.
            emoji:
                The emoji to delete. This can either be a series of unicode characters making up a valid Discord
                emoji, or it can be a snowflake ID for a custom emoji.
            user_id:
                The ID of the user who made the reaction that you wish to remove.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel or message or emoji or user is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_MESSAGES` permission, or are in DMs.
        """
        route = routes.REACTION.compile(
            self.DELETE, channel_id=channel_id, message_id=message_id, emoji=emoji, user_id=user_id,
        )
        await self._request(route)

    async def get_reactions(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
        *,
        before: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        after: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
    ) -> typing.Sequence[containers.JSONObject]:
        """
        Get a list of users who reacted with the given emoji on the given message in the given channel or user DM.

        Args:
            channel_id:
                The channel to get the message from.
            message_id:
                The ID of the message to retrieve.
            emoji:
                The emoji to get. This can either be a series of unicode characters making up a valid Discord
                emoji, or it can be a snowflake ID for a custom emoji.
            before:
                An optional user ID. If specified, only users with a snowflake that is lexicographically less than the
                value will be returned.
            after:
                An optional user ID. If specified, only users with a snowflake that is lexicographically greater than
                the value will be returned.
            limit:
                An optional limit of the number of values to return. Must be between 1 and 100 inclusive. If
                unspecified, it defaults to 25.

        Returns:
            A list of user objects.
        """
        query = {}
        transformations.put_if_specified(query, "before", before)
        transformations.put_if_specified(query, "after", after)
        transformations.put_if_specified(query, "limit", limit)
        route = routes.REACTIONS.compile(self.GET, channel_id=channel_id, message_id=message_id, emoji=emoji)
        return await self._request(route, query=query)

    async def delete_all_reactions(self, channel_id: str, message_id: str) -> None:
        """
        Deletes all reactions from a given message in a given channel.

        Args:
            channel_id:
                The channel ID to remove reactions within.
            message_id:
                The message ID to remove reactions from.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel_id or message_id was not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_MESSAGES` permission.
        """
        route = routes.ALL_REACTIONS.compile(self.DELETE, channel_id=channel_id, message_id=message_id)
        await self._request(route)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        content: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        embed: type_hints.NotRequired[containers.JSONObject] = unspecified.UNSPECIFIED,
        flags: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Update the given message.

        Args:
            channel_id:
                The channel ID (or user ID if a direct message) to operate in.
            message_id:
                The message ID to edit.
            content:
                Optional string content to replace with in the message. If unspecified, it is not changed.
            embed:
                Optional embed to replace with in the message. If unspecified, it is not changed.
            flags:
                Optional integer to replace the message's current flags. If unspecified, it is not changed.

        Returns:
            A replacement message object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel_id or message_id is not found.
            hikari.net.errors.BadRequestError:
                If the embed exceeds any of the embed limits if specified, or the content is specified and consists
                only of whitespace, is empty, or is more than 2,000 characters in length.
            hikari.net.errors.ForbiddenError:
                If you try to edit content or embed on a message you did not author or try to edit the flags
                on a message you did not author without the `MANAGE_MESSAGES` permission.
        """
        payload = {}
        transformations.put_if_specified(payload, "content", content)
        transformations.put_if_specified(payload, "embed", embed)
        transformations.put_if_specified(payload, "flags", flags)
        route = routes.CHANNEL_MESSAGE.compile(self.PATCH, channel_id=channel_id, message_id=message_id)
        return await self._request(route, json_body=payload)

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        """
        Delete a message in a given channel.

        Args:
            channel_id:
                The channel ID or user ID that the message was sent to.
            message_id:
                The message ID that was sent.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you did not author the message and are in a DM, or if you did not author the message and lack the
                `MANAGE_MESSAGES` permission in a guild channel.
            hikari.net.errors.NotFoundError:
                If the channel or message was not found.
        """
        route = routes.CHANNEL_MESSAGE.compile(self.DELETE, channel_id=channel_id, message_id=message_id)
        await self._request(route)

    async def bulk_delete_messages(self, channel_id: str, messages: typing.Sequence[str]) -> None:
        """
        Delete multiple messages in one request.

        Args:
            channel_id:
                The channel_id to delete from.
            messages:
                A list of 2-100 message IDs to remove in the channel.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel_id is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_MESSAGES` permission in the channel.
            hikari.net.errors.BadRequestError:
                If any of the messages passed are older than 2 weeks in age or any duplicate message IDs are passed.

        Notes:
            This can only be used on guild text channels.

            Any message IDs that do not exist or are invalid add towards the total 100 max messages to remove.

            This can only delete messages that are newer than 2 weeks in age. If any of the messages are older than 2 weeks
            then this call will fail.
        """
        payload = {"messages": messages}
        route = routes.CHANNEL_MESSAGES_BULK_DELETE.compile(self.POST, channel_id=channel_id)
        await self._request(route, json_body=payload)

    async def edit_channel_permissions(
        self,
        channel_id: str,
        overwrite_id: str,
        *,
        allow: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        deny: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        type_: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Edit permissions for a given channel.

        Args:
            channel_id:
                The channel to edit permissions for.
            overwrite_id:
                The overwrite ID to edit.
            allow:
                The bitwise value of all permissions to set to be allowed.
            deny:
                The bitwise value of all permissions to set to be denied.
            type_:
                "member" if it is for a member, or "role" if it is for a role.
            reason:
                An optional audit log reason explaining why the change was made.
        """
        payload = {}
        transformations.put_if_specified(payload, "allow", allow)
        transformations.put_if_specified(payload, "deny", deny)
        transformations.put_if_specified(payload, "type", type_)
        route = routes.CHANNEL_PERMISSIONS.compile(self.PATCH, channel_id=channel_id, overwrite_id=overwrite_id)
        await self._request(route, json_body=payload, reason=reason)

    async def get_channel_invites(self, channel_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Get invites for a given channel.

        Args:
            channel_id:
                The channel to get invites for.

        Returns:
            A list of invite objects.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_CHANNELS` permission.
            hikari.net.errors.NotFoundError:
                If the channel does not exist.
        """
        route = routes.CHANNEL_INVITES.compile(self.GET, channel_id=channel_id)
        return await self._request(route)

    async def create_channel_invite(
        self,
        channel_id: str,
        *,
        max_age: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        max_uses: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        temporary: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        unique: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Create a new invite for the given channel.

        Args:
            channel_id:
                The channel ID to create the invite for.
            max_age:
                The max age of the invite in seconds, defaults to 86400 (24 hours). Set to 0 to never expire.
            max_uses:
                The max number of uses this invite can have, or 0 for unlimited (as per the default).
            temporary:
                If `True`, grant temporary membership, meaning the user is kicked when their session ends unless they
                are given a role. Defaults to `False`.
            unique:
                If `True`, never reuse a similar invite. Defaults to `False`.
            reason:
                An optional audit log reason explaining why the change was made.

        Returns:
            An invite object.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack the `CREATE_INSTANT_MESSAGES` permission.
            hikari.net.errors.NotFoundError:
                If the channel does not exist.
            hikari.net.errors.BadRequestError:
                If the arguments provided are not valid (e.g. negative age, etc).
        """
        payload = {}
        transformations.put_if_specified(payload, "max_age", max_age)
        transformations.put_if_specified(payload, "max_uses", max_uses)
        transformations.put_if_specified(payload, "temporary", temporary)
        transformations.put_if_specified(payload, "unique", unique)
        route = routes.CHANNEL_INVITES.compile(self.POST, channel_id=channel_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def delete_channel_permission(self, channel_id: str, overwrite_id: str) -> None:
        """
        Delete a channel permission overwrite for a user or a role in a channel.

        Args:
            channel_id:
                The channel ID to delete from.
            overwrite_id:
                The override ID to remove.

        Raises:
            hikari.net.errors.NotFoundError:
                If the overwrite or channel ID does not exist.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission for that channel.
        """
        route = routes.CHANNEL_PERMISSIONS.compile(self.DELETE, channel_id=channel_id, overwrite_id=overwrite_id)
        await self._request(route)

    async def trigger_typing_indicator(self, channel_id: str) -> None:
        """
        Trigger the account to appear to be typing for the next 10 seconds in the given channel.

        Args:
            channel_id:
                The channel ID to appear to be typing in. This may be a user ID if you wish to appear to be typing
                in DMs.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel is not found.
            hikari.net.errors.ForbiddenError:
                If you are not in the guild the channel is in
        """
        route = routes.CHANNEL_TYPING.compile(self.POST, channel_id=channel_id)
        await self._request(route)

    async def get_pinned_messages(self, channel_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Get pinned messages for a given channel.

        Args:
            channel_id:
                The channel ID to get messages for.

        Returns:
            A list of messages.

        Raises:
            hikari.net.errors.NotFoundError:
                If no channel matching the ID exists.
        """
        route = routes.CHANNEL_PINS.compile(self.GET, channel_id=channel_id)
        return await self._request(route)

    async def add_pinned_channel_message(self, channel_id: str, message_id: str) -> None:
        """
        Add a pinned message to the channel.

        Args:
            channel_id:
                The channel ID to add a pin to.
            message_id:
                The message in the channel to pin.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_MESSAGES` permission.
            hikari.net.errors.NotFoundError:
                If the message or channel does not exist.
        """
        route = routes.CHANNEL_PINS.compile(self.PUT, channel_id=channel_id, message_id=message_id)
        await self._request(route)

    async def delete_pinned_channel_message(self, channel_id: str, message_id: str) -> None:
        """
        Remove a pinned message from the channel. This will only unpin the message. It will not delete it.

        Args:
            channel_id:
                The channel ID to remove a pin from.
            message_id:
                The message in the channel to unpin.

        Raises:
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_MESSAGES` permission.
            hikari.net.errors.NotFoundError:
                If the message or channel does not exist.
        """
        route = routes.CHANNEL_PIN.compile(self.DELETE, channel_id=channel_id, message_id=message_id)
        await self._request(route)

    async def list_guild_emojis(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets emojis for a given guild ID.

        Args:
            guild_id:
                The guild ID to get the emojis for.

        Returns:
            A list of emoji objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you aren't a member of said guild.
        """
        route = routes.GUILD_EMOJIS.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def get_guild_emoji(self, guild_id: str, emoji_id: str) -> containers.JSONObject:
        """
        Gets an emoji from a given guild and emoji IDs

        Args:
            guild_id:
                The ID of the guild to get the emoji from.
            emoji_id:
                The ID of the emoji to get.

        Returns:
            An emoji object.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the emoji aren't found.
            hikari.net.errors.ForbiddenError:
                If you aren't a member of said guild.
        """
        route = routes.GUILD_EMOJI.compile(self.GET, guild_id=guild_id, emoji_id=emoji_id)
        return await self._request(route)

    async def create_guild_emoji(
        self,
        guild_id: str,
        name: str,
        image: bytes,
        *,
        roles: type_hints.NotRequired[typing.Sequence[str]] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Creates a new emoji for a given guild.

        Args:
            guild_id:
                The ID of the guild to create the emoji in.
            name:
                The new emoji's name.
            image:
                The 128x128 image in bytes form.
            roles:
                A list of roles for which the emoji will be whitelisted. If empty, all roles are whitelisted.
            reason:
                An optional audit log reason explaining why the change was made.

        Returns:
            The newly created emoji object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_EMOJIS` permission or aren't a member of said guild.
            hikari.net.errors.BadRequestError:
                If you attempt to upload an image larger than 256kb, an empty image or an invalid image format.
        """
        assertions.assert_not_none(image, "image must be a valid image")
        payload = {
            "name": name,
            "roles": [] if roles is unspecified.UNSPECIFIED else roles,
            "image": conversions.image_bytes_to_image_data(image),
        }
        route = routes.GUILD_EMOJIS.compile(self.POST, guild_id=guild_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def modify_guild_emoji(
        self,
        guild_id: str,
        emoji_id: str,
        *,
        name: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        roles: type_hints.NotRequired[typing.Sequence[str]] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Edits an emoji of a given guild

        Args:
            guild_id:
                The ID of the guild to which the edited emoji belongs to.
            emoji_id:
                The ID of the edited emoji.
            name:
                The new emoji name string. Keep unspecified to keep the name the same.
            roles:
                A list of IDs for the new whitelisted roles.
                Set to an empty list to whitelist all roles.
                Keep unspecified to leave the same roles already set.
            reason:
                An optional audit log reason explaining why the change was made.

        Returns:
            The updated emoji object.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the emoji aren't found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_EMOJIS` permission or are not a member of the given guild.
        """
        payload = {"name": name, "roles": roles}
        route = routes.GUILD_EMOJI.compile(self.PATCH, guild_id=guild_id, emoji_id=emoji_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def delete_guild_emoji(self, guild_id: str, emoji_id: str) -> None:
        """
        Deletes an emoji from a given guild

        Args:
            guild_id:
                The ID of the guild to delete the emoji from.
            emoji_id:
                The ID of the emoji to be deleted.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the emoji aren't found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_EMOJIS` permission or aren't a member of said guild.
        """
        route = routes.GUILD_EMOJI.compile(self.DELETE, guild_id=guild_id, emoji_id=emoji_id)
        await self._request(route)

    async def create_guild(
        self,
        name: str,
        *,
        region: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        icon: type_hints.NotRequired[bytes] = unspecified.UNSPECIFIED,
        verification_level: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        default_message_notifications: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        explicit_content_filter: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        roles: type_hints.NotRequired[typing.Sequence[containers.JSONObject]] = unspecified.UNSPECIFIED,
        channels: type_hints.NotRequired[typing.Sequence[containers.JSONObject]] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Creates a new guild. Can only be used by bots in less than 10 guilds.

        Args:
            name:
                The name string for the new guild (2-100 characters).
            region:
                The voice region ID for new guild. You can use `list_voice_regions` to see which region IDs are
                available.
            icon:
                The guild icon image in bytes form.
            verification_level:
                The verification level integer (0-5).
            default_message_notifications:
                The default notification level integer (0-1).
            explicit_content_filter:
                The explicit content filter integer (0-2).
            roles:
                An array of role objects to be created alongside the guild. First element changes the `@everyone` role.
            channels:
                An array of channel objects to be created alongside the guild.

        Returns:
            The newly created guild object.

        Raises:
            hikari.net.errors.ForbiddenError:
                If your bot is on 10 or more guilds.
            hikari.net.errors.BadRequestError:
                If you provide unsupported fields like `parent_id` in channel objects.
        """
        payload = {"name": name}
        transformations.put_if_specified(payload, "region", region)
        transformations.put_if_specified(payload, "verification_level", verification_level)
        transformations.put_if_specified(payload, "default_message_notifications", default_message_notifications)
        transformations.put_if_specified(payload, "explicit_content_filter", explicit_content_filter)
        transformations.put_if_specified(payload, "roles", roles)
        transformations.put_if_specified(payload, "channels", channels)
        transformations.put_if_specified(payload, "icon", icon, conversions.image_bytes_to_image_data)
        route = routes.GUILDS.compile(self.POST)
        return await self._request(route, json_body=payload)

    async def get_guild(self, guild_id: str) -> containers.JSONObject:
        """
        Gets a given guild's object.

        Args:
            guild_id:
                The ID of the guild to get.

        Returns:
            The requested guild object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
        """
        route = routes.GUILD.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    # pylint: disable=too-many-locals
    async def modify_guild(  # lgtm [py/similar-function]
        self,
        guild_id: str,
        *,
        name: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        region: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        verification_level: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        default_message_notifications: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        explicit_content_filter: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        afk_channel_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        afk_timeout: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        icon: type_hints.NotRequired[bytes] = unspecified.UNSPECIFIED,
        owner_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        splash: type_hints.NotRequired[bytes] = unspecified.UNSPECIFIED,
        system_channel_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Edits a given guild.

        Args:
            guild_id:
                The ID of the guild to be edited.
            name:
                The new name string.
            region:
                The voice region ID for new guild. You can use `list_voice_regions` to see which region IDs are
                available.
            verification_level:
                The verification level integer (0-5).
            default_message_notifications:
                The default notification level integer (0-1).
            explicit_content_filter:
                The explicit content filter integer (0-2).
            afk_channel_id:
                The ID for the AFK voice channel.
            afk_timeout:
                The AFK timeout period in seconds
            icon:
                The guild icon image in bytes form.
            owner_id:
                The ID of the new guild owner.
            splash:
                The new splash image in bytes form.
            system_channel_id:
                The ID of the new system channel.
            reason:
                Optional reason to apply to the audit log.

        Returns:
            The edited guild object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        payload = {}
        transformations.put_if_specified(payload, "name", name)
        transformations.put_if_specified(payload, "region", region)
        transformations.put_if_specified(payload, "verification_level", verification_level)
        transformations.put_if_specified(payload, "default_message_notifications", default_message_notifications)
        transformations.put_if_specified(payload, "explicit_content_filter", explicit_content_filter)
        transformations.put_if_specified(payload, "afk_channel_id", afk_channel_id)
        transformations.put_if_specified(payload, "afk_timeout", afk_timeout)
        transformations.put_if_specified(payload, "icon", icon, conversions.image_bytes_to_image_data)
        transformations.put_if_specified(payload, "owner_id", owner_id)
        transformations.put_if_specified(payload, "splash", splash, conversions.image_bytes_to_image_data)
        transformations.put_if_specified(payload, "system_channel_id", system_channel_id)
        route = routes.GUILD.compile(self.PATCH, guild_id=guild_id)
        return await self._request(route, json_body=payload, reason=reason)

    # pylint: enable=too-many-locals

    async def delete_guild(self, guild_id: str) -> None:
        """
        Permanently deletes the given guild. You must be owner.

        Args:
            guild_id:
                The ID of the guild to be deleted.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you're not the guild owner.
        """
        route = routes.GUILD.compile(self.DELETE, guild_id=guild_id)
        await self._request(route)

    async def get_guild_channels(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets all the channels for a given guild.

        Args:
            guild_id:
                The ID of the guild to get the channels from.

        Returns:
            A list of channel objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you're not in the guild.
        """
        route = routes.GUILD_CHANNELS.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def create_guild_channel(
        self,
        guild_id: str,
        name: str,
        *,
        type_: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        topic: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        bitrate: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        user_limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        rate_limit_per_user: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        position: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        permission_overwrites: type_hints.NotRequired[typing.Sequence[containers.JSONObject]] = unspecified.UNSPECIFIED,
        parent_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        nsfw: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Creates a channel in a given guild.

        Args:
            guild_id:
                The ID of the guild to create the channel in.
            name:
                The new channel name string (2-100 characters).
            type_:
                The channel type integer (0-6).
            topic:
                The string for the channel topic (0-1024 characters).
            bitrate:
                The bitrate integer (in bits) for the voice channel, if applicable.
            user_limit:
                The maximum user count for the voice channel, if applicable.
            rate_limit_per_user:
                The seconds a user has to wait before posting another message (0-21600).
                Having the `MANAGE_MESSAGES` or `MANAGE_CHANNELS` permissions gives you immunity.
            position:
                The sorting position for the channel.
            permission_overwrites:
                A list of overwrite objects to apply to the channel.
            parent_id:
                The ID of the parent category.
            nsfw:
                Marks the channel as NSFW if `True`.
            reason:
                The optional reason for the operation being performed.

        Returns:
            The newly created channel object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_CHANNEL` permission or are not in the target guild or are not in the guild.
            hikari.net.errors.BadRequestError:
                If you omit the `name` argument.
        """
        payload = {"name": name}
        transformations.put_if_specified(payload, "type", type_)
        transformations.put_if_specified(payload, "topic", topic)
        transformations.put_if_specified(payload, "bitrate", bitrate)
        transformations.put_if_specified(payload, "user_limit", user_limit)
        transformations.put_if_specified(payload, "rate_limit_per_user", rate_limit_per_user)
        transformations.put_if_specified(payload, "position", position)
        transformations.put_if_specified(payload, "permission_overwrites", permission_overwrites)
        transformations.put_if_specified(payload, "parent_id", parent_id)
        transformations.put_if_specified(payload, "nsfw", nsfw)
        route = routes.GUILD_CHANNELS.compile(self.POST, guild_id=guild_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def modify_guild_channel_positions(
        self, guild_id: str, channel: typing.Tuple[str, int], *channels: typing.Tuple[str, int]
    ) -> None:
        """
        Edits the position of one or more given channels.

        Args:
            guild_id:
                The ID of the guild in which to edit the channels.
            channel:
                The first channel to change the position of. This is a tuple of the channel ID and the integer position.
            channels:
                Optional additional channels to change the position of. These must be tuples of the channel ID and the
                integer positions to change to.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or any of the channels aren't found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_CHANNELS` permission or are not a member of said guild or are not in
                The guild.
            hikari.net.errors.BadRequestError:
                If you provide anything other than the `id` and `position` fields for the channels.
        """
        payload = [{"id": ch[0], "position": ch[1]} for ch in (channel, *channels)]
        route = routes.GUILD_CHANNELS.compile(self.PATCH, guild_id=guild_id)
        await self._request(route, json_body=payload)

    async def get_guild_member(self, guild_id: str, user_id: str) -> containers.JSONObject:
        """
        Gets a given guild member.

        Args:
            guild_id:
                The ID of the guild to get the member from.
            user_id:
                The ID of the member to get.

        Returns:
            The requested member object.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the member aren't found or are not in the guild.
        """
        route = routes.GUILD_MEMBER.compile(self.GET, guild_id=guild_id, user_id=user_id)
        return await self._request(route)

    async def list_guild_members(
        self,
        guild_id: str,
        *,
        limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        after: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> typing.Sequence[containers.JSONObject]:
        """
        Lists all members of a given guild.

        Args:
            guild_id:
                The ID of the guild to get the members from.
            limit:
                The maximum number of members to return (1-1000).
            after:
                The highest ID in the previous page. This is used for retrieving more than 1000 members in a server
                using consecutive requests.
                
        Example:
            .. code-block:: python
                
                members = []
                last_id = 0
                
                while True:
                    next_members = await client.list_guild_members(1234567890, limit=1000, after=last_id)
                    members += next_members
                    
                    if len(next_members) == 1000:
                        last_id = max(m["id"] for m in next_members)
                    else:
                        break                  

        Returns:
            A list of member objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you are not in the guild.
            hikari.net.errors.BadRequestError:
                If you provide invalid values for the `limit` and `after` fields.
        """
        query = {}
        transformations.put_if_specified(query, "limit", limit)
        transformations.put_if_specified(query, "after", after)
        route = routes.GUILD_MEMBERS.compile(self.GET, guild_id=guild_id)
        return await self._request(route, query=query)

    async def modify_guild_member(  # lgtm [py/similar-function]
        self,
        guild_id: str,
        user_id: str,
        *,
        nick: type_hints.NullableNotRequired[str] = unspecified.UNSPECIFIED,
        roles: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        mute: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        deaf: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        channel_id: type_hints.NullableNotRequired[str] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Edits a member of a given guild.

        Args:
            guild_id:
                The ID of the guild to edit the member from.
            user_id:
                The ID of the member to edit.
            nick:
                The new nickname string. Setting it to None explicitly will clear the nickname.
            roles:
                A list of role IDs the member should have.
            mute:
                Whether the user should be muted in the voice channel or not, if applicable.
            deaf:
                Whether the user should be deafen in the voice channel or not, if applicable.
            channel_id:
                The ID of the channel to move the member to, if applicable. Pass None to disconnect the user.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.
        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild, user, channel or any of the roles aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack any of the applicable permissions
                (`MANAGE_NICKNAMES`, `MANAGE_ROLES`, `MUTE_MEMBERS`, `DEAFEN_MEMBERS` or `MOVE_MEMBERS`).
                Note that to move a member you must also have permission to connect to the end channel.
                This will also be raised if you're not in the guild.
            hikari.net.errors.BadRequestError:
                If you pass `mute`, `deaf` or `channel_id` while the member is not connected to a voice channel.
        """
        payload = {}
        transformations.put_if_specified(payload, "nick", nick)
        transformations.put_if_specified(payload, "roles", roles)
        transformations.put_if_specified(payload, "mute", mute)
        transformations.put_if_specified(payload, "deaf", deaf)
        transformations.put_if_specified(payload, "channel_id", channel_id)
        route = routes.GUILD_MEMBER.compile(self.PATCH, guild_id=guild_id, user_id=user_id)
        await self._request(route, json_body=payload, reason=reason)

    async def modify_current_user_nick(
        self,
        guild_id: str,
        nick: type_hints.Nullable[str],
        *,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Edits the current user's nickname for a given guild.

        Args:
            guild_id:
                The ID of the guild you want to change the nick on.
            nick:
                The new nick string. Setting this to `None` clears the nickname.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.
                
        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `CHANGE_NICKNAME` permission or are not in the guild.
            hikari.net.errors.BadRequestError:
                If you provide a disallowed nickname, one that is too long, or one that is empty.
        """
        payload = {"nick": nick}
        route = routes.OWN_GUILD_NICKNAME.compile(self.GET, guild_id=guild_id)
        await self._request(route, json_body=payload, reason=reason)

    async def add_guild_member_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
        *,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Adds a role to a given member.

        Args:
            guild_id:
                The ID of the guild the member belongs to.
            user_id:
                The ID of the member you want to add the role to.
            role_id:
                The ID of the role you want to add.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild, member or role aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission or are not in the guild.
        """
        route = routes.GUILD_MEMBER_ROLE.compile(self.PUT, guild_id=guild_id, user_id=user_id, role_id=role_id)
        await self._request(route, reason=reason)

    async def remove_guild_member_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
        *,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Removed a role from a given member.

        Args:
            guild_id:
                The ID of the guild the member belongs to.
            user_id:
                The ID of the member you want to remove the role from.
            role_id:
                The ID of the role you want to remove.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild, member or role aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission or are not in the guild.
        """
        route = routes.GUILD_MEMBER_ROLE.compile(self.DELETE, guild_id=guild_id, user_id=user_id, role_id=role_id)
        await self._request(route, reason=reason)

    async def remove_guild_member(
        self, guild_id: str, user_id: str, *, reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED
    ) -> None:
        """
        Kicks a user from a given guild.

        Args:
            guild_id:
                The ID of the guild the member belongs to.
            user_id:
                The ID of the member you want to kick.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or member aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `KICK_MEMBERS` permission or are not in the guild.
        """
        route = routes.GUILD_MEMBER.compile(self.DELETE, guild_id=guild_id, user_id=user_id)
        await self._request(route, reason=reason)

    async def get_guild_bans(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the bans for a given guild.

        Args:
            guild_id:
                The ID of the guild you want to get the bans from.

        Returns:
            A list of ban objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `BAN_MEMBERS` permission or are not in the guild.
        """
        route = routes.GUILD_BANS.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def get_guild_ban(self, guild_id: str, user_id: str) -> containers.JSONObject:
        """
        Gets a ban from a given guild.

        Args:
            guild_id:
                The ID of the guild you want to get the ban from.
            user_id:
                The ID of the user to get the ban information for.

        Returns:
            A ban object for the requested user.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the user aren't found, or if the user is not banned.
            hikari.net.errors.ForbiddenError:
                If you lack the `BAN_MEMBERS` permission or are not in the guild.
        """
        route = routes.GUILD_BAN.compile(self.GET, guild_id=guild_id, user_id=user_id)
        return await self._request(route)

    async def create_guild_ban(
        self,
        guild_id: str,
        user_id: str,
        *,
        delete_message_days: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Bans a user from a given guild.

        Args:
            guild_id:
                The ID of the guild the member belongs to.
            user_id:
                The ID of the member you want to ban.
            delete_message_days:
                How many days of messages from the user should be removed. Default is to not delete anything.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or member aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `BAN_MEMBERS` permission or are not in the guild.
        """
        query = {}
        transformations.put_if_specified(query, "delete-message-days", delete_message_days)
        transformations.put_if_specified(query, "reason", reason)
        route = routes.GUILD_BAN.compile(self.PUT, guild_id=guild_id, user_id=user_id)
        await self._request(route, query=query)

    async def remove_guild_ban(
        self, guild_id: str, user_id: str, *, reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED
    ) -> None:
        """
        Un-bans a user from a given guild.

        Args:
            guild_id:
                The ID of the guild to un-ban the user from.
            user_id:
                The ID of the user you want to un-ban.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or member aren't found, or the member is not banned.
            hikari.net.errors.ForbiddenError:
                If you lack the `BAN_MEMBERS` permission or are not a in the guild.
        """
        route = routes.GUILD_BAN.compile(self.DELETE, guild_id=guild_id, user_id=user_id)
        await self._request(route, reason=reason)

    async def get_guild_roles(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the roles for a given guild.

        Args:
            guild_id:
                The ID of the guild you want to get the roles from.

        Returns:
            A list of role objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you're not in the guild.
        """
        route = routes.GUILD_ROLES.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def create_guild_role(
        self,
        guild_id: str,
        *,
        name: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        permissions: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        color: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        hoist: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        mentionable: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Creates a new role for a given guild.

        Args:
            guild_id:
                The ID of the guild you want to create the role on.
            name:
                The new role name string.
            permissions:
                The permissions integer for the role.
            color:
                The color for the new role.
            hoist:
                Whether the role should hoist or not.
            mentionable:
                Whether the role should be able to be mentioned by users or not.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Returns:
            The newly created role object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission or you're not in the guild.
            hikari.net.errors.BadRequestError:
                If you provide invalid values for the role attributes.
        """
        payload = {}
        transformations.put_if_specified(payload, "name", name)
        transformations.put_if_specified(payload, "permissions", permissions)
        transformations.put_if_specified(payload, "color", color)
        transformations.put_if_specified(payload, "hoist", hoist)
        transformations.put_if_specified(payload, "mentionable", mentionable)
        route = routes.GUILD_ROLES.compile(self.POST, guild_id=guild_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def modify_guild_role_positions(
        self, guild_id: str, role: typing.Tuple[str, int], *roles: typing.Tuple[str, int]
    ) -> typing.Sequence[containers.JSONObject]:
        """
        Edits the position of two or more roles in a given guild.

        Args:
            guild_id:
                The ID of the guild the roles belong to.
            role:
                The first role to move.
            roles:
                Optional extra roles to move.

        Returns:
            A list of all the guild roles.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or any of the roles aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission or you're not in the guild.
            hikari.net.errors.BadRequestError:
                If you provide invalid values for the `position` fields.
        """
        payload = [{"id": r[0], "position": r[1]} for r in (role, *roles)]
        route = routes.GUILD_ROLES.compile(self.PATCH, guild_id=guild_id)
        return await self._request(route, json_body=payload)

    async def modify_guild_role(  # lgtm [py/similar-function]
        self,
        guild_id: str,
        role_id: str,
        *,
        name: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        permissions: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        color: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        hoist: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        mentionable: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Edits a role in a given guild.

        Args:
            guild_id:
                The ID of the guild the role belong to.
            role_id:
                The ID of the role you want to edit.
            name:
                THe new role's name string.
            permissions:
                The new permissions integer for the role.
            color:
                The new color for the new role.
            hoist:
                Whether the role should hoist or not.
            mentionable:
                Whether the role should be mentionable or not.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.
                
        Returns:
            The edited role object.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or role aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission or you're not in the guild.
            hikari.net.errors.BadRequestError:
                If you provide invalid values for the role attributes.
        """
        payload = {}
        transformations.put_if_specified(payload, "name", name)
        transformations.put_if_specified(payload, "permissions", permissions)
        transformations.put_if_specified(payload, "color", color)
        transformations.put_if_specified(payload, "hoist", hoist)
        transformations.put_if_specified(payload, "mentionable", mentionable)
        route = routes.GUILD_ROLE.compile(self.PATCH, guild_id=guild_id, role_id=role_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def delete_guild_role(self, guild_id: str, role_id: str) -> None:
        """
        Deletes a role from a given guild.

        Args:
            guild_id:
                The ID of the guild you want to remove the role from.
            role_id:
                The ID of the role you want to delete.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the role aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_ROLES` permission or are not in the guild.
        """
        route = routes.GUILD_ROLE.compile(self.DELETE, guild_id=guild_id, role_id=role_id)
        await self._request(route)

    async def get_guild_prune_count(self, guild_id: str, days: int) -> int:
        """
        Gets the estimated prune count for a given guild.

        Args:
            guild_id:
                The ID of the guild you want to get the count for.
            days:
                The number of days to count prune for (at least 1).

        Returns:
            the number of members estimated to be pruned.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `KICK_MEMBERS` or you are not in the guild.
            hikari.net.errors.BadRequestError:
                If you pass an invalid amount of days.
        """
        payload = {"days": days}
        route = routes.GUILD_PRUNE.compile(self.GET, guild_id=guild_id)
        result = await self._request(route, query=payload)
        return int(result["pruned"])

    async def begin_guild_prune(
        self,
        guild_id: str,
        days: int,
        *,
        compute_prune_count: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> type_hints.Nullable[int]:
        """
        Prunes members of a given guild based on the number of inactive days.

        Args:
            guild_id:
                The ID of the guild you want to prune member of.
            days:
                The number of inactivity days you want to use as filter.
            compute_prune_count:
                Whether a count of pruned members is returned or not. Discouraged for large guilds out of politeness.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Returns:
            :class:`None` if `compute_prune_count` is `False`, or an :class:`int` representing the number
            of members who were kicked.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found:
            hikari.net.errors.ForbiddenError:
                If you lack the `KICK_MEMBER` permission or are not in the guild.
            hikari.net.errors.BadRequestError:
                If you provide invalid values for the `days` and `compute_prune_count` fields.
        """
        query = {
            "days": days,
            "compute_prune_count": compute_prune_count if compute_prune_count is not unspecified.UNSPECIFIED else False,
        }
        route = routes.GUILD_PRUNE.compile(self.POST, guild_id=guild_id)
        result = await self._request(route, query=query, reason=reason)

        try:
            return int(result["pruned"])
        except (TypeError, KeyError):
            return None

    async def get_guild_voice_regions(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the voice regions for a given guild.

        Args:
            guild_id:
                The ID of the guild to get the voice regions for.

        Returns:
            A list of voice region objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found:
            hikari.net.errors.ForbiddenError:
                If you are not in the guild.
        """
        route = routes.GUILD_VOICE_REGIONS.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def get_guild_invites(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the invites for a given guild.

        Args:
            guild_id:
                The ID of the guild to get the invites for.

        Returns:
            A list of invite objects (with metadata).

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_INVITES.compile(self.GET, guild_id=guild_id)
        return await self._request(route, guild_id=guild_id)

    async def get_guild_integrations(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the integrations for a given guild.

        Args:
            guild_id:
                The ID of the guild to get the integrations for.

        Returns:
            A list of integration objects.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_INTEGRATIONS.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def create_guild_integration(
        self,
        guild_id: str,
        type_: str,
        integration_id: str,
        *,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Creates an integrations for a given guild.

        Args:
            guild_id:
                The ID of the guild to create the integrations in.
            type_:
                The integration type string (e.g. "twitch" or "youtube").
            integration_id:
                The ID for the new integration.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Returns:
            The newly created integration object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        payload = {"type": type_, "id": integration_id}
        route = routes.GUILD_INTEGRATIONS.compile(self.POST, guild_id=guild_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def modify_guild_integration(
        self,
        guild_id: str,
        integration_id: str,
        *,
        expire_behaviour: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        expire_grace_period: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
        enable_emojis: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> None:
        """
        Edits an integrations for a given guild.

        Args:
            guild_id:
                The ID of the guild to which the integration belongs to.
            integration_id:
                The ID of the integration.
            expire_behaviour:
                The behaviour for when an integration subscription lapses.
            expire_grace_period:
                Time interval in seconds in which the integration will ignore lapsed subscriptions.
            enable_emojis:
                Whether emojis should be synced for this integration.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the integration aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        payload = {
            "expire_behaviour": expire_behaviour,
            "expire_grace_period": expire_grace_period,
            # This is inconsistently named in their API.
            "enable_emoticons": enable_emojis,
        }
        route = routes.GUILD_INTEGRATION.compile(self.PATCH, guild_id=guild_id, integration_id=integration_id)
        await self._request(route, json_body=payload, reason=reason)

    async def delete_guild_integration(
        self, guild_id: str, integration_id: str, *, reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED
    ) -> None:
        """
        Deletes an integration for the given guild.

        Args:
            guild_id:
                The ID of the guild from which to delete an integration.
            integration_id:
                The ID of the integration to delete.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the integration aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_INTEGRATION.compile(self.DELETE, guild_id=guild_id, integration_id=integration_id)
        await self._request(route, reason=reason)

    async def sync_guild_integration(self, guild_id: str, integration_id: str) -> None:
        """
        Syncs the given integration.

        Args:
            guild_id:
                The ID of the guild to which the integration belongs to.
            integration_id:
                The ID of the integration to sync.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the guild or the integration aren't found.
            hikari.net.errors.ForbiddenError:
                If you lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_INTEGRATION_SYNC.compile(self.POST, guild_id=guild_id, integration_id=integration_id)
        await self._request(route)

    async def get_guild_embed(self, guild_id: str) -> containers.JSONObject:
        """
        Gets the embed for a given guild.

        Args:
            guild_id:
                The ID of the guild to get the embed for.

        Returns:
            A guild embed object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_EMBED.compile(self.DELETE, guild_id=guild_id)
        return await self._request(route)

    async def modify_guild_embed(
        self,
        guild_id: str,
        embed: containers.JSONObject,
        *,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Edits the embed for a given guild.

        Args:
            guild_id:
                The ID of the guild to edit the embed for.
            embed:
                The new embed object to be set.
            reason:
                Optional reason to add to audit logs for the guild explaining why the operation was performed.

        Returns:
            The updated embed object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_EMBED.compile(self.PATCH, guild_id=guild_id)
        return await self._request(route, json_body=embed, reason=reason)

    async def get_guild_vanity_url(self, guild_id: str) -> containers.JSONObject:
        """
        Gets the vanity URL for a given guild.

        Args:
            guild_id:
                The ID of the guild to get the vanity URL for.

        Returns:
            A partial invite object containing the vanity URL in the `code` field.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_GUILD` permission or are not in the guild.
        """
        route = routes.GUILD_VANITY_URL.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    def get_guild_widget_image_url(
        self, guild_id: str, *, style: type_hints.NotRequired[str] = unspecified.UNSPECIFIED
    ) -> str:
        """
        Get the URL for a guild widget.

        Args:
            guild_id:
                The guild ID to use for the widget.
            style:
                Optional and one of "shield", "banner1", "banner2", "banner3" or "banner4".

        Returns:
            A URL to retrieve a PNG widget for your guild.

        Note:
            This does not actually make any form of request, and shouldn't be awaited. Thus, it doesn't have rate limits
            either.

        Warning:
            The guild must have the widget enabled in the guild settings for this to be valid.
        """
        query = "" if style is unspecified.UNSPECIFIED else f"?style={style}"
        return f"{self.base_url}/guilds/{guild_id}/widget.png" + query

    async def get_invite(
        self, invite_code: str, *, with_counts: type_hints.NotRequired[bool] = unspecified.UNSPECIFIED
    ) -> containers.JSONObject:
        """
        Gets the given invite.

        Args:
            invite_code:
                The ID for wanted invite.
            with_counts:
                If `True`, attempt to count the number of times the invite has been used, otherwise (and as the
                default), do not try to track this information.

        Returns:
            The requested invite object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the invite is not found.
        """
        query = {}
        transformations.put_if_specified(query, "with_counts", with_counts, str)
        route = routes.INVITE.compile(self.GET, invite_code=invite_code)
        return await self._request(route, query=query)

    async def delete_invite(self, invite_code: str) -> containers.JSONObject:
        """
        Deletes a given invite.

        Args:
            invite_code:
                The ID for the invite to be deleted.

        Returns:
            The deleted invite object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the invite is not found.
            hikari.net.errors.ForbiddenError
                If you lack either `MANAGE_CHANNELS` on the channel the invite belongs to or `MANAGE_GUILD` for
                guild-global delete.
        """
        route = routes.INVITE.compile(self.DELETE, invite_code=invite_code)
        return await self._request(route)

    ##########
    # OAUTH2 #
    ##########

    async def get_current_application_info(self) -> containers.JSONObject:
        """
        Get the current application information.

        Returns:
            An application info object.
        """
        route = routes.OAUTH2_APPLICATIONS_ME.compile(self.GET)
        return await self._request(route)

    ##########
    # USERS  #
    ##########

    async def get_current_user(self) -> containers.JSONObject:
        """
        Gets the current user that is represented by token given to the client.

        Returns:
            The current user object.
        """
        route = routes.OWN_USER.compile(self.GET)
        return await self._request(route)

    async def get_user(self, user_id: str) -> containers.JSONObject:
        """
        Gets a given user.

        Args:
            user_id:
                The ID of the user to get.

        Returns:
            The requested user object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the user is not found.
        """
        route = routes.USER.compile(self.GET, user_id=user_id)
        return await self._request(route)

    async def modify_current_user(
        self,
        *,
        username: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        avatar: type_hints.NullableNotRequired[bytes] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Edits the current user. If any arguments are unspecified, then that subject is not changed on Discord.

        Args:
            username:
                The new username string. If unspecified, then it is not changed.
            avatar:
                The new avatar image in bytes form. If unspecified, then it is not changed. If it is `None`, the
                avatar is removed.

        Returns:
            The updated user object.

        Raises:
            hikari.net.errors.BadRequestError:
                If you pass username longer than the limit (2-32) or an invalid image.
        """
        payload = {}
        transformations.put_if_specified(payload, "username", username)
        transformations.put_if_specified(payload, "avatar", avatar, conversions.image_bytes_to_image_data)
        route = routes.OWN_USER.compile(self.PATCH)
        return await self._request(route, json_body=payload)

    async def get_current_user_connections(self) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the current user's connections. This endpoint can be used with both Bearer and Bot tokens
        but will usually return an empty list for bots (with there being some exceptions to this
        like user accounts that have been converted to bots).

        Returns:
            A list of connection objects.
        """
        route = routes.OWN_CONNECTIONS.compile(self.GET)
        return await self._request(route)

    async def get_current_user_guilds(
        self,
        *,
        before: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        after: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        limit: type_hints.NotRequired[int] = unspecified.UNSPECIFIED,
    ) -> typing.Sequence[containers.JSONObject]:
        """
        Gets the guilds the current user is in.

        Returns:
            A list of partial guild objects.

        Raises:
            hikari.net.errors.BadRequestError:
                If you pass both `before` and `after`.
        """
        query = {}
        transformations.put_if_specified(query, "before", before)
        transformations.put_if_specified(query, "after", after)
        transformations.put_if_specified(query, "limit", limit)
        route = routes.OWN_GUILDS.compile(self.GET)
        return await self._request(route, query=query)

    async def leave_guild(self, guild_id: str) -> None:
        """
        Makes the current user leave a given guild.

        Args:
            guild_id:
                The ID of the guild to leave.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
        """
        route = routes.LEAVE_GUILD.compile(self.DELETE, guild_id=guild_id)
        await self._request(route)

    async def create_dm(self, recipient_id: str) -> containers.JSONObject:
        """
        Creates a new DM channel with a given user.

        Args:
            recipient_id:
                The ID of the user to create the new DM channel with.

        Returns:
            The newly created DM channel object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the recipient is not found.
        """
        payload = {"recipient_id": recipient_id}
        route = routes.OWN_DMS.compile(self.POST)
        return await self._request(route, json_body=payload)

    async def list_voice_regions(self) -> typing.Sequence[containers.JSONObject]:
        """
        Get the voice regions that are available.

        Returns:
            A list of voice regions available

        Note:
            This does not include VIP servers.
        """
        route = routes.VOICE_REGIONS.compile(self.GET)
        return await self._request(route)

    async def create_webhook(
        self,
        channel_id: str,
        name: str,
        *,
        avatar: type_hints.NotRequired[bytes] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Creates a webhook for a given channel.

        Args:
            channel_id:
                The ID of the channel for webhook to be created in.
            name:
                The webhook's name string.
            avatar:
                The avatar image in bytes form. If unspecified, no avatar is made.
            reason:
                An optional audit log reason explaining why the change was made.

        Returns:
            The newly created webhook object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_WEBHOOKS` permission or can not see the given channel.
            hikari.net.errors.BadRequestError:
                If the avatar image is too big or the format is invalid.
        """
        payload = {"name": name}
        transformations.put_if_specified(payload, "avatar", avatar, conversions.image_bytes_to_image_data)
        route = routes.CHANNEL_WEBHOOKS.compile(self.POST, channel_id=channel_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def get_channel_webhooks(self, channel_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets all webhooks from a given channel.

        Args:
            channel_id:
                The ID of the channel to get the webhooks from.

        Returns:
            A list of webhook objects for the give channel.

        Raises:
            hikari.net.errors.NotFoundError:
                If the channel is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_WEBHOOKS` permission or can not see the given channel.
        """
        route = routes.CHANNEL_WEBHOOKS.compile(self.GET, channel_id=channel_id)
        return await self._request(route)

    async def get_guild_webhooks(self, guild_id: str) -> typing.Sequence[containers.JSONObject]:
        """
        Gets all webhooks for a given guild.

        Args:
            guild_id:
                The ID for the guild to get the webhooks from.

        Returns:
            A list of webhook objects for the given guild.

        Raises:
            hikari.net.errors.NotFoundError:
                If the guild is not found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_WEBHOOKS` permission or aren't a member of the given guild.
        """
        route = routes.GUILD_WEBHOOKS.compile(self.GET, guild_id=guild_id)
        return await self._request(route)

    async def get_webhook(self, webhook_id: str) -> containers.JSONObject:
        """
        Gets a given webhook.

        Args:
            webhook_id:
                The ID of the webhook to get.

        Returns:
            The requested webhook object.

        Raises:
            hikari.net.errors.NotFoundError:
                If the webhook is not found.
        """
        route = routes.WEBHOOK.compile(self.GET, webhook_id=webhook_id)
        return await self._request(route)

    async def modify_webhook(
        self,
        webhook_id: str,
        *,
        name: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        avatar: type_hints.NullableNotRequired[bytes] = unspecified.UNSPECIFIED,
        channel_id: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        reason: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
    ) -> containers.JSONObject:
        """
        Edits a given webhook.

        Args:
            webhook_id:
                The ID of the webhook to edit.
            name:
                The new name string.
            avatar:
                The new avatar image in bytes form. If unspecified, it is not changed, but if None, then
                it is removed.
            channel_id:
                The ID of the new channel the given webhook should be moved to.
            reason:
                An optional audit log reason explaining why the change was made.

        Returns:
            The updated webhook object.

        Raises:
            hikari.net.errors.NotFoundError:
                If either the webhook or the channel aren't found.
            hikari.net.errors.ForbiddenError:
                If you either lack the `MANAGE_WEBHOOKS` permission or aren't a member of the guild this webhook belongs
                to.
        """
        payload = {}
        transformations.put_if_specified(payload, "name", name)
        transformations.put_if_specified(payload, "channel_id", channel_id)
        transformations.put_if_specified(payload, "avatar", avatar, conversions.image_bytes_to_image_data)
        route = routes.WEBHOOK.compile(self.PATCH, webhook_id=webhook_id)
        return await self._request(route, json_body=payload, reason=reason)

    async def delete_webhook(self, webhook_id: str) -> None:
        """
        Deletes a given webhook.

        Args:
            webhook_id:
                The ID of the webhook to delete

        Raises:
            hikari.net.errors.NotFoundError:
                If the webhook is not found.
            hikari.net.errors.ForbiddenError:
                If you're not the webhook owner.
        """
        route = routes.WEBHOOK.compile(self.DELETE, webhook_id=webhook_id)
        await self._request(route)


__all__ = ["HTTPClient"]

from __future__ import annotations

import asyncio
import re
from typing import Dict, Optional, Type
from zoneinfo import ZoneInfo

from maubot import Plugin, MessageEvent
from maubot.handlers import command, event
from mautrix.types import (
    EventType,
    Membership,
    MessageType,
    RoomID,
    UserID,
    EventID,
    StateEvent,
    Format,
)
from mautrix.util.config import BaseProxyConfig

from .config import Config
from .db import upgrade_table
from .flow import StandupFlow, FlowState, StandupItem, new_flow
from .state_events import UserConfigCache, STATE_PREVIOUS_POST, state_key
from .post import (
    CHECKMARK,
    RED_X,
    format_post,
    show_message_preview,
    edit_preview,
    send_to_send_room,
    go_to_state_and_notify,
)
from .scheduler import NotificationScheduler


class StandupBot(Plugin):
    flows: Dict[str, StandupFlow]
    user_config: UserConfigCache
    _notify_task: Optional[asyncio.Task]

    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()
        self.flows = {}
        self.user_config = UserConfigCache(self.client, self.log)
        loaded = await StandupFlow.load_all(self.database)
        self.flows = loaded
        await self.user_config.populate_from_joined_rooms()
        self._scheduler = NotificationScheduler(
            client=self.client,
            log=self.log,
            user_config=self.user_config,
            flows=self.flows,
            start_flow_callback=self._start_new_flow,
        )
        self._notify_task = asyncio.create_task(self._scheduler.run())

    async def stop(self) -> None:
        if self._notify_task is not None:
            self._notify_task.cancel()
            try:
                await self._notify_task
            except asyncio.CancelledError:
                pass

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls):
        return upgrade_table

    # ── Commands ──────────────────────────────────────────────────────

    @command.new(name="standupbot", aliases=("su",), require_subcommand=False)
    async def cmd(self, evt: MessageEvent) -> None:
        await self._do_help(evt)

    @cmd.subcommand("new")
    async def cmd_new(self, evt: MessageEvent) -> None:
        await self._handle_new(evt)

    @cmd.subcommand("help")
    async def cmd_help(self, evt: MessageEvent) -> None:
        await self._do_help(evt)

    @cmd.subcommand("show")
    async def cmd_show(self, evt: MessageEvent) -> None:
        await self._handle_show(evt)

    @cmd.subcommand("edit")
    @command.argument("section", pass_raw=True)
    async def cmd_edit(self, evt: MessageEvent, section: str) -> None:
        await self._handle_edit(evt, section)

    @cmd.subcommand("cancel")
    async def cmd_cancel(self, evt: MessageEvent) -> None:
        await self._handle_cancel(evt)

    @cmd.subcommand("undo")
    async def cmd_undo(self, evt: MessageEvent) -> None:
        await self._handle_undo(evt)

    @cmd.subcommand("tz")
    @command.argument("timezone", required=False, pass_raw=True)
    async def cmd_tz(self, evt: MessageEvent, timezone: str) -> None:
        await self._handle_timezone(evt, timezone if timezone else None)

    @cmd.subcommand("notify")
    @command.argument("time_spec", required=False, pass_raw=True)
    async def cmd_notify(self, evt: MessageEvent, time_spec: str) -> None:
        await self._handle_notify(evt, time_spec if time_spec else None)

    @cmd.subcommand("room")
    @command.argument("room_id", required=False, pass_raw=True)
    async def cmd_room(self, evt: MessageEvent, room_id: str) -> None:
        await self._handle_room(evt, room_id if room_id else None)

    @cmd.subcommand("threads")
    @command.argument("enabled", required=False, pass_raw=True)
    async def cmd_threads(self, evt: MessageEvent, enabled: str) -> None:
        await self._handle_threads(evt, enabled if enabled else None)

    @cmd.subcommand("vanquish")
    async def cmd_vanquish(self, evt: MessageEvent) -> None:
        await self.client.leave_room(evt.room_id)

    # ── Event handlers ────────────────────────────────────────────────

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, evt: MessageEvent) -> None:
        if evt.sender == self.client.mxid:
            return
        content = evt.content
        if content.msgtype != MessageType.TEXT:
            return
        body = content.body or ""
        if body.startswith("!"):
            return

        cmd_text = self._extract_mention_command(body)
        if cmd_text is not None:
            await self._dispatch_mention_command(evt, cmd_text)
        else:
            await self._handle_flow_message(evt)

    @event.on(EventType.REACTION)
    async def on_reaction(self, evt) -> None:
        if evt.sender == self.client.mxid:
            return
        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if not flow or flow.state == FlowState.NOT_STARTED:
            return

        relates_to = evt.content.relates_to
        reacted_event_id = str(relates_to.event_id)
        if reacted_event_id not in flow.reactable_events:
            return

        await self.client.send_receipt(evt.room_id, evt.event_id)

        key = relates_to.key
        if key == CHECKMARK:
            await self._handle_checkmark(evt, flow)
        elif key == RED_X:
            await self._handle_red_x(evt, flow)

    @event.on(EventType.ROOM_REDACTION)
    async def on_redaction(self, evt) -> None:
        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if not flow:
            return

        redacts = str(evt.redacts)
        removed = await flow.remove_item_by_event_id(redacts, self.database)
        if removed and flow.preview_event_id:
            await edit_preview(
                self.client, str(evt.room_id), uid, flow,
            )

    @event.on(EventType.ROOM_MEMBER)
    async def on_member(self, evt: StateEvent) -> None:
        if (
            evt.state_key == str(self.client.mxid)
            and evt.content.membership == Membership.INVITE
        ):
            await self.client.join_room(evt.room_id)

    # ── Mention parsing ──────────────────────────────────────────────

    def _extract_mention_command(self, body: str) -> Optional[str]:
        localpart = str(self.client.mxid).split(":")[0].lstrip("@")
        body_stripped = body.strip()
        lower = body_stripped.lower()

        prefixes = [
            f"@{localpart}:",
            f"@{localpart}",
            f"{localpart}:",
        ]
        for prefix in prefixes:
            if lower.startswith(prefix.lower()):
                remainder = body_stripped[len(prefix):].strip()
                return remainder if remainder else "help"
        return None

    async def _dispatch_mention_command(self, evt: MessageEvent, cmd_text: str) -> None:
        parts = cmd_text.split(None, 1)
        cmd_name = parts[0].lower() if parts else "help"
        args = parts[1] if len(parts) > 1 else ""

        dispatch = {
            "new": lambda: self._handle_new(evt),
            "help": lambda: self._do_help(evt),
            "show": lambda: self._handle_show(evt),
            "edit": lambda: self._handle_edit(evt, args),
            "cancel": lambda: self._handle_cancel(evt),
            "undo": lambda: self._handle_undo(evt),
            "tz": lambda: self._handle_timezone(evt, args if args else None),
            "notify": lambda: self._handle_notify(evt, args if args else None),
            "room": lambda: self._handle_room(evt, args if args else None),
            "threads": lambda: self._handle_threads(evt, args if args else None),
            "vanquish": lambda: self.client.leave_room(evt.room_id),
        }

        handler = dispatch.get(cmd_name, lambda: self._do_help(evt))
        await handler()

    # ── Flow message handling ─────────────────────────────────────────

    async def _handle_flow_message(self, evt: MessageEvent) -> None:
        config_room = self.user_config.get_config_room(evt.sender)
        if config_room != evt.room_id:
            return

        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if not flow:
            return

        content = evt.content
        relates_to = content.relates_to
        if relates_to:
            rel_type = relates_to.rel_type if hasattr(relates_to, "rel_type") else None
            if rel_type is None and hasattr(relates_to, "type"):
                rel_type = relates_to.type

            if rel_type == "m.replace":
                await self._handle_edit_event(evt, flow)
                await self.client.send_receipt(evt.room_id, evt.event_id)
                return
            elif rel_type in ("m.in_reply_to", "io.element.thread", "m.thread"):
                await self._handle_reply_event(evt, flow)
                await self.client.send_receipt(evt.room_id, evt.event_id)
                return

        if flow.state in (FlowState.DONE, FlowState.PLANNED, FlowState.BLOCKERS, FlowState.NOTES):
            await flow.add_item(
                flow.state,
                str(evt.event_id),
                content.body or "",
                getattr(content, "formatted_body", "") or "",
                self.database,
            )
            await self.client.react(evt.room_id, evt.event_id, CHECKMARK)
            flow.reactable_events.append(str(evt.event_id))
            await flow.save(self.database, uid)
            await self.client.send_receipt(evt.room_id, evt.event_id)

    # ── Command implementations ───────────────────────────────────────

    async def _handle_new(self, evt: MessageEvent) -> None:
        self.user_config.set_config_room(evt.sender, evt.room_id)
        flow = new_flow()
        flow.room_id = str(evt.room_id)
        self.flows[str(evt.sender)] = flow

        use_threads = self.user_config.get_use_threads(evt.sender)
        next_state = FlowState.THREADS if use_threads else FlowState.DONE

        await go_to_state_and_notify(
            self.client, str(evt.room_id), str(evt.sender), flow, next_state, self.database,
        )
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _do_help(self, evt: MessageEvent) -> None:
        version = self.config.get("version", "unknown")
        source_url = self.config.get("source_url", "https://gitlab.com/beeper/standupbot/")

        notice_text = (
            "COMMANDS:\n"
            "* new -- prepare a new standup post\n"
            "* show -- show the current standup post\n"
            "* edit [Done|Planned|Blockers|Notes] -- edit the given section of the standup post\n"
            "* cancel -- cancel the current standup post\n"
            "* undo -- undo sending the current standup post to the send room\n"
            "* help -- show this help\n"
            "* vanquish -- tell the bot to leave the room\n"
            "* tz [timezone] -- show or set the timezone to use for configuring notifications\n"
            "* notify [time]|stop -- show or set the time at which the standup notification will be sent\n"
            "* room [room alias or ID] -- show or set the room where your standup notification will be sent\n"
            "* threads [true|false] -- whether or not to use threads for composing standup posts\n"
            f"\nVersion {version}. Source code: {source_url}"
        )
        notice_html = (
            "<b>COMMANDS:</b>\n<ul>"
            "<li><b>new</b> &mdash; prepare a new standup post</li>"
            "<li><b>show</b> &mdash; show the current standup post</li>"
            "<li><b>edit [Done|Planned|Blockers|Notes]</b> &mdash; edit the given section of the standup post</li>"
            "<li><b>cancel</b> &mdash; cancel the current standup post</li>"
            "<li><b>undo</b> &mdash; undo sending the current standup post to the send room</li>"
            "<li><b>help</b> &mdash; show this help</li>"
            "<li><b>vanquish</b> &mdash; tell the bot to leave the room</li>"
            "<li><b>tz [timezone]</b> &mdash; show or set the timezone to use for configuring notifications</li>"
            "<li><b>notify [time]|stop</b> &mdash; show or set the time at which the standup notification will be sent</li>"
            "<li><b>room [room alias or ID]</b> &mdash; show or set the room where your standup notification will be sent</li>"
            "<li><b>threads [true|false]</b> &mdash; whether or not to use threads for composing standup posts</li>"
            "</ul>\n"
            f'Version {version}. <a href="{source_url}">Source code</a>.'
        )

        await self.client.send_message_event(
            evt.room_id,
            EventType.ROOM_MESSAGE,
            {
                "msgtype": "m.notice",
                "body": notice_text,
                "format": "org.matrix.custom.html",
                "formatted_body": notice_html,
            },
        )
        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_show(self, evt: MessageEvent) -> None:
        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if flow and flow.state != FlowState.NOT_STARTED:
            content = format_post(uid, flow, preview=True, send_confirmation=False, is_edit_of_existing=False)
            await self.client.send_message_event(evt.room_id, EventType.ROOM_MESSAGE, content)
        else:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.text", "body": "No standup post to show."},
            )
        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_edit(self, evt: MessageEvent, section: str) -> None:
        section = section.strip()
        if self.user_config.get_use_threads(evt.sender):
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {
                    "msgtype": "m.notice",
                    "body": "You cannot use !edit when using threads. Just reply to the corresponding thread.",
                },
            )
            self.user_config.set_config_room(evt.sender, evt.room_id)
            await self.client.send_receipt(evt.room_id, evt.event_id)
            return

        section_map = {
            "done": FlowState.DONE,
            "planned": FlowState.PLANNED,
            "blockers": FlowState.BLOCKERS,
            "notes": FlowState.NOTES,
        }
        target = section_map.get(section.lower())
        if target is None:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {
                    "msgtype": "m.notice",
                    "body": "Invalid item to edit! Must be one of Done, Planned, Blockers, or Notes",
                },
            )
            self.user_config.set_config_room(evt.sender, evt.room_id)
            await self.client.send_receipt(evt.room_id, evt.event_id)
            return

        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if not flow or flow.state == FlowState.NOT_STARTED:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "No standup post to edit."},
            )
            self.user_config.set_config_room(evt.sender, evt.room_id)
            await self.client.send_receipt(evt.room_id, evt.event_id)
            return

        await go_to_state_and_notify(
            self.client, str(evt.room_id), uid, flow, target, self.database,
        )
        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_cancel(self, evt: MessageEvent) -> None:
        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if not flow or flow.state == FlowState.NOT_STARTED:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "No standup post to cancel."},
            )
        else:
            self.flows[uid] = new_flow()
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "Standup post cancelled"},
            )
        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_undo(self, evt: MessageEvent) -> None:
        uid = str(evt.sender)
        flow = self.flows.get(uid)
        if not flow or flow.state != FlowState.SENT:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "No sent standup post to undo."},
            )
            self.user_config.set_config_room(evt.sender, evt.room_id)
            await self.client.send_receipt(evt.room_id, evt.event_id)
            return

        send_room = self.user_config.get_send_room(evt.sender)
        if not send_room:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "No send room configured. Can't undo anything."},
            )
            self.user_config.set_config_room(evt.sender, evt.room_id)
            await self.client.send_receipt(evt.room_id, evt.event_id)
            return

        sk = state_key(str(evt.sender))
        try:
            previous_post = await self.client.get_state_event(evt.room_id, STATE_PREVIOUS_POST, sk)
        except Exception:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "No previous standup post to undo."},
            )
            self.user_config.set_config_room(evt.sender, evt.room_id)
            await self.client.send_receipt(evt.room_id, evt.event_id)
            return

        edit_event_id = previous_post.get("EditEventID", "")
        try:
            await self.client.redact(send_room, EventID(edit_event_id))
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {
                    "msgtype": "m.text",
                    "body": f"Redacted standup post with ID: {edit_event_id} in {evt.room_id}",
                },
            )
            flow.state = FlowState.CONFIRM
            await flow.save(self.database, uid)
            await self.client.send_state_event(evt.room_id, STATE_PREVIOUS_POST, {}, state_key=sk)
        except Exception:
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.text", "body": "Failed to redact the standup post!"},
            )

        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_timezone(self, evt: MessageEvent, tz: Optional[str]) -> None:
        if not tz:
            user_tz = self.user_config.get_timezone(evt.sender)
            tz_str = str(user_tz) if user_tz else "not set"
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": f"Timezone is set to {tz_str}"},
            )
        else:
            tz = tz.strip()
            try:
                location = ZoneInfo(tz)
            except (KeyError, Exception):
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {
                        "msgtype": "m.notice",
                        "body": f"{tz} is not a recognized timezone. Use the name corresponding to a file in the IANA Time Zone database, such as 'America/New_York'",
                    },
                )
                self.user_config.set_config_room(evt.sender, evt.room_id)
                await self.client.send_receipt(evt.room_id, evt.event_id)
                return

            try:
                await self.user_config.set_timezone(evt.sender, evt.room_id, str(location))
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {"msgtype": "m.notice", "body": f"Timezone set to {location}"},
                )
            except Exception as e:
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {
                        "msgtype": "m.notice",
                        "body": f"Failed setting timezone: {e}\nCheck to make sure that standupbot is a mod/admin in the room!",
                    },
                )

        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_notify(self, evt: MessageEvent, time_spec: Optional[str]) -> None:
        if not time_spec:
            minutes = self.user_config.get_notify_minutes(evt.sender)
            if minutes is None:
                text = "Notification time is not set"
            else:
                h = minutes // 60
                m = minutes % 60
                text = f"Notification time is set to {h:02d}:{m:02d}"
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": text},
            )
        elif time_spec.strip().lower() == "stop":
            try:
                await self.user_config.clear_notify(evt.sender, evt.room_id)
                text = "Notifications successfully disabled"
            except Exception:
                text = "Failed to disable notifications"
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": text},
            )
        else:
            time_spec = time_spec.strip()
            match = re.match(r"(\d\d?):?(\d\d)", time_spec)
            if not match:
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {
                        "msgtype": "m.notice",
                        "body": f"{time_spec} is not a valid time. Please specify it in 24-hour time like: 13:30.",
                    },
                )
            else:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
                    await self.client.send_message_event(
                        evt.room_id,
                        EventType.ROOM_MESSAGE,
                        {
                            "msgtype": "m.notice",
                            "body": f"{time_spec} is not a valid time. Please specify it in 24-hour time like: 13:30.",
                        },
                    )
                else:
                    minutes_after_midnight = hours * 60 + minutes
                    try:
                        await self.user_config.set_notify(evt.sender, evt.room_id, minutes_after_midnight)
                        text = f"Notification time set to {hours:02d}:{minutes:02d}"
                    except Exception as e:
                        text = f"Failed setting notification time: {e}\nCheck to make sure that standupbot is a mod/admin in the room!"
                    await self.client.send_message_event(
                        evt.room_id,
                        EventType.ROOM_MESSAGE,
                        {"msgtype": "m.notice", "body": text},
                    )

        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_room(self, evt: MessageEvent, room_id: Optional[str]) -> None:
        if not room_id:
            send_room = self.user_config.get_send_room(evt.sender)
            if send_room:
                text = f"Send room is set to {send_room}"
            else:
                text = "Send room not set"
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": text},
            )
        else:
            room_id = room_id.strip()
            try:
                resp = await self.client.join_room(room_id)
                joined_room_id = resp.room_id if hasattr(resp, "room_id") else RoomID(room_id)
                await self.user_config.set_send_room(evt.sender, evt.room_id, joined_room_id)
                text = f"Joined {room_id} and set that as your send room"
            except Exception as e:
                text = f"Could not join room {room_id}: {e}"
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {"msgtype": "m.notice", "body": text},
                )
                self.user_config.set_config_room(evt.sender, evt.room_id)
                await self.client.send_receipt(evt.room_id, evt.event_id)
                return

            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": text},
            )

            uid = str(evt.sender)
            flow = self.flows.get(uid)
            if flow and flow.state == FlowState.CONFIRM:
                try:
                    await self.client.redact(evt.room_id, EventID(flow.preview_event_id))
                except Exception:
                    pass
                await show_message_preview(
                    self.client, str(evt.room_id), uid, flow, False,
                )
                await flow.save(self.database, uid)

        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    async def _handle_threads(self, evt: MessageEvent, enabled: Optional[str]) -> None:
        if not enabled:
            use_threads = self.user_config.get_use_threads(evt.sender)
            if use_threads:
                text = "Using threads is enabled."
            else:
                text = "Using threads is not enabled."
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": text},
            )
        else:
            enabled = enabled.strip().lower()
            if enabled == "true":
                value = True
            elif enabled == "false":
                value = False
            else:
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {
                        "msgtype": "m.notice",
                        "body": f"Failed setting use threads option: {enabled} is not valid. Use 'true' or 'false'.",
                    },
                )
                self.user_config.set_config_room(evt.sender, evt.room_id)
                await self.client.send_receipt(evt.room_id, evt.event_id)
                return

            try:
                await self.user_config.set_use_threads(evt.sender, evt.room_id, value)
                text = f"Set use threads option to {str(value).lower()}"
            except Exception as e:
                text = f"Failed setting use threads option: {e}"
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": text},
            )

        self.user_config.set_config_room(evt.sender, evt.room_id)
        await self.client.send_receipt(evt.room_id, evt.event_id)

    # ── Reaction handling ─────────────────────────────────────────────

    async def _handle_checkmark(self, evt, flow: StandupFlow) -> None:
        uid = str(evt.sender)
        flow.reactable_events = []

        sk = state_key(uid)
        previous_post = None
        try:
            previous_post = await self.client.get_state_event(evt.room_id, STATE_PREVIOUS_POST, sk)
            if not previous_post or not previous_post.get("FlowID"):
                previous_post = None
        except Exception:
            previous_post = None

        if previous_post and flow.flow_id == previous_post.get("FlowID", ""):
            if flow.state != FlowState.SENT:
                try:
                    await self.client.redact(evt.room_id, EventID(flow.preview_event_id))
                except Exception:
                    pass
                await show_message_preview(
                    self.client, str(evt.room_id), uid, flow, False,
                )
                flow.state = FlowState.SENT
                await flow.save(self.database, uid)
                return
        elif flow.preview_event_id:
            if flow.state not in (FlowState.CONFIRM, FlowState.SENT, FlowState.THREADS):
                try:
                    await self.client.redact(evt.room_id, EventID(flow.preview_event_id))
                except Exception:
                    pass
                flow.state = FlowState.NOTES

        if flow.state == FlowState.DONE:
            await go_to_state_and_notify(
                self.client, str(evt.room_id), uid, flow, FlowState.PLANNED, self.database,
            )
        elif flow.state == FlowState.PLANNED:
            await go_to_state_and_notify(
                self.client, str(evt.room_id), uid, flow, FlowState.BLOCKERS, self.database,
            )
        elif flow.state == FlowState.BLOCKERS:
            await go_to_state_and_notify(
                self.client, str(evt.room_id), uid, flow, FlowState.NOTES, self.database,
            )
        elif flow.state == FlowState.NOTES:
            await show_message_preview(
                self.client, str(evt.room_id), uid, flow, False,
            )
            flow.state = FlowState.CONFIRM
            await flow.save(self.database, uid)
        elif flow.state in (FlowState.THREADS, FlowState.CONFIRM):
            sent_event_id = await send_to_send_room(
                self.client, self.user_config, str(evt.room_id), uid, flow,
            )
            if sent_event_id:
                sk = state_key(uid)
                await self.client.send_state_event(
                    evt.room_id,
                    STATE_PREVIOUS_POST,
                    {
                        "EditEventID": sent_event_id,
                        "FlowID": flow.flow_id,
                        "Day": self.user_config.get_current_weekday_in_user_timezone(uid),
                        "PlannedItems": [
                            {
                                "EventID": i.event_id,
                                "Body": i.body,
                                "FormattedBody": i.formatted_body,
                            }
                            for i in flow.planned
                        ],
                    },
                    state_key=sk,
                )
            await flow.save(self.database, uid)
        elif flow.state == FlowState.SENT:
            if not previous_post:
                await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {"msgtype": "m.text", "body": "No previous post info found!"},
                )
                self.flows[uid] = new_flow()
                return
            sent_event_id = await send_to_send_room(
                self.client,
                self.user_config,
                str(evt.room_id),
                uid,
                flow,
                edit_event_id=previous_post.get("EditEventID"),
            )
            if sent_event_id:
                sk = state_key(uid)
                await self.client.send_state_event(
                    evt.room_id,
                    STATE_PREVIOUS_POST,
                    {
                        "EditEventID": sent_event_id,
                        "FlowID": flow.flow_id,
                        "Day": self.user_config.get_current_weekday_in_user_timezone(uid),
                        "PlannedItems": [
                            {
                                "EventID": i.event_id,
                                "Body": i.body,
                                "FormattedBody": i.formatted_body,
                            }
                            for i in flow.planned
                        ],
                    },
                    state_key=sk,
                )
            await flow.save(self.database, uid)

    async def _handle_red_x(self, evt, flow: StandupFlow) -> None:
        if flow.state in (FlowState.CONFIRM, FlowState.SENT):
            self.flows[str(evt.sender)] = new_flow()
            await self.client.send_message_event(
                evt.room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.notice", "body": "Standup post cancelled"},
            )

    # ── Edit / reply event handling ───────────────────────────────────

    async def _handle_edit_event(self, evt: MessageEvent, flow: StandupFlow) -> None:
        uid = str(evt.sender)
        content = evt.content
        relates_to = content.relates_to
        edit_event_id = str(relates_to.event_id)

        new_content = getattr(content, "new_content", None)
        if new_content is None:
            return

        new_body = getattr(new_content, "body", "") or ""
        new_formatted = getattr(new_content, "formatted_body", "") or ""

        updated = await flow.update_item(edit_event_id, new_body, new_formatted, self.database)
        if updated:
            if flow.state == FlowState.THREADS:
                await edit_preview(self.client, str(evt.room_id), uid, flow)
            elif flow.state == FlowState.CONFIRM:
                await edit_preview(self.client, str(evt.room_id), uid, flow)
            elif flow.state == FlowState.SENT:
                try:
                    await self.client.redact(evt.room_id, EventID(flow.preview_event_id))
                except Exception:
                    pass
                await show_message_preview(
                    self.client, str(evt.room_id), uid, flow, True,
                )
                await flow.save(self.database, uid)

    async def _handle_reply_event(self, evt: MessageEvent, flow: StandupFlow) -> None:
        if flow.state not in (FlowState.THREADS, FlowState.CONFIRM, FlowState.SENT):
            return

        uid = str(evt.sender)
        content = evt.content
        relates_to = content.relates_to

        reply_to_id = None
        if hasattr(relates_to, "event_id") and relates_to.event_id:
            reply_to_id = str(relates_to.event_id)
        elif hasattr(relates_to, "in_reply_to") and relates_to.in_reply_to:
            reply_to_id = str(relates_to.in_reply_to.event_id) if hasattr(relates_to.in_reply_to, "event_id") else str(relates_to.in_reply_to)

        if not reply_to_id:
            return

        body = content.body or ""
        formatted_body = ""
        if hasattr(content, "formatted_body"):
            formatted_body = content.formatted_body or ""
        elif hasattr(content, "get"):
            formatted_body = content.get("formatted_body", "")

        added = await flow.add_thread_reply(
            str(evt.event_id),
            reply_to_id,
            body,
            formatted_body,
            self.database,
        )

        if added:
            await edit_preview(self.client, str(evt.room_id), uid, flow)

            if flow.state == FlowState.SENT and not flow.resend_event_id:
                resp = await self.client.send_message_event(
                    evt.room_id,
                    EventType.ROOM_MESSAGE,
                    {
                        "msgtype": "m.text",
                        "body": f"Send Edit ({CHECKMARK}) or Cancel ({RED_X})?",
                        "format": "org.matrix.custom.html",
                        "formatted_body": f"Send Edit ({CHECKMARK}) or Cancel ({RED_X})?",
                    },
                )
                await self.client.react(evt.room_id, resp.event_id, CHECKMARK)
                await self.client.react(evt.room_id, resp.event_id, RED_X)
                flow.reactable_events.append(resp.event_id)
                flow.resend_event_id = resp.event_id
                await flow.save(self.database, uid)

    # ── Scheduler callback ────────────────────────────────────────────

    async def _start_new_flow(self, room_id: str, user_id: str) -> None:
        flow = new_flow()
        flow.room_id = room_id
        self.flows[user_id] = flow

        use_threads = self.user_config.get_use_threads(UserID(user_id))
        next_state = FlowState.THREADS if use_threads else FlowState.DONE

        await go_to_state_and_notify(
            self.client, room_id, user_id, flow, next_state, self.database,
        )

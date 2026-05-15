from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Awaitable, Dict, TYPE_CHECKING

from mautrix.types import EventType, RoomID, UserID

from .flow import FlowState

if TYPE_CHECKING:
    from .flow import StandupFlow
    from .state_events import UserConfigCache


class NotificationScheduler:
    def __init__(
        self,
        client,
        log,
        user_config: UserConfigCache,
        flows: Dict[str, StandupFlow],
        start_flow_callback: Callable[[str, str], Awaitable[None]],
    ):
        self.client = client
        self.log = log
        self.user_config = user_config
        self.flows = flows
        self.start_flow_callback = start_flow_callback

    async def run(self) -> None:
        self.log.debug("Starting notification loop")
        try:
            while True:
                try:
                    users = self.user_config.get_users_to_notify_now()
                    for user_id, room_id in users.items():
                        await self._notify_user(user_id, room_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.log.exception("Error in notification loop iteration")

                seconds_until_next_minute = 60 - datetime.now(timezone.utc).second
                await asyncio.sleep(seconds_until_next_minute)
        except asyncio.CancelledError:
            self.log.debug("Notification loop cancelled")

    async def _notify_user(self, user_id: UserID, room_id: RoomID) -> None:
        self.log.info(f"Notifying {user_id}")
        uid = str(user_id)
        flow = self.flows.get(uid)

        if (
            flow is None
            or flow.state == FlowState.NOT_STARTED
            or flow.state == FlowState.SENT
        ):
            await self.client.send_message_event(
                room_id,
                EventType.ROOM_MESSAGE,
                {"msgtype": "m.text", "body": "Time to write your standup post!"},
            )
            await self.start_flow_callback(str(room_id), uid)
        else:
            await self.client.send_message_event(
                room_id,
                EventType.ROOM_MESSAGE,
                {
                    "msgtype": "m.text",
                    "body": "Looks like you are already writing a standup post! If you want to start over, type `!standupbot new`",
                    "format": "org.matrix.custom.html",
                    "formatted_body": "Looks like you are already writing a standup post! If you want to start over, type <code>!standupbot new</code>",
                },
            )

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from mautrix.types import EventType, RoomID, UserID

STATE_TZ = EventType.find("com.nevarro.standupbot.timezone", t_class=EventType.Class.STATE)
STATE_NOTIFY = EventType.find("com.nevarro.standupbot.notify", t_class=EventType.Class.STATE)
STATE_SEND_ROOM = EventType.find("com.nevarro.standupbot.send_room", t_class=EventType.Class.STATE)
STATE_USE_THREADS = EventType.find("com.nevarro.standupbot.use_threads", t_class=EventType.Class.STATE)
STATE_PREVIOUS_POST = EventType.find("com.nevarro.standupbot.previous_post", t_class=EventType.Class.STATE)


def state_key(user_id: UserID) -> str:
    return str(user_id).lstrip("@")


@dataclass
class UserSettings:
    config_room: Optional[RoomID] = None
    timezone: Optional[str] = None
    notify_minutes: Optional[int] = None
    send_room: Optional[RoomID] = None
    use_threads: bool = False


class UserConfigCache:
    def __init__(self, client, log):
        self.client = client
        self.log = log
        self._users: Dict[UserID, UserSettings] = {}

    def _ensure(self, user_id: UserID) -> UserSettings:
        if user_id not in self._users:
            self._users[user_id] = UserSettings()
        return self._users[user_id]

    # -- read methods (cache only) --

    def get_config_room(self, user_id: UserID) -> Optional[RoomID]:
        s = self._users.get(user_id)
        return s.config_room if s else None

    def get_timezone(self, user_id: UserID) -> Optional[ZoneInfo]:
        s = self._users.get(user_id)
        if not s or not s.timezone:
            return None
        try:
            return ZoneInfo(s.timezone)
        except (KeyError, Exception):
            return None

    def get_notify_minutes(self, user_id: UserID) -> Optional[int]:
        s = self._users.get(user_id)
        return s.notify_minutes if s else None

    def get_send_room(self, user_id: UserID) -> Optional[RoomID]:
        s = self._users.get(user_id)
        return s.send_room if s else None

    def get_use_threads(self, user_id: UserID) -> bool:
        s = self._users.get(user_id)
        return s.use_threads if s else False

    # -- write methods --

    def set_config_room(self, user_id: UserID, room_id: RoomID) -> None:
        self._ensure(user_id).config_room = room_id

    async def set_timezone(self, user_id: UserID, room_id: RoomID, tz_str: str) -> None:
        sk = state_key(user_id)
        await self.client.send_state_event(room_id, STATE_TZ, {"TzString": tz_str}, state_key=sk)
        self._ensure(user_id).timezone = tz_str

    async def set_notify(self, user_id: UserID, room_id: RoomID, minutes: int) -> None:
        sk = state_key(user_id)
        await self.client.send_state_event(
            room_id, STATE_NOTIFY, {"MinutesAfterMidnight": minutes}, state_key=sk
        )
        self._ensure(user_id).notify_minutes = minutes

    async def clear_notify(self, user_id: UserID, room_id: RoomID) -> None:
        sk = state_key(user_id)
        await self.client.send_state_event(room_id, STATE_NOTIFY, {}, state_key=sk)
        s = self._users.get(user_id)
        if s:
            s.notify_minutes = None

    async def set_send_room(
        self, user_id: UserID, config_room_id: RoomID, send_room_id: RoomID
    ) -> None:
        sk = state_key(user_id)
        await self.client.send_state_event(
            config_room_id, STATE_SEND_ROOM, {"SendRoomID": str(send_room_id)}, state_key=sk
        )
        self._ensure(user_id).send_room = send_room_id

    async def set_use_threads(self, user_id: UserID, room_id: RoomID, value: bool) -> None:
        sk = state_key(user_id)
        await self.client.send_state_event(
            room_id, STATE_USE_THREADS, {"UseThreads": value}, state_key=sk
        )
        self._ensure(user_id).use_threads = value

    async def populate_from_joined_rooms(self) -> None:
        self.log.info("Loading user settings from joined rooms...")
        try:
            joined_rooms = await self.client.get_joined_rooms()
        except Exception:
            self.log.exception("Failed to get joined rooms")
            return

        for room_id in joined_rooms:
            try:
                members = await self.client.get_joined_members(room_id)
            except Exception:
                continue

            for user_id in members:
                sk = state_key(user_id)

                try:
                    content = await self.client.get_state_event(room_id, STATE_TZ, sk)
                    tz_str = content.get("TzString", "") if content else ""
                    if tz_str:
                        ZoneInfo(tz_str)  # validate
                        self.log.info(f"Loaded timezone ({tz_str}) for {user_id} from state")
                        self.set_config_room(user_id, room_id)
                        self._ensure(user_id).timezone = tz_str
                except Exception:
                    pass

                try:
                    content = await self.client.get_state_event(room_id, STATE_NOTIFY, sk)
                    minutes = content.get("MinutesAfterMidnight") if content else None
                    if minutes is not None:
                        self.log.info(
                            f"Loaded notification minutes ({minutes}) for {user_id} from state"
                        )
                        self.set_config_room(user_id, room_id)
                        self._ensure(user_id).notify_minutes = minutes
                except Exception:
                    pass

                try:
                    content = await self.client.get_state_event(room_id, STATE_SEND_ROOM, sk)
                    send_room = content.get("SendRoomID", "") if content else ""
                    if send_room:
                        self.log.info(f"Loaded send room ({send_room}) for {user_id} from state")
                        self.set_config_room(user_id, room_id)
                        self._ensure(user_id).send_room = RoomID(send_room)
                except Exception:
                    pass

                try:
                    content = await self.client.get_state_event(room_id, STATE_USE_THREADS, sk)
                    if content:
                        use_threads = content.get("UseThreads", False)
                        self.log.info(
                            f"Loaded thread usage setting ({use_threads}) for {user_id} from state"
                        )
                        self.set_config_room(user_id, room_id)
                        self._ensure(user_id).use_threads = use_threads
                except Exception:
                    pass

        self.log.info("Finished loading user settings from joined rooms")

    def get_users_to_notify_now(self) -> Dict[UserID, RoomID]:
        now_utc = datetime.now(timezone.utc)
        current_utc_minutes = now_utc.hour * 60 + now_utc.minute
        result: Dict[UserID, RoomID] = {}

        for user_id, settings in self._users.items():
            if settings.notify_minutes is None or settings.config_room is None:
                continue

            tz = self.get_timezone(user_id)
            if tz is None:
                continue

            local_now = datetime.now(tz)
            if local_now.weekday() >= 5:  # Saturday=5, Sunday=6
                continue

            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            notify_local = local_midnight.replace(
                hour=settings.notify_minutes // 60,
                minute=settings.notify_minutes % 60,
            )
            notify_utc = notify_local.astimezone(timezone.utc)
            notify_utc_minutes = notify_utc.hour * 60 + notify_utc.minute

            if notify_utc_minutes == current_utc_minutes:
                result[user_id] = settings.config_room

        return result

    def get_current_weekday_in_user_timezone(self, user_id: UserID) -> int:
        tz = self.get_timezone(user_id)
        if tz is None:
            return datetime.now(timezone.utc).weekday()
        return datetime.now(tz).weekday()

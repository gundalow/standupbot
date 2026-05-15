from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional

from . import db as db_module


class FlowState(IntEnum):
    NOT_STARTED = 0
    DONE = 1
    PLANNED = 2
    BLOCKERS = 3
    NOTES = 4
    CONFIRM = 5
    SENT = 6
    THREADS = 7


SECTION_NAMES = {
    FlowState.DONE: "done",
    FlowState.PLANNED: "planned",
    FlowState.BLOCKERS: "blockers",
    FlowState.NOTES: "notes",
}


@dataclass
class StandupItem:
    event_id: str
    body: str
    formatted_body: str = ""


@dataclass
class StandupFlow:
    flow_id: str
    state: FlowState = FlowState.NOT_STARTED
    room_id: str = ""
    reactable_events: List[str] = field(default_factory=list)
    preview_event_id: Optional[str] = None
    resend_event_id: Optional[str] = None

    done: List[StandupItem] = field(default_factory=list)
    planned: List[StandupItem] = field(default_factory=list)
    blockers: List[StandupItem] = field(default_factory=list)
    notes: List[StandupItem] = field(default_factory=list)

    done_thread_events: List[str] = field(default_factory=list)
    planned_thread_events: List[str] = field(default_factory=list)
    blockers_thread_events: List[str] = field(default_factory=list)
    notes_thread_events: List[str] = field(default_factory=list)

    def get_section_list(self, state: FlowState) -> List[StandupItem]:
        return {
            FlowState.DONE: self.done,
            FlowState.PLANNED: self.planned,
            FlowState.BLOCKERS: self.blockers,
            FlowState.NOTES: self.notes,
        }[state]

    def get_thread_events(self, state: FlowState) -> List[str]:
        return {
            FlowState.DONE: self.done_thread_events,
            FlowState.PLANNED: self.planned_thread_events,
            FlowState.BLOCKERS: self.blockers_thread_events,
            FlowState.NOTES: self.notes_thread_events,
        }[state]

    async def add_item(
        self,
        section: FlowState,
        event_id: str,
        body: str,
        formatted_body: str,
        db,
    ) -> None:
        items = self.get_section_list(section)
        items.append(StandupItem(event_id=event_id, body=body, formatted_body=formatted_body))
        position = len(items) - 1
        await db_module.save_item(
            db, self.flow_id, SECTION_NAMES[section], event_id, body, formatted_body, position
        )

    async def remove_item_by_event_id(self, event_id: str, db) -> bool:
        for section_list in (self.done, self.planned, self.blockers, self.notes):
            for i, item in enumerate(section_list):
                if item.event_id == event_id:
                    section_list.pop(i)
                    await db_module.remove_item(db, self.flow_id, event_id)
                    return True
        return False

    async def update_item(self, event_id: str, body: str, formatted_body: str, db) -> bool:
        for section_list in (self.done, self.planned, self.blockers, self.notes):
            for item in section_list:
                if item.event_id == event_id:
                    item.body = body
                    item.formatted_body = formatted_body
                    await db_module.update_item(db, self.flow_id, event_id, body, formatted_body)
                    return True
        return False

    async def add_thread_reply(
        self,
        event_id: str,
        reply_to_event_id: str,
        body: str,
        formatted_body: str,
        db,
    ) -> bool:
        for state in (FlowState.DONE, FlowState.PLANNED, FlowState.BLOCKERS, FlowState.NOTES):
            thread_events = self.get_thread_events(state)
            if reply_to_event_id in thread_events:
                section_list = self.get_section_list(state)
                section_list.append(
                    StandupItem(event_id=event_id, body=body, formatted_body=formatted_body)
                )
                position = len(section_list) - 1
                section_name = SECTION_NAMES[state]
                await db_module.save_item(
                    db, self.flow_id, section_name, event_id, body, formatted_body, position
                )
                thread_events.append(event_id)
                await db_module.save_thread_events(db, self.flow_id, section_name, thread_events)
                return True
        return False

    async def save(self, db, user_id: str) -> None:
        await db_module.save_flow(
            db,
            user_id,
            self.flow_id,
            int(self.state),
            self.room_id,
            self.preview_event_id,
            self.resend_event_id,
        )
        await db_module.save_reactable_events(db, self.flow_id, self.reactable_events)
        for state in (FlowState.DONE, FlowState.PLANNED, FlowState.BLOCKERS, FlowState.NOTES):
            section_name = SECTION_NAMES[state]
            await db_module.save_thread_events(
                db, self.flow_id, section_name, self.get_thread_events(state)
            )

    async def delete(self, db, user_id: str) -> None:
        await db_module.delete_flow(db, user_id)

    @classmethod
    async def load_all(cls, db) -> Dict[str, StandupFlow]:
        rows = await db_module.load_all_flows(db)
        flows: Dict[str, StandupFlow] = {}

        for row in rows:
            flow = cls(
                flow_id=row["flow_id"],
                state=FlowState(row["state"]),
                room_id=row["room_id"],
                preview_event_id=row["preview_event_id"],
                resend_event_id=row["resend_event_id"],
            )

            items = await db_module.load_all_items(db, flow.flow_id)
            for item_row in items:
                si = StandupItem(
                    event_id=item_row["event_id"],
                    body=item_row["body"],
                    formatted_body=item_row["formatted_body"],
                )
                section = item_row["section"]
                if section == "done":
                    flow.done.append(si)
                elif section == "planned":
                    flow.planned.append(si)
                elif section == "blockers":
                    flow.blockers.append(si)
                elif section == "notes":
                    flow.notes.append(si)

            flow.reactable_events = await db_module.load_reactable_events(db, flow.flow_id)

            for state in (FlowState.DONE, FlowState.PLANNED, FlowState.BLOCKERS, FlowState.NOTES):
                section_name = SECTION_NAMES[state]
                evts = await db_module.load_thread_events(db, flow.flow_id, section_name)
                if state == FlowState.DONE:
                    flow.done_thread_events = evts
                elif state == FlowState.PLANNED:
                    flow.planned_thread_events = evts
                elif state == FlowState.BLOCKERS:
                    flow.blockers_thread_events = evts
                elif state == FlowState.NOTES:
                    flow.notes_thread_events = evts

            flows[row["user_id"]] = flow

        return flows


def new_flow() -> StandupFlow:
    return StandupFlow(flow_id=str(uuid.uuid4()))

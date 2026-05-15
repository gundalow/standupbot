from __future__ import annotations

from typing import List, Optional

from mautrix.util.async_db import Connection, Scheme, UpgradeTable

upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial schema")
async def upgrade_v1(conn: Connection, scheme: Scheme) -> None:
    if scheme == Scheme.SQLITE:
        id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
    else:
        id_col = "id SERIAL PRIMARY KEY"

    await conn.execute(
        """CREATE TABLE IF NOT EXISTS standup_flow (
            user_id          TEXT NOT NULL,
            flow_id          TEXT NOT NULL,
            state            INTEGER NOT NULL,
            room_id          TEXT NOT NULL,
            preview_event_id TEXT,
            resend_event_id  TEXT,
            created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id)
        )"""
    )

    await conn.execute(
        f"""CREATE TABLE IF NOT EXISTS standup_item (
            {id_col},
            flow_id        TEXT NOT NULL,
            section        TEXT NOT NULL,
            event_id       TEXT NOT NULL,
            body           TEXT NOT NULL,
            formatted_body TEXT NOT NULL DEFAULT '',
            position       INTEGER NOT NULL
        )"""
    )

    await conn.execute(
        """CREATE TABLE IF NOT EXISTS reactable_event (
            flow_id  TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (flow_id, event_id)
        )"""
    )

    await conn.execute(
        """CREATE TABLE IF NOT EXISTS thread_event (
            flow_id  TEXT NOT NULL,
            section  TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (flow_id, section, event_id)
        )"""
    )


async def save_flow(
    db,
    user_id: str,
    flow_id: str,
    state: int,
    room_id: str,
    preview_event_id: Optional[str],
    resend_event_id: Optional[str],
) -> None:
    await db.execute(
        """INSERT INTO standup_flow (user_id, flow_id, state, room_id, preview_event_id,
               resend_event_id, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT (user_id) DO UPDATE SET
               flow_id = $2,
               state = $3,
               room_id = $4,
               preview_event_id = $5,
               resend_event_id = $6,
               updated_at = CURRENT_TIMESTAMP""",
        user_id,
        flow_id,
        state,
        room_id,
        preview_event_id,
        resend_event_id,
    )


async def load_flow(db, user_id: str):
    return await db.fetchrow(
        "SELECT user_id, flow_id, state, room_id, preview_event_id, resend_event_id,"
        " created_at, updated_at FROM standup_flow WHERE user_id = $1",
        user_id,
    )


async def delete_flow(db, user_id: str) -> None:
    row = await db.fetchrow(
        "SELECT flow_id FROM standup_flow WHERE user_id = $1", user_id
    )
    if row:
        flow_id = row["flow_id"]
        await db.execute("DELETE FROM standup_item WHERE flow_id = $1", flow_id)
        await db.execute("DELETE FROM reactable_event WHERE flow_id = $1", flow_id)
        await db.execute("DELETE FROM thread_event WHERE flow_id = $1", flow_id)
    await db.execute("DELETE FROM standup_flow WHERE user_id = $1", user_id)


async def save_item(
    db,
    flow_id: str,
    section: str,
    event_id: str,
    body: str,
    formatted_body: str,
    position: int,
) -> None:
    await db.execute(
        """INSERT INTO standup_item (flow_id, section, event_id, body, formatted_body, position)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        flow_id,
        section,
        event_id,
        body,
        formatted_body,
        position,
    )


async def load_items(db, flow_id: str, section: str) -> list:
    return await db.fetch(
        "SELECT * FROM standup_item WHERE flow_id = $1 AND section = $2 ORDER BY position",
        flow_id,
        section,
    )


async def load_all_items(db, flow_id: str) -> list:
    return await db.fetch(
        "SELECT * FROM standup_item WHERE flow_id = $1 ORDER BY section, position",
        flow_id,
    )


async def update_item(db, flow_id: str, event_id: str, body: str, formatted_body: str) -> None:
    await db.execute(
        "UPDATE standup_item SET body = $1, formatted_body = $2"
        " WHERE flow_id = $3 AND event_id = $4",
        body,
        formatted_body,
        flow_id,
        event_id,
    )


async def remove_item(db, flow_id: str, event_id: str) -> bool:
    result = await db.execute(
        "DELETE FROM standup_item WHERE flow_id = $1 AND event_id = $2",
        flow_id,
        event_id,
    )
    # maubot's execute returns the status string; "DELETE N" where N is rows affected
    if isinstance(result, str):
        return not result.endswith("0")
    return bool(result)


async def save_reactable_events(db, flow_id: str, event_ids: List[str]) -> None:
    await db.execute("DELETE FROM reactable_event WHERE flow_id = $1", flow_id)
    for eid in event_ids:
        await db.execute(
            "INSERT INTO reactable_event (flow_id, event_id) VALUES ($1, $2)",
            flow_id,
            eid,
        )


async def load_reactable_events(db, flow_id: str) -> List[str]:
    rows = await db.fetch(
        "SELECT event_id FROM reactable_event WHERE flow_id = $1", flow_id
    )
    return [row["event_id"] for row in rows]


async def save_thread_events(db, flow_id: str, section: str, event_ids: List[str]) -> None:
    await db.execute(
        "DELETE FROM thread_event WHERE flow_id = $1 AND section = $2",
        flow_id,
        section,
    )
    for eid in event_ids:
        await db.execute(
            "INSERT INTO thread_event (flow_id, section, event_id) VALUES ($1, $2, $3)",
            flow_id,
            section,
            eid,
        )


async def load_thread_events(db, flow_id: str, section: str) -> List[str]:
    rows = await db.fetch(
        "SELECT event_id FROM thread_event WHERE flow_id = $1 AND section = $2",
        flow_id,
        section,
    )
    return [row["event_id"] for row in rows]


async def load_all_flows(db) -> list:
    return await db.fetch(
        "SELECT user_id, flow_id, state, room_id, preview_event_id, resend_event_id,"
        " created_at, updated_at FROM standup_flow"
    )

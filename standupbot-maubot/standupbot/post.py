from __future__ import annotations

import re
from typing import Optional

from mautrix.types import EventType, MessageType, RoomID

from .flow import FlowState, StandupFlow, StandupItem

CHECKMARK = "✅"
RED_X = "❌"


def trim_reply_fallback_text(body: str) -> str:
    if not body.startswith("> "):
        return body
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith("> "):
        i += 1
    if i < len(lines) and lines[i] == "":
        i += 1
    return "\n".join(lines[i:])


def trim_reply_fallback_html(html: str) -> str:
    if not html:
        return html
    match = re.match(r"^<mx-reply>.*?</mx-reply>", html, re.DOTALL)
    if match:
        return html[match.end():]
    return html

SECTION_QUESTIONS = {
    FlowState.DONE: "What did you get done since last time?",
    FlowState.PLANNED: "What are you planning to do next?",
    FlowState.BLOCKERS: "Do you have any blockers?",
    FlowState.NOTES: "Do you have any other notes?",
}

SECTION_LABELS = {
    FlowState.DONE: "Done",
    FlowState.PLANNED: "Planned",
    FlowState.BLOCKERS: "Blockers",
    FlowState.NOTES: "Notes",
}


def _format_list(items: list[StandupItem]) -> tuple[str, str]:
    plain_lines = []
    html_parts = []
    for item in items:
        plain_lines.append(f"- {item.body}")
        html_parts.append(f"<li>{item.formatted_body or item.body}</li>")
    return "\n".join(plain_lines), "".join(html_parts)


def format_post(
    user_id: str,
    flow: StandupFlow,
    preview: bool,
    send_confirmation: bool,
    is_edit_of_existing: bool,
) -> dict:
    post_text = f"{user_id}'s standup post:\n\n"
    post_html = (
        f'<a href="https://matrix.to/#/{user_id}">{user_id}</a>\'s standup post:<br><br>'
    )

    first_section = True
    for state in (FlowState.DONE, FlowState.PLANNED, FlowState.BLOCKERS, FlowState.NOTES):
        items = flow.get_section_list(state)
        if not items:
            continue
        label = SECTION_LABELS[state]
        plain, html = _format_list(items)
        if not first_section:
            post_text += "\n"
        post_text += f"**{label}**\n{plain}"
        post_html += f"<b>{label}</b><br><ul>{html}</ul>"
        first_section = False

    if preview:
        post_text = (
            "Standup post preview:\n"
            "----------------------------------------\n"
            + post_text
        )
        post_html = "<i>Standup post preview:</i><hr>" + post_html

    if send_confirmation:
        if is_edit_of_existing:
            post_text += (
                f"\n----------------------------------------\n"
                f"Send Edit ({CHECKMARK}) or Cancel ({RED_X})?"
            )
            post_html += f"<hr><b>Send Edit ({CHECKMARK}) or Cancel ({RED_X})?</b>"
        else:
            post_text += (
                f"\n----------------------------------------\n"
                f"Send ({CHECKMARK}) or Cancel ({RED_X})?"
            )
            post_html += f"<hr><b>Send ({CHECKMARK}) or Cancel ({RED_X})?</b>"

    return {
        "msgtype": "m.text",
        "body": post_text,
        "format": "org.matrix.custom.html",
        "formatted_body": post_html,
    }


async def show_message_preview(
    client,
    room_id: str,
    user_id: str,
    flow: StandupFlow,
    is_edit_of_existing: bool,
) -> str:
    content = format_post(
        user_id, flow, preview=True, send_confirmation=True,
        is_edit_of_existing=is_edit_of_existing,
    )
    resp = await client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)
    await client.react(room_id, resp.event_id, CHECKMARK)
    await client.react(room_id, resp.event_id, RED_X)
    flow.preview_event_id = resp.event_id
    flow.reactable_events.append(resp.event_id)
    return resp.event_id


async def edit_preview(
    client,
    room_id: str,
    user_id: str,
    flow: StandupFlow,
) -> None:
    new_content = format_post(
        user_id, flow, preview=True, send_confirmation=True, is_edit_of_existing=False,
    )
    content = {
        "msgtype": "m.text",
        "body": " * " + new_content["body"],
        "format": "org.matrix.custom.html",
        "formatted_body": " * " + new_content["formatted_body"],
        "m.relates_to": {
            "rel_type": "m.replace",
            "event_id": flow.preview_event_id,
        },
        "m.new_content": new_content,
    }
    resp = await client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)
    flow.reactable_events.append(resp.event_id)


async def send_to_send_room(
    client,
    user_config,
    evt_room_id: str,
    sender: str,
    flow: StandupFlow,
    edit_event_id: Optional[str] = None,
) -> Optional[str]:
    send_room = user_config.get_send_room(sender)
    if not send_room:
        await client.send_message_event(
            evt_room_id,
            EventType.ROOM_MESSAGE,
            {
                "msgtype": "m.notice",
                "body": "No send room set! Set one using `!standupbot room [room ID or alias]`.",
                "format": "org.matrix.custom.html",
                "formatted_body": (
                    "No send room set! Set one using <code>!standupbot room [room ID or alias]</code>."
                ),
            },
        )
        return None

    try:
        members = await client.get_joined_members(send_room)
        if sender not in members:
            await client.send_message_event(
                evt_room_id,
                EventType.ROOM_MESSAGE,
                {
                    "msgtype": "m.notice",
                    "body": "**You are not a member of the configured send room!** "
                            "Refusing to send a message to the room. "
                            "Set a new one using `!standupbot room [room ID or alias]`.",
                    "format": "org.matrix.custom.html",
                    "formatted_body": (
                        "<b>You are not a member of the configured send room!</b> "
                        "Refusing to send a message to the room. "
                        "Set a new one using <code>!standupbot room [room ID or alias]</code>."
                    ),
                },
            )
            return None
    except Exception:
        pass

    post = format_post(sender, flow, preview=False, send_confirmation=False, is_edit_of_existing=False)
    post["space.nevarro.msc3464.on_behalf_of"] = sender

    try:
        sent_event_id: Optional[str] = None
        if edit_event_id:
            content = {
                "msgtype": "m.text",
                "body": " * " + post["body"],
                "format": "org.matrix.custom.html",
                "formatted_body": " * " + post["formatted_body"],
                "space.nevarro.msc3464.on_behalf_of": sender,
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": edit_event_id,
                },
                "m.new_content": post,
            }
            await client.send_message_event(send_room, EventType.ROOM_MESSAGE, content)
            sent_event_id = edit_event_id
        else:
            resp = await client.send_message_event(send_room, EventType.ROOM_MESSAGE, post)
            sent_event_id = str(resp.event_id)

        edit_str = " edit" if edit_event_id else ""
        await client.send_message_event(
            evt_room_id,
            EventType.ROOM_MESSAGE,
            {
                "msgtype": "m.notice",
                "body": f"Sent standup post{edit_str} to {send_room}",
            },
        )
        flow.state = FlowState.SENT
        flow.resend_event_id = None
        return sent_event_id
    except Exception:
        edit_str = " edit" if edit_event_id else ""
        await client.send_message_event(
            evt_room_id,
            EventType.ROOM_MESSAGE,
            {
                "msgtype": "m.notice",
                "body": f"Failed to send standup post{edit_str} to {send_room}",
            },
        )
        return None


async def go_to_state_and_notify(
    client,
    room_id: str,
    user_id: str,
    flow: StandupFlow,
    state: FlowState,
    db,
) -> None:
    flow.state = state

    if state == FlowState.THREADS:
        content = {
            "msgtype": "m.text",
            "body": "**Fill out the standup post by replying in each thread.** *Enter one item per message.*",
            "format": "org.matrix.custom.html",
            "formatted_body": (
                "<b>Fill out the standup post by replying in each thread.</b> "
                "<i>Enter one item per message.</i>"
            ),
        }
        resp = await client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)
        flow.reactable_events.append(resp.event_id)

        for section_state, label in SECTION_LABELS.items():
            thread_content = {
                "msgtype": "m.text",
                "body": f"**{label}** *(thread)*",
                "format": "org.matrix.custom.html",
                "formatted_body": f"<b>{label}</b> <i>(thread)</i>",
            }
            resp = await client.send_message_event(room_id, EventType.ROOM_MESSAGE, thread_content)
            thread_events = flow.get_thread_events(section_state)
            thread_events.clear()
            thread_events.append(resp.event_id)

        await show_message_preview(client, room_id, user_id, flow, is_edit_of_existing=False)
    else:
        question = SECTION_QUESTIONS[state]
        content = {
            "msgtype": "m.text",
            "body": f"{question} *Enter one item per message. React with {CHECKMARK} when done.*",
            "format": "org.matrix.custom.html",
            "formatted_body": (
                f"{question} <i>Enter one item per message. React with {CHECKMARK} when done.</i>"
            ),
        }
        resp = await client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)
        await client.react(room_id, resp.event_id, CHECKMARK)
        flow.reactable_events.append(resp.event_id)

    await flow.save(db, user_id)

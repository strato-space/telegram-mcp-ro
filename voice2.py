"""Voice-focused entrypoint for running the Telegram MCP server without
modifying the original ``main.py`` module.

This script imports ``main.py`` so that all global initialisation stays in one
place (Telegram client, logging, MCP tool registrations). It then defines its
own ``if __name__ == "__main__"`` block to start the server, allowing voice-
specific overrides to be added here without touching ``main.py``.

v:0.1
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta

import nest_asyncio

# Import the original module so that all globals are initialised once.
import main as telegram_main  # noqa: F401

try:  # Voice integration for ingesting transcripts
    from voice.src.lib.core import VoicebotClient  # type: ignore
except Exception:  # pragma: no cover - fallback for different layouts
    try:
        from lib.core import VoicebotClient  # type: ignore
    except Exception:  # pragma: no cover - VoicebotClient unavailable
        VoicebotClient = None  # type: ignore


# Re-export frequently used objects for possible local overrides/extensions.
client = telegram_main.client
mcp = telegram_main.mcp
logger = telegram_main.logger
VoicebotClient = getattr(telegram_main, "VoicebotClient", None)
log_and_format_error = getattr(telegram_main, "log_and_format_error", None)
get_sender_name = getattr(telegram_main, "get_sender_name", None)

@mcp.tool()
async def messages_to_voicebot(
    chat_id: int,
    limit: int = 2000,
    search_query: str = None,
    from_date: str = None,
    to_date: str = None,
) -> str:
    """
    Retrieve messages with optional filters and ingest them into the voicebot.

    Args:
        chat_id: The ID of the chat to get messages from.
        limit: Maximum number of messages to retrieve.
        search_query: Filter messages containing this text.
        from_date: Filter messages starting from this date (format: YYYY-MM-DD).
        to_date: Filter messages until this date (format: YYYY-MM-DD).
    """
    try:
        entity = await client.get_entity(chat_id)

        if VoicebotClient is None:
            return "VoicebotClient is not available."

        try:
            voice_client = VoicebotClient()
        except Exception as voice_err:
            logger.exception("VoicebotClient init failed", exc_info=voice_err)
            return log_and_format_error(
                "messages_to_voicebot",
                voice_err,
                chat_id=chat_id,
            )

        # Parse date filters if provided
        from_date_obj = None
        to_date_obj = None

        if from_date:
            try:
                from_date_obj = datetime.strptime(from_date, "%Y-%m-%d")
                # Make it timezone aware by adding UTC timezone info
                # Use datetime.timezone.utc for Python 3.9+ or import timezone directly for 3.13+
                try:
                    # For Python 3.9+
                    from_date_obj = from_date_obj.replace(tzinfo=datetime.timezone.utc)
                except AttributeError:
                    # For Python 3.13+
                    from datetime import timezone

                    from_date_obj = from_date_obj.replace(tzinfo=timezone.utc)
            except ValueError:
                return f"Invalid from_date format. Use YYYY-MM-DD."

        if to_date:
            try:
                to_date_obj = datetime.strptime(to_date, "%Y-%m-%d")
                # Set to end of day and make timezone aware
                to_date_obj = to_date_obj + timedelta(days=1, microseconds=-1)
                # Add timezone info
                try:
                    to_date_obj = to_date_obj.replace(tzinfo=datetime.timezone.utc)
                except AttributeError:
                    from datetime import timezone

                    to_date_obj = to_date_obj.replace(tzinfo=timezone.utc)
            except ValueError:
                return f"Invalid to_date format. Use YYYY-MM-DD."

        # Prepare filter parameters mirroring list_messages
        params = {}
        if search_query:
            params["search"] = search_query
            messages = []
            async for msg in client.iter_messages(entity, **params):
                if to_date_obj and msg.date > to_date_obj:
                    continue
                if from_date_obj and msg.date < from_date_obj:
                    break
                messages.append(msg)
                if len(messages) >= limit:
                    break

        else:
            if from_date_obj or to_date_obj:
                messages = []
                if from_date_obj:
                    async for msg in client.iter_messages(
                        entity, offset_date=from_date_obj, reverse=True
                    ):
                        if to_date_obj and msg.date > to_date_obj:
                            break
                        if msg.date < from_date_obj:
                            continue
                        messages.append(msg)
                        if len(messages) >= limit:
                            break
                else:
                    async for msg in client.iter_messages(
                        entity, offset_date=to_date_obj + timedelta(microseconds=1)
                    ):
                        messages.append(msg)
                        if len(messages) >= limit:
                            break
            else:
                messages = await client.get_messages(entity, limit=limit, **params)

        if not messages:
            return "No messages found matching the criteria."

        lines = []
        session_id = getattr(entity, "username", None) or str(chat_id)
        session_header = f"session: https://voice.stratospace.fun/session/{session_id}"
        lines.append(session_header)

        for msg in messages:
            sender_name = get_sender_name(msg)
            message_text = msg.message or "[Media/No text]"
            reply_info = ""
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                reply_info = f" | reply to {msg.reply_to.reply_to_msg_id}"

            lines.append(
                f"ID: {msg.id} | {sender_name} | Date: {msg.date}{reply_info} | Message: {message_text}"
            )

            try:
                voice_client.ingest_text_message(chat_id, message_text, speaker=sender_name)
            except Exception as ingest_err:
                logger.exception(
                    "Voicebot ingest failed",
                    exc_info=ingest_err,
                    extra={"chat_id": chat_id, "message_id": msg.id},
                )

        return "\n".join(lines)
    except Exception as e:
        return log_and_format_error("messages_to_voicebot", e, chat_id=chat_id)

if __name__ == "__main__":
    nest_asyncio.apply()

    async def _run() -> None:
        try:
            print("Starting Telegram client (voice entrypoint)...")
            await client.start()

            print("Telegram client started. Running MCP server (voice)...")
            await mcp.run_stdio_async()
        except Exception as e:  # pragma: no cover - startup failure path
            print(f"Error starting client: {e}", file=sys.stderr)
            if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
                print(
                    "Database lock detected. Please ensure no other instances are running.",
                    file=sys.stderr,
                )
            sys.exit(1)

    asyncio.run(_run())

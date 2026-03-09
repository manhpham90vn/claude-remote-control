import time
import os
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction
from dotenv import load_dotenv

from acp_client import AcpClient

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Store sessions by chat_id
sessions: dict[int, dict] = {}

# List of allowed user IDs
ALLOWED_USER_IDS = set(
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x
)


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def create_session(chat_id: int, cwd: str = "/tmp") -> dict:
    """Initialize a new ACP client and session."""
    default_acp = os.path.join(
        os.path.dirname(__file__), "claude-agent-acp/dist/index.js"
    )
    acp_path = os.getenv("ACP_PATH", default_acp)
    client = AcpClient(acp_path)
    await client.start()

    session_data = {
        "client": client,
        "session_id": None,
        "buffer": "",
        "cwd": cwd,
        "tool_messages": {},  # tool_call_id -> message_id
    }

    await client.initialize({"fs": {"readTextFile": True, "writeTextFile": True}})
    session_data["session_id"] = await client.new_session(cwd)
    sessions[chat_id] = session_data
    return session_data


async def close_session(chat_id: int):
    """Close the session for a given chat_id."""
    s = sessions.pop(chat_id, None)
    if s and s.get("client"):
        await s["client"].close()


# --- Commands ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Claude Bot\n\n"
        "Commands:\n"
        "/new <dir> - Create new session with working directory\n"
        "/close   - Close current session\n"
        "/status  - View session status\n\n"
        "Send a message to chat with Claude."
    )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("You are not allowed to use this bot.")
        return

    if chat_id in sessions:
        await close_session(chat_id)

    cwd = context.args[0] if context.args else "/tmp"

    if not os.path.isdir(cwd):
        await update.message.reply_text(f"Directory does not exist: {cwd}")
        return

    try:
        await create_session(chat_id, cwd)
        await update.message.reply_text(f"Created new session at: {cwd}")
    except Exception as e:
        logger.error("Error creating session: %s", e, exc_info=True)
        await update.message.reply_text(f"Error creating session: {e}")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("You are not allowed to use this bot.")
        return

    if chat_id not in sessions:
        await update.message.reply_text("No active session.")
        return

    await close_session(chat_id)
    await update.message.reply_text("Session closed.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(update.effective_user.id):
        return

    s = sessions.get(chat_id)
    if not s:
        await update.message.reply_text(
            "No active session.\nUse /new <dir> to create a new session."
        )
    else:
        await update.message.reply_text(
            f"Session running\nWorking directory: {s.get('cwd', 'N/A')}"
        )


# --- Message Handler ---


STREAM_DEBOUNCE_SEC = 1.0


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    bot = context.bot

    if not text:
        return

    if not is_allowed(update.effective_user.id):
        return

    session = sessions.get(chat_id)

    if not session:
        await update.message.reply_text(
            "No session yet. Use /new <dir> to create a session."
        )
        return

    try:
        session["buffer"] = ""
        session["tool_messages"] = {}

        # Send placeholder response message
        response_msg = await update.message.reply_text("...")
        last_edit_time = 0.0

        async def on_notification(msg: dict):
            nonlocal last_edit_time
            if msg.get("method") != "session/update":
                return

            update_data = msg.get("params", {}).get("update", {})
            session_update = update_data.get("sessionUpdate")

            if session_update == "agent_message_chunk":
                content = update_data.get("content", {})
                if content.get("type") == "text":
                    session["buffer"] += content.get("text", "")

                    # Debounced streaming edit
                    now = time.monotonic()
                    if now - last_edit_time >= STREAM_DEBOUNCE_SEC:
                        last_edit_time = now
                        preview = session["buffer"].strip()
                        if preview:
                            try:
                                await bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=response_msg.message_id,
                                    text=preview[:4096],
                                )
                            except Exception:
                                pass

            elif session_update == "tool_call":
                tool_name = update_data.get("toolName", "Unknown")
                tool_call_id = update_data.get("toolCallId")
                msg_obj = await bot.send_message(
                    chat_id=chat_id, text=f"🔧 {tool_name}..."
                )
                session["tool_messages"][tool_call_id] = msg_obj.message_id

            elif session_update == "tool_call_update":
                tool_call_id = update_data.get("toolCallId")
                status = update_data.get("status")
                meta = update_data.get("_meta", {}).get("claudeCode", {})
                tool_name = meta.get("toolName", "Tool")

                msg_id = session["tool_messages"].get(tool_call_id)
                if msg_id:
                    icon = "✅" if status == "completed" else "❌"
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=f"{icon} {tool_name}",
                        )
                    except Exception:
                        pass

        async def on_permission(params: dict):
            tool_call = params.get("toolCall", {})
            title = tool_call.get("title", "Permission requested")
            await bot.send_message(chat_id=chat_id, text=f"🔑 {title}")

            options = params.get("options", [])
            option_id = next(
                (o["optionId"] for o in options if o.get("kind") == "allow_once"),
                options[0]["optionId"] if options else "allow_once",
            )
            return {"outcome": {"outcome": "selected", "optionId": option_id}}

        session["client"].notification_callback = on_notification
        session["client"].permission_callback = on_permission

        # Typing indicator
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Send prompt and wait for completion
        await session["client"].prompt(session["session_id"], text)

        # Final edit with complete response
        response = session["buffer"].strip()
        if response:
            # Split into 4096-char chunks
            chunks = [response[i : i + 4096] for i in range(0, len(response), 4096)]
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=response_msg.message_id,
                    text=chunks[0],
                )
            except Exception:
                await bot.send_message(chat_id=chat_id, text=chunks[0])
            for chunk in chunks[1:]:
                await bot.send_message(chat_id=chat_id, text=chunk)
        else:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=response_msg.message_id,
                    text="(no response)",
                )
            except Exception:
                pass

    except Exception as e:
        logger.error(
            "Error handling message for chat %s: %s", chat_id, e, exc_info=True
        )
        await close_session(chat_id)
        await update.message.reply_text("An error occurred, please try again.")


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("status", status_command))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()

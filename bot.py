import asyncio
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction
from dotenv import load_dotenv

from acp_client import AcpClient

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress httpx HTTP request logs
logging.getLogger("httpx").setLevel(logging.WARNING)

sessions: dict[int, dict] = {}

pending_permissions: dict[int, asyncio.Future] = {}

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
        "tool_messages": {},
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


def build_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ New Session", callback_data="menu:new")],
            [InlineKeyboardButton("⏹️ Close Session", callback_data="menu:close")],
            [InlineKeyboardButton("📱 Status", callback_data="menu:status")],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Claude Bot\n\nUse the buttons below or send a message to chat with Claude.",
        reply_markup=build_menu_keyboard(),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    bot = context.bot

    if not is_allowed(user_id):
        await query.edit_message_text("You are not allowed to use this bot.")
        return

    action = query.data

    # Handle permission button clicks
    if action.startswith("perm:"):
        option_id = action.removeprefix("perm:")
        future = pending_permissions.pop(chat_id, None)
        if future and not future.done():
            future.set_result(option_id)

        # Delete the permission message
        try:
            await bot.delete_message(
                chat_id=chat_id, message_id=query.message.message_id
            )
        except Exception:
            pass

        await query.answer("Permission granted!")
        return

    action = action.removeprefix("menu:")

    if action == "new":
        context.user_data["awaiting_cwd"] = True
        await query.edit_message_text(
            "Send the working directory path (or send /tmp for default):"
        )

    elif action == "close":
        if chat_id not in sessions:
            await query.edit_message_text(
                "No active session.", reply_markup=build_menu_keyboard()
            )
            return
        await close_session(chat_id)
        await query.edit_message_text(
            "Session closed.", reply_markup=build_menu_keyboard()
        )

    elif action == "status":
        s = sessions.get(chat_id)
        if not s:
            await query.edit_message_text(
                "No active session.\nTap ✨ New Session to create one.",
                reply_markup=build_menu_keyboard(),
            )
        else:
            await query.edit_message_text(
                f"🟢 Session Active\n📁 Working directory: `{s.get('cwd', 'N/A')}`",
                reply_markup=build_menu_keyboard(),
            )


# --- Message Handler ---


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if not update.effective_user:
        return

    chat_id = update.effective_chat.id
    text = update.message.text
    bot = context.bot

    logger.info(
        "Received message: %s (reply_to: %s, chat_id: %s, msg_id: %s)",
        text,
        update.message.reply_to_message,
        chat_id,
        update.message.message_id,
    )

    if not is_allowed(update.effective_user.id):
        return

    # Handle "awaiting_cwd" state from inline menu
    if context.user_data.get("awaiting_cwd"):
        context.user_data.pop("awaiting_cwd", None)
        cwd = text.strip()

        if chat_id in sessions:
            await close_session(chat_id)

        if not os.path.isdir(cwd):
            await update.message.reply_text(
                f"Directory does not exist: {cwd}",
                reply_markup=build_menu_keyboard(),
            )
            return

        try:
            await create_session(chat_id, cwd)
            await update.message.reply_text(
                f"Created new session at: {cwd}",
                reply_markup=build_menu_keyboard(),
            )
        except Exception as e:
            logger.error("Error creating session: %s", e, exc_info=True)
            await update.message.reply_text(f"Error creating session: {e}")
        return

    session = sessions.get(chat_id)

    if not session:
        await update.message.reply_text(
            "No session yet. Tap ✨ New Session to create one.",
            reply_markup=build_menu_keyboard(),
        )
        return

    try:
        session["buffer"] = ""
        session["tool_messages"] = {}
        session["cost"] = ""

        # Send placeholder response message
        response_msg = await update.message.reply_text("...")

        async def on_notification(msg: dict):
            if msg.get("method") != "session/update":
                return

            update_data = msg.get("params", {}).get("update", {})
            session_update = update_data.get("sessionUpdate")

            if session_update == "agent_message_chunk":
                content = update_data.get("content", {})
                if content.get("type") == "text":
                    session["buffer"] += content.get("text", "")
            elif session_update == "usage_update":
                cost_info = update_data.get("cost", {})
                if cost_info:
                    amount = cost_info.get("amount", 0)
                    currency = cost_info.get("currency", "USD")
                    session["cost"] = f"\n\n💵 Cost: `${amount:.6f}` {currency}"

        async def on_permission(params: dict):
            tool_call = params.get("toolCall", {})
            title = tool_call.get("title", "Permission requested")
            desc = tool_call.get("description", "")
            options = params.get("options", [])

            keyboard = []
            for opt in options:
                opt_id = opt.get("optionId")
                kind = opt.get("kind", "")
                label = opt.get("label", opt_id)
                if kind == "deny":
                    icon = "⛔"
                elif "always" in kind or "remember" in kind:
                    icon = "🔐"
                else:
                    icon = "✅"
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{label} {icon}", callback_data=f"perm:{opt_id}"
                        )
                    ]
                )

            perm_text = f"🔐 Permission Required\n\n{title}"
            if desc:
                perm_text += f"\n{desc}"

            await bot.send_message(
                chat_id=chat_id,
                text=perm_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

            # Wait for user to pick an option
            future = asyncio.get_running_loop().create_future()
            pending_permissions[chat_id] = future
            selected = await future

            return {"outcome": {"outcome": "selected", "optionId": selected}}

        session["client"].notification_callback = on_notification
        session["client"].permission_callback = on_permission

        # Typing indicator
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Build prompt with reply context if replying to a message
        prompt_text = text
        reply_msg = update.message.reply_to_message
        if reply_msg and reply_msg.text:
            prompt_text = f"[Replying to: {reply_msg.text}]\n\n{text}"

        # Send prompt and wait for completion
        await session["client"].prompt(session["session_id"], prompt_text)

        # Send response (buffer already updated via notifications)
        response = session["buffer"].strip()
        cost = session.get("cost", "")
        if response:
            response += cost
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=response_msg.message_id,
                    text=response[:4096],
                )
            except Exception:
                await bot.send_message(chat_id=chat_id, text=response[:4096])
                # Send remaining chunks
                if len(response) > 4096:
                    for i in range(4096, len(response), 4096):
                        await bot.send_message(
                            chat_id=chat_id, text=response[i : i + 4096]
                        )
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

    app = Application.builder().token(token).concurrent_updates(True).build()

    # Inline menu callbacks
    app.add_handler(CallbackQueryHandler(menu_callback))

    # Commands (fallback - main interaction via inline menu)
    app.add_handler(CommandHandler("start", start_command))

    # Messages - includes / commands now (forwarded to Claude)
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()

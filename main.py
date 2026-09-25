import os
import re
import random
import asyncio
import logging
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.channels import (
    JoinChannelRequest,
    GetFullChannelRequest,
)
from telethon.tl.functions.messages import (
    SendReactionRequest,
    ImportChatInviteRequest,
)
from telethon.tl.functions.phone import JoinGroupCallRequest
from telethon.tl.types import (
    ReactionEmoji,
    DataJSON,
    InputGroupCall,
)
from telethon.sessions import StringSession
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ============ Bot configuration ============
BOT_TOKEN = "8791027904:AAF24D9_QX9ozjAo-EtLZT7fqo4gbCQagTw"
API_ID = 30261902
API_HASH = "1bf7aa58a278a68517272fcc247cb2df"
SESSIONS_FILE = "sessions.txt"

# ============ Logging ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ============ User state ============
class UserState:
    def __init__(self):
        self.phone_number = None
        self.waiting_for = None
        self.two_fa_password = None
        self.client = None
        self.logged_in = False
        self.otp_digits = []


user_states = {}


# ============ Session helpers ============
def load_sessions():
    sessions = {}
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if ":" in line:
                        phone, session_str = line.split(":", 1)
                        sessions[phone] = session_str
        except Exception as e:
            logger.error(f"Error loading sessions: {e}")
    return sessions


def save_session(phone, session_str):
    sessions = load_sessions()
    sessions[phone] = session_str
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            for ph, sess in sessions.items():
                f.write(f"{ph}:{sess}\n")
    except Exception as e:
        logger.error(f"Error saving session: {e}")


# ============ Link parsers ============
def parse_invite_hash(link: str):
    m = re.search(r"t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", link)
    return m.group(1) if m else None


def parse_post_link(link: str):
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def parse_livestream_link(link: str):
    """
    Parse: https://t.me/BLUEFREENET?livestream=4d0c341409331a241f
    Return: (channel_username, livestream_id) or (None, None)
    """
    m = re.search(r"t\.me/([A-Za-z0-9_]+)\?livestream=([A-Za-z0-9]+)", link)
    if m:
        return m.group(1), m.group(2)
    return None, None


def base_channel_link(link: str):
    return link.split("?")[0].rstrip("/")


# ============ Login helpers ============
async def login_with_phone(phone, user_id):
    try:
        sessions = load_sessions()
        if phone in sessions:
            client = TelegramClient(StringSession(sessions[phone]), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                return client, "AUTHORIZED"
            await client.disconnect()

        session_file = f"sessions/{phone.replace('+', '')}.session"
        client = TelegramClient(session_file, API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            return client, "OTP"

        session_str = StringSession.save(client.session)
        save_session(phone, session_str)
        return client, "AUTHORIZED"
    except Exception as e:
        return None, f"Error: {e}"


async def complete_login_with_otp(phone, code, user_id):
    try:
        client = user_states[user_id].client
        if not client:
            return False, "Client not found."
        await client.sign_in(phone=phone, code=code)
        session_str = StringSession.save(client.session)
        save_session(phone, session_str)
        return True, "Login successful!"
    except SessionPasswordNeededError:
        return False, "2FA password required"
    except Exception as e:
        return False, f"OTP error: {e}"


async def complete_2fa(phone, password, user_id):
    try:
        client = user_states[user_id].client
        if not client:
            return False, "Client not found"
        await client.sign_in(password=password)
        session_str = StringSession.save(client.session)
        save_session(phone, session_str)
        return True, "2FA login successful!"
    except Exception as e:
        return False, f"2FA error: {e}"


async def safe_disconnect(client):
    try:
        if client and client.is_connected():
            await client.disconnect()
    except Exception:
        pass


# ============ Keyboards ============
def create_otp_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="otp_1"),
         InlineKeyboardButton("2", callback_data="otp_2"),
         InlineKeyboardButton("3", callback_data="otp_3")],
        [InlineKeyboardButton("4", callback_data="otp_4"),
         InlineKeyboardButton("5", callback_data="otp_5"),
         InlineKeyboardButton("6", callback_data="otp_6")],
        [InlineKeyboardButton("7", callback_data="otp_7"),
         InlineKeyboardButton("8", callback_data="otp_8"),
         InlineKeyboardButton("9", callback_data="otp_9")],
        [InlineKeyboardButton("<", callback_data="otp_back"),
         InlineKeyboardButton("0", callback_data="otp_0"),
         InlineKeyboardButton("⌫", callback_data="otp_delete")],
        [InlineKeyboardButton("✅ Submit", callback_data="otp_submit")],
    ])


def format_otp_display(digits):
    return "Enter OTP code:" if not digits else f"OTP: {' '.join(digits)}"


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Login Account", callback_data="menu_login"),
         InlineKeyboardButton("📊 Total Accounts", callback_data="menu_total_accounts")],
        [InlineKeyboardButton("📢 Add Channel", callback_data="menu_add_channel"),
         InlineKeyboardButton("🔴 Live Join", callback_data="menu_live_join")],
        [InlineKeyboardButton("🗑️ Delete Channel", callback_data="menu_delete_channel"),
         InlineKeyboardButton("❤️ Reaction", callback_data="menu_reaction")],
    ])


async def show_main_menu(update, text="👋 Welcome! ZX AMER CLUB TEAM\n\nনিচের মেনু থেকে একটি অপশন সিলেক্ট করুন:"):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())


# ============ Core: join channel ============
async def join_single_account(client: TelegramClient, target_link: str):
    invite_hash = parse_invite_hash(target_link)
    base_link = base_channel_link(target_link)

    try:
        if invite_hash:
            try:
                await client(ImportChatInviteRequest(invite_hash))
                await asyncio.sleep(1.5)
                return True, "joined via invite"
            except Exception as e:
                err = str(e)
                if "UserAlreadyParticipant" in err or "already" in err.lower():
                    return True, "already joined"
                return False, f"invite error: {err}"

        try:
            entity = await client.get_entity(base_link)
        except Exception as e:
            return False, f"entity error: {e}"

        try:
            await client(JoinChannelRequest(entity))
            await asyncio.sleep(1.5)
            return True, "joined"
        except Exception as e:
            err = str(e)
            if "UserAlreadyParticipant" in err or "already" in err.lower():
                return True, "already joined"
            return False, f"join error: {err}"
    except Exception as e:
        return False, f"unknown error: {e}"


async def join_all_accounts(target_link: str, user_id: int):
    sessions = load_sessions()
    if not sessions:
        return 0, 0, []

    success = 0
    failed = 0
    errors = []

    for phone, session_str in sessions.items():
        client = None
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()

            if not await client.is_user_authorized():
                failed += 1
                errors.append(f"{phone}: unauthorized")
                continue

            ok, info = await join_single_account(client, target_link)
            if ok:
                success += 1
                logger.info(f"{phone}: {info}")
            else:
                failed += 1
                errors.append(f"{phone}: {info}")
                logger.warning(f"{phone}: {info}")
        except Exception as e:
            failed += 1
            errors.append(f"{phone}: {e}")
        finally:
            if client:
                await safe_disconnect(client)

    return success, failed, errors


# ============ Core: LIVE STREAM JOIN ============
async def join_livestream_single(client: TelegramClient, channel_username: str, livestream_id: str):
    """
    Join a live stream as a viewer.
    """
    try:
        entity = await client.get_entity(channel_username)

        # First join channel if not joined
        try:
            await client(JoinChannelRequest(entity))
            await asyncio.sleep(1)
        except Exception:
            pass

        # Get the group call (livestream) info
        try:
            full = await client(GetFullChannelRequest(entity))
        except Exception as e:
            return False, f"full channel error: {e}"

        if not full.full_chat.call:
            return False, "no active livestream in this channel"

        call = full.full_chat.call

        # Build minimal WebRTC params (muted, video off)
        params = DataJSON(data=(
            '{"ufrag":"' + os.urandom(4).hex() + '",'
            '"pwd":"' + os.urandom(8).hex() + '",'
            '"fingerprints":[],'
            '"ssrc":' + str(random.randint(1000000, 9999999)) + '}'
        ))

        try:
            await client(JoinGroupCallRequest(
                call=InputGroupCall(id=call.id, access_hash=call.access_hash),
                join_as=await client.get_input_entity("me"),
                params=params,
                muted=True,
                video_stopped=True,
            ))
            await asyncio.sleep(1.5)
            return True, "joined livestream"
        except Exception as e:
            err = str(e)
            if "already" in err.lower():
                return True, "already in livestream"
            return False, f"join call error: {err}"

    except Exception as e:
        return False, f"unknown error: {e}"


async def join_livestream_all(link: str, user_id: int):
    channel_username, livestream_id = parse_livestream_link(link)
    if not channel_username:
        base = base_channel_link(link)
        m = re.search(r"t\.me/([A-Za-z0-9_]+)", base)
        if m:
            channel_username = m.group(1)
            livestream_id = None
        else:
            return 0, 0, ["invalid link"]

    sessions = load_sessions()
    if not sessions:
        return 0, 0, []

    success = 0
    failed = 0
    errors = []

    for phone, session_str in sessions.items():
        client = None
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()

            if not await client.is_user_authorized():
                failed += 1
                errors.append(f"{phone}: unauthorized")
                continue

            ok, info = await join_livestream_single(client, channel_username, livestream_id)
            if ok:
                success += 1
                logger.info(f"{phone}: {info}")
            else:
                failed += 1
                errors.append(f"{phone}: {info}")
                logger.warning(f"{phone}: {info}")
        except Exception as e:
            failed += 1
            errors.append(f"{phone}: {e}")
        finally:
            if client:
                await safe_disconnect(client)

    return success, failed, errors


# ============ Core: reaction ============
async def react_single_account(client: TelegramClient, post_link: str):
    username, msg_id = parse_post_link(post_link)
    if not username or not msg_id:
        return False, "invalid post link (use https://t.me/username/123)"

    try:
        entity = await client.get_entity(username)
    except Exception as e:
        return False, f"cannot resolve channel: {e}"

    try:
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            return False, "message not found (maybe deleted)"
    except Exception as e:
        return False, f"cannot fetch message: {e}"

    try:
        await client(SendReactionRequest(
            peer=entity,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon="❤️")]
        ))
        await asyncio.sleep(1.5)
        return True, "reacted ❤️"
    except Exception as e:
        err = str(e)
        if "ReactionInvalid" in err or "not valid" in err.lower():
            return False, f"reaction not allowed: {err}"
        if "CHAT_WRITE_FORBIDDEN" in err:
            return False, "cannot react (no permission)"
        if "MESSAGE_ID_INVALID" in err:
            return False, "message id invalid"
        return False, f"reaction error: {err}"


async def react_all_accounts(post_link: str, user_id: int):
    sessions = load_sessions()
    if not sessions:
        return 0, 0, []

    success = 0
    failed = 0
    errors = []

    for phone, session_str in sessions.items():
        client = None
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()

            if not await client.is_user_authorized():
                failed += 1
                errors.append(f"{phone}: unauthorized")
                continue

            ok, info = await react_single_account(client, post_link)
            if ok:
                success += 1
                logger.info(f"{phone}: {info}")
            else:
                failed += 1
                errors.append(f"{phone}: {info}")
                logger.warning(f"{phone}: {info}")
        except Exception as e:
            failed += 1
            errors.append(f"{phone}: {e}")
        finally:
            if client:
                await safe_disconnect(client)

    return success, failed, errors


# ============ Handlers ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = UserState()
    await show_main_menu(update)


async def start_login_with_phone(update: Update, phone: str, user_id: int):
    state = user_states.setdefault(user_id, UserState())
    if not phone.startswith("+"):
        phone = "+" + phone
    state.phone_number = phone

    # reply vs edit handle
    if update.callback_query:
        await update.callback_query.edit_message_text("🔑 দয়া করে একটু ওয়েট করুন...")
    else:
        await update.message.reply_text("🔑 দয়া করে একটু ওয়েট করুন...")

    client, status = await login_with_phone(phone, user_id)

    if client is None:
        msg = f"❌ Login failed: {status}"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    state.client = client

    if status == "AUTHORIZED":
        state.logged_in = True
        await safe_disconnect(client)
        user_states[user_id] = UserState()
        await show_main_menu(update, "✅ Login successful!\n\n👋 Welcome! ZX AMER CLUB TEAM")
    elif status == "OTP":
        state.waiting_for = "OTP"
        state.otp_digits = []
        otp_text = "📲 OTP sent to your phone.\n\n" + format_otp_display(state.otp_digits)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                otp_text, reply_markup=create_otp_keyboard()
            )
        else:
            await update.message.reply_text(
                otp_text, reply_markup=create_otp_keyboard()
            )


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    contact = update.message.contact
    if not contact:
        await update.message.reply_text("❌ Please share your contact using the button.")
        return
    await start_login_with_phone(update, contact.phone_number, user_id)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state = user_states.setdefault(user_id, UserState())

    if query.data == "menu_login":
        state.waiting_for = "PHONE_NUMBER"
        await query.edit_message_text(
            "📱 আপনার ফোন নাম্বার পাঠান (country code সহ)\n\n"
            "উদাহরণ: `8801877998894` অথবা `+8801877998894`\n\n"
            "বাতিল করতে /cancel",
            parse_mode="Markdown"
        )
        return

    if query.data == "menu_total_accounts":
        await query.edit_message_text(
            f"📊 Total Logged-in Accounts: {len(load_sessions())}\n\n/start দিয়ে মেনুতে ফিরুন।"
        )
        return

    if query.data == "menu_add_channel":
        state.waiting_for = "ADD_CHANNEL"
        await query.edit_message_text(
            "📢 চ্যানেল লিংক পাঠান\n"
            "Public: https://t.me/yourchannel\n"
            "Private: https://t.me/+xxxxxxxx\n\n"
            "বাতিল করতে /cancel"
        )
        return

    if query.data == "menu_live_join":
        state.waiting_for = "LIVE_JOIN"
        await query.edit_message_text(
            "🔴 লাইভ লিংক পাঠান\n"
            "যেমন: https://t.me/yourchannel?livestream=xxxxx\n\n"
            "বাতিল করতে /cancel"
        )
        return

    if query.data == "menu_delete_channel":
        await query.edit_message_text("🗑️ এই ফিচারটি বর্তমানে উপলব্ধ নয়।\n\n/start দিয়ে মেনুতে ফিরুন।")
        return

    if query.data == "menu_reaction":
        state.waiting_for = "REACTION"
        await query.edit_message_text(
            "❤️ পোস্টের লিংক পাঠান\n"
            "যেমন: https://t.me/yourchannel/123\n\n"
            "বাতিল করতে /cancel"
        )
        return

    if query.data.startswith("otp_"):
        if state.waiting_for != "OTP":
            await query.edit_message_text("❌ OTP input not expected.")
            return
        action = query.data.split("_", 1)[1]

        if action.isdigit():
            if len(state.otp_digits) < 10:
                state.otp_digits.append(action)
        elif action == "back":
            await safe_disconnect(state.client)
            user_states[user_id] = UserState()
            await show_main_menu(update)
            return
        elif action == "delete":
            if state.otp_digits:
                state.otp_digits.pop()
        elif action == "submit":
            if not state.otp_digits:
                await query.answer("❌ OTP লিখুন", show_alert=True)
                return
            otp_code = "".join(state.otp_digits)
            state.waiting_for = None
            await query.edit_message_text("⏳ Verifying OTP...")
            success, message = await complete_login_with_otp(state.phone_number, otp_code, user_id)
            if success:
                state.logged_in = True
                await safe_disconnect(state.client)
                user_states[user_id] = UserState()
                await show_main_menu(update, "✅ Login successful!\n\n👋 Welcome! ZX AMER CLUB TEAM")
            else:
                if "2FA password required" in message:
                    state.waiting_for = "2FA"
                    await query.edit_message_text("🔒 2FA password দিন:")
                else:
                    await query.edit_message_text(f"❌ {message}\n\n/start দিয়ে আবার চেষ্টা করুন।")
            return

        await query.edit_message_text(
            "📲 OTP sent to your phone.\n\n" + format_otp_display(state.otp_digits),
            reply_markup=create_otp_keyboard()
        )
        return


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.setdefault(user_id, UserState())
    text = update.message.text.strip()

    if state.waiting_for == "PHONE_NUMBER":
        state.waiting_for = None
        cleaned = re.sub(r"[^\d+]", "", text)  # শুধু ডিজিট আর +
        if not cleaned or len(re.sub(r"\D", "", cleaned)) < 8:
            await update.message.reply_text(
                "❌ ভ্যালিড ফোন নাম্বার পাঠান (country code সহ)।\n"
                "উদাহরণ: `8801877998894`",
                parse_mode="Markdown"
            )
            return
        await start_login_with_phone(update, cleaned, user_id)
        return

    if state.waiting_for == "2FA":
        state.waiting_for = None
        success, message = await complete_2fa(state.phone_number, text, user_id)
        if success:
            state.logged_in = True
            await safe_disconnect(state.client)
            user_states[user_id] = UserState()
            await show_main_menu(update, "✅ Login successful!\n\n👋 Welcome! ZX AMER CLUB TEAM")
        else:
            await update.message.reply_text(f"❌ {message}")
        return

    if state.waiting_for == "ADD_CHANNEL":
        state.waiting_for = None
        if "t.me/" not in text:
            await update.message.reply_text("❌ ভ্যালিড Telegram চ্যানেল লিংক পাঠান।")
            return
        await update.message.reply_text("⏳ সব একাউন্ট থেকে জয়েন করা হচ্ছে...")
        s, f, errs = await join_all_accounts(text, user_id)
        extra = ("\n\n⚠️ Errors:\n" + "\n".join(errs[:5])) if errs else ""
        await update.message.reply_text(
            f"✅ Add Channel সম্পন্ন।\n\nসফল: {s}\nব্যর্থ: {f}{extra}\n\n/start দিয়ে মেনুতে ফিরুন।"
        )
        return

    if state.waiting_for == "LIVE_JOIN":
        state.waiting_for = None
        if "t.me/" not in text:
            await update.message.reply_text("❌ ভ্যালিড লাইভ লিংক পাঠান।")
            return
        await update.message.reply_text(
            "⏳ সব একাউন্ট থেকে লাইভ স্ট্রিমে জয়েন করা হচ্ছে..."
        )
        s, f, errs = await join_livestream_all(text, user_id)
        extra = ("\n\n⚠️ Errors:\n" + "\n".join(errs[:5])) if errs else ""
        await update.message.reply_text(
            f"✅ Live Join সম্পন্ন।\n\nসফল: {s}\nব্যর্থ: {f}{extra}\n\n"
            f"/start দিয়ে মেনুতে ফিরুন।"
        )
        return

    if state.waiting_for == "REACTION":
        state.waiting_for = None
        if "t.me/" not in text or not parse_post_link(text)[0]:
            await update.message.reply_text("❌ ভ্যালিড পোস্ট লিংক পাঠান। যেমন: https://t.me/yourchannel/123")
            return
        await update.message.reply_text("⏳ সব একাউন্ট থেকে ❤️ রিঅ্যাকশন দেওয়া হচ্ছে...")
        s, f, errs = await react_all_accounts(text, user_id)
        extra = ("\n\n⚠️ Errors:\n" + "\n".join(errs[:5])) if errs else ""
        await update.message.reply_text(
            f"✅ Reaction সম্পন্ন।\n\nসফল: {s}\nব্যর্থ: {f}{extra}\n\n/start দিয়ে মেনুতে ফিরুন।"
        )
        return


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_states:
        if user_states[user_id].client:
            await safe_disconnect(user_states[user_id].client)
        user_states[user_id] = UserState()
    await update.message.reply_text("✅ Operation cancelled.")
    await show_main_menu(update)


# ============ Main ============
def main():
    os.makedirs("sessions", exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
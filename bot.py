import os
import json
import logging
import asyncio
import re
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- ENV CONFIG --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOBBY_CHAT_ID = int(os.getenv("LOBBY_CHAT_ID"))

DEAL_ROOMS = {
    1: int(os.getenv("DEAL_ROOM_1")),
    2: int(os.getenv("DEAL_ROOM_2")),
    3: int(os.getenv("DEAL_ROOM_3")),
}

ESCROW_WALLETS = json.loads(os.getenv("ESCROW_WALLETS"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS").split(",")]

# -------------------- CONSTANTS --------------------
FEE_THRESHOLD = 1000
FEE_FIXED = 1
FEE_PERCENT = 0.15

PAYMENT_MODES = ['CDM', 'CC (Cash Counter)', 'Cash (Hand to Hand)', 'Cash (Angadiya)']
DEAL_ROOM_TIMEOUT = 300

# -------------------- DATA --------------------
active_deals = {}
deal_queue = []
room_availability = {1: True, 2: True, 3: True}
deal_statistics = []

# -------------------- HELPERS --------------------
def get_available_room():
    for r, free in room_availability.items():
        if free:
            return r
    return None

def calculate_fees(amount):
    fee = FEE_FIXED if amount <= FEE_THRESHOLD else (amount * FEE_PERCENT) / 100
    return {
        "amount": amount,
        "fee": round(fee, 2),
        "total": round(amount + fee, 2)
    }

def get_deal(room):
    return active_deals.get(room)

# -------------------- COMMANDS --------------------
async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID: {update.message.chat_id}")

async def deal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != LOBBY_CHAT_ID:
        return

    match = re.search(r'/deal\s+@(\w+)', update.message.text)
    if not match:
        await update.message.reply_text("Use: /deal @username")
        return

    seller_id = update.message.from_user.id
    seller_user = update.message.from_user.username or update.message.from_user.first_name
    buyer_user = match.group(1)

    room = get_available_room()
    if not room:
        await update.message.reply_text("⏳ All rooms busy")
        return

    room_availability[room] = False
    deal_id = f"DEAL{1000 + len(active_deals)}"

    active_deals[room] = {
        "deal_id": deal_id,
        "seller_id": None,
        "seller_user": None,
        "buyer_id": None,
        "buyer_user": None,
        "roles_selected": False,
        "created_at": datetime.now(),
        "status": "init",
        "original_msg_id": update.message.message_id,
        "initiator_user": seller_user,
        "other_user": buyer_user
    }

    asyncio.create_task(check_deal_timeout(context, room))

    invite = await context.bot.create_chat_invite_link(
        DEAL_ROOMS[room],
        member_limit=2,
        name=deal_id
    )

    msg = await update.message.reply_text(
        f"🏠 ROOM {room}\n\n"
        f"🔗 {invite.invite_link}\n\n"
        f"Parties: @{seller_user} and @{buyer_user}"
    )

    active_deals[room]["lobby_msg_id"] = msg.message_id
    
    asyncio.create_task(send_role_buttons(context, room))

# -------------------- ROLE SELECTION --------------------
async def send_role_buttons(context, room):
    await asyncio.sleep(3)
    
    deal = get_deal(room)
    if not deal or deal.get("roles_selected"):
        return
    
    keyboard = [
        [
            InlineKeyboardButton("🛒 I'm Seller", callback_data=f"seller_{room}"),
            InlineKeyboardButton("💰 I'm Buyer", callback_data=f"buyer_{room}")
        ]
    ]
    
    try:
        await context.bot.send_message(
            chat_id=DEAL_ROOMS[room],
            text=f"{deal['deal_id']}\n\n👥 Both parties select your roles:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error sending buttons: {e}")

async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    role = parts[0]
    room = int(parts[1])
    
    deal = get_deal(room)
    if not deal:
        return
    
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name
    
    if role == "seller":
        if deal.get("seller_id"):
            await query.answer("⚠️ Seller role already taken!", show_alert=True)
            return
        deal["seller_id"] = user_id
        deal["seller_user"] = username
    
    elif role == "buyer":
        if deal.get("buyer_id"):
            await query.answer("⚠️ Buyer role already taken!", show_alert=True)
            return
        deal["buyer_id"] = user_id
        deal["buyer_user"] = username
    
    seller = f"@{deal['seller_user']}" if deal.get('seller_user') else "Waiting..."
    buyer = f"@{deal['buyer_user']}" if deal.get('buyer_user') else "Waiting..."
    
    await query.edit_message_text(
        f"{deal['deal_id']}\n\n"
        f"🛒 Seller: {seller}\n"
        f"💰 Buyer: {buyer}"
    )
    
    if deal.get("seller_id") and deal.get("buyer_id"):
        deal["roles_selected"] = True
        deal["status"] = "roles_confirmed"
        await context.bot.send_message(
            DEAL_ROOMS[room],
            f"✅ Roles confirmed!\n\n"
            f"🛒 Seller: {seller}\n"
            f"💰 Buyer: {buyer}"
        )

# -------------------- TIMEOUT --------------------
async def check_deal_timeout(context, room):
    await asyncio.sleep(DEAL_ROOM_TIMEOUT)
    deal = get_deal(room)
    if deal and deal["status"] != "completed":
        room_availability[room] = True
        active_deals.pop(room, None)

# -------------------- BOT START --------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("getchatid", get_chat_id))
    app.add_handler(CommandHandler("deal", deal_cmd))
    app.add_handler(CallbackQueryHandler(role_callback))

    logger.info("🚀 Bot running")
    app.run_polling()

if __name__ == "__main__":
    main()
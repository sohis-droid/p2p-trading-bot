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

    initiator_id = update.message.from_user.id
    initiator_user = update.message.from_user.username or update.message.from_user.first_name
    other_user = match.group(1)

    room = get_available_room()
    if not room:
        await update.message.reply_text("⏳ All rooms busy")
        return

    room_availability[room] = False
    deal_id = f"DEAL{1000 + len(active_deals)}"

    active_deals[room] = {
        "deal_id": deal_id,
        "initiator_id": initiator_id,
        "initiator_user": initiator_user,
        "other_user": other_user,
        "seller_id": None,
        "seller_user": None,
        "buyer_id": None,
        "buyer_user": None,
        "roles_selected": False,
        "created_at": datetime.now(),
        "status": "init",
        "original_msg_id": update.message.message_id
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
        f"Initiator: @{initiator_user}\n"
        f"Other Party: @{other_user}"
    )

    active_deals[room]["lobby_msg_id"] = msg.message_id
    
    # Send role selection buttons after delay
    asyncio.create_task(send_role_buttons(context, room))

# -------------------- ROLE SELECTION --------------------
async def send_role_buttons(context, room):
    """Send role selection buttons to deal room"""
    await asyncio.sleep(3)  # Wait for users to join
    
    deal = get_deal(room)
    if not deal or deal["roles_selected"]:
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
            text=f"<b>{deal['deal_id']}</b>\n\n👥 Both parties select your roles:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error sending role buttons: {e}")

async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle role selection button clicks"""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    role = parts[0]
    room = int(parts[1])
    
    deal = get_deal(room)
    if not deal:
        await query.edit_message_text("❌ Deal not found")
        return
    
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name
    
    # Handle seller selection
    if role == "seller":
        if deal["seller_id"]:
            await query.answer("⚠️ Seller role already taken!", show_alert=True)
            return
        
        deal["seller_id"] = user_id
        deal["seller_user"] = username
    
    # Handle buyer selection
    elif role == "buyer":
        if deal["buyer_id"]:
            await query.answer("⚠️ Buyer role already taken!", show_alert=True)
            return
        
        deal["buyer_id"] = user_id
        deal["buyer_user"] = username
    
    # Update message with current selections
    seller_text = f"@{deal['seller_user']}" if deal['seller_user'] else "Waiting..."
    buyer_text = f"@{deal['buyer_user']}" if deal['buyer_user'] else "Waiting..."
    
    await query.edit_message_text(
        f"<b>{deal['deal_id']}</b>\n\n"
        f"🛒 Seller: {seller_text}\n"
        f"💰 Buyer: {buyer_text}",
        parse_mode="HTML"
    )
    
    # Check if both roles selected
    if deal["seller_id"] and deal["buyer_id"]:
        deal["roles_selected"] = True
        deal["status"] = "roles_confirmed"
        
        await context.bot.send_message(
            chat_id=DEAL_ROOMS[room],
            text=f"✅ <b>Roles Confirmed</b>\n\n"
                 f"🛒 Seller: @{deal['seller_user']}\n"
                 f"💰 Buyer: @{deal['buyer_user']}\n\n"
                 f"Deal can now proceed...",
            parse_mode="HTML"
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
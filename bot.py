import os
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes
)
from datetime import datetime, timedelta, time
import re
import asyncio

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# CONFIG - FROM RAILWAY ENVIRONMENT VARIABLES
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOBBY_CHAT_ID = int(os.getenv("LOBBY_CHAT_ID"))

DEAL_ROOMS = {
    1: int(os.getenv("DEAL_ROOM_1")),
    2: int(os.getenv("DEAL_ROOM_2")),
    3: int(os.getenv("DEAL_ROOM_3"))
}

# PRODUCTION ESCROW WALLETS
ESCROW_WALLETS = json.loads(os.getenv("ESCROW_WALLETS"))

# MODIFIED FEE STRUCTURE
FEE_THRESHOLD = 1000
FEE_FIXED = 1
FEE_PERCENT = 0.15

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",")]

# MODIFIED PAYMENT MODES
PAYMENT_MODES = ['CDM', 'CC (Cash Counter)', 'Cash (Hand to Hand)', 'Cash (Angadiya)']

# DATA
active_deals = {}
deal_queue = []
room_availability = {1: True, 2: True, 3: True}
deal_statistics = []
DEAL_ROOM_TIMEOUT = 300

def get_available_room():
    for room_num, available in room_availability.items():
        if available:
            return room_num
    return None

def calculate_fees(amount):
    if amount <= FEE_THRESHOLD:
        fee = FEE_FIXED
    else:
        fee = (amount * FEE_PERCENT) / 100
    
    return {
        'amount': amount,
        'fee': round(fee, 2),
        'total': round(amount + fee, 2)
    }

def get_deal(room_num):
    return active_deals.get(room_num)

def get_ist_time():
    """Get current time in IST (UTC+5:30)"""
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%I:%M %p')

async def send_daily_stats(context: ContextTypes.DEFAULT_TYPE):
    if not deal_statistics:
        return
    
    now = datetime.now()
    last_24h = [d for d in deal_statistics if (now - d['completed_at']).total_seconds() <= 86400]
    
    if not last_24h:
        return
    
    amounts = [d['amount'] for d in last_24h]
    durations = [d['duration'] for d in last_24h]
    
    highest_bid = max(amounts)
    lowest_bid = min(amounts)
    longest_time = max(durations)
    quickest_time = min(durations)
    
    stats_message = (
        f"📊 24-HOUR TRADING STATISTICS\n\n"
        f"💰 Highest Bid: ${highest_bid:,.2f}\n"
        f"💵 Lowest Bid: ${lowest_bid:,.2f}\n"
        f"⏱️ Longest Deal: {longest_time} minutes\n"
        f"⚡ Quickest Deal: {quickest_time} minutes\n\n"
        f"📈 Total Deals: {len(last_24h)}"
    )
    
    try:
        await context.bot.send_message(LOBBY_CHAT_ID, stats_message)
    except Exception as e:
        logger.error(f"Error sending daily stats: {e}")

async def check_deal_timeout(context: ContextTypes.DEFAULT_TYPE, room_num: int):
    await asyncio.sleep(DEAL_ROOM_TIMEOUT)
    
    deal = get_deal(room_num)
    if not deal:
        return
    
    # Only expire if deal hasn't started yet (still in role selection)
    if deal['status'] == 'init' and not deal.get('roles_selected'):
        original_msg_id = deal.get('original_msg_id')
        
        timeout_msg = (
            f"⏱️ DEAL ROOM EXPIRED\n\n"
            f"⚠️ No activity for 5 minutes. Both parties have been removed from the deal room."
        )
        
        try:
            await context.bot.send_message(
                LOBBY_CHAT_ID,
                timeout_msg,
                reply_to_message_id=original_msg_id
            )
            
            if 'lobby_msg_id' in deal:
                await context.bot.delete_message(LOBBY_CHAT_ID, deal['lobby_msg_id'])
        except Exception as e:
            logger.error(f"Error sending timeout message: {e}")
        
        try:
            if deal.get('seller_id'):
                await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
                await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
            if deal.get('buyer_id'):
                await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
                await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
        except Exception as e:
            logger.error(f"Error kicking users: {e}")
        
        room_availability[room_num] = True
        del active_deals[room_num]
        
        logger.info(f"Deal in room {room_num} expired due to inactivity")
    else:
        logger.info(f"Deal in room {room_num} has started, timeout skipped")

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    chat_type = update.message.chat.type
    chat_title = update.message.chat.title if update.message.chat.title else "Private"
    
    await update.message.reply_text(
        f"📊 Chat Info\n\n"
        f"ID: {chat_id}\n"
        f"Type: {chat_type}\n"
        f"Title: {chat_title}"
    )

async def deal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != LOBBY_CHAT_ID:
        return
    
    match = re.search(r'/deal\s+@(\w+)', update.message.text)
    if not match:
        await update.message.reply_text("❌ Use: /deal @username")
        return
    
    initiator_id = update.message.from_user.id
    initiator_user = update.message.from_user.username or update.message.from_user.first_name
    other_user = match.group(1)
    
    room_num = get_available_room()
    if not room_num:
        deal_queue.append((initiator_id, other_user, initiator_user))
        await update.message.reply_text(f"⏳ All rooms busy! Queued (Position: {len(deal_queue)})")
        return
    
    room_availability[room_num] = False
    deal_id = f"DEAL{len(active_deals) + 1001}"
    
    # Save the message ID of the /deal command FIRST
    original_msg_id = update.message.message_id
    
    # MODIFIED: Flexible roles - not assigned at creation
    active_deals[room_num] = {
        'deal_id': deal_id,
        'room_num': room_num,
        'initiator_id': initiator_id,
        'initiator_user': initiator_user,
        'other_user': other_user,
        'seller_id': None,
        'seller_user': None,
        'buyer_id': None,
        'buyer_user': None,
        'status': 'init',
        'created_at': datetime.now(),
        'roles': [],
        'roles_selected': False,
        'original_msg_id': original_msg_id,
        'seller_joined': False,
        'buyer_joined': False,
        'process_msg_sent': False
    }
    
    asyncio.create_task(check_deal_timeout(context, room_num))
    
    try:
        room_id = DEAL_ROOMS[room_num]
        
        invite_link = await context.bot.create_chat_invite_link(
            room_id,
            member_limit=2,
            name=f"Deal {deal_id}",
            creates_join_request=False
        )
        
        current_time = get_ist_time()
        
        lobby_msg = await update.message.reply_text(
            f"🏠 Deal Room Created [ROOM {room_num}]\n\n"
            f"🔗 Join Link: {invite_link.invite_link}\n\n"
            f"👥 Participants:\n"
            f"• @{initiator_user} (Initiator)\n"
            f"• @{other_user} (Counterparty)\n\n"
            f"⚠️ Note: Only the mentioned members can join. "
            f"Never join any link shared via DM.\n\n"
            f"⏱️ Started: {current_time} IST\n"
            f"⏳ Auto-expires in 5 minutes if roles not selected",
            disable_web_page_preview=True
        )
        
        active_deals[room_num]['lobby_msg_id'] = lobby_msg.message_id
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")
        room_availability[room_num] = True
        del active_deals[room_num]

# MODIFIED: Flexible role selection - anyone can be buyer or seller
async def role_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    parts = q.data.split('_')
    role = parts[1]
    room_num = int(parts[2])
    user_id = q.from_user.id
    username = q.from_user.username or q.from_user.first_name
    
    deal = get_deal(room_num)
    if not deal:
        await q.edit_message_text("❌ Deal not found")
        return
    
    if deal.get('roles_selected'):
        await q.answer("✅ Roles already confirmed!", show_alert=True)
        return
    
    # FLEXIBLE ROLE ASSIGNMENT
    if role == 'seller':
        if deal.get('seller_id'):
            await q.answer("❌ Seller role already taken!", show_alert=True)
            return
        
        deal['seller_id'] = user_id
        deal['seller_user'] = username
        if 'seller' not in deal['roles']:
            deal['roles'].append('seller')
        await q.answer("✅ You are now the Seller!")
        
    elif role == 'buyer':
        if deal.get('buyer_id'):
            await q.answer("❌ Buyer role already taken!", show_alert=True)
            return
        
        deal['buyer_id'] = user_id
        deal['buyer_user'] = username
        if 'buyer' not in deal['roles']:
            deal['roles'].append('buyer')
        await q.answer("✅ You are now the Buyer!")
    
    seller_text = f"@{deal['seller_user']}" if deal.get('seller_user') else "Waiting..."
    buyer_text = f"@{deal['buyer_user']}" if deal.get('buyer_user') else "Waiting..."
    
    if len(deal['roles']) == 2:
        deal['roles_selected'] = True
        deal['status'] = 'roles_confirmed'
        
        await q.edit_message_text(
            f"✅ Both parties confirmed!\n\n"
            f"🛒 Seller: {seller_text}\n"
            f"💰 Buyer: {buyer_text}"
        )
        
        kb = [[InlineKeyboardButton("🚀 Start Setup", callback_data=f'setup_{room_num}')]]
        await context.bot.send_message(
            DEAL_ROOMS[room_num],
            "✅ Both roles confirmed! Ready to start?\n\n"
            "⏱️ Note: Once you start, there is NO time limit. Admin can manage the deal if needed.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        if 'seller' in deal['roles']:
            status = "✅ Seller confirmed!\n⏳ Waiting for buyer..."
        else:
            status = "✅ Buyer confirmed!\n⏳ Waiting for seller..."
        
        kb = [
            [InlineKeyboardButton("🛒 I'm Seller", callback_data=f'role_seller_{room_num}')],
            [InlineKeyboardButton("💰 I'm Buyer", callback_data=f'role_buyer_{room_num}')]
        ]
        
        await q.edit_message_text(
            f"🆕 NEW DEAL\n\n"
            f"ID: {deal['deal_id']}\n"
            f"🛒 Seller: {seller_text}\n"
            f"💰 Buyer: {buyer_text}\n\n"
            f"{status}",
            reply_markup=InlineKeyboardMarkup(kb)
        )

async def start_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    
    # Mark deal as started - no more timeout
    deal['status'] = 'in_progress'
    deal['started_at'] = datetime.now()
    
    context.bot_data[f'room_{room_num}'] = room_num
    context.bot_data[f'step_{room_num}'] = 'amount'
    
    await q.edit_message_text(f"💰 Seller @{deal['seller_user']}, enter amount (e.g., 1000):")

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id not in DEAL_ROOMS.values():
        return
    
    room_num = None
    for num, cid in DEAL_ROOMS.items():
        if cid == update.message.chat_id:
            room_num = num
            break
    
    if not room_num:
        return
    
    deal = get_deal(room_num)
    if not deal:
        return
    
    step = context.bot_data.get(f'step_{room_num}')
    text = update.message.text.strip()
    
    if step == 'amount':
        try:
            deal['amount'] = float(text)
            context.bot_data[f'step_{room_num}'] = 'rate'
            
            calc = calculate_fees(deal['amount'])
            await update.message.reply_text(
                f"✅ Amount: ${text}\n"
                f"💵 Fee: ${calc['fee']}\n"
                f"💎 Total with fee: ${calc['total']}\n\n"
                f"📊 Enter rate (e.g., 93):"
            )
        except:
            await update.message.reply_text("❌ Invalid! Enter number")
            
    elif step == 'rate':
        try:
            deal['rate'] = float(text)
            context.bot_data[f'step_{room_num}'] = None
            kb = [
                [InlineKeyboardButton("BSC (BNB Chain)", callback_data=f'chain_BSC_{room_num}')],
                [InlineKeyboardButton("Polygon", callback_data=f'chain_Polygon_{room_num}')],
                [InlineKeyboardButton("Solana", callback_data=f'chain_SOL_{room_num}')]
            ]
            await update.message.reply_text(
                f"✅ Rate: {text}\n\n⛓️ Select chain:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except:
            await update.message.reply_text("❌ Invalid! Enter number")
            
    elif step == 'seller_wallet':
        if update.message.from_user.id != deal['seller_id']:
            return
        deal['seller_wallet'] = text
        context.bot_data[f'step_{room_num}'] = 'payment_details'
        
        pm = deal.get('payment_method', PAYMENT_MODES[0])
        prompt = f"💳 Seller, enter your payment details for {pm}:"
        
        await update.message.reply_text(f"✅ Seller wallet saved!\n\n{prompt}")
        
    elif step == 'buyer_wallet':
        if update.message.from_user.id != deal['buyer_id']:
            return
        deal['buyer_wallet'] = text
        deal['status'] = 'wallets_set'
        context.bot_data[f'step_{room_num}'] = None
        
        calc = calculate_fees(deal['amount'])
        escrow = ESCROW_WALLETS[deal['chain']]
        kb = [[InlineKeyboardButton("✅ I Sent Crypto", callback_data=f'sent_{room_num}')]]
        
        await update.message.reply_text(
            f"✅ Buyer wallet saved!\n\n"
            f"🔐 ESCROW\n\n"
            f"💰 Amount: {deal['amount']} {deal['coin']}\n"
            f"📊 Fee: {calc['fee']} {deal['coin']}\n"
            f"━━━━━━━━━━━━━\n"
            f"💎 Total: {calc['total']} {deal['coin']}\n\n"
            f"⛓️ {deal['chain']}\n"
            f"📥 {escrow}\n\n"
            f"⚠️ Seller, send {calc['total']} {deal['coin']} to escrow, then click button.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        
    elif step == 'payment_details':
        if update.message.from_user.id != deal['seller_id']:
            return
        deal['payment_details'] = text
        context.bot_data[f'step_{room_num}'] = 'buyer_wallet'
        
        await update.message.reply_text(
            f"✅ Payment details saved!\n\n"
            f"👛 Buyer @{deal['buyer_user']}, enter your wallet address:"
        )
        
    elif step == 'tx_hash':
        if update.message.from_user.id != deal['seller_id']:
            return
        tx_hash = text
        deal['tx_hash'] = tx_hash
        deal['status'] = 'pending_verification'
        context.bot_data[f'step_{room_num}'] = None
        
        calc = calculate_fees(deal['amount'])
        chain = deal['chain']
        escrow = ESCROW_WALLETS[chain]
        
        await update.message.reply_text(
            f"✅ TX HASH RECEIVED\n\n"
            f"Hash: {tx_hash}\n"
            f"Chain: {chain}\n"
            f"Expected: {calc['total']} {deal['coin']}\n\n"
            f"⏳ Admin will verify manually..."
        )
        
        kb = [[InlineKeyboardButton("✅ Verify & Approve", callback_data=f'verify_{room_num}')]]
        
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🔔 MANUAL VERIFICATION NEEDED\n\n"
                    f"Room: {room_num}\n"
                    f"Deal ID: {deal['deal_id']}\n"
                    f"Seller: @{deal['seller_user']}\n"
                    f"Buyer: @{deal['buyer_user']}\n\n"
                    f"Hash: {tx_hash}\n"
                    f"Chain: {chain}\n"
                    f"Expected: {calc['total']} {deal['coin']}\n"
                    f"Escrow: {escrow}\n\n"
                    f"⚠️ Please verify the transaction on blockchain and approve if valid.",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            except:
                pass

async def chain_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    parts = q.data.split('_')
    chain = parts[1]
    room_num = int(parts[2])
    deal = get_deal(room_num)
    deal['chain'] = chain
    
    kb = [
        [InlineKeyboardButton("USDT", callback_data=f'coin_USDT_{room_num}')],
        [InlineKeyboardButton("USDC", callback_data=f'coin_USDC_{room_num}')]
    ]
    await q.edit_message_text(f"✅ Chain: {chain}\n\n💎 Select coin:", reply_markup=InlineKeyboardMarkup(kb))

async def coin_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    parts = q.data.split('_')
    coin = parts[1]
    room_num = int(parts[2])
    deal = get_deal(room_num)
    deal['coin'] = coin
    
    kb = []
    for mode in PAYMENT_MODES:
        mode_short = mode.replace(' ', '_').replace('(', '').replace(')', '')[:15]
        kb.append([InlineKeyboardButton(f"💳 {mode}", callback_data=f'pay_{mode_short}_{room_num}')])
    
    await q.edit_message_text(f"✅ Coin: {coin}\n\n💳 Payment method:", reply_markup=InlineKeyboardMarkup(kb))

async def pay_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    parts = q.data.split('_')
    pay_short = parts[1]
    room_num = int(parts[2])
    deal = get_deal(room_num)
    
    # Find the full payment method name
    selected_method = None
    for mode in PAYMENT_MODES:
        mode_key = mode.replace(' ', '_').replace('(', '').replace(')', '')[:15]
        if mode_key == pay_short:
            selected_method = mode
            break
    
    if selected_method:
        deal['payment_method'] = selected_method
        
        # Check if Angadiya is selected (check the actual mode name)
        if 'Angadiya' in selected_method or 'angadiya' in selected_method.lower():
            await q.edit_message_text(
                f"✅ Payment Method Selected: {selected_method}\n\n"
                f"⚠️ ⚠️ WARNING ⚠️ ⚠️\n\n"
                f"We do NOT take any accountability for the place of cash transfer when using Angadiya method.\n\n"
                f"Both parties are responsible for ensuring safe exchange locations.\n\n"
                f"👛 Seller, please enter your crypto wallet address:"
            )
        else:
            await q.edit_message_text(
                f"✅ Payment Method: {selected_method}\n\n"
                f"👛 Seller, enter your crypto wallet address:"
            )
    
    context.bot_data[f'step_{room_num}'] = 'seller_wallet'

async def crypto_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    context.bot_data[f'step_{room_num}'] = 'tx_hash'
    await q.edit_message_text("📝 Seller, enter TX hash:")

async def verify_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌ Admin only!", show_alert=True)
        return
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    deal['status'] = 'verified'
    
    await q.edit_message_text("✅ TX Verified by Admin!")
    
    fiat = deal['amount'] * deal['rate']
    kb = [[InlineKeyboardButton("✅ I Paid Seller", callback_data=f'paid_{room_num}')]]
    
    await context.bot.send_message(
        DEAL_ROOMS[room_num],
        f"✅ CRYPTO IN ESCROW!\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 BUYER'S TURN\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Pay seller: ₹{fiat:,.2f}\n"
        f"📱 Method: {deal['payment_method']}\n\n"
        f"Payment Details:\n"
        f"{deal['payment_details']}\n\n"
        f"⚠️ After payment, click button below:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def buyer_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    
    await q.edit_message_text("✅ Payment claimed! Waiting seller...")
    
    kb = [
        [InlineKeyboardButton("✅ Received", callback_data=f'release_{room_num}')],
        [InlineKeyboardButton("❌ Dispute", callback_data=f'dispute_{room_num}')]
    ]
    
    await context.bot.send_message(
        DEAL_ROOMS[room_num],
        f"💳 Seller, did you receive payment?",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def release_req(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    
    await q.edit_message_text("✅ Seller confirmed! Releasing...")
    
    calc = calculate_fees(deal['amount'])
    kb = [[InlineKeyboardButton("✅ Release", callback_data=f'final_{room_num}')]]
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🔔 RELEASE\n\n"
                f"Room: {room_num}\n"
                f"Amount: {calc['amount']} {deal['coin']}\n"
                f"To: {deal['buyer_wallet']}",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except:
            pass

async def final_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌ Admin only!", show_alert=True)
        return
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    
    deal['completed_at'] = datetime.now()
    duration = (deal['completed_at'] - deal['created_at']).seconds // 60
    
    deal_statistics.append({
        'amount': deal['amount'],
        'duration': duration,
        'completed_at': deal['completed_at']
    })
    
    await q.edit_message_text("✅ Released!")
    
    calc = calculate_fees(deal['amount'])
    await context.bot.send_message(
        DEAL_ROOMS[room_num],
        f"🎉 COMPLETED!\n\n✅ {calc['amount']} {deal['coin']} released\n\nThank you! 🚀"
    )
    
    original_msg_id = deal.get('original_msg_id')
    await context.bot.send_message(
        LOBBY_CHAT_ID,
        f"✅ DEAL COMPLETED\n\n"
        f"👥 Participants:\n"
        f"• @{deal['seller_user']} (Seller)\n"
        f"• @{deal['buyer_user']} (Buyer)\n\n"
        f"⏱️ Duration: {duration} minutes\n"
        f"🏠 Room {room_num} is now available",
        reply_to_message_id=original_msg_id
    )
    
    try:
        if 'lobby_msg_id' in deal:
            await context.bot.delete_message(LOBBY_CHAT_ID, deal['lobby_msg_id'])
    except:
        pass
    
    try:
        await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
        await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
        await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
        await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
    except:
        pass
    
    room_availability[room_num] = True
    del active_deals[room_num]

async def dispute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    
    await q.edit_message_text("⚠️ Dispute! Admin investigating...")
    
    kb = [
        [InlineKeyboardButton("✅ Release", callback_data=f'final_{room_num}')],
        [InlineKeyboardButton("🔄 Refund", callback_data=f'refund_{room_num}')]
    ]
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"⚠️ DISPUTE\n\nRoom: {room_num}\nInvestigate!",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except:
            pass

async def cancel_deal_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to cancel any active deal"""
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only!")
        return
    
    try:
        room_num = int(context.args[0])
        deal = get_deal(room_num)
        
        if not deal:
            await update.message.reply_text(f"❌ No active deal in room {room_num}")
            return
        
        try:
            if deal.get('seller_id'):
                await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
                await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
            if deal.get('buyer_id'):
                await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
                await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
        except:
            pass
        
        await context.bot.send_message(
            DEAL_ROOMS[room_num],
            "❌ Deal cancelled by admin"
        )
        
        room_availability[room_num] = True
        del active_deals[room_num]
        
        await update.message.reply_text(f"✅ Deal in room {room_num} cancelled")
        
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /canceldeal <room_number>\nExample: /canceldeal 1")

async def check_deals_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to see all active deals"""
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only!")
        return
    
    if not active_deals:
        await update.message.reply_text("✅ No active deals")
        return
    
    msg = "📊 ACTIVE DEALS:\n\n"
    for room_num, deal in active_deals.items():
        duration = (datetime.now() - deal['created_at']).seconds // 60
        msg += (
            f"🏠 Room {room_num}\n"
            f"ID: {deal['deal_id']}\n"
            f"Seller: @{deal.get('seller_user', 'Not set')}\n"
            f"Buyer: @{deal.get('buyer_user', 'Not set')}\n"
            f"Status: {deal['status']}\n"
            f"Duration: {duration} min\n"
            f"────────────\n"
        )
    
    await update.message.reply_text(msg)

async def on_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id not in DEAL_ROOMS.values():
        return
    
    room_num = None
    for num, chat_id in DEAL_ROOMS.items():
        if chat_id == update.message.chat_id:
            room_num = num
            break
    
    if not room_num:
        return
    
    deal = get_deal(room_num)
    if not deal:
        return
    
    new_members = update.message.new_chat_members
    
    for member in new_members:
        user_id = member.id
        username = member.username or member.first_name
        
        if member.is_bot:
            continue
        
        if user_id in ADMIN_IDS:
            continue
        
        is_initiator = user_id == deal.get('initiator_id')
        is_other = username == deal.get('other_user') or f"@{username}" == deal.get('other_user')
        
        if not (is_initiator or is_other):
            try:
                await context.bot.ban_chat_member(update.message.chat_id, user_id)
                await context.bot.unban_chat_member(update.message.chat_id, user_id)
                
                await context.bot.send_message(
                    update.message.chat_id,
                    f"❌ @{username} is not authorized for this deal and has been removed."
                )
                
                logger.warning(f"Kicked unauthorized user {username} ({user_id}) from room {room_num}")
            except Exception as e:
                logger.error(f"Failed to kick user: {e}")
        else:
            if is_initiator:
                deal['seller_joined'] = True
                logger.info(f"Initiator @{username} joined room {room_num}")
            if is_other:
                deal['buyer_joined'] = True
                logger.info(f"Other party @{username} joined room {room_num}")
            
            # NEW: Send "Deal in process" message when both join
            if deal.get('seller_joined') and deal.get('buyer_joined') and not deal.get('process_msg_sent'):
                deal['process_msg_sent'] = True
                
                # Send to LOBBY - Reply to original /deal command
                try:
                    await context.bot.send_message(
                        LOBBY_CHAT_ID,
                        f"🤝 Deal between @{deal['initiator_user']} & @{deal['other_user']} is now in process.",
                        reply_to_message_id=deal.get('original_msg_id')
                    )
                except Exception as e:
                    logger.error(f"Error sending process message: {e}")
                
                logger.info(f"Both parties joined room {room_num}, sending role selection")
                
                # Send role selection in DEAL ROOM
                kb = [
                    [InlineKeyboardButton("🛒 I'm Seller", callback_data=f'role_seller_{room_num}')],
                    [InlineKeyboardButton("💰 I'm Buyer", callback_data=f'role_buyer_{room_num}')]
                ]
                
                await context.bot.send_message(
                    update.message.chat_id,
                    f"🆕 NEW DEAL\n\n"
                    f"ID: {deal['deal_id']}\n"
                    f"Parties: @{deal['initiator_user']} & @{deal['other_user']}\n\n"
                    f"⚠️ Both parties have joined! Please select your role:",
                    reply_markup=InlineKeyboardMarkup(kb)
                )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    try:
        if app.job_queue:
            app.job_queue.run_daily(
                send_daily_stats,
                time=time(hour=8, minute=30),
                name='daily_stats'
            )
            logger.info("✅ Daily stats scheduler enabled")
        else:
            logger.warning("⚠️ JobQueue not available. Install with: pip install python-telegram-bot[job-queue]")
    except Exception as e:
        logger.warning(f"⚠️ Could not schedule daily stats: {e}")
    
    app.add_handler(CommandHandler('getchatid', get_chat_id))
    app.add_handler(CommandHandler('deal', deal_cmd))
    app.add_handler(CommandHandler('canceldeal', cancel_deal_admin))
    app.add_handler(CommandHandler('activedeals', check_deals_admin))
    app.add_handler(CallbackQueryHandler(role_select, pattern='^role_'))
    app.add_handler(CallbackQueryHandler(start_setup, pattern='^setup_'))
    app.add_handler(CallbackQueryHandler(chain_select, pattern='^chain_'))
    app.add_handler(CallbackQueryHandler(coin_select, pattern='^coin_'))
    app.add_handler(CallbackQueryHandler(pay_select, pattern='^pay_'))
    app.add_handler(CallbackQueryHandler(crypto_sent, pattern='^sent_'))
    app.add_handler(CallbackQueryHandler(verify_tx, pattern='^verify_'))
    app.add_handler(CallbackQueryHandler(buyer_paid, pattern='^paid_'))
    app.add_handler(CallbackQueryHandler(release_req, pattern='^release_'))
    app.add_handler(CallbackQueryHandler(final_release, pattern='^final_'))
    app.add_handler(CallbackQueryHandler(dispute, pattern='^dispute_'))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_member_join))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    
    logger.info("🚀 Bot Starting...")
    app.run_polling()

if __name__ == '__main__':
    main()
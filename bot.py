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
from web3 import Web3
from solders.rpc.requests import GetTransaction
from solana.rpc.async_api import AsyncClient
import base58

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

# BLOCKCHAIN RPC ENDPOINTS
RPC_ENDPOINTS = {
    'BSC': os.getenv("BSC_RPC", "https://bsc-dataseed1.binance.org"),
    'Polygon': os.getenv("POLYGON_RPC", "https://polygon-rpc.com"),
    'SOL': os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
}

# TOKEN CONTRACT ADDRESSES (ERC20/BEP20)
TOKEN_CONTRACTS = {
    'BSC': {
        'USDT': '0x55d398326f99059fF775485246999027B3197955',
        'USDC': '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d'
    },
    'Polygon': {
        'USDT': '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
        'USDC': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
    }
}

# FEE STRUCTURE
FEE_THRESHOLD = 1000
FEE_FIXED = 1
FEE_PERCENT = 0.15

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",")]

# PAYMENT MODES
PAYMENT_MODES = ['CDM', 'CC (Cash Counter)', 'Cash (Hand to Hand)', 'Cash (Angadiya)']

# DATA STORAGE
active_deals = {}
deal_queue = []
room_availability = {1: True, 2: True, 3: True}
deal_statistics = []
user_deal_history = {}
DEAL_ROOM_TIMEOUT = 300

# Web3 instances
web3_instances = {}

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

def format_duration(minutes):
    """Convert minutes to HH:MM format"""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours} hours {mins} minutes"

def get_web3(chain):
    """Get or create Web3 instance for chain"""
    if chain not in web3_instances:
        rpc = RPC_ENDPOINTS.get(chain)
        if rpc:
            web3_instances[chain] = Web3(Web3.HTTPProvider(rpc))
    return web3_instances.get(chain)

async def verify_evm_transaction(chain, tx_hash, expected_address, expected_amount, token):
    """
    Verify EVM transaction (BSC, Polygon)
    Returns: (success: bool, message: str)
    """
    try:
        w3 = get_web3(chain)
        if not w3 or not w3.is_connected():
            return False, f"Cannot connect to {chain} network"
        
        # Get transaction receipt
        tx_receipt = w3.eth.get_transaction_receipt(tx_hash)
        if not tx_receipt:
            return False, "Transaction not found or not confirmed yet"
        
        # Check if transaction was successful
        if tx_receipt['status'] != 1:
            return False, "Transaction failed on blockchain"
        
        # Get transaction details
        tx = w3.eth.get_transaction(tx_hash)
        
        # For native token transfers (not our case, but good to have)
        if tx['to'] and tx['to'].lower() == expected_address.lower():
            # This would be ETH/BNB/MATIC transfer
            # We're dealing with tokens, so this shouldn't match
            pass
        
        # Check token transfer in logs
        contract_address = TOKEN_CONTRACTS.get(chain, {}).get(token)
        if not contract_address:
            return False, f"Token {token} not supported on {chain}"
        
        # ERC20 Transfer event signature
        transfer_signature = w3.keccak(text="Transfer(address,address,uint256)").hex()
        
        found_transfer = False
        actual_amount = 0
        
        for log in tx_receipt['logs']:
            if log['topics'][0].hex() == transfer_signature:
                # Check if this is from our token contract
                if log['address'].lower() == contract_address.lower():
                    # Decode recipient (2nd topic)
                    recipient = '0x' + log['topics'][2].hex()[-40:]
                    
                    if recipient.lower() == expected_address.lower():
                        # Decode amount (data field)
                        amount_wei = int(log['data'].hex(), 16)
                        # USDT/USDC typically have 6 decimals (except USDT on some chains has 18)
                        # For BSC USDT and Polygon USDT/USDC = 6 decimals
                        decimals = 6 if token == 'USDT' and chain == 'BSC' else 6
                        actual_amount = amount_wei / (10 ** decimals)
                        found_transfer = True
                        break
        
        if not found_transfer:
            return False, f"No {token} transfer found to escrow wallet"
        
        # Allow 1% tolerance for amount (to account for rounding/fees)
        tolerance = expected_amount * 0.01
        if abs(actual_amount - expected_amount) > tolerance:
            return False, f"Amount mismatch. Expected: {expected_amount}, Got: {actual_amount}"
        
        return True, f"✅ Verified: {actual_amount} {token} sent to escrow"
        
    except Exception as e:
        logger.error(f"EVM verification error: {e}")
        return False, f"Verification error: {str(e)}"

async def verify_solana_transaction(tx_hash, expected_address, expected_amount, token):
    """
    Verify Solana transaction
    Returns: (success: bool, message: str)
    """
    try:
        client = AsyncClient(RPC_ENDPOINTS['SOL'])
        
        # Get transaction
        response = await client.get_transaction(
            tx_hash,
            encoding="json",
            max_supported_transaction_version=0
        )
        
        if not response or not response.value:
            return False, "Transaction not found or not confirmed yet"
        
        tx_data = response.value
        
        # Check if transaction was successful
        if tx_data.transaction.meta.err:
            return False, "Transaction failed on blockchain"
        
        # For Solana, we need to check the token transfers in meta
        # This is more complex - simplified version here
        # In production, you'd parse the instruction data more carefully
        
        post_balances = tx_data.transaction.meta.post_token_balances
        pre_balances = tx_data.transaction.meta.pre_token_balances
        
        # Find transfers to expected address
        found_transfer = False
        actual_amount = 0
        
        for post_balance in post_balances:
            account = post_balance.owner
            if account == expected_address:
                # Find corresponding pre-balance
                pre_amount = 0
                for pre_balance in pre_balances:
                    if pre_balance.account_index == post_balance.account_index:
                        pre_amount = float(pre_balance.ui_token_amount.ui_amount)
                        break
                
                post_amount = float(post_balance.ui_token_amount.ui_amount)
                actual_amount = post_amount - pre_amount
                
                if actual_amount > 0:
                    found_transfer = True
                    break
        
        if not found_transfer:
            return False, f"No {token} transfer found to escrow wallet"
        
        # Allow 1% tolerance
        tolerance = expected_amount * 0.01
        if abs(actual_amount - expected_amount) > tolerance:
            return False, f"Amount mismatch. Expected: {expected_amount}, Got: {actual_amount}"
        
        return True, f"✅ Verified: {actual_amount} {token} sent to escrow"
        
    except Exception as e:
        logger.error(f"Solana verification error: {e}")
        return False, f"Verification error: {str(e)}"

async def verify_blockchain_transaction(chain, tx_hash, expected_address, expected_amount, token):
    """
    Main verification function - routes to appropriate chain
    """
    try:
        if chain in ['BSC', 'Polygon']:
            return await verify_evm_transaction(chain, tx_hash, expected_address, expected_amount, token)
        elif chain == 'SOL':
            return await verify_solana_transaction(tx_hash, expected_address, expected_amount, token)
        else:
            return False, f"Chain {chain} not supported for auto-verification"
    except Exception as e:
        logger.error(f"Blockchain verification failed: {e}")
        return False, f"Verification failed: {str(e)}"

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
        f"⏱️ Longest Deal: {format_duration(longest_time)}\n"
        f"⚡ Quickest Deal: {format_duration(quickest_time)}\n\n"
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

async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show fee structure"""
    await update.message.reply_text(
        f"💰 FEE STRUCTURE\n\n"
        f"Below $1000: $1 fixed\n"
        f"Above $1000: 0.15%\n\n"
        f"Examples:\n"
        f"• $500 → Fee: $1\n"
        f"• $5,000 → Fee: $7.50\n"
        f"• $10,000 → Fee: $15"
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
    
    original_msg_id = update.message.message_id
    
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
            "✅ Both roles confirmed! Ready to start?",
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
    
    elif step == 'tx_hash':
        if update.message.from_user.id != deal['seller_id']:
            return
        
        tx_hash = text
        deal['tx_hash'] = tx_hash
        deal['status'] = 'verifying'
        context.bot_data[f'step_{room_num}'] = None
        
        calc = calculate_fees(deal['amount'])
        chain = deal['chain']
        escrow = ESCROW_WALLETS[chain]
        
        await update.message.reply_text(
            f"✅ TX HASH RECEIVED\n\n"
            f"Hash: {tx_hash}\n"
            f"Chain: {chain}\n"
            f"Expected: {calc['total']} {deal['coin']}\n\n"
            f"🔄 Auto-verifying on blockchain..."
        )
        
        # AUTO-VERIFY ON BLOCKCHAIN
        success, message = await verify_blockchain_transaction(
            chain, 
            tx_hash, 
            escrow, 
            calc['total'], 
            deal['coin']
        )
        
        if success:
            # AUTO-VERIFIED SUCCESSFULLY
            deal['status'] = 'auto_verified'
            deal['verification_message'] = message
            
            kb = [[InlineKeyboardButton("✅ Approve & Continue", callback_data=f'verify_{room_num}')]]
            
            # Notify admins with auto-verification result
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"🤖 AUTO-VERIFICATION SUCCESS\n\n"
                        f"Room: {room_num}\n"
                        f"Deal ID: {deal['deal_id']}\n"
                        f"Seller: @{deal['seller_user']}\n"
                        f"Buyer: @{deal['buyer_user']}\n\n"
                        f"Hash: {tx_hash}\n"
                        f"Chain: {chain}\n"
                        f"{message}\n\n"
                        f"✅ Click to approve and continue",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                except:
                    pass
            
            # Notify in deal room
            await context.bot.send_message(
                DEAL_ROOMS[room_num],
                f"✅ AUTO-VERIFICATION SUCCESSFUL!\n\n"
                f"{message}\n\n"
                f"⏳ Waiting for admin final approval..."
            )
        else:
            # AUTO-VERIFICATION FAILED - FALLBACK TO MANUAL
            deal['status'] = 'pending_verification'
            deal['verification_message'] = message
            
            kb = [[InlineKeyboardButton("✅ Verify Manually", callback_data=f'verify_{room_num}')]]
            
            # Notify admins to verify manually
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"⚠️ AUTO-VERIFICATION FAILED\n\n"
                        f"Room: {room_num}\n"
                        f"Deal ID: {deal['deal_id']}\n"
                        f"Seller: @{deal['seller_user']}\n"
                        f"Buyer: @{deal['buyer_user']}\n\n"
                        f"Hash: {tx_hash}\n"
                        f"Chain: {chain}\n"
                        f"Expected: {calc['total']} {deal['coin']}\n"
                        f"Escrow: {escrow}\n\n"
                        f"❌ Reason: {message}\n\n"
                        f"⚠️ Please verify manually on blockchain",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                except:
                    pass
            
            # Notify in deal room
            await context.bot.send_message(
                DEAL_ROOMS[room_num],
                f"⚠️ Auto-verification failed\n\n"
                f"Reason: {message}\n\n"
                f"⏳ Admin will verify manually..."
            )
    
    elif step == 'payment_details':
        if update.message.from_user.id != deal['seller_id']:
            return
        deal['payment_details'] = text
        deal['status'] = 'waiting_buyer_payment'
        context.bot_data[f'step_{room_num}'] = None
        
        await update.message.reply_text("✅ Payment details saved!")
        
        calc = calculate_fees(deal['amount'])
        fiat = deal['amount'] * deal['rate']
        kb = [[InlineKeyboardButton("✅ I Paid Seller", callback_data=f'paid_{room_num}')]]
        
        await context.bot.send_message(
            DEAL_ROOMS[room_num],
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💳 BUYER'S TURN\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Total USDT: {calc['total']} {deal['coin']}\n"
            f"💵 Pay Seller: ₹{fiat:,.2f}\n"
            f"📱 Payment Mode: {deal['payment_method']}\n"
            f"📊 Rate: {deal['rate']}\n\n"
            f"Payment Details:\n"
            f"{deal['payment_details']}\n\n"
            f"⚠️ After payment, click button below:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

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
    
    kb = [
        [InlineKeyboardButton("💳 CDM", callback_data=f'paymode_cdm_{room_num}')],
        [InlineKeyboardButton("💳 CC (Cash Counter)", callback_data=f'paymode_cc_{room_num}')],
        [InlineKeyboardButton("💳 Cash (Hand to Hand)", callback_data=f'paymode_cash_{room_num}')],
        [InlineKeyboardButton("💳 Cash (Angadiya)", callback_data=f'paymode_angadiya_{room_num}')]
    ]
    
    await q.edit_message_text(f"✅ Coin: {coin}\n\n💳 Payment method:", reply_markup=InlineKeyboardMarkup(kb))

async def pay_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    parts = q.data.split('_')
    payment_type = parts[1]
    room_num = int(parts[2])
    deal = get_deal(room_num)
    
    payment_map = {
        'cdm': 'CDM',
        'cc': 'CC (Cash Counter)',
        'cash': 'Cash (Hand to Hand)',
        'angadiya': 'Cash (Angadiya)'
    }
    
    selected_method = payment_map.get(payment_type)
    deal['payment_method'] = selected_method
    
    calc = calculate_fees(deal['amount'])
    escrow = ESCROW_WALLETS[deal['chain']]
    
    warning = ""
    if payment_type == 'angadiya':
        warning = (
            f"\n⚠️ ⚠️ WARNING ⚠️ ⚠️\n\n"
            f"We do NOT take any accountability for the place of cash transfer when using Angadiya method.\n"
            f"Both parties are responsible for ensuring safe exchange locations.\n"
        )
    
    kb = [[InlineKeyboardButton("✅ I Sent Crypto", callback_data=f'sent_{room_num}')]]
    
    await q.edit_message_text(
        f"✅ Payment Method: {selected_method}\n"
        f"{warning}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔐 ESCROW WALLET\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Amount: {deal['amount']} {deal['coin']}\n"
        f"📊 Fee: {calc['fee']} {deal['coin']}\n"
        f"💎 Total: {calc['total']} {deal['coin']}\n\n"
        f"⛓️ Chain: {deal['chain']}\n"
        f"📥 Wallet:\n"
        f"`{escrow}`\n\n"
        f"⚠️ Seller @{deal['seller_user']}, send {calc['total']} {deal['coin']} to the above wallet, then click button.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown'
    )
    
    context.bot_data[f'step_{room_num}'] = None

async def crypto_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    context.bot_data[f'step_{room_num}'] = 'tx_hash'
    await q.edit_message_text("📝 Seller, enter transaction hash:")

async def verify_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("❌ Admin only!", show_alert=True)
        return
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    deal['status'] = 'verified'
    
    verification_msg = deal.get('verification_message', 'Verified by admin')
    await q.edit_message_text(f"✅ TX Verified!\n\n{verification_msg}")
    
    context.bot_data[f'step_{room_num}'] = 'payment_details'
    
    await context.bot.send_message(
        DEAL_ROOMS[room_num],
        f"✅ CRYPTO VERIFIED & IN ESCROW!\n\n"
        f"💳 Seller @{deal['seller_user']}, please provide your payment details for {deal['payment_method']}:"
    )

async def buyer_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    
    await q.edit_message_text("✅ Payment claimed! Waiting for seller confirmation...")
    
    kb = [
        [InlineKeyboardButton("✅ Received", callback_data=f'release_{room_num}')],
        [InlineKeyboardButton("❌ Dispute", callback_data=f'dispute_{room_num}')]
    ]
    
    await context.bot.send_message(
        DEAL_ROOMS[room_num],
        f"💳 Seller @{deal['seller_user']}, did you receive the payment?",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def release_req(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    room_num = int(q.data.split('_')[1])
    deal = get_deal(room_num)
    
    await q.edit_message_text("✅ Seller confirmed! Notifying admin to release crypto...")
    
    calc = calculate_fees(deal['amount'])
    kb = [[InlineKeyboardButton("✅ Release", callback_data=f'final_{room_num}')]]
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🔔 RELEASE REQUEST\n\n"
                f"Room: {room_num}\n"
                f"Deal ID: {deal['deal_id']}\n"
                f"Amount: {calc['amount']} {deal['coin']}\n"
                f"To: Buyer @{deal['buyer_user']}\n\n"
                f"Seller confirmed receiving payment. Please release crypto.",
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
        'completed_at': deal['completed_at'],
        'seller': deal['seller_user'],
        'buyer': deal['buyer_user']
    })
    
    if deal['seller_user'] not in user_deal_history:
        user_deal_history[deal['seller_user']] = []
    if deal['buyer_user'] not in user_deal_history:
        user_deal_history[deal['buyer_user']] = []
    
    user_deal_history[deal['seller_user']].append({
        'deal_id': deal['deal_id'],
        'role': 'seller',
        'amount': deal['amount'],
        'completed_at': deal['completed_at']
    })
    user_deal_history[deal['buyer_user']].append({
        'deal_id': deal['deal_id'],
        'role': 'buyer',
        'amount': deal['amount'],
        'completed_at': deal['completed_at']
    })
    
    await q.edit_message_text("✅ Crypto Released!")
    
    calc = calculate_fees(deal['amount'])
    await context.bot.send_message(
        DEAL_ROOMS[room_num],
        f"🎉 DEAL COMPLETED!\n\n✅ {calc['amount']} {deal['coin']} released to buyer\n\nThank you! 🚀"
    )
    
    original_msg_id = deal.get('original_msg_id')
    await context.bot.send_message(
        LOBBY_CHAT_ID,
        f"✅ DEAL COMPLETED\n\n"
        f"👥 Participants:\n"
        f"• @{deal['seller_user']} (Seller)\n"
        f"• @{deal['buyer_user']} (Buyer)\n\n"
        f"⏱️ Duration: {format_duration(duration)}\n"
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
    
    await q.edit_message_text("⚠️ Dispute raised! Admin will investigate...")
    
    kb = [
        [InlineKeyboardButton("✅ Release to Buyer", callback_data=f'final_{room_num}')],
        [InlineKeyboardButton("🔄 Refund to Seller", callback_data=f'refund_{room_num}')]
    ]
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"⚠️ DISPUTE RAISED\n\nRoom: {room_num}\nPlease investigate and resolve!",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except:
            pass

# ADMIN COMMANDS

async def cancel_deal_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"Duration: {format_duration(duration)}\n"
            f"────────────\n"
        )
    
    await update.message.reply_text(msg)

async def complete_deal_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only!")
        return
    
    try:
        room_num = int(context.args[0])
        deal = get_deal(room_num)
        
        if not deal:
            await update.message.reply_text(f"❌ No active deal in room {room_num}")
            return
        
        deal['completed_at'] = datetime.now()
        duration = (deal['completed_at'] - deal['created_at']).seconds // 60
        
        if deal.get('amount'):
            deal_statistics.append({
                'amount': deal['amount'],
                'duration': duration,
                'completed_at': deal['completed_at'],
                'seller': deal.get('seller_user', 'N/A'),
                'buyer': deal.get('buyer_user', 'N/A')
            })
        
        calc = calculate_fees(deal.get('amount', 0)) if deal.get('amount') else None
        completion_msg = f"✅ DEAL COMPLETED BY ADMIN\n\n"
        
        if calc and deal.get('coin'):
            completion_msg += f"💰 Amount: {calc['amount']} {deal['coin']}\n\n"
        
        completion_msg += "Thank you! 🚀"
        
        await context.bot.send_message(
            DEAL_ROOMS[room_num],
            completion_msg
        )
        
        original_msg_id = deal.get('original_msg_id')
        lobby_msg = (
            f"✅ DEAL COMPLETED (Admin)\n\n"
            f"👥 Participants:\n"
            f"• @{deal.get('seller_user', 'N/A')} (Seller)\n"
            f"• @{deal.get('buyer_user', 'N/A')} (Buyer)\n\n"
            f"⏱️ Duration: {format_duration(duration)}\n"
            f"🏠 Room {room_num} is now available"
        )
        
        if original_msg_id:
            await context.bot.send_message(
                LOBBY_CHAT_ID,
                lobby_msg,
                reply_to_message_id=original_msg_id
            )
        else:
            await context.bot.send_message(LOBBY_CHAT_ID, lobby_msg)
        
        try:
            if 'lobby_msg_id' in deal:
                await context.bot.delete_message(LOBBY_CHAT_ID, deal['lobby_msg_id'])
        except:
            pass
        
        try:
            if deal.get('seller_id'):
                await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
                await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['seller_id'])
            if deal.get('buyer_id'):
                await context.bot.ban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
                await context.bot.unban_chat_member(DEAL_ROOMS[room_num], deal['buyer_id'])
        except:
            pass
        
        room_availability[room_num] = True
        del active_deals[room_num]
        
        await update.message.reply_text(f"✅ Deal in room {room_num} marked as completed")
        
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /completedeal <room_number>\nExample: /completedeal 1")

async def my_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.from_user.username or update.message.from_user.first_name
    
    if username not in user_deal_history or not user_deal_history[username]:
        await update.message.reply_text("📊 You have no completed deals yet.")
        return
    
    deals = user_deal_history[username]
    total_deals = len(deals)
    
    msg = f"📊 YOUR DEAL HISTORY\n\n"
    msg += f"Total Deals: {total_deals}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n\n"
    
    for deal in deals[-10:]:
        role_emoji = "🛒" if deal['role'] == 'seller' else "💰"
        msg += (
            f"{role_emoji} {deal['deal_id']}\n"
            f"Role: {deal['role'].title()}\n"
            f"Amount: ${deal['amount']:.2f}\n"
            f"Date: {deal['completed_at'].strftime('%d %b %Y')}\n"
            f"────────────\n"
        )
    
    if total_deals > 10:
        msg += f"\n... and {total_deals - 10} more deals"
    
    await update.message.reply_text(msg)

async def total_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only!")
        return
    
    if not deal_statistics:
        await update.message.reply_text("📊 No deals completed yet.")
        return
    
    now = datetime.now()
    
    daily = [d for d in deal_statistics if (now - d['completed_at']).total_seconds() <= 86400]
    weekly = [d for d in deal_statistics if (now - d['completed_at']).days <= 7]
    monthly = [d for d in deal_statistics if (now - d['completed_at']).days <= 30]
    
    def calc_stats(deals):
        if not deals:
            return None
        amounts = [d['amount'] for d in deals]
        total_vol = sum(amounts)
        avg_deal = total_vol / len(deals)
        return {
            'count': len(deals),
            'volume': total_vol,
            'average': avg_deal,
            'highest': max(amounts),
            'lowest': min(amounts)
        }
    
    daily_stats = calc_stats(daily)
    weekly_stats = calc_stats(weekly)
    monthly_stats = calc_stats(monthly)
    
    msg = "📊 DEAL STATISTICS\n\n"
    
    if daily_stats:
        msg += (
            f"📅 DAILY (Last 24 Hours)\n"
            f"Deals: {daily_stats['count']}\n"
            f"Volume: ${daily_stats['volume']:,.2f}\n"
            f"Average: ${daily_stats['average']:,.2f}\n"
            f"Highest: ${daily_stats['highest']:,.2f}\n"
            f"Lowest: ${daily_stats['lowest']:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )
    
    if weekly_stats:
        msg += (
            f"📅 WEEKLY (Last 7 Days)\n"
            f"Deals: {weekly_stats['count']}\n"
            f"Volume: ${weekly_stats['volume']:,.2f}\n"
            f"Average: ${weekly_stats['average']:,.2f}\n"
            f"Highest: ${weekly_stats['highest']:,.2f}\n"
            f"Lowest: ${weekly_stats['lowest']:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )
    
    if monthly_stats:
        msg += (
            f"📅 MONTHLY (Last 30 Days)\n"
            f"Deals: {monthly_stats['count']}\n"
            f"Volume: ${monthly_stats['volume']:,.2f}\n"
            f"Average: ${monthly_stats['average']:,.2f}\n"
            f"Highest: ${monthly_stats['highest']:,.2f}\n"
            f"Lowest: ${monthly_stats['lowest']:,.2f}\n"
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
            
            if deal.get('seller_joined') and deal.get('buyer_joined') and not deal.get('process_msg_sent'):
                deal['process_msg_sent'] = True
                
                try:
                    if 'lobby_msg_id' in deal:
                        await context.bot.delete_message(LOBBY_CHAT_ID, deal['lobby_msg_id'])
                        logger.info(f"Deleted invite link message for room {room_num}")
                except Exception as e:
                    logger.error(f"Error deleting invite link message: {e}")
                
                try:
                    await context.bot.send_message(
                        LOBBY_CHAT_ID,
                        f"🤝 Deal between @{deal['initiator_user']} & @{deal['other_user']} is now in process.",
                        reply_to_message_id=deal.get('original_msg_id')
                    )
                except Exception as e:
                    logger.error(f"Error sending process message: {e}")
                
                logger.info(f"Both parties joined room {room_num}, sending role selection")
                
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
    
    # Command handlers
    app.add_handler(CommandHandler('getchatid', get_chat_id))
    app.add_handler(CommandHandler('deal', deal_cmd))
    app.add_handler(CommandHandler('fees', fees_cmd))  # NEW
    app.add_handler(CommandHandler('canceldeal', cancel_deal_admin))
    app.add_handler(CommandHandler('activedeals', check_deals_admin))
    app.add_handler(CommandHandler('completedeal', complete_deal_admin))
    app.add_handler(CommandHandler('mydeals', my_deals))
    app.add_handler(CommandHandler('totaldeals', total_deals))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(role_select, pattern='^role_'))
    app.add_handler(CallbackQueryHandler(start_setup, pattern='^setup_'))
    app.add_handler(CallbackQueryHandler(chain_select, pattern='^chain_'))
    app.add_handler(CallbackQueryHandler(coin_select, pattern='^coin_'))
    app.add_handler(CallbackQueryHandler(pay_select, pattern='^paymode_'))
    app.add_handler(CallbackQueryHandler(crypto_sent, pattern='^sent_'))
    app.add_handler(CallbackQueryHandler(verify_tx, pattern='^verify_'))
    app.add_handler(CallbackQueryHandler(buyer_paid, pattern='^paid_'))
    app.add_handler(CallbackQueryHandler(release_req, pattern='^release_'))
    app.add_handler(CallbackQueryHandler(final_release, pattern='^final_'))
    app.add_handler(CallbackQueryHandler(dispute, pattern='^dispute_'))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_member_join))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    
    logger.info("🚀 Bot Starting with Auto-Verification...")
    app.run_polling()

if __name__ == '__main__':
    main()
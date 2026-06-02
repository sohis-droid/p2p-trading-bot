# P2P Crypto Escrow Trading Bot

A production-ready Telegram P2P Escrow Trading Bot designed for secure cryptocurrency transactions between buyers and sellers.

The bot automates deal creation, escrow management, blockchain verification, payment confirmation, dispute handling, and trade completion.

## Features

### Escrow-Based Trading

* Secure escrow workflow
* Seller deposits crypto into escrow wallet
* Buyer sends fiat payment
* Crypto released only after confirmation

### Multi-Chain Support

* BNB Smart Chain (BSC)
* Polygon
* Solana

### Supported Assets

* USDT
* USDC

### Automated Blockchain Verification

* Transaction hash validation
* Transfer amount verification
* Escrow wallet verification
* Automatic blockchain confirmation

### Deal Room Management

* Multiple concurrent deal rooms
* Invite-link based access
* Unauthorized user protection
* Auto-expiry for inactive deals

### Admin Controls

* Manual verification fallback
* Deal cancellation
* Deal completion override
* Dispute resolution tools

### Trading Statistics

* Daily trade reports
* Volume tracking
* User trade history
* Performance analytics

## Workflow

### Step 1 - Create Deal

```text
/deal @username
```

A private deal room is created for both parties.

### Step 2 - Select Roles

* Seller
* Buyer

### Step 3 - Setup Trade

Seller enters:

* Amount
* Rate
* Blockchain
* Token
* Payment Method

### Step 4 - Escrow Deposit

Seller sends crypto to escrow wallet.

Supported:

* USDT
* USDC

Chains:

* BSC
* Polygon
* Solana

### Step 5 - Auto Verification

Bot verifies:

* Transaction hash
* Recipient wallet
* Amount transferred
* Blockchain confirmation

### Step 6 - Fiat Payment

Buyer sends payment through selected method:

* CDM
* Cash Counter
* Hand-to-Hand Cash
* Angadiya

### Step 7 - Release

Seller confirms payment received.

Admin releases escrowed crypto to buyer.

### Step 8 - Deal Completion

Statistics and history are recorded automatically.

## Fee Structure

| Amount  | Fee      |
| ------- | -------- |
| ≤ $1000 | $1 Fixed |
| > $1000 | 0.1%     |

Examples:

* $500 → $1 Fee
* $5,000 → $5 Fee
* $10,000 → $10 Fee

## Environment Variables

```env
BOT_TOKEN=
LOBBY_CHAT_ID=

DEAL_ROOM_1=
DEAL_ROOM_2=
DEAL_ROOM_3=

ADMIN_IDS=

ESCROW_WALLETS=

BSC_RPC=
POLYGON_RPC=
SOLANA_RPC=
```

## Installation

```bash
git clone https://github.com/sohis-droid/p2p-trading-bot.git

cd p2p-trading-bot

pip install -r requirements.txt
```

## Run

```bash
python bot.py
```

## Admin Commands

```text
/canceldeal <room>
/activedeals
/completedeal <room>
/totaldeals
```

## User Commands

```text
/deal @username
/fees
/mydeals
```

## Security Features

* Escrow wallet verification
* Automatic blockchain validation
* Restricted deal room access
* Admin-controlled release
* Transaction amount verification
* Unauthorized participant removal
* Dispute management system

## Deployment

Designed for:

* Railway
* VPS Servers
* Linux Hosts
* Docker Containers

## Tech Stack

* Python
* python-telegram-bot
* Web3.py
* Solana RPC
* AsyncIO

## Disclaimer

This software is provided for educational and operational purposes. Cryptocurrency trading involves financial risk. Operators are responsible for regulatory compliance, escrow management, wallet security, and dispute resolution.

## License

MIT License

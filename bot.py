# -*- coding: utf-8 -*-
import atexit
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import sqlite3
from flask import Flask
from threading import Thread
import psutil
import requests
import telebot
from telebot import types

# ==========================================================================
# --- Flask Keep Alive ---
# ==========================================================================
app = Flask("")


@app.route("/")
def home():
    return "I'm Mahim's File Host"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("Flask Keep-Alive server started.")


# ==========================================================================
# --- Configuration (fallback defaults — most of these can be changed live
#     from Admin -> Settings, and are then loaded from the DB at startup) ---
# ==========================================================================
TOKEN = "8874410113:AAG_OA_YO8a7g-M4NWlk_ah_ViL1nzwC8kk"
OWNER_ID = 7176443600
ADMIN_ID = 7176443600
DEFAULT_YOUR_USERNAME = "@mahim_2422"
DEFAULT_UPDATE_CHANNEL = "https://t.me/earnmastermind"
DEFAULT_USDT_BDT_RATE = 120.0
DEFAULT_DEPOSIT_GROUP_ID = ""  # e.g. "-1001234567890" — set from Admin -> Settings

BINANCE_API_KEY = "YOUR_BINANCE_API_KEY"
BINANCE_SECRET_KEY = "YOUR_BINANCE_SECRET_KEY"
BINANCE_PAY_ID = "YOUR_BINANCE_PAY_ID"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, "upload_bots")
IROTECH_DIR = os.path.join(BASE_DIR, "inf")
DATABASE_PATH = os.path.join(IROTECH_DIR, "bot_data.db")

FREE_USER_LIMIT = 0
SUBSCRIBED_USER_LIMIT = 15
ADMIN_LIMIT = 999
OWNER_LIMIT = float("inf")

AUTO_BACKUP_ENABLED = True
BACKUP_INTERVAL_SECONDS = 12 * 60 * 60
BACKUP_CHECK_INTERVAL_SECONDS = 600

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# --- Core in-memory state ---
bot_scripts = {}
user_subscriptions = {}
user_storage_subs = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
storage_alert_sent = {}
STORAGE_ALERT_THRESHOLDS = [50, 60, 70, 80, 90, 100]

user_referrals = {}
user_wallets = {}
user_free_hours = {}
user_auto_restart = {}
user_languages = {}          # 🆕 user_id -> 'bn' / 'en'
deposit_methods = {}         # 🆕 method_id -> {name, number, note}
deposit_flow_temp = {}       # 🆕 user_id -> in-progress deposit data
manually_stopped_scripts = set()
BOT_USERNAME = None

MALWARE_SIGNATURES = [b"MZ", b"\x7fELF", b"\xfe\xed\xfa", b"\xce\xfa\xed\xfe", b"PK", b"Rar!"]
ENCRYPTED_FILE_INDICATORS = [b"openssl", b"encrypted", b"cipher", b"AES", b"DES", b"RSA", b"GPG", b"PGP"]
SUSPICIOUS_KEYWORDS = [b"ransomware", b"trojan", b"virus", b"malware", b"backdoor",
                        b"exploit", b"payload", b"botnet", b"keylogger", b"rootkit"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_LOCK = threading.Lock()

# ==========================================================================
# --- Database Setup ---
# ==========================================================================
def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()

        c.execute("""CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, plan_name TEXT, expiry TEXT,
                      file_limit INTEGER, storage_mb INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS plans
                     (plan_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, file_limit INTEGER,
                      storage_mb INTEGER, price TEXT, duration INTEGER, buy_link TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS subscription_plans
                     (sub_plan_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                      storage_mb INTEGER, price TEXT, duration INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS storage_subscriptions
                     (user_id INTEGER PRIMARY KEY, plan_name TEXT, storage_mb INTEGER, expiry TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT, display_name TEXT,
                      PRIMARY KEY (user_id, file_name))""")
        c.execute("""CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pending_payments
                     (user_id INTEGER, plan_type TEXT, plan_id INTEGER, paid_amount REAL,
                      PRIMARY KEY (user_id, plan_type, plan_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS used_txids (tx_id TEXT PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS backup_access
                     (user_id INTEGER PRIMARY KEY, enabled INTEGER, expiry TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS backup_log (user_id INTEGER PRIMARY KEY, last_sent TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS referrals
                     (user_id INTEGER PRIMARY KEY, referred_by INTEGER, referral_count INTEGER DEFAULT 0,
                      reward_claimed INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_wallet (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS free_hours
                     (user_id INTEGER PRIMARY KEY, hours_remaining REAL DEFAULT 0, last_checked TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS auto_restart_settings
                     (user_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1)""")

        # 🆕 Language preference
        c.execute("""CREATE TABLE IF NOT EXISTS user_language (user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'bn')""")

        # 🆕 Deposit system
        c.execute("""CREATE TABLE IF NOT EXISTS deposit_methods
                     (method_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, number TEXT, note TEXT, currency TEXT DEFAULT 'BDT')""")
        c.execute("""CREATE TABLE IF NOT EXISTS deposit_requests
                     (request_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, method_name TEXT,
                      sender_number TEXT, amount REAL, tx_id TEXT, status TEXT DEFAULT 'pending',
                      created_at TEXT, group_message_id INTEGER)""")

        # 🆕 Purchase history (for admin stats — count & revenue)
        c.execute("""CREATE TABLE IF NOT EXISTS purchase_log
                     (log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_type TEXT,
                      plan_name TEXT, amount_usdt REAL, method TEXT, created_at TEXT)""")

        # 🆕 Package/Subscription grant history — every grant/purchase gets its own permanent,
        #    human-readable ID (#1, #2, #3...) that shows up to both the user and the admin,
        #    and is required (together with the User ID) to Grant/Revoke precisely.
        c.execute("""CREATE TABLE IF NOT EXISTS package_grants
                     (grant_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT,
                      file_limit INTEGER, storage_mb INTEGER, granted_at TEXT, expiry TEXT,
                      status TEXT DEFAULT 'active', source TEXT DEFAULT 'admin_grant')""")
        c.execute("""CREATE TABLE IF NOT EXISTS subscription_grants
                     (grant_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT,
                      storage_mb INTEGER, granted_at TEXT, expiry TEXT,
                      status TEXT DEFAULT 'active', source TEXT DEFAULT 'admin_grant')""")

        # 🆕 Telegram username/first-name cache (so admin exports can show a real handle, else N/A)
        c.execute("""CREATE TABLE IF NOT EXISTS user_info
                     (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_seen TEXT)""")

        for stmt in [
            "ALTER TABLE user_files ADD COLUMN display_name TEXT",
            "ALTER TABLE plans ADD COLUMN storage_mb INTEGER",
            "ALTER TABLE subscriptions ADD COLUMN file_limit INTEGER",
            "ALTER TABLE subscriptions ADD COLUMN storage_mb INTEGER",
            "ALTER TABLE pending_payments ADD COLUMN plan_type TEXT",
            "ALTER TABLE deposit_requests ADD COLUMN group_message_id INTEGER",
            "ALTER TABLE deposit_methods ADD COLUMN currency TEXT DEFAULT 'BDT'",
            "ALTER TABLE subscriptions ADD COLUMN grant_id INTEGER",
            "ALTER TABLE storage_subscriptions ADD COLUMN grant_id INTEGER",
        ]:
            try:
                c.execute(stmt)
            except Exception:
                pass

        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (ADMIN_ID,))

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Database initialization error: {e}", exc_info=True)


def load_data():
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()

        c.execute("SELECT user_id, plan_name, expiry, file_limit, storage_mb, grant_id FROM subscriptions")
        for user_id, plan_name, expiry, file_limit, storage_mb, grant_id in c.fetchall():
            try:
                user_subscriptions[user_id] = {
                    "plan_name": plan_name or "Premium",
                    "expiry": datetime.fromisoformat(expiry),
                    "file_limit": file_limit if file_limit is not None else SUBSCRIBED_USER_LIMIT,
                    "storage_mb": storage_mb if storage_mb is not None else 0,
                    "grant_id": grant_id,
                }
            except ValueError:
                pass

        c.execute("SELECT user_id, plan_name, storage_mb, expiry, grant_id FROM storage_subscriptions")
        for user_id, plan_name, storage_mb, expiry, grant_id in c.fetchall():
            try:
                user_storage_subs[user_id] = {"plan_name": plan_name, "storage_mb": storage_mb,
                                               "expiry": datetime.fromisoformat(expiry), "grant_id": grant_id}
            except ValueError:
                pass

        c.execute("SELECT user_id, file_name, file_type, display_name FROM user_files")
        for user_id, file_name, file_type, display_name in c.fetchall():
            user_files.setdefault(user_id, []).append((file_name, file_type, display_name))

        c.execute("SELECT user_id FROM active_users")
        active_users.update(uid for (uid,) in c.fetchall())

        c.execute("SELECT user_id FROM admins")
        admin_ids.update(uid for (uid,) in c.fetchall())

        c.execute("SELECT user_id, referred_by, referral_count, reward_claimed FROM referrals")
        for user_id, referred_by, referral_count, reward_claimed in c.fetchall():
            user_referrals[user_id] = {"referred_by": referred_by, "count": referral_count or 0,
                                        "reward_claimed": bool(reward_claimed)}

        c.execute("SELECT user_id, balance FROM user_wallet")
        for user_id, balance in c.fetchall():
            user_wallets[user_id] = balance or 0.0

        c.execute("SELECT user_id, hours_remaining, last_checked FROM free_hours")
        for user_id, hours_remaining, last_checked in c.fetchall():
            try:
                lc = datetime.fromisoformat(last_checked) if last_checked else datetime.now()
            except ValueError:
                lc = datetime.now()
            user_free_hours[user_id] = {"remaining": hours_remaining or 0.0, "last_checked": lc}

        c.execute("SELECT user_id, enabled FROM auto_restart_settings")
        for user_id, enabled in c.fetchall():
            user_auto_restart[user_id] = bool(enabled)

        # 🆕 Language
        c.execute("SELECT user_id, lang FROM user_language")
        for user_id, lang in c.fetchall():
            user_languages[user_id] = lang or "bn"

        # 🆕 Deposit methods
        c.execute("SELECT method_id, name, number, note, currency FROM deposit_methods")
        for mid, name, number, note, currency in c.fetchall():
            deposit_methods[mid] = {"name": name, "number": number, "note": note, "currency": currency or "BDT"}

        conn.close()
        logger.info("Data loaded successfully.")
    except Exception as e:
        logger.error(f"❌ Error loading data: {e}", exc_info=True)


def get_setting(key, default=None):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()


init_db()
load_data()

# --- Runtime-mutable settings loaded from DB (fallback to hardcoded defaults) ---
BINANCE_API_KEY = get_setting("binance_api_key", BINANCE_API_KEY)
BINANCE_SECRET_KEY = get_setting("binance_secret_key", BINANCE_SECRET_KEY)
BINANCE_PAY_ID = get_setting("binance_pay_id", BINANCE_PAY_ID)
YOUR_USERNAME = get_setting("contact_owner_username", DEFAULT_YOUR_USERNAME)
UPDATE_CHANNEL = get_setting("update_channel_link", DEFAULT_UPDATE_CHANNEL)
USDT_BDT_RATE = float(get_setting("usdt_bdt_rate", DEFAULT_USDT_BDT_RATE))
DEPOSIT_GROUP_ID = get_setting("deposit_group_id", DEFAULT_DEPOSIT_GROUP_ID)
REFERRAL_MILESTONE = int(get_setting("referral_milestone", 3))
REFERRAL_REWARD_HOURS = float(get_setting("referral_reward_hours", 5.0))
FREE_POOL_MODE = get_setting("free_pool_mode", "0") == "1"
FREE_POOL_HOURS = float(get_setting("free_pool_hours", 0.0))
FREE_POOL_LAST_CHECK = datetime.now()
BACKUP_FORCE_STOPPED = get_setting("backup_force_stopped", "0") == "1"  # 🆕 kill-switch for everyone except admin/owner

FREE_TRIAL_HOURS = 1.0
FREE_HOURS_CHECK_INTERVAL = 300
AUTO_RESTART_CHECK_INTERVAL = 120


# ==========================================================================
# --- 🆕 i18n / Translation layer ---
# ==========================================================================
def get_lang(user_id):
    return user_languages.get(user_id, "bn")


def set_lang(user_id, lang):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_language (user_id, lang) VALUES (?, ?)", (user_id, lang))
        conn.commit()
        conn.close()
    user_languages[user_id] = lang


TR = {
    "bn": {
        "cancel": "❌ বাতিল",
        "cancelled": "🚫 প্রক্রিয়াটি বাতিল করা হয়েছে।",
        "choose_lang": "🌐 *আপনার ভাষা সিলেক্ট করুন:*",
        "lang_set": "✅ ভাষা বাংলা সেট করা হয়েছে।",
        "btn_upload": "🚀 𝗨𝗽𝗹𝗼𝗮𝗱 𝗙𝗶𝗹𝗲",
        "btn_files": "📁 𝗠𝗮𝗻𝗮𝗴𝗲 𝗙𝗶𝗹𝗲𝘀",
        "btn_plans": "💳 𝗩𝗶𝗲𝘄 𝗣𝗹𝗮𝗻𝘀",
        "btn_subplans": "🎫 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗣𝗹𝗮𝗻𝘀",
        "btn_profile": "👤 𝗠𝘆 𝗣𝗿𝗼𝗳𝗶𝗹𝗲",
        "btn_referral": "🤝 𝗥𝗲𝗳𝗲𝗿𝗿𝗮𝗹",
        "btn_deposit": "💰 𝗗𝗲𝗽𝗼𝘀𝗶𝘁",
        "btn_speed": "⚡ 𝗦𝗽𝗲𝗲𝗱 & 𝗣𝗶𝗻𝗴",
        "btn_stats": "📊 𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝘀",
        "btn_channel": "✨ 𝗨𝗽𝗱𝗮𝘁𝗲𝘀 𝗖𝗵𝗮𝗻𝗻𝗲𝗹 ✨",
        "btn_contact": "👑 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗢𝘄𝗻𝗲𝗿",
        "btn_language": "🌐 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲",
        "btn_admin_panel": "🛡️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹",
        "welcome": (
            "✨ *𝗪𝗲𝗹𝗰𝗼𝗺𝗲, {name}!* ✨\n\n"
            "🆔 *𝗬𝗼𝘂𝗿 𝗜𝗗:* `{uid}`\n"
            "🔰 *𝗦𝘁𝗮𝘁𝘂𝘀:* {status}\n"
            "📁 *𝗨𝗽𝗹𝗼𝗮𝗱𝗲𝗱 𝗙𝗶𝗹𝗲𝘀:* `{count}` / `{limit}`\n\n"
            "💡 *𝗛𝗼𝘀𝘁 & 𝗥𝘂𝗻 𝘆𝗼𝘂𝗿 𝗣𝘆𝘁𝗵𝗼𝗻 (.𝗽𝘆) & 𝗝𝗦 (.𝗷𝘀) 𝗯𝗼𝘁𝘀 𝟮𝟰/𝟳.*\n"
            "👇 _নিচের মেনু থেকে অপশন সিলেক্ট করুন:_"
        ),
        "status_owner": "👑 *Owner*",
        "status_admin": "🛡️ *Admin*",
        "status_sub": "🎫 *{plan} Subscription Active* ({days} দিন বাকি)",
        "status_pkg": "💎 *{plan} Active* ({days} দিন বাকি)",
        "status_trial": "🆓 *Free Trial* ({hrs}h বাকি)",
        "status_none": "🆓 *No Active Plan*",
        "locked": "⚠️ *Bot is temporarily locked by Admin.*",
        "no_plan_upload": (
            "❌ *আপনার কোন এক্টিভ প্ল্যান নেই!*\n\n"
            "ফাইল আপলোড করতে হলে প্রথমে একটি Package বা Subscription প্ল্যান কিনতে হবে।"
        ),
        "btn_view_plans": "💳 View Plans & Buy",
        "btn_view_subplans": "🎫 View Subscription Plans",
        "active_plan_detected": "🔰 *𝗔𝗰𝘁𝗶𝘃𝗲 𝗣𝗹𝗮𝗻 𝗗𝗲𝘁𝗲𝗰𝘁𝗲𝗱:* `{plan}`\n\nফাইল আপলোড চালু করতে নিচের বাটনে সিলেক্ট করুন:",
        "btn_continue_plan": "✅ Continue with {plan}",
        "ask_send_file": "🚀 *এখন আপনার Python (.py), JS (.js) অথবা ZIP (.zip) ফাইল মেসেজে পাঠান।*",
        "no_pkg_plans": "ℹ️ *বর্তমানে কোনো Package Plan উপলব্ধ নেই।*",
        "no_sub_plans": "ℹ️ *বর্তমানে কোনো Subscription Plan উপলব্ধ নেই।*",
        "avail_pkg_plans": "💳 *𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗣𝗮𝗰𝗸𝗮𝗴𝗲 𝗣𝗹𝗮𝗻𝘀:*",
        "avail_sub_plans": "🎫 *𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗣𝗹𝗮𝗻𝘀:*",
        "btn_buy": "🛒 Buy {name} ({price} USDT)",
        "no_files": "_(No files uploaded yet)_",
        "storage_used": "💾 *Storage Used:* `{used:.2f} MB` / `{limit}`",
        "manage_files_title": "📁 *𝗠𝗮𝗻𝗮𝗴𝗲 𝗬𝗼𝘂𝗿 𝗙𝗶𝗹𝗲𝘀:*",
        "profile_title": "👤 *𝗠𝘆 𝗣𝗿𝗼𝗳𝗶𝗹𝗲*",
        "btn_toggle_autorestart": "🔄 Toggle Auto-Restart",
        "btn_referral_link": "🔗 Referral Link",
        "referral_title": (
            "🤝 *𝗥𝗲𝗳𝗲𝗿𝗿𝗮𝗹 𝗣𝗿𝗼𝗴𝗿𝗮𝗺*\n━━━━━━━━━━━━━━━━━━━\n"
            "বন্ধুদের ইনভাইট করুন এবং ফ্রি ঘন্টা জিতুন!\n\n"
            "👥 *আপনার Referrals:* `{progress}/{milestone}`\n"
            "🏆 *Rank:* `{rank}`\n"
            "🎁 *{milestone} জন রেফারে পুরস্কার:* `{reward}h` Free Hours\n\n"
            "🔗 *আপনার রেফারেল লিংক:*\n`{link}`\n━━━━━━━━━━━━━━━━━━━"
        ),
        "admin_only": "❌ *আপনি এডমিন নন!*",
        "admin_panel_title": "🛡️ *𝗔𝗱𝗺𝗶𝗻 𝗖𝗼𝗻𝘁𝗿𝗼𝗹 𝗣𝗮𝗻𝗲𝗹*\n\nক্যাটাগরি সিলেক্ট করুন 👇",
        "settings_title": (
            "⚙️ *𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀*\n\n"
            "🔐 Bot Locked: `{locked}`\n"
            "👑 Contact Owner: `{owner}`\n"
            "📢 Update Channel: `{channel}`\n"
            "💱 USDT/BDT Rate: `{rate}`\n"
            "🏦 Deposit Group ID: `{group}`\n"
        ),
        "back_main": "🔙 Back to Main Menu",
        "back_admin": "🔙 Back to Admin Panel",
        "back_settings": "🔙 Back to Settings",
        "deposit_no_methods": "ℹ️ *এখনো কোনো Deposit Method সেট করা হয়নি। এডমিনের সাথে যোগাযোগ করুন।*",
        "deposit_choose_method": "🏦 *Deposit করতে একটি Method সিলেক্ট করুন:*",
        "deposit_ask_sender": "📱 *যেই নাম্বার থেকে টাকা পাঠিয়েছেন, সেই নাম্বারটি লিখুন:*",
        "deposit_ask_amount": "💰 *কত {currency} Deposit করেছেন, লিখুন:*",
        "deposit_ask_txid": "🧾 *Transaction ID লিখুন:*",
        "deposit_confirm": (
            "📋 *Deposit Request Summary*\n━━━━━━━━━━━━━━━━━━━\n"
            "🏦 Method: `{method}`\n📱 Sender: `{sender}`\n💰 Amount: `{amount} {currency}`\n"
            "🧾 TxID: `{txid}`\n━━━━━━━━━━━━━━━━━━━\nসবকিছু ঠিক থাকলে Confirm করুন:"
        ),
        "btn_confirm": "✅ Confirm",
        "deposit_submitted": "✅ *আপনার Deposit Request জমা হয়েছে!* এডমিন এপ্রুভ করলেই ব্যালেন্স যোগ হয়ে যাবে।",
        "deposit_approved_user": "🎉 *আপনার Deposit `{amount} BDT` Approve হয়েছে!*\nনতুন Balance: `{bal} BDT`",
        "deposit_rejected_user": "❌ *দুঃখিত, আপনার Deposit `{amount} BDT` Reject করা হয়েছে।*\nবিস্তারিত জানতে Owner-এর সাথে যোগাযোগ করুন।",
        "wallet_pay_btn": "💰 Pay with Wallet (~{amt} USDT)",
        "wallet_insufficient": "❌ ওয়ালেটে যথেষ্ট ব্যালেন্স নেই। বর্তমান ব্যালেন্স: {bal} BDT (~{usdt} USDT)",
        "wallet_pay_success": "🎉 *Wallet Balance দিয়ে সফলভাবে {label} কেনা হয়েছে!*\n💰 কাটা হয়েছে: `{deducted} BDT`",
        "both_payment_options": "✅ *আপনার Wallet Balance যথেষ্ট আছে!* আপনি চাইলে Binance Pay অথবা Wallet Balance — দুইভাবেই এই প্ল্যান কিনতে পারবেন।",
        "bot_locked_button": "⚠️ *বট বর্তমানে Maintenance-এর জন্য লক করা আছে। শুধু Contact Owner / Update Channel বাটন কাজ করবে।*",
    },
    "en": {
        "cancel": "❌ Cancel",
        "cancelled": "🚫 Action cancelled.",
        "choose_lang": "🌐 *Choose your language:*",
        "lang_set": "✅ Language set to English.",
        "btn_upload": "🚀 Upload File",
        "btn_files": "📁 Manage Files",
        "btn_plans": "💳 View Plans",
        "btn_subplans": "🎫 Subscription Plans",
        "btn_profile": "👤 My Profile",
        "btn_referral": "🤝 Referral",
        "btn_deposit": "💰 Deposit",
        "btn_speed": "⚡ Speed & Ping",
        "btn_stats": "📊 Bot Stats",
        "btn_channel": "✨ Updates Channel ✨",
        "btn_contact": "👑 Contact Owner",
        "btn_language": "🌐 Language",
        "btn_admin_panel": "🛡️ Admin Panel",
        "welcome": (
            "✨ *Welcome, {name}!* ✨\n\n"
            "🆔 *Your ID:* `{uid}`\n"
            "🔰 *Status:* {status}\n"
            "📁 *Uploaded Files:* `{count}` / `{limit}`\n\n"
            "💡 *Host & run your Python (.py) & JS (.js) bots 24/7.*\n"
            "👇 _Select an option from the menu below:_"
        ),
        "status_owner": "👑 *Owner*",
        "status_admin": "🛡️ *Admin*",
        "status_sub": "🎫 *{plan} Subscription Active* ({days} days left)",
        "status_pkg": "💎 *{plan} Active* ({days} days left)",
        "status_trial": "🆓 *Free Trial* ({hrs}h left)",
        "status_none": "🆓 *No Active Plan*",
        "locked": "⚠️ *Bot is temporarily locked by Admin.*",
        "no_plan_upload": (
            "❌ *You don't have any active plan!*\n\n"
            "You need a Package or Subscription plan before uploading a file."
        ),
        "btn_view_plans": "💳 View Plans & Buy",
        "btn_view_subplans": "🎫 View Subscription Plans",
        "active_plan_detected": "🔰 *Active Plan Detected:* `{plan}`\n\nTap below to start uploading a file:",
        "btn_continue_plan": "✅ Continue with {plan}",
        "ask_send_file": "🚀 *Now send your Python (.py), JS (.js) or ZIP (.zip) file.*",
        "no_pkg_plans": "ℹ️ *No Package Plans available right now.*",
        "no_sub_plans": "ℹ️ *No Subscription Plans available right now.*",
        "avail_pkg_plans": "💳 *Available Package Plans:*",
        "avail_sub_plans": "🎫 *Available Subscription Plans:*",
        "btn_buy": "🛒 Buy {name} ({price} USDT)",
        "no_files": "_(No files uploaded yet)_",
        "storage_used": "💾 *Storage Used:* `{used:.2f} MB` / `{limit}`",
        "manage_files_title": "📁 *Manage Your Files:*",
        "profile_title": "👤 *My Profile*",
        "btn_toggle_autorestart": "🔄 Toggle Auto-Restart",
        "btn_referral_link": "🔗 Referral Link",
        "referral_title": (
            "🤝 *Referral Program*\n━━━━━━━━━━━━━━━━━━━\n"
            "Invite friends and earn free hours!\n\n"
            "👥 *Your Referrals:* `{progress}/{milestone}`\n"
            "🏆 *Rank:* `{rank}`\n"
            "🎁 *Reward at {milestone} referrals:* `{reward}h` Free Hours\n\n"
            "🔗 *Your referral link:*\n`{link}`\n━━━━━━━━━━━━━━━━━━━"
        ),
        "admin_only": "❌ *You are not an admin!*",
        "admin_panel_title": "🛡️ *Admin Control Panel*\n\nSelect a category 👇",
        "settings_title": (
            "⚙️ *Settings*\n\n"
            "🔐 Bot Locked: `{locked}`\n"
            "👑 Contact Owner: `{owner}`\n"
            "📢 Update Channel: `{channel}`\n"
            "💱 USDT/BDT Rate: `{rate}`\n"
            "🏦 Deposit Group ID: `{group}`\n"
        ),
        "back_main": "🔙 Back to Main Menu",
        "back_admin": "🔙 Back to Admin Panel",
        "back_settings": "🔙 Back to Settings",
        "deposit_no_methods": "ℹ️ *No deposit method has been set up yet. Please contact the admin.*",
        "deposit_choose_method": "🏦 *Select a method to deposit:*",
        "deposit_ask_sender": "📱 *Enter the number you sent money from:*",
        "deposit_ask_amount": "💰 *Enter the amount ({currency}) you deposited:*",
        "deposit_ask_txid": "🧾 *Enter the Transaction ID:*",
        "deposit_confirm": (
            "📋 *Deposit Request Summary*\n━━━━━━━━━━━━━━━━━━━\n"
            "🏦 Method: `{method}`\n📱 Sender: `{sender}`\n💰 Amount: `{amount} {currency}`\n"
            "🧾 TxID: `{txid}`\n━━━━━━━━━━━━━━━━━━━\nConfirm if everything looks correct:"
        ),
        "btn_confirm": "✅ Confirm",
        "deposit_submitted": "✅ *Your deposit request has been submitted!* Balance will be added once approved.",
        "deposit_approved_user": "🎉 *Your deposit of `{amount} BDT` has been approved!*\nNew balance: `{bal} BDT`",
        "deposit_rejected_user": "❌ *Sorry, your deposit of `{amount} BDT` was rejected.*\nContact the owner for details.",
        "wallet_pay_btn": "💰 Pay with Wallet (~{amt} USDT)",
        "wallet_insufficient": "❌ Not enough wallet balance. Current: {bal} BDT (~{usdt} USDT)",
        "wallet_pay_success": "🎉 *Successfully purchased {label} using Wallet Balance!*\n💰 Deducted: `{deducted} BDT`",
        "both_payment_options": "✅ *Your wallet balance is sufficient!* You can pay via Binance Pay OR Wallet Balance — whichever you prefer.",
        "bot_locked_button": "⚠️ *The bot is currently locked for maintenance. Only Contact Owner / Update Channel are available.*",
    },
}


def tr(user_id, key, **kwargs):
    lang = get_lang(user_id)
    text = TR.get(lang, TR["bn"]).get(key) or TR["bn"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


CANCEL_TEXTS = {"❌ Cancel", "❌ বাতিল"}


def cancel_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton(tr(user_id, "cancel")))
    return markup


def admin_panel_keyboard(user_id):
    """🆕 Keyboard that keeps an admin inside the Admin Panel (used after any admin action/cancel)."""
    return _kb_from_keys(user_id, ADMIN_PANEL_KEYS)


def return_keyboard(user_id, admin_flow=False):
    """🆕 Which keyboard to show after a step completes/cancels — Admin Panel for admin flows, else Main Menu."""
    if admin_flow and user_id in admin_ids:
        return admin_panel_keyboard(user_id)
    return create_reply_keyboard_main_menu(user_id)


def guarded(next_func, admin_flow, *extra_args):
    """Wrap a register_next_step_handler callback so the user can bail out with Cancel.
    admin_flow=True keeps the admin inside the Admin Panel instead of dropping to the user menu."""
    def handler(m):
        uid = m.from_user.id
        if m.text and m.text.strip() in CANCEL_TEXTS:
            bot.send_message(m.chat.id, tr(uid, "cancelled"), reply_markup=return_keyboard(uid, admin_flow))
            return
        next_func(m, *extra_args)
    return handler


def step_prompt(chat_id, user_id, text, next_func, *extra_args, admin_flow=False):
    msg = bot.send_message(chat_id, text, reply_markup=cancel_keyboard(user_id), parse_mode="Markdown")
    bot.register_next_step_handler(msg, guarded(next_func, admin_flow, *extra_args))


# ==========================================================================
# --- Menu Layouts (built dynamically per-language via action keys) ---
# ==========================================================================
def user_menu_layout(user_id):
    is_admin = user_id in admin_ids
    rows = [
        [tr(user_id, "btn_upload"), tr(user_id, "btn_files")],
        [tr(user_id, "btn_plans"), tr(user_id, "btn_subplans")],
        [tr(user_id, "btn_profile"), tr(user_id, "btn_referral")],
        [tr(user_id, "btn_deposit"), tr(user_id, "btn_language")],
        [tr(user_id, "btn_speed"), tr(user_id, "btn_stats")],
        [tr(user_id, "btn_channel"), tr(user_id, "btn_contact")],
    ]
    if is_admin:
        rows.append([tr(user_id, "btn_admin_panel")])
    return rows


ACTION_LABEL_KEYS = {
    "upload": "btn_upload", "files": "btn_files", "plans": "btn_plans", "subplans": "btn_subplans",
    "profile": "btn_profile", "referral": "btn_referral", "deposit": "btn_deposit",
    "language": "btn_language", "speed": "btn_speed", "stats": "btn_stats",
    "channel": "btn_channel", "contact": "btn_contact", "admin_panel": "btn_admin_panel",
}


def all_label_to_action():
    """Reverse-map every localized label (both languages) back to its action key."""
    mapping = {}
    for action, key in ACTION_LABEL_KEYS.items():
        mapping[TR["bn"][key]] = action
        mapping[TR["en"][key]] = action
    return mapping


ADMIN_PANEL_KEYS = {
    # 🆕 Renamed pkg_panel/sub_panel to "Manage ..." so they never collide, in any language,
    #    with the user-facing "View Plans" / "Subscription Plans" buttons (this was the bug
    #    behind "Subscription Plans not working" — the labels were identical in English).
    "pkg_panel": {"bn": "📦 Manage Package Plans", "en": "📦 Manage Package Plans"},
    "sub_panel": {"bn": "🎫 Manage Subscriptions", "en": "🎫 Manage Subscriptions"},
    "admin_mgmt": {"bn": "👑 Admin Management", "en": "👑 Admin Management"},
    "broadcast": {"bn": "📣 Broadcast", "en": "📣 Broadcast"},
    "run_all": {"bn": "⚙️ Run All Scripts", "en": "⚙️ Run All Scripts"},
    "stop_all": {"bn": "🛑 Stop All Scripts", "en": "🛑 Stop All Scripts"},
    "bot_stats_a": {"bn": "📊 Full Bot Stats (Admin)", "en": "📊 Full Bot Stats (Admin)"},
    "backup_panel": {"bn": "💾 Backup Control", "en": "💾 Backup Control"},
    "wallet_panel": {"bn": "💰 Wallet Control", "en": "💰 Wallet Control"},
    "freehours_panel": {"bn": "🎁 Free Hours Control", "en": "🎁 Free Hours Control"},
    "settings_panel": {"bn": "⚙️ Settings", "en": "⚙️ Settings"},
    "back_main2": {"bn": "🔙 Back to Main Menu", "en": "🔙 Back to Main Menu"},
}

PACKAGE_PANEL_KEYS = {
    "add_plan": {"bn": "➕ Add Package Plan", "en": "➕ Add Package Plan"},
    "manage_plans": {"bn": "🗑️ Manage Package Plans", "en": "🗑️ Manage Package Plans"},
    "grant_pkg": {"bn": "💎 Grant Package", "en": "💎 Grant Package"},
    "revoke_pkg": {"bn": "❌ Revoke Package", "en": "❌ Revoke Package"},
    "total_pkg": {"bn": "📊 Total Packages (Export)", "en": "📊 Total Packages (Export)"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}

SUBSCRIPTION_PANEL_KEYS = {
    "add_subplan": {"bn": "➕ Add Subscription Plan", "en": "➕ Add Subscription Plan"},
    "manage_subplans": {"bn": "🗑️ Manage Subscription Plans", "en": "🗑️ Manage Subscription Plans"},
    "grant_sub": {"bn": "🎟️ Grant Subscription", "en": "🎟️ Grant Subscription"},
    "revoke_sub": {"bn": "❎ Revoke Subscription", "en": "❎ Revoke Subscription"},
    "total_sub": {"bn": "📊 Total Subscriptions (Export)", "en": "📊 Total Subscriptions (Export)"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}

ADMIN_MGMT_KEYS = {
    "add_admin": {"bn": "👑 Add Admin", "en": "👑 Add Admin"},
    "remove_admin": {"bn": "➖ Remove Admin", "en": "➖ Remove Admin"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}

SETTINGS_PANEL_KEYS = {
    "api_settings_sub": {"bn": "🔑 API Settings", "en": "🔑 API Settings"},
    "deposit_methods_sub": {"bn": "🏦 Deposit Methods", "en": "🏦 Deposit Methods"},
    "deposit_group_sub": {"bn": "👥 Deposit Group ID", "en": "👥 Deposit Group ID"},
    "usdt_rate_sub": {"bn": "💱 USDT Rate", "en": "💱 USDT Rate"},
    "lock_toggle": {"bn": "🔐 Lock/Unlock Bot", "en": "🔐 Lock/Unlock Bot"},
    "contact_owner_edit": {"bn": "👑 Edit Contact Owner", "en": "👑 Edit Contact Owner"},
    "channel_edit": {"bn": "📢 Edit Update Channel", "en": "📢 Edit Update Channel"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}

API_SETTINGS_KEYS = {
    "change_api_key": {"bn": "🔑 Change API Key", "en": "🔑 Change API Key"},
    "change_secret": {"bn": "🔒 Change Secret Key", "en": "🔒 Change Secret Key"},
    "change_payid": {"bn": "🆔 Change Pay ID", "en": "🆔 Change Pay ID"},
    "back_settings2": {"bn": "🔙 Back to Settings", "en": "🔙 Back to Settings"},
}

BACKUP_PANEL_KEYS = {
    "grant_backup": {"bn": "➕ Grant Backup Access", "en": "➕ Grant Backup Access"},
    "revoke_backup": {"bn": "➖ Revoke Backup Access", "en": "➖ Revoke Backup Access"},
    "list_backup": {"bn": "📋 List Backup Users", "en": "📋 List Backup Users"},
    "toggle_autobackup": {"bn": "🔁 Toggle Auto-Backup", "en": "🔁 Toggle Auto-Backup"},
    "stop_all_backup": {"bn": "🛑 Stop All Backups (Users)", "en": "🛑 Stop All Backups (Users)"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}

WALLET_PANEL_KEYS = {
    "add_balance": {"bn": "➕ Add Balance", "en": "➕ Add Balance"},
    "deduct_balance": {"bn": "➖ Deduct Balance", "en": "➖ Deduct Balance"},
    "check_balance": {"bn": "📋 Check Balance", "en": "📋 Check Balance"},
    "pending_deposits": {"bn": "📥 All Pending Deposits", "en": "📥 All Pending Deposits"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}

FREE_HOURS_PANEL_KEYS = {
    "grant_freehours": {"bn": "🎁 Grant Free Hours (1 user)", "en": "🎁 Grant Free Hours (1 user)"},
    "grant_all_freehours": {"bn": "🎁 Grant Hours to ALL Users", "en": "🎁 Grant Hours to ALL Users"},
    "remove_freehours": {"bn": "➖ Remove Free Hours", "en": "➖ Remove Free Hours"},
    "check_freehours": {"bn": "📋 Check Free Hours", "en": "📋 Check Free Hours"},
    "toggle_pool_mode": {"bn": "🌐 Toggle Shared Pool Mode", "en": "🌐 Toggle Shared Pool Mode"},
    "set_pool_hours": {"bn": "⏳ Set Shared Pool Hours", "en": "⏳ Set Shared Pool Hours"},
    "set_referral_config": {"bn": "⚙️ Set Referral Reward", "en": "⚙️ Set Referral Reward"},
    "back_admin2": {"bn": "🔙 Back to Admin Panel", "en": "🔙 Back to Admin Panel"},
}


def _kb_from_keys(user_id, keys_dict, row_width=2):
    lang = get_lang(user_id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=row_width)
    labels = [v[lang] for v in keys_dict.values()]
    # add two-per-row except keep last (back) on its own row
    items = labels[:-1]
    back = labels[-1]
    for i in range(0, len(items), row_width):
        markup.add(*[types.KeyboardButton(t) for t in items[i:i + row_width]])
    markup.add(types.KeyboardButton(back))
    return markup


def create_reply_keyboard_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for row in user_menu_layout(user_id):
        markup.add(*[types.KeyboardButton(t) for t in row])
    return markup


def _reverse_map(keys_dict):
    m = {}
    for action, langs in keys_dict.items():
        m[langs["bn"]] = action
        m[langs["en"]] = action
    return m


ADMIN_PANEL_REV = _reverse_map(ADMIN_PANEL_KEYS)
PACKAGE_PANEL_REV = _reverse_map(PACKAGE_PANEL_KEYS)
SUBSCRIPTION_PANEL_REV = _reverse_map(SUBSCRIPTION_PANEL_KEYS)
ADMIN_MGMT_REV = _reverse_map(ADMIN_MGMT_KEYS)
SETTINGS_PANEL_REV = _reverse_map(SETTINGS_PANEL_KEYS)
API_SETTINGS_REV = _reverse_map(API_SETTINGS_KEYS)
BACKUP_PANEL_REV = _reverse_map(BACKUP_PANEL_KEYS)
WALLET_PANEL_REV = _reverse_map(WALLET_PANEL_KEYS)
FREE_HOURS_PANEL_REV = _reverse_map(FREE_HOURS_PANEL_KEYS)


# --- Markdown-safe escaping ---
def esc_md(text):
    if text is None:
        return ""
    text = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def safe_reply(message, text, **kwargs):
    """🆕 Defensive reply: if Markdown parsing fails (e.g. an admin-set value has a stray
    special character), retry as plain text instead of silently swallowing the message."""
    try:
        return bot.reply_to(message, text, **kwargs)
    except Exception as e:
        logger.warning(f"safe_reply Markdown fallback triggered: {e}")
        kwargs.pop("parse_mode", None)
        return bot.reply_to(message, re.sub(r"[*_`\[\]]", "", text), **kwargs)


def parse_price_to_usdt(price_str):
    price_clean = str(price_str).upper().strip()
    numbers = re.findall(r"[-+]?\d*\.\d+|\d+", price_clean)
    if not numbers:
        return 0.0, price_str
    val = float(numbers[0])
    if "BDT" in price_clean or "TAKA" in price_clean or "TK" in price_clean:
        usdt_val = round(val / USDT_BDT_RATE, 2)
        return usdt_val, f"{price_str} (~{usdt_val} USDT)"
    elif "USDT" in price_clean or "$" in price_clean or "USD" in price_clean:
        return round(val, 2), f"{val} USDT"
    else:
        return round(val, 2), f"{val} USDT"


def plan_icon_for_duration(duration_days):
    if duration_days >= 3650:
        return "👑"
    elif duration_days >= 300:
        return "🎯"
    return "📅"


def format_file_limit(limit):
    if limit == float("inf") or limit == OWNER_LIMIT:
        return "Unlimited"
    return str(int(limit))


# ==========================================================================
# --- Package Plan DB helpers ---
# ==========================================================================
def add_plan_db(name, file_limit, storage_mb, price, duration, buy_link):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO plans (name, file_limit, storage_mb, price, duration, buy_link) VALUES (?, ?, ?, ?, ?, ?)",
                  (name, file_limit, storage_mb, price, duration, buy_link))
        conn.commit()
        conn.close()


def get_all_plans():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT plan_id, name, file_limit, storage_mb, price, duration, buy_link FROM plans")
    plans = c.fetchall()
    conn.close()
    return plans


def get_plan_by_id(plan_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT plan_id, name, file_limit, storage_mb, price, duration, buy_link FROM plans WHERE plan_id = ?", (plan_id,))
    plan = c.fetchone()
    conn.close()
    return plan


def delete_plan_db(plan_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
        conn.commit()
        conn.close()


def add_subscription_plan_db(name, storage_mb, price, duration):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO subscription_plans (name, storage_mb, price, duration) VALUES (?, ?, ?, ?)",
                  (name, storage_mb, price, duration))
        conn.commit()
        conn.close()


def get_all_subscription_plans():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT sub_plan_id, name, storage_mb, price, duration FROM subscription_plans")
    plans = c.fetchall()
    conn.close()
    return plans


def get_subscription_plan_by_id(sub_plan_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT sub_plan_id, name, storage_mb, price, duration FROM subscription_plans WHERE sub_plan_id = ?", (sub_plan_id,))
    plan = c.fetchone()
    conn.close()
    return plan


def delete_subscription_plan_db(sub_plan_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM subscription_plans WHERE sub_plan_id = ?", (sub_plan_id,))
        conn.commit()
        conn.close()


def get_pending_payment(user_id, plan_type, plan_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT paid_amount FROM pending_payments WHERE user_id=? AND plan_type=? AND plan_id=?", (user_id, plan_type, plan_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0.0


def update_pending_payment(user_id, plan_type, plan_id, amount):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO pending_payments (user_id, plan_type, plan_id, paid_amount) VALUES (?, ?, ?, ?)",
                  (user_id, plan_type, plan_id, amount))
        conn.commit()
        conn.close()


def clear_pending_payment(user_id, plan_type, plan_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM pending_payments WHERE user_id=? AND plan_type=? AND plan_id=?", (user_id, plan_type, plan_id))
        conn.commit()
        conn.close()


def is_txid_used(tx_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT tx_id FROM used_txids WHERE tx_id=?", (str(tx_id).strip(),))
    row = c.fetchone()
    conn.close()
    return row is not None


def add_used_txid(tx_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO used_txids (tx_id) VALUES (?)", (str(tx_id).strip(),))
        conn.commit()
        conn.close()


def check_binance_payment(pay_order_id):
    if not BINANCE_API_KEY or BINANCE_API_KEY == "YOUR_NEW_BINANCE_API_KEY_HERE":
        return False, 0.0, "Binance API Key configured নেই।"
    endpoint = "https://api.binance.com/sapi/v1/pay/transactions"
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            transactions = data.get("data", []) if isinstance(data, dict) else data
            for item in transactions:
                order_id_str = str(item.get("orderId", "") or item.get("transactionId", ""))
                if order_id_str.strip() == str(pay_order_id).strip():
                    amount = float(item.get("amount", 0.0))
                    currency = item.get("currency", "USDT")
                    return True, amount, f"{amount} {currency}"
            return False, 0.0, "এই Order/Transaction ID টি আপনার Binance Pay হিস্টোরিতে পাওয়া যায়নি।"
        else:
            logger.error(f"Binance Pay API Error: {res.text}")
            return False, 0.0, "Binance Server Error বা API পারমিশন ইস্যু।"
    except Exception as e:
        logger.error(f"Binance Verification Error: {e}")
        return False, 0.0, f"Error: {str(e)}"


def is_suspicious_file(file_content, file_name):
    file_lower = file_name.lower()
    suspicious_extensions = [".exe", ".dll", ".bat", ".cmd", ".scr", ".com", ".pif", ".application",
                              ".gadget", ".msi", ".msp", ".hta", ".cpl", ".msc", ".jar", ".bin",
                              ".deb", ".rpm", ".apk", ".app", ".dmg", ".iso", ".img"]
    if any(file_lower.endswith(ext) for ext in suspicious_extensions):
        return True, f"Suspicious file extension: {file_name}"
    for signature in MALWARE_SIGNATURES:
        if file_content.startswith(signature):
            return True, f"Malware signature detected: {signature}"
    sample_size = min(len(file_content), 4096)
    file_sample = file_content[:sample_size]
    for indicator in ENCRYPTED_FILE_INDICATORS:
        if indicator in file_sample:
            return True, f"Encrypted file indicator: {indicator.decode('utf-8', errors='ignore')}"
    sample_text = file_sample.decode("utf-8", errors="ignore").lower()
    for keyword in SUSPICIOUS_KEYWORDS:
        if keyword.decode("utf-8").lower() in sample_text:
            return True, f"Suspicious keyword found: {keyword.decode('utf-8')}"
    return False, "File appears safe"


def scan_file_for_malware(file_content, file_name, user_id):
    if user_id == OWNER_ID:
        return True, "Owner bypassed security check"
    is_suspicious, reason = is_suspicious_file(file_content, file_name)
    if is_suspicious:
        logger.warning(f"🚨 Malware detected in {file_name} from user {user_id}: {reason}")
        return False, f"Security violation: {reason}"
    return True, "File passed security check"


def get_user_folder(user_id):
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder


def has_paid_plan(user_id):
    if user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now():
        return True
    if user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        return True
    return False


def get_user_file_limit(user_id):
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    if user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now():
        return float("inf")
    if user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        return user_subscriptions[user_id].get("file_limit") or SUBSCRIBED_USER_LIMIT
    if get_free_hours_remaining(user_id) > 0:
        return 1
    return FREE_USER_LIMIT


def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))


def get_user_storage_usage_mb(user_id):
    ufolder = get_user_folder(user_id)
    total = 0
    if os.path.isdir(ufolder):
        for root, dirs, files in os.walk(ufolder):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except Exception:
                    pass
    return total / (1024 * 1024)


def get_user_storage_limit_mb(user_id):
    if user_id == OWNER_ID or user_id in admin_ids:
        return None
    if user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now():
        return user_storage_subs[user_id]["storage_mb"]
    if user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        return user_subscriptions[user_id].get("storage_mb") or 0
    if get_free_hours_remaining(user_id) > 0:
        return 50
    return 0


def check_and_notify_storage_usage(user_id):
    limit_mb = get_user_storage_limit_mb(user_id)
    if not limit_mb:
        return
    usage_mb = get_user_storage_usage_mb(user_id)
    percent = (usage_mb / limit_mb) * 100 if limit_mb > 0 else 0
    last_notified = storage_alert_sent.get(user_id, 0)
    for threshold in STORAGE_ALERT_THRESHOLDS:
        if percent >= threshold and threshold > last_notified:
            try:
                extra = ("🚫 আপনার স্টোরেজ সম্পূর্ণ পূর্ণ! নতুন ফাইল আপলোডের আগে পুরাতন ফাইল ডিলিট করুন অথবা প্ল্যান আপগ্রেড করুন।"
                         if threshold == 100 else "💡 প্রয়োজনে প্ল্যান আপগ্রেড করে স্টোরেজ বাড়িয়ে নিন।")
                bot.send_message(user_id,
                                  f"⚠️ *Storage Alert!*\nআপনি আপনার স্টোরেজের {threshold}% ব্যবহার করে ফেলেছেন।\n"
                                  f"💾 Used: `{usage_mb:.2f} MB` / `{limit_mb} MB`\n{extra}",
                                  parse_mode="Markdown")
            except Exception:
                pass
            storage_alert_sent[user_id] = threshold


def is_bot_running(script_owner_id, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    if script_info and script_info.get("process"):
        try:
            proc = psutil.Process(script_info["process"].pid)
            is_running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            if not is_running:
                if ("log_file" in script_info and hasattr(script_info["log_file"], "close")
                        and not script_info["log_file"].closed):
                    try:
                        script_info["log_file"].close()
                    except Exception:
                        pass
                if script_key in bot_scripts:
                    del bot_scripts[script_key]
            return is_running
        except psutil.NoSuchProcess:
            if script_key in bot_scripts:
                del bot_scripts[script_key]
            return False
        except Exception:
            return False
    return False


def kill_process_tree(process_info):
    try:
        if ("log_file" in process_info and hasattr(process_info["log_file"], "close")
                and not process_info["log_file"].closed):
            try:
                process_info["log_file"].close()
            except Exception:
                pass
        process = process_info.get("process")
        if process and hasattr(process, "pid"):
            pid = process.pid
            if pid:
                parent = psutil.Process(pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                try:
                    parent.terminate()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"❌ Error killing process: {e}")


TELEGRAM_MODULES = {
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot", "python_telegram_bot": "python-telegram-bot",
    "aiogram": "aiogram", "pyrogram": "pyrogram", "telethon": "telethon", "bs4": "beautifulsoup4",
    "requests": "requests", "pillow": "Pillow", "cv2": "opencv-python", "flask": "Flask", "psutil": "psutil",
}


def monitor_and_guide_error(process, log_file_path, script_owner_id, file_name, message_obj_for_reply):
    time.sleep(3)
    if process.poll() is not None:
        try:
            with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
                log_content = f.read()
            match_py = re.search(r"(?:ModuleNotFoundError|ImportError): No module named '(.+?)'", log_content)
            match_js = re.search(r"Cannot find module '(.+?)'", log_content)
            missing_module = None
            if match_py:
                missing_module = match_py.group(1).split(".")[0].strip("'\"")
            elif match_js:
                missing_module = match_js.group(1).split("/")[0].strip("'\"")

            if missing_module:
                pkg_name = TELEGRAM_MODULES.get(missing_module.lower(), missing_module)
                ext = os.path.splitext(file_name)[1].lower()
                cmd_text = f"npm install {pkg_name}" if ext == ".js" else f"pip install {pkg_name}"
                error_msg = (f"⚠️ *ফাইল রান হতে সমস্যা হয়েছে!*\n\n📄 *File:* `{esc_md(file_name)}`\n"
                             f"❌ *সমস্যা:* আপনার কোডে `{esc_md(missing_module)}` মডিউলটি মিসিং আছে।\n"
                             f"💻 *প্রয়োজনীয় কমান্ড:* `{esc_md(cmd_text)}`\n\n👇 _নিচের বাটনে প্রেস করে সরাসরি মডিউলটি ইনস্টল করুন:_")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(f"📦 Install {pkg_name}",
                                                       callback_data=f"instmod_{script_owner_id}_{missing_module}_{file_name}"))
                markup.add(types.InlineKeyboardButton("📄 View Error Logs", callback_data=f"viewlog_{script_owner_id}_{file_name}"))
                bot.reply_to(message_obj_for_reply, error_msg, reply_markup=markup, parse_mode="Markdown")
            else:
                error_msg = (f"⚠️ *আপনার কোডে ভুল (Syntax/Runtime Error) পাওয়া গেছে!*\n\n📄 *File:* `{esc_md(file_name)}`\n"
                             f"সুনির্দিষ্ট এরর জানতে নিচের *View Logs* বাটনে ক্লিক করুন।")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📄 View Error Logs", callback_data=f"viewlog_{script_owner_id}_{file_name}"))
                bot.reply_to(message_obj_for_reply, error_msg, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error checking log file: {e}")


def run_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply):
    script_key = f"{script_owner_id}_{file_name}"
    manually_stopped_scripts.discard(script_key)
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, "w", encoding="utf-8", errors="ignore")
        process = subprocess.Popen([sys.executable, script_path], cwd=user_folder,
                                    stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {"process": process, "log_file": log_file, "file_name": file_name,
                                    "script_owner_id": script_owner_id, "start_time": datetime.now(),
                                    "user_folder": user_folder, "type": "py", "script_key": script_key}
        bot.reply_to(message_obj_for_reply, f"🚀 *Python Script Started!*\n📄 File: `{esc_md(file_name)}`\n🆔 PID: `{process.pid}`", parse_mode="Markdown")
        threading.Thread(target=monitor_and_guide_error, args=(process, log_file_path, script_owner_id, file_name, message_obj_for_reply)).start()
    except Exception as e:
        bot.reply_to(message_obj_for_reply, f"❌ Error running script: {esc_md(str(e))}", parse_mode="Markdown")


def run_js_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply):
    script_key = f"{script_owner_id}_{file_name}"
    manually_stopped_scripts.discard(script_key)
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, "w", encoding="utf-8", errors="ignore")
        process = subprocess.Popen(["node", script_path], cwd=user_folder,
                                    stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {"process": process, "log_file": log_file, "file_name": file_name,
                                    "script_owner_id": script_owner_id, "start_time": datetime.now(),
                                    "user_folder": user_folder, "type": "js", "script_key": script_key}
        bot.reply_to(message_obj_for_reply, f"🚀 *JS Script Started!*\n📄 File: `{esc_md(file_name)}`\n🆔 PID: `{process.pid}`", parse_mode="Markdown")
        threading.Thread(target=monitor_and_guide_error, args=(process, log_file_path, script_owner_id, file_name, message_obj_for_reply)).start()
    except Exception as e:
        bot.reply_to(message_obj_for_reply, f"❌ Error running JS script: {esc_md(str(e))}", parse_mode="Markdown")


def save_user_file(user_id, file_name, file_type="py", display_name=None):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, display_name) VALUES (?, ?, ?, ?)",
                  (user_id, file_name, file_type, display_name))
        conn.commit()
        conn.close()
        user_files.setdefault(user_id, [])
        user_files[user_id] = [(fn, ft, dn) for fn, ft, dn in user_files[user_id] if fn != file_name]
        user_files[user_id].append((file_name, file_type, display_name))


def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM user_files WHERE user_id = ? AND file_name = ?", (user_id, file_name))
        conn.commit()
        conn.close()
        if user_id in user_files:
            user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]


def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO active_users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()


def save_subscription(user_id, plan_name, expiry, file_limit=None, storage_mb=None, source="admin_grant"):
    """🆕 Every call creates a new permanent, auto-numbered grant_id in package_grants
    (any previous active grant for this user is marked 'superseded'), and that ID is
    attached to the live subscriptions row so it's always visible to user & admin."""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE package_grants SET status='superseded' WHERE user_id=? AND status='active'", (user_id,))
        c.execute("INSERT INTO package_grants (user_id, plan_name, file_limit, storage_mb, granted_at, expiry, status, source) "
                  "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                  (user_id, plan_name, file_limit, storage_mb, datetime.now().isoformat(), expiry.isoformat(), source))
        grant_id = c.lastrowid
        c.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan_name, expiry, file_limit, storage_mb, grant_id) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, plan_name, expiry.isoformat(), file_limit, storage_mb, grant_id))
        conn.commit()
        conn.close()
        user_subscriptions[user_id] = {"plan_name": plan_name, "expiry": expiry,
                                        "file_limit": file_limit if file_limit is not None else SUBSCRIBED_USER_LIMIT,
                                        "storage_mb": storage_mb if storage_mb is not None else 0,
                                        "grant_id": grant_id}
    return grant_id


def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE package_grants SET status='revoked' WHERE user_id=? AND status='active'", (user_id,))
        c.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        user_subscriptions.pop(user_id, None)


def save_storage_subscription(user_id, plan_name, storage_mb, expiry, source="admin_grant"):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE subscription_grants SET status='superseded' WHERE user_id=? AND status='active'", (user_id,))
        c.execute("INSERT INTO subscription_grants (user_id, plan_name, storage_mb, granted_at, expiry, status, source) "
                  "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                  (user_id, plan_name, storage_mb, datetime.now().isoformat(), expiry.isoformat(), source))
        grant_id = c.lastrowid
        c.execute("INSERT OR REPLACE INTO storage_subscriptions (user_id, plan_name, storage_mb, expiry, grant_id) VALUES (?, ?, ?, ?, ?)",
                  (user_id, plan_name, storage_mb, expiry.isoformat(), grant_id))
        conn.commit()
        conn.close()
        user_storage_subs[user_id] = {"plan_name": plan_name, "storage_mb": storage_mb, "expiry": expiry, "grant_id": grant_id}
    return grant_id


def remove_storage_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE subscription_grants SET status='revoked' WHERE user_id=? AND status='active'", (user_id,))
        c.execute("DELETE FROM storage_subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        user_storage_subs.pop(user_id, None)


# ==========================================================================
# --- 🆕 Grant ID lookup / history helpers ---
# ==========================================================================
def get_package_grant_by_id(grant_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT grant_id, user_id, plan_name, file_limit, storage_mb, granted_at, expiry, status FROM package_grants WHERE grant_id=?", (grant_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_subscription_grant_by_id(grant_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT grant_id, user_id, plan_name, storage_mb, granted_at, expiry, status FROM subscription_grants WHERE grant_id=?", (grant_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_user_package_history(user_id, limit=10):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT grant_id, plan_name, expiry, status FROM package_grants WHERE user_id=? ORDER BY grant_id DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_subscription_history(user_id, limit=10):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT grant_id, plan_name, expiry, status FROM subscription_grants WHERE user_id=? ORDER BY grant_id DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_active_package_holders():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT grant_id, user_id, plan_name, expiry FROM package_grants WHERE status='active' ORDER BY grant_id DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_active_subscription_holders():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT grant_id, user_id, plan_name, expiry FROM subscription_grants WHERE status='active' ORDER BY grant_id DESC")
    rows = c.fetchall()
    conn.close()
    return rows


# ==========================================================================
# --- 🆕 Telegram username cache (for admin exports; N/A if never set) ---
# ==========================================================================
def update_user_info(user_id, username, first_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_info (user_id, username, first_name, last_seen) VALUES (?, ?, ?, ?)",
                  (user_id, username, first_name, datetime.now().isoformat()))
        conn.commit()
        conn.close()


def get_user_display(user_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT username, first_name FROM user_info WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return "N/A"
    username, first_name = row
    if username:
        return f"@{username}"
    return first_name or "N/A"


def add_admin_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
        admin_ids.add(user_id)


def remove_admin_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        admin_ids.discard(user_id)


def get_backup_access_db(user_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT enabled, expiry FROM backup_access WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    enabled, expiry = row
    return {"enabled": bool(enabled), "expiry": datetime.fromisoformat(expiry) if expiry else None}


def grant_backup_access_db(user_id, days=0):
    expiry = None if days == 0 else (datetime.now() + timedelta(days=days))
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO backup_access (user_id, enabled, expiry) VALUES (?, ?, ?)",
                  (user_id, 1, expiry.isoformat() if expiry else None))
        conn.commit()
        conn.close()


def revoke_backup_access_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM backup_access WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()


def list_backup_access_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT user_id, enabled, expiry FROM backup_access")
    rows = c.fetchall()
    conn.close()
    return rows


def get_last_backup_time_db(user_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT last_sent FROM backup_log WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0])
        except Exception:
            return None
    return None


def set_last_backup_time_db(user_id, dt):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO backup_log (user_id, last_sent) VALUES (?, ?)", (user_id, dt.isoformat()))
        conn.commit()
        conn.close()


def get_last_main_backup_time():
    val = get_setting("last_main_backup")
    return datetime.fromisoformat(val) if val else None


def set_last_main_backup_time(dt):
    set_setting("last_main_backup", dt.isoformat())


def has_backup_access(user_id):
    if user_id in admin_ids or user_id == OWNER_ID:
        return True
    if BACKUP_FORCE_STOPPED:  # 🆕 admin kill-switch: nobody but admin/owner gets backups while this is on
        return False
    if user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now():
        return True
    if user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        return True
    access = get_backup_access_db(user_id)
    if access and access["enabled"] and (access["expiry"] is None or access["expiry"] > datetime.now()):
        return True
    return False


# ==========================================================================
# --- Referral System ---
# ==========================================================================
def ensure_referral_row(user_id):
    if user_id not in user_referrals:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO referrals (user_id, referred_by, referral_count, reward_claimed) VALUES (?, NULL, 0, 0)", (user_id,))
            conn.commit()
            conn.close()
        user_referrals[user_id] = {"referred_by": None, "count": 0, "reward_claimed": False}
    return user_referrals[user_id]


def record_referral(new_user_id, referrer_id):
    if new_user_id == referrer_id:
        return
    info = ensure_referral_row(new_user_id)
    if info["referred_by"] is not None:
        return
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE referrals SET referred_by=? WHERE user_id=?", (referrer_id, new_user_id))
        conn.commit()
        conn.close()
    user_referrals[new_user_id]["referred_by"] = referrer_id

    ref_info = ensure_referral_row(referrer_id)
    new_count = ref_info["count"] + 1
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE referrals SET referral_count=? WHERE user_id=?", (new_count, referrer_id))
        conn.commit()
        conn.close()
    user_referrals[referrer_id]["count"] = new_count

    try:
        bot.send_message(referrer_id, f"🤝 *একজন নতুন ইউজার আপনার রেফারেল লিংক দিয়ে জয়েন করেছে!*\n👥 *মোট Referral:* `{new_count}/{REFERRAL_MILESTONE}`", parse_mode="Markdown")
    except Exception:
        pass

    if new_count >= REFERRAL_MILESTONE and not user_referrals[referrer_id]["reward_claimed"]:
        grant_free_hours(referrer_id, REFERRAL_REWARD_HOURS)
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("UPDATE referrals SET reward_claimed=1 WHERE user_id=?", (referrer_id,))
            conn.commit()
            conn.close()
        user_referrals[referrer_id]["reward_claimed"] = True
        try:
            bot.send_message(referrer_id, f"🎉 *অভিনন্দন! আপনি {REFERRAL_MILESTONE}টি রেফারেল সম্পূর্ণ করেছেন!*\n🎁 বোনাস হিসেবে `{REFERRAL_REWARD_HOURS}` Free Hours যোগ করা হয়েছে।", parse_mode="Markdown")
        except Exception:
            pass


def get_referral_count(user_id):
    return ensure_referral_row(user_id)["count"]


def get_referral_rank(user_id):
    my_count = get_referral_count(user_id)
    if my_count <= 0:
        return None
    ranked = sorted([(uid, info["count"]) for uid, info in user_referrals.items() if info["count"] > 0], key=lambda x: x[1], reverse=True)
    for idx, (uid, cnt) in enumerate(ranked, start=1):
        if uid == user_id:
            return idx
    return None


def get_referral_link(user_id):
    uname = BOT_USERNAME or "your_bot"
    return f"https://t.me/{uname}?start=ref_{user_id}"


# ==========================================================================
# --- Wallet (BDT) ---
# ==========================================================================
def get_balance(user_id):
    return user_wallets.get(user_id, 0.0)


def _save_balance_db(user_id, new_balance):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_wallet (user_id, balance) VALUES (?, ?)", (user_id, new_balance))
        conn.commit()
        conn.close()
    user_wallets[user_id] = new_balance


def add_balance(user_id, amount):
    new_balance = round(get_balance(user_id) + amount, 2)
    _save_balance_db(user_id, new_balance)
    return new_balance


def deduct_balance(user_id, amount):
    new_balance = round(max(0.0, get_balance(user_id) - amount), 2)
    _save_balance_db(user_id, new_balance)
    return new_balance


# ==========================================================================
# --- Free Trial Hours (supports per-user mode AND shared-pool mode) ---
# ==========================================================================
def _save_free_hours_db(user_id, remaining, last_checked):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO free_hours (user_id, hours_remaining, last_checked) VALUES (?, ?, ?)",
                  (user_id, remaining, last_checked.isoformat()))
        conn.commit()
        conn.close()
    user_free_hours[user_id] = {"remaining": remaining, "last_checked": last_checked}


def set_free_pool_hours(hours):
    global FREE_POOL_HOURS
    FREE_POOL_HOURS = round(max(0.0, hours), 2)
    set_setting("free_pool_hours", str(FREE_POOL_HOURS))


def set_free_pool_mode(enabled):
    global FREE_POOL_MODE, FREE_POOL_LAST_CHECK
    FREE_POOL_MODE = enabled
    FREE_POOL_LAST_CHECK = datetime.now()
    set_setting("free_pool_mode", "1" if enabled else "0")


def get_free_hours_remaining(user_id):
    if FREE_POOL_MODE:
        return FREE_POOL_HOURS
    info = user_free_hours.get(user_id)
    return info["remaining"] if info else 0.0


def grant_free_hours(user_id, hours):
    if FREE_POOL_MODE:
        set_free_pool_hours(FREE_POOL_HOURS + hours)
        return FREE_POOL_HOURS
    new_remaining = round(get_free_hours_remaining(user_id) + hours, 2)
    _save_free_hours_db(user_id, new_remaining, datetime.now())
    return new_remaining


def grant_free_hours_to_all(hours):
    """🆕 Grant `hours` free hours to every active (non-paid/non-admin) user individually."""
    count = 0
    for uid in list(active_users):
        if uid in admin_ids or uid == OWNER_ID:
            continue
        grant_free_hours(uid, hours)
        count += 1
    return count


def remove_free_hours(user_id, hours):
    if FREE_POOL_MODE:
        set_free_pool_hours(FREE_POOL_HOURS - hours)
        return FREE_POOL_HOURS
    new_remaining = round(max(0.0, get_free_hours_remaining(user_id) - hours), 2)
    _save_free_hours_db(user_id, new_remaining, datetime.now())
    return new_remaining


def init_free_trial_if_new(user_id):
    if FREE_POOL_MODE:
        return
    if user_id not in user_free_hours and user_id not in admin_ids and user_id != OWNER_ID:
        _save_free_hours_db(user_id, FREE_TRIAL_HOURS, datetime.now())


def _any_free_trial_bot_running():
    for uid, files in list(user_files.items()):
        if has_paid_plan(uid) or uid in admin_ids or uid == OWNER_ID:
            continue
        for file_name, file_type, display_name in files:
            if is_bot_running(uid, file_name):
                return True
    return False


def _stop_all_free_trial_bots(notify=True):
    for uid, files in list(user_files.items()):
        if has_paid_plan(uid) or uid in admin_ids or uid == OWNER_ID:
            continue
        for file_name, file_type, display_name in files:
            skey = f"{uid}_{file_name}"
            if skey in bot_scripts:
                kill_process_tree(bot_scripts[skey])
                del bot_scripts[skey]
        if notify:
            try:
                bot.send_message(uid, "⏰ *Free Trial সময়/পুল শেষ হয়ে গেছে! বট বন্ধ করে দেওয়া হয়েছে।*", parse_mode="Markdown")
            except Exception:
                pass


def free_hours_scheduler_loop():
    global FREE_POOL_LAST_CHECK
    while True:
        try:
            now = datetime.now()
            if FREE_POOL_MODE:
                if FREE_POOL_HOURS > 0 and _any_free_trial_bot_running():
                    elapsed_hours = (now - FREE_POOL_LAST_CHECK).total_seconds() / 3600.0
                    set_free_pool_hours(FREE_POOL_HOURS - elapsed_hours)
                    if FREE_POOL_HOURS <= 0:
                        _stop_all_free_trial_bots()
                FREE_POOL_LAST_CHECK = now
            else:
                for user_id in list(user_free_hours.keys()):
                    if has_paid_plan(user_id) or user_id in admin_ids or user_id == OWNER_ID:
                        continue
                    info = user_free_hours[user_id]
                    remaining = info["remaining"]
                    if remaining <= 0:
                        continue
                    running_any = any(is_bot_running(user_id, fn) for fn, ft, dn in user_files.get(user_id, []))
                    if running_any:
                        elapsed_hours = (now - info["last_checked"]).total_seconds() / 3600.0
                        new_remaining = round(max(0.0, remaining - elapsed_hours), 3)
                        _save_free_hours_db(user_id, new_remaining, now)
                        if new_remaining <= 0:
                            for file_name, file_type, display_name in user_files.get(user_id, []):
                                skey = f"{user_id}_{file_name}"
                                if skey in bot_scripts:
                                    kill_process_tree(bot_scripts[skey])
                                    del bot_scripts[skey]
                            try:
                                bot.send_message(user_id, "⏰ *আপনার Free Trial সময় শেষ হয়ে গেছে! বটটি বন্ধ করে দেওয়া হয়েছে।*", parse_mode="Markdown")
                            except Exception:
                                pass
                    else:
                        _save_free_hours_db(user_id, remaining, now)
        except Exception as e:
            logger.error(f"❌ Free hours scheduler error: {e}")
        time.sleep(FREE_HOURS_CHECK_INTERVAL)


# ==========================================================================
# --- Auto-Restart ---
# ==========================================================================
def is_auto_restart_enabled(user_id):
    return user_auto_restart.get(user_id, True)


def set_auto_restart_db(user_id, enabled):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO auto_restart_settings (user_id, enabled) VALUES (?, ?)", (user_id, 1 if enabled else 0))
        conn.commit()
        conn.close()
    user_auto_restart[user_id] = enabled


def can_deploy_now(user_id):
    if user_id in admin_ids or user_id == OWNER_ID:
        return True
    if has_paid_plan(user_id):
        return True
    if get_free_hours_remaining(user_id) > 0:
        return True
    return False


def auto_restart_scheduler_loop():
    while True:
        try:
            for uid, files in list(user_files.items()):
                if not is_auto_restart_enabled(uid) or not can_deploy_now(uid):
                    continue
                ufolder = get_user_folder(uid)
                for file_name, file_type, display_name in files:
                    script_key = f"{uid}_{file_name}"
                    if script_key in manually_stopped_scripts or is_bot_running(uid, file_name):
                        continue
                    fpath = os.path.join(ufolder, file_name)
                    if not os.path.exists(fpath):
                        continue
                    try:
                        if file_type == "js":
                            threading.Thread(target=_silent_run_js, args=(fpath, uid, ufolder, file_name)).start()
                        else:
                            threading.Thread(target=_silent_run_py, args=(fpath, uid, ufolder, file_name)).start()
                    except Exception as e:
                        logger.error(f"❌ Auto-restart error for {script_key}: {e}")
        except Exception as e:
            logger.error(f"❌ Auto-restart scheduler error: {e}")
        time.sleep(AUTO_RESTART_CHECK_INTERVAL)


def _silent_run_py(script_path, script_owner_id, user_folder, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, "w", encoding="utf-8", errors="ignore")
        process = subprocess.Popen([sys.executable, script_path], cwd=user_folder, stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {"process": process, "log_file": log_file, "file_name": file_name,
                                    "script_owner_id": script_owner_id, "start_time": datetime.now(),
                                    "user_folder": user_folder, "type": "py", "script_key": script_key}
        try:
            bot.send_message(script_owner_id, f"🔄 *Auto-Restart:* `{esc_md(file_name)}` বটটি ক্র্যাশ করেছিল, অটোমেটিক আবার চালু করা হয়েছে।", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"❌ Silent auto-restart (py) failed: {e}")


def _silent_run_js(script_path, script_owner_id, user_folder, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, "w", encoding="utf-8", errors="ignore")
        process = subprocess.Popen(["node", script_path], cwd=user_folder, stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {"process": process, "log_file": log_file, "file_name": file_name,
                                    "script_owner_id": script_owner_id, "start_time": datetime.now(),
                                    "user_folder": user_folder, "type": "js", "script_key": script_key}
        try:
            bot.send_message(script_owner_id, f"🔄 *Auto-Restart:* `{esc_md(file_name)}` বটটি ক্র্যাশ করেছিল, অটোমেটিক আবার চালু করা হয়েছে।", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"❌ Silent auto-restart (js) failed: {e}")


# ==========================================================================
# --- Auto-Backup Engine ---
# ==========================================================================
def send_user_backup(user_id):
    try:
        files_list = user_files.get(user_id, [])
        if not files_list:
            return
        ufolder = get_user_folder(user_id)
        if not os.path.isdir(ufolder) or not os.listdir(ufolder):
            return
        titles = [dn if dn else fn for fn, ft, dn in files_list]
        title_text = ", ".join(titles[:5]) + (" ও আরও কিছু" if len(titles) > 5 else "")
        zip_path = os.path.join(tempfile.gettempdir(), f"backup_{user_id}_{int(time.time())}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(ufolder):
                for fname in files:
                    zf.write(os.path.join(root, fname), arcname=fname)
        with open(zip_path, "rb") as f:
            bot.send_document(user_id, f,
                               caption=(f"💾 *আপনার `{esc_md(title_text)}` এর All Backup File*\n"
                                        f"🕐 *Backup Time:* `{datetime.now().strftime('%Y-%m-%d %H:%M')}`\n"
                                        f"♻️ প্রতি ১২ ঘন্টা পর পর অটোমেটিক ব্যাকআপ পাঠানো হয়।"),
                               parse_mode="Markdown", visible_file_name=f"backup_{user_id}.zip")
        os.remove(zip_path)
        set_last_backup_time_db(user_id, datetime.now())
        logger.info(f"✅ Backup sent to user {user_id}")
    except Exception as e:
        logger.error(f"❌ Backup send error for user {user_id}: {e}")


def send_owner_main_backup():
    try:
        zip_path = os.path.join(tempfile.gettempdir(), f"main_bot_backup_{int(time.time())}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            script_file = os.path.abspath(sys.argv[0]) if sys.argv and os.path.exists(sys.argv[0]) else __file__
            if os.path.exists(script_file):
                zf.write(script_file, arcname=os.path.basename(script_file))
            for root, dirs, files in os.walk(IROTECH_DIR):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    zf.write(fpath, arcname=os.path.relpath(fpath, BASE_DIR))
        with open(zip_path, "rb") as f:
            bot.send_document(OWNER_ID, f,
                               caption=(f"🛡️ *Main Bot Full Backup (Owner Only)*\n"
                                        f"🕐 *Backup Time:* `{datetime.now().strftime('%Y-%m-%d %H:%M')}`\n"
                                        f"📦 Includes: Bot Source Code + Database"),
                               parse_mode="Markdown", visible_file_name=f"main_bot_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.zip")
        os.remove(zip_path)
        set_last_main_backup_time(datetime.now())
        logger.info("✅ Main bot backup sent to owner")
    except Exception as e:
        logger.error(f"❌ Owner main backup error: {e}")


def backup_scheduler_loop():
    while True:
        try:
            if AUTO_BACKUP_ENABLED:
                now = datetime.now()
                for user_id in list(user_files.keys()):
                    if not has_backup_access(user_id):
                        continue
                    last = get_last_backup_time_db(user_id)
                    if last is None or (now - last).total_seconds() >= BACKUP_INTERVAL_SECONDS:
                        send_user_backup(user_id)
                last_main = get_last_main_backup_time()
                if last_main is None or (now - last_main).total_seconds() >= BACKUP_INTERVAL_SECONDS:
                    send_owner_main_backup()
        except Exception as e:
            logger.error(f"❌ Backup scheduler error: {e}")
        time.sleep(BACKUP_CHECK_INTERVAL_SECONDS)


# ==========================================================================
# --- 🆕 Deposit System DB helpers ---
# ==========================================================================
def add_deposit_method_db(name, number, note, currency="BDT"):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO deposit_methods (name, number, note, currency) VALUES (?, ?, ?, ?)", (name, number, note, currency))
        mid = c.lastrowid
        conn.commit()
        conn.close()
    deposit_methods[mid] = {"name": name, "number": number, "note": note, "currency": currency}
    return mid


def delete_deposit_method_db(method_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM deposit_methods WHERE method_id=?", (method_id,))
        conn.commit()
        conn.close()
    deposit_methods.pop(method_id, None)


def create_deposit_request_db(user_id, method_name, sender_number, amount, tx_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""INSERT INTO deposit_requests (user_id, method_name, sender_number, amount, tx_id, status, created_at)
                     VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                  (user_id, method_name, sender_number, amount, tx_id, datetime.now().isoformat()))
        rid = c.lastrowid
        conn.commit()
        conn.close()
    return rid


def set_deposit_group_message_id(request_id, msg_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE deposit_requests SET group_message_id=? WHERE request_id=?", (msg_id, request_id))
        conn.commit()
        conn.close()


def get_deposit_request(request_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT request_id, user_id, method_name, sender_number, amount, tx_id, status FROM deposit_requests WHERE request_id=?", (request_id,))
    row = c.fetchone()
    conn.close()
    return row


def set_deposit_status(request_id, status):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE deposit_requests SET status=? WHERE request_id=?", (status, request_id))
        conn.commit()
        conn.close()


def count_deposit_requests():
    """🆕 Returns (pending, approved, rejected) counts for the Wallet Control panel."""
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for status in counts:
        c.execute("SELECT COUNT(*) FROM deposit_requests WHERE status=?", (status,))
        counts[status] = c.fetchone()[0]
    conn.close()
    return counts


def list_pending_deposit_requests(limit=15):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT request_id, user_id, method_name, amount, tx_id FROM deposit_requests WHERE status='pending' ORDER BY request_id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


# ==========================================================================
# --- 🆕 Purchase Log (for admin revenue/count stats) ---
# ==========================================================================
def log_purchase(user_id, plan_type, plan_name, amount_usdt, method):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT INTO purchase_log (user_id, plan_type, plan_name, amount_usdt, method, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, plan_type, plan_name, amount_usdt, method, datetime.now().isoformat()))
        conn.commit()
        conn.close()


def get_purchase_stats():
    """🆕 Returns dict with total pkg/sub counts and total USDT earned."""
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(amount_usdt), 0) FROM purchase_log WHERE plan_type='pkg'")
    pkg_count, pkg_total = c.fetchone()
    c.execute("SELECT COUNT(*), COALESCE(SUM(amount_usdt), 0) FROM purchase_log WHERE plan_type='sub'")
    sub_count, sub_total = c.fetchone()
    conn.close()
    return {"pkg_count": pkg_count, "pkg_total_usdt": round(pkg_total, 2),
            "sub_count": sub_count, "sub_total_usdt": round(sub_total, 2)}


# ==========================================================================
# --- Core User Logic ---
# ==========================================================================
def _logic_send_welcome(message, referrer_id=None):
    user_id = message.from_user.id
    chat_id = message.chat.id
    update_user_info(user_id, message.from_user.username, message.from_user.first_name)  # 🆕 keep username fresh

    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, tr(user_id, "locked"), parse_mode="Markdown")
        return

    is_new_user = user_id not in active_users
    if is_new_user:
        add_active_user(user_id)

    ensure_referral_row(user_id)
    init_free_trial_if_new(user_id)

    if referrer_id and is_new_user and referrer_id != user_id:
        record_referral(user_id, referrer_id)

    if user_id == OWNER_ID:
        user_status = tr(user_id, "status_owner")
    elif user_id in admin_ids:
        user_status = tr(user_id, "status_admin")
    elif user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now():
        sub = user_storage_subs[user_id]
        days_left = (sub["expiry"] - datetime.now()).days
        user_status = tr(user_id, "status_sub", plan=esc_md(sub["plan_name"]), days=days_left)
    elif user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        sub = user_subscriptions[user_id]
        days_left = (sub["expiry"] - datetime.now()).days
        user_status = tr(user_id, "status_pkg", plan=esc_md(sub.get("plan_name", "Premium")), days=days_left)
    elif get_free_hours_remaining(user_id) > 0:
        user_status = tr(user_id, "status_trial", hrs=f"{get_free_hours_remaining(user_id):.1f}")
    else:
        user_status = tr(user_id, "status_none")

    limit_text = format_file_limit(get_user_file_limit(user_id))
    welcome_msg = tr(user_id, "welcome", name=esc_md(message.from_user.first_name), uid=user_id,
                      status=user_status, count=get_user_file_count(user_id), limit=limit_text)
    bot.send_message(chat_id, welcome_msg, reply_markup=create_reply_keyboard_main_menu(user_id), parse_mode="Markdown")


def _logic_view_plans(message_or_call):
    chat_id = message_or_call.chat.id if isinstance(message_or_call, telebot.types.Message) else message_or_call.message.chat.id
    user_id = message_or_call.from_user.id if isinstance(message_or_call, telebot.types.Message) else message_or_call.from_user.id
    plans = get_all_plans()
    if not plans:
        bot.send_message(chat_id, tr(user_id, "no_pkg_plans"), parse_mode="Markdown")
        return
    bot.send_message(chat_id, tr(user_id, "avail_pkg_plans"), parse_mode="Markdown")
    for plan in plans:
        plan_id, name, limit, storage_mb, price, duration, _ = plan
        usdt_price, formatted_price = parse_price_to_usdt(price)
        card_text = (f"📦 *𝗣𝗹𝗮𝗻:* `{esc_md(name)}`\n━━━━━━━━━━━━━━━━━━━\n"
                     f"🤖 *Bot Deploy Limit:* `{limit} Files`\n💾 *Storage:* `{storage_mb or 0} MB`\n"
                     f"⏱️ *Duration:* `{duration} Days`\n💰 *Price:* `{esc_md(formatted_price)}`\n"
                     f"👉 *Binance/Wallet-এ পেমেন্ট করতে হবে:* `{usdt_price} USDT`\n━━━━━━━━━━━━━━━━━━━")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(user_id, "btn_buy", name=name, price=usdt_price), callback_data=f"buy_pkg_{plan_id}"))
        bot.send_message(chat_id, card_text, reply_markup=markup, parse_mode="Markdown")


def _logic_view_subscription_plans(message_or_call):
    chat_id = message_or_call.chat.id if isinstance(message_or_call, telebot.types.Message) else message_or_call.message.chat.id
    user_id = message_or_call.from_user.id if isinstance(message_or_call, telebot.types.Message) else message_or_call.from_user.id
    plans = get_all_subscription_plans()
    if not plans:
        bot.send_message(chat_id, tr(user_id, "no_sub_plans"), parse_mode="Markdown")
        return
    bot.send_message(chat_id, tr(user_id, "avail_sub_plans"), parse_mode="Markdown")
    for plan in plans:
        sub_plan_id, name, storage_mb, price, duration = plan
        usdt_price, formatted_price = parse_price_to_usdt(price)
        icon = plan_icon_for_duration(duration)
        card_text = (f"{icon} *𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻:* `{esc_md(name)}`\n━━━━━━━━━━━━━━━━━━━\n"
                     f"🤖 *Bot Deploy:* `Unlimited`\n💾 *Storage:* `{storage_mb} MB`\n"
                     f"⏱️ *Duration:* `{duration} Days`\n♻️ *Auto-Backup:* `Included`\n"
                     f"💰 *Price:* `{esc_md(formatted_price)}`\n👉 *Binance/Wallet-এ পেমেন্ট করতে হবে:* `{usdt_price} USDT`\n━━━━━━━━━━━━━━━━━━━")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(user_id, "btn_buy", name=name, price=usdt_price), callback_data=f"buy_sub_{sub_plan_id}"))
        bot.send_message(chat_id, card_text, reply_markup=markup, parse_mode="Markdown")


def _initiate_binance_purchase(chat_id, user_id, plan_type, plan_id):
    if plan_type == "pkg":
        plan = get_plan_by_id(plan_id)
        if not plan:
            bot.send_message(chat_id, "❌ প্যাকেজটি খুঁজে পাওয়া যায়নি বা মুছে ফেলা হয়েছে!")
            return
        _, name, file_limit, storage_mb, price, duration, _ = plan
        label = "Package"
    else:
        plan = get_subscription_plan_by_id(plan_id)
        if not plan:
            bot.send_message(chat_id, "❌ সাবস্ক্রিপশন প্ল্যানটি খুঁজে পাওয়া যায়নি বা মুছে ফেলা হয়েছে!")
            return
        _, name, storage_mb, price, duration = plan
        file_limit = None
        label = "Subscription"

    usdt_price, formatted_price = parse_price_to_usdt(price)
    already_paid = get_pending_payment(user_id, plan_type, plan_id)
    due_amount = max(0.0, round(usdt_price - already_paid, 2))

    pay_msg = (f"💛 *Payment Options*\n\n📌 *Selected {label}:* `{esc_md(name)}`\n"
               f"💰 *Total Price:* `{usdt_price} USDT` ({esc_md(formatted_price)})\n")
    if already_paid > 0:
        pay_msg += f"✅ *পূর্বে জমা:* `{already_paid} USDT`\n⚠️ *এখন বাকি:* `{due_amount} USDT`\n\n"
    else:
        pay_msg += f"⏱️ *Duration:* `{duration} Days`\n\n"

    balance = get_balance(user_id)
    wallet_usdt = round(balance / USDT_BDT_RATE, 2) if USDT_BDT_RATE else 0
    pay_msg += (f"👇 *Binance Pay দিয়ে পেমেন্ট করতে:*\n"
                f"1️⃣ Binance App ➔ *Pay* ➔ *Send*।\n"
                f"2️⃣ ঠিক *`{due_amount} USDT`* এই Binance Pay ID-তে পাঠান:\n🔸 *Pay ID:* `{BINANCE_PAY_ID}`\n\n"
                f"3️⃣ Order ID / TxID জমা দিতে নিচের বাটন চাপুন।\n\n"
                f"💰 *আপনার Wallet Balance:* `{balance} BDT` (~{wallet_usdt} USDT)")

    wallet_sufficient = wallet_usdt >= due_amount and due_amount > 0
    if wallet_sufficient:
        pay_msg += "\n\n" + tr(user_id, "both_payment_options")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔍 Order ID / TxID জমা দিন", callback_data=f"submit_txid_{plan_type}_{plan_id}"))
    if wallet_sufficient:
        markup.add(types.InlineKeyboardButton(tr(user_id, "wallet_pay_btn", amt=due_amount), callback_data=f"walletpay_{plan_type}_{plan_id}"))
    bot.send_message(chat_id, pay_msg, reply_markup=markup, parse_mode="Markdown")


def _activate_plan(user_id, plan_type, plan_id, plan_row, source="admin_grant"):
    """Shared activation logic used by both Binance TxID success & Wallet-pay success.
    Returns (name, expiry, grant_id) — grant_id is the new auto-numbered package/subscription ID."""
    if plan_type == "pkg":
        _, name, file_limit, storage_mb, price, duration, _ = plan_row
        expiry = datetime.now() + timedelta(days=duration)
        grant_id = save_subscription(user_id, name, expiry, file_limit, storage_mb, source=source)
    else:
        _, name, storage_mb, price, duration = plan_row
        expiry = datetime.now() + timedelta(days=duration)
        grant_id = save_storage_subscription(user_id, name, storage_mb, expiry, source=source)
    return name, expiry, grant_id


def _logic_upload_file(message):
    user_id = message.from_user.id
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, tr(user_id, "locked"), parse_mode="Markdown")
        return

    has_active_plan = False
    plan_name = "None"

    if user_id in admin_ids or user_id == OWNER_ID:
        has_active_plan = True
        plan_name = "Admin / Owner Unlimited"
    elif user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now():
        has_active_plan = True
        plan_name = user_storage_subs[user_id]["plan_name"] + " (Subscription)"
    elif user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        has_active_plan = True
        plan_name = user_subscriptions[user_id].get("plan_name", "Package Plan")
    elif get_free_hours_remaining(user_id) > 0:
        has_active_plan = True
        plan_name = f"Free Trial ({get_free_hours_remaining(user_id):.1f}h left)"

    if not has_active_plan:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(tr(user_id, "btn_view_plans"), callback_data="view_plans_cb"))
        markup.add(types.InlineKeyboardButton(tr(user_id, "btn_view_subplans"), callback_data="view_subplans_cb"))
        bot.reply_to(message, tr(user_id, "no_plan_upload"), reply_markup=markup, parse_mode="Markdown")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(tr(user_id, "btn_continue_plan", plan=plan_name), callback_data="confirm_plan_upload"))
    bot.reply_to(message, tr(user_id, "active_plan_detected", plan=esc_md(plan_name)), reply_markup=markup, parse_mode="Markdown")


def _logic_check_files(message):
    user_id = message.from_user.id
    user_files_list = user_files.get(user_id, [])

    limit_mb = get_user_storage_limit_mb(user_id)
    usage_mb = get_user_storage_usage_mb(user_id)
    limit_disp = "Unlimited" if limit_mb is None else f"{limit_mb} MB"
    storage_line = tr(user_id, "storage_used", used=usage_mb, limit=limit_disp)

    if not user_files_list:
        bot.reply_to(message, f"{storage_line}\n\n{tr(user_id, 'manage_files_title')}\n\n{tr(user_id, 'no_files')}", parse_mode="Markdown")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for file_name, file_type, display_name in sorted(user_files_list, key=lambda x: x[0]):
        is_running = is_bot_running(user_id, file_name)
        status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        label = display_name if display_name else file_name
        btn_text = f"🤖 {label} ({file_type}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"file_{user_id}_{file_name}"))

    bot.reply_to(message, f"{storage_line}\n\n{tr(user_id, 'manage_files_title')}", reply_markup=markup, parse_mode="Markdown")


def _logic_my_profile(message):
    user_id = message.from_user.id
    lines = [tr(user_id, "profile_title"), "━━━━━━━━━━━━━━━━━━━", f"🆔 *User ID:* `{user_id}`"]

    if user_id == OWNER_ID:
        lines.append(f"🔰 *Status:* {tr(user_id, 'status_owner')}")
    elif user_id in admin_ids:
        lines.append(f"🔰 *Status:* {tr(user_id, 'status_admin')}")
    else:
        pkg_status = "🆓 None"
        if user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
            sub = user_subscriptions[user_id]
            days_left = (sub["expiry"] - datetime.now()).days
            gid = sub.get("grant_id")
            pkg_status = f"💎 {esc_md(sub.get('plan_name', 'Package'))} ({days_left}d left) — 🆔 `#{gid}`" if gid else f"💎 {esc_md(sub.get('plan_name', 'Package'))} ({days_left}d left)"
        lines.append(f"📦 *Package:* {pkg_status}")

        sub2_status = "🆓 None"
        storsub = user_storage_subs.get(user_id)
        if storsub and storsub["expiry"] > datetime.now():
            days_left = (storsub["expiry"] - datetime.now()).days
            gid2 = storsub.get("grant_id")
            sub2_status = f"🎫 {esc_md(storsub['plan_name'])} ({days_left}d left) — 🆔 `#{gid2}`" if gid2 else f"🎫 {esc_md(storsub['plan_name'])} ({days_left}d left)"
        lines.append(f"🎫 *Subscription:* {sub2_status}")

    limit_text = format_file_limit(get_user_file_limit(user_id))
    lines.append(f"🤖 *Bots Deployed:* `{get_user_file_count(user_id)}` / `{limit_text}`")

    storage_limit = get_user_storage_limit_mb(user_id)
    usage = get_user_storage_usage_mb(user_id)
    if storage_limit is None:
        lines.append(f"💾 *Storage:* `{usage:.2f} MB` / `Unlimited`")
    else:
        pct = (usage / storage_limit * 100) if storage_limit > 0 else 0
        lines.append(f"💾 *Storage:* `{usage:.2f} MB` / `{storage_limit} MB` ({pct:.0f}%)")

    lines.append(f"♻️ *Auto-Backup:* {'✅ Enabled' if has_backup_access(user_id) else '❌ Disabled'}")

    ref_count = get_referral_count(user_id)
    rank = get_referral_rank(user_id)
    lines.append(f"🤝 *Referrals:* `{ref_count}/{REFERRAL_MILESTONE}`")
    lines.append(f"🏆 *Rank:* `{f'#{rank}' if rank else 'N/A'}`")
    lines.append(f"⏳ *Free Hours:* `{get_free_hours_remaining(user_id):.1f}h`" + (" (Shared Pool)" if FREE_POOL_MODE else ""))
    lines.append(f"💰 *Balance:* `{get_balance(user_id):.2f} BDT`")
    lines.append(f"🔄 *Auto-Restart:* {'✅ ON' if is_auto_restart_enabled(user_id) else '❌ OFF'}")
    lines.append(f"🌐 *Language:* `{get_lang(user_id).upper()}`")
    lines.append("━━━━━━━━━━━━━━━━━━━")

    if user_id != OWNER_ID and user_id not in admin_ids:
        pkg_hist = get_user_package_history(user_id, limit=8)
        sub_hist = get_user_subscription_history(user_id, limit=8)
        if pkg_hist:
            lines.append("📦 *𝗣𝗮𝗰𝗸𝗮𝗴𝗲 𝗜𝗗 𝗛𝗶𝘀𝘁𝗼𝗿𝘆:*")
            status_icon = {"active": "🟢", "revoked": "🔴", "superseded": "⚪", "expired": "⚫"}
            for gid, pname, exp, status in pkg_hist:
                lines.append(f"  {status_icon.get(status, '•')} `#{gid}` {esc_md(pname)} — `{status}`")
        if sub_hist:
            lines.append("🎫 *𝗦𝘂𝗯𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻 𝗜𝗗 𝗛𝗶𝘀𝘁𝗼𝗿𝘆:*")
            status_icon = {"active": "🟢", "revoked": "🔴", "superseded": "⚪", "expired": "⚫"}
            for gid, pname, exp, status in sub_hist:
                lines.append(f"  {status_icon.get(status, '•')} `#{gid}` {esc_md(pname)} — `{status}`")
        if pkg_hist or sub_hist:
            lines.append("━━━━━━━━━━━━━━━━━━━")

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton(tr(user_id, "btn_toggle_autorestart"), callback_data="toggle_autorestart"),
               types.InlineKeyboardButton(tr(user_id, "btn_referral_link"), callback_data="get_referral_link"))
    bot.reply_to(message, "\n".join(lines), reply_markup=markup, parse_mode="Markdown")


def _logic_referral_menu(message):
    user_id = message.from_user.id
    ensure_referral_row(user_id)
    ref_count = get_referral_count(user_id)
    rank = get_referral_rank(user_id)
    rank_text = f"#{rank}" if rank else "N/A"
    link = get_referral_link(user_id)
    progress = min(ref_count, REFERRAL_MILESTONE)
    msg = tr(user_id, "referral_title", progress=progress, milestone=REFERRAL_MILESTONE, rank=rank_text,
             reward=REFERRAL_REWARD_HOURS, link=link)
    bot.reply_to(message, msg, parse_mode="Markdown")


def _logic_language_menu(message):
    user_id = message.from_user.id
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🇧🇩 বাংলা", callback_data="setlang_bn"),
               types.InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en"))
    bot.reply_to(message, tr(user_id, "choose_lang"), reply_markup=markup, parse_mode="Markdown")


# ==========================================================================
# --- 🆕 Deposit Flow (user-facing) ---
# ==========================================================================
def _logic_deposit_menu(message):
    user_id = message.from_user.id
    if not deposit_methods:
        bot.reply_to(message, tr(user_id, "deposit_no_methods"), parse_mode="Markdown")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for mid, info in deposit_methods.items():
        markup.add(types.InlineKeyboardButton(f"🏦 {info['name']} ({info['number']})", callback_data=f"depmethod_{mid}"))
    bot.reply_to(message, tr(user_id, "deposit_choose_method"), reply_markup=markup, parse_mode="Markdown")


def _deposit_ask_sender(call_message, user_id, method_id):
    info = deposit_methods.get(method_id)
    if not info:
        bot.send_message(call_message.chat.id, "❌ Method not found.")
        return
    deposit_flow_temp[user_id] = {"method_name": info["name"], "currency": info.get("currency", "BDT")}
    note_line = f"\nℹ️ _{esc_md(info['note'])}_" if info.get("note") else ""
    step_prompt(call_message.chat.id, user_id,
                f"🏦 *{esc_md(info['name'])}*\n🔢 Number: `{info['number']}`{note_line}\n\n" + tr(user_id, "deposit_ask_sender"),
                _deposit_process_sender)


def _deposit_process_sender(message):
    user_id = message.from_user.id
    deposit_flow_temp.setdefault(user_id, {})["sender_number"] = message.text.strip()
    currency = deposit_flow_temp[user_id].get("currency", "BDT")
    step_prompt(message.chat.id, user_id, tr(user_id, "deposit_ask_amount", currency=currency), _deposit_process_amount)


def _deposit_process_amount(message):
    user_id = message.from_user.id
    currency = deposit_flow_temp.get(user_id, {}).get("currency", "BDT")
    try:
        amount = float(message.text.strip())
    except ValueError:
        step_prompt(message.chat.id, user_id, "⚠️ সঠিক সংখ্যা লিখুন / Enter a valid number:\n\n" + tr(user_id, "deposit_ask_amount", currency=currency), _deposit_process_amount)
        return
    deposit_flow_temp.setdefault(user_id, {})["amount"] = amount
    step_prompt(message.chat.id, user_id, tr(user_id, "deposit_ask_txid"), _deposit_process_txid)


def _deposit_process_txid(message):
    user_id = message.from_user.id
    data = deposit_flow_temp.setdefault(user_id, {})
    data["tx_id"] = message.text.strip()

    summary = tr(user_id, "deposit_confirm", method=data.get("method_name"), sender=data.get("sender_number"),
                 amount=data.get("amount"), currency=data.get("currency", "BDT"), txid=data.get("tx_id"))
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(tr(user_id, "btn_confirm"), callback_data="deposit_final_confirm"))
    bot.send_message(message.chat.id, summary, reply_markup=create_reply_keyboard_main_menu(user_id), parse_mode="Markdown")
    bot.send_message(message.chat.id, "👆", reply_markup=markup)


def _deposit_finalize(call):
    user_id = call.from_user.id
    data = deposit_flow_temp.pop(user_id, None)
    if not data:
        bot.answer_callback_query(call.id, "Session expired, please start again.", show_alert=True)
        return
    rid = create_deposit_request_db(user_id, data["method_name"], data["sender_number"], data["amount"], data["tx_id"])
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, tr(user_id, "deposit_submitted"), parse_mode="Markdown")

    if DEPOSIT_GROUP_ID:
        group_text = (f"🆕 *New Deposit Request* `#{rid}`\n━━━━━━━━━━━━━━━━━━━\n"
                       f"👤 User: `{user_id}`\n🏦 Method: `{esc_md(data['method_name'])}`\n"
                       f"📱 Sender: `{esc_md(data['sender_number'])}`\n💰 Amount: `{data['amount']} {data.get('currency', 'BDT')}`\n"
                       f"🧾 TxID: `{esc_md(data['tx_id'])}`\n━━━━━━━━━━━━━━━━━━━")
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("✅ Approve", callback_data=f"dep_appr_{rid}"),
                   types.InlineKeyboardButton("❌ Reject", callback_data=f"dep_rej_{rid}"))
        try:
            sent = bot.send_message(DEPOSIT_GROUP_ID, group_text, reply_markup=markup, parse_mode="Markdown")
            set_deposit_group_message_id(rid, sent.message_id)
        except Exception as e:
            logger.error(f"❌ Could not post deposit request to group: {e}")
    else:
        try:
            bot.send_message(OWNER_ID, f"⚠️ Deposit Group ID সেট করা নেই! Request #{rid} ম্যানুয়ালি চেক করুন (Settings থেকে Group ID সেট করুন)।")
        except Exception:
            pass


def _handle_deposit_decision(call, approve):
    admin_uid = call.from_user.id
    if admin_uid not in admin_ids:
        bot.answer_callback_query(call.id, "❌ You are not authorized.", show_alert=True)
        return
    rid = int(call.data.split("_")[2])
    row = get_deposit_request(rid)
    if not row:
        bot.answer_callback_query(call.id, "Request not found.", show_alert=True)
        return
    request_id, user_id, method_name, sender_number, amount, tx_id, status = row
    if status != "pending":
        bot.answer_callback_query(call.id, "Already processed.", show_alert=True)
        return

    if approve:
        new_bal = add_balance(user_id, amount)
        set_deposit_status(rid, "approved")
        result_line = f"✅ *Approved by {esc_md(call.from_user.first_name)}*"
        try:
            bot.send_message(user_id, tr(user_id, "deposit_approved_user", amount=amount, bal=new_bal), parse_mode="Markdown")
        except Exception:
            pass
    else:
        set_deposit_status(rid, "rejected")
        result_line = f"❌ *Rejected by {esc_md(call.from_user.first_name)}*"
        try:
            bot.send_message(user_id, tr(user_id, "deposit_rejected_user", amount=amount), parse_mode="Markdown")
        except Exception:
            pass

    try:
        base_text = (f"🆕 *Deposit Request* `#{rid}`\n━━━━━━━━━━━━━━━━━━━\n"
                     f"👤 User: `{user_id}`\n🏦 Method: `{esc_md(method_name)}`\n"
                     f"📱 Sender: `{esc_md(sender_number)}`\n💰 Amount: `{amount} BDT`\n"
                     f"🧾 TxID: `{esc_md(tx_id)}`\n━━━━━━━━━━━━━━━━━━━\n{result_line}")
        bot.edit_message_text(base_text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception as e:
        logger.error(f"❌ Could not edit deposit group message: {e}")
    bot.answer_callback_query(call.id, "Done!")


# ==========================================================================
# --- Admin Panel Logic ---
# ==========================================================================
def _logic_open_admin_panel(message):
    uid = message.from_user.id
    if uid not in admin_ids:
        bot.reply_to(message, tr(uid, "admin_only"), parse_mode="Markdown")
        return
    bot.send_message(message.chat.id, tr(uid, "admin_panel_title"), reply_markup=_kb_from_keys(uid, ADMIN_PANEL_KEYS), parse_mode="Markdown")


def _logic_open_package_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, "📦 *Package Plans Panel*\n_(Bot Deploy Limit + Storage + Time Limit)_",
                      reply_markup=_kb_from_keys(uid, PACKAGE_PANEL_KEYS), parse_mode="Markdown")


def _logic_open_subscription_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, "🎫 *Subscription Plans Panel*\n_(Unlimited Bot Deploy — Storage & Time Limit, Auto-Backup included)_",
                      reply_markup=_kb_from_keys(uid, SUBSCRIPTION_PANEL_KEYS), parse_mode="Markdown")


def _logic_open_admin_mgmt_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, "👑 *Admin Management*", reply_markup=_kb_from_keys(uid, ADMIN_MGMT_KEYS), parse_mode="Markdown")


def _logic_open_settings_panel(message):
    uid = message.from_user.id
    text = tr(uid, "settings_title", locked=bot_locked, owner=YOUR_USERNAME, channel=UPDATE_CHANNEL,
              rate=USDT_BDT_RATE, group=(DEPOSIT_GROUP_ID or "Not set"))
    bot.send_message(message.chat.id, text, reply_markup=_kb_from_keys(uid, SETTINGS_PANEL_KEYS), parse_mode="Markdown")


def _logic_open_api_settings_sub(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, f"🔑 *API Settings*\n\nবর্তমান Binance Pay ID: `{BINANCE_PAY_ID}`\n⚠️ নিরাপত্তার কারণে Key/Secret দেখানো হয় না।",
                      reply_markup=_kb_from_keys(uid, API_SETTINGS_KEYS), parse_mode="Markdown")


def _logic_back_to_admin_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, "🔙 " + tr(uid, "admin_panel_title"), reply_markup=_kb_from_keys(uid, ADMIN_PANEL_KEYS), parse_mode="Markdown")


def _logic_back_to_settings(message):
    _logic_open_settings_panel(message)


def _logic_back_to_main_menu(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, "🔙", reply_markup=create_reply_keyboard_main_menu(uid))


# --- Package Plan admin actions ---
def _logic_add_plan_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid,
                "📝 *Enter Package Plan Details (space separated):*\n`Name FileLimit StorageMB Price DurationDays BuyLink`\n\n"
                "_Example:_ `Basic 5 200 500BDT 30 https://t.me/shiyam744`\n\n"
                "ℹ️ Name-এ স্পেস থাকতে পারে (যেমন `VIP Gold`), কিন্তু Price-এ স্পেস দেওয়া যাবে না (যেমন `500BDT`, `5USDT`)।",
                process_add_plan)


def process_add_plan(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        buy_link, duration, price, storage_mb, limit = parts[-1], int(parts[-2]), parts[-3], int(parts[-4]), int(parts[-5])
        name = " ".join(parts[:-5])
        if not name:
            raise ValueError("Plan Name missing")
        add_plan_db(name, limit, storage_mb, price, duration, buy_link)
        bot.reply_to(message, f"✅ *Package Plan `{esc_md(name)}` added successfully!*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_manage_plans(message):
    plans = get_all_plans()
    if not plans:
        bot.send_message(message.chat.id, "ℹ️ কোনো Package Plan পাওয়া যায়নি।")
        return
    markup = types.InlineKeyboardMarkup()
    for p in plans:
        markup.add(types.InlineKeyboardButton(f"🗑️ Delete {p[1]}", callback_data=f"del_plan_{p[0]}"))
    bot.send_message(message.chat.id, "🗑️ *Select a Package Plan to Delete:*", reply_markup=markup, parse_mode="Markdown")


def _export_holders_file(message, rows, title, filename):
    """🆕 Shared helper: builds a CSV of (GrantID, UserID, Username, PlanName, DaysLeft, Expiry)
    from a list of (grant_id, user_id, plan_name, expiry) rows, and sends it as a document
    directly in the admin panel chat — gives the admin a full DB-backed export on demand."""
    if not rows:
        bot.reply_to(message, f"ℹ️ *বর্তমানে কোনো এক্টিভ {title} নেই।*", parse_mode="Markdown")
        return
    lines = ["GrantID,UserID,TelegramUsername,PlanName,DaysLeft,ExpiryDate"]
    now = datetime.now()
    for grant_id, user_id, plan_name, expiry_str in rows:
        try:
            expiry_dt = datetime.fromisoformat(expiry_str)
            days_left = max(0, (expiry_dt - now).days)
            expiry_disp = expiry_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            days_left, expiry_disp = "N/A", "N/A"
        username = get_user_display(user_id)
        safe_plan = str(plan_name).replace(",", " ")
        lines.append(f"{grant_id},{user_id},{username},{safe_plan},{days_left},{expiry_disp}")

    path = f"/tmp/{filename}"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    try:
        with open(path, "rb") as f:
            bot.send_document(message.chat.id, f, caption=f"📊 *{title} — Full Export*\n🕐 Generated: `{now.strftime('%Y-%m-%d %H:%M')}`\n📋 Total Active: `{len(rows)}`",
                               parse_mode="Markdown", visible_file_name=filename)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _logic_total_packages(message):
    rows = get_all_active_package_holders()
    _export_holders_file(message, rows, "Active Packages", f"packages_export_{int(time.time())}.csv")


def _logic_total_subscriptions(message):
    rows = get_all_active_subscription_holders()
    _export_holders_file(message, rows, "Active Subscriptions", f"subscriptions_export_{int(time.time())}.csv")


def _logic_add_subscription_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "💎 *Grant Package (Manual):*\nFormat: `UserID PlanName Days FileLimit StorageMB`\n_Example:_ `123456789 VIP 30 10 500`\n\nℹ️ একটা ইউনিক 🆔 ID অটোমেটিক তৈরি হবে।", process_add_subscription, admin_flow=True)


def process_add_subscription(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        sub_uid, pname, days, file_limit, storage_mb = int(parts[0]), parts[1], int(parts[2]), int(parts[3]), int(parts[4])
        exp = datetime.now() + timedelta(days=days)
        grant_id = save_subscription(sub_uid, pname, exp, file_limit, storage_mb, source="admin_grant")
        bot.reply_to(message, f"✅ *Package active for User `{sub_uid}` — {esc_md(pname)} ({file_limit} files, {storage_mb}MB, {days} days)!*\n🆔 *Grant ID:* `#{grant_id}`",
                     reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        try:
            bot.send_message(sub_uid, f"🎉 *আপনার `{esc_md(pname)}` প্যাকেজ এক্টিভ করা হয়েছে ({days} দিনের জন্য)!*\n🆔 *আপনার Package ID:* `#{grant_id}` (প্রোফাইলে দেখা যাবে)", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_remove_subscription_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "❌ *Revoke Package*\n\nFormat: `UserID GrantID`\n_Example:_ `123456789 7`\n\nℹ️ প্রোফাইল/Grant confirmation-এ দেখানো 🆔 আইডি ব্যবহার করুন — ভুল ID দিলে বাতিল হবে না, নিরাপত্তার জন্য।", process_remove_subscription, admin_flow=True)


def process_remove_subscription(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, grant_id = int(parts[0]), int(parts[1])
        active = user_subscriptions.get(target)
        if active and active.get("grant_id") == grant_id:
            remove_subscription_db(target)
            bot.reply_to(message, f"✅ *User `{target}` এর Package `#{grant_id}` বাতিল করা হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
            try:
                bot.send_message(target, f"❌ *আপনার Package `#{grant_id}` এডমিন কর্তৃক বাতিল করা হয়েছে।*", parse_mode="Markdown")
            except Exception:
                pass
        else:
            bot.reply_to(message, f"ℹ️ *User `{target}` এর সাথে Package ID `#{grant_id}` মিলছে না, অথবা কোনো এক্টিভ Package নেই। সঠিক ID-র জন্য ইউজারের প্রোফাইল চেক করুন।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Use: `UserID GrantID`. Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_add_sub_plan_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid,
                "📝 *Enter Subscription Plan Details (space separated):*\n`Name StorageMB Price DurationDays`\n\n"
                "_Example:_ `Pro Unlimited 500 15USDT 30`\n\n"
                "ℹ️ Name-এ স্পেস থাকতে পারে, Price-এ স্পেস দেওয়া যাবে না (যেমন `15USDT`)।",
                process_add_sub_plan, admin_flow=True)


def process_add_sub_plan(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        duration, price, storage_mb = int(parts[-1]), parts[-2], int(parts[-3])
        name = " ".join(parts[:-3])
        if not name:
            raise ValueError("Plan Name missing")
        add_subscription_plan_db(name, storage_mb, price, duration)
        bot.reply_to(message, f"✅ *Subscription Plan `{esc_md(name)}` added successfully!*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_manage_sub_plans(message):
    plans = get_all_subscription_plans()
    if not plans:
        bot.send_message(message.chat.id, "ℹ️ কোনো Subscription Plan পাওয়া যায়নি।")
        return
    markup = types.InlineKeyboardMarkup()
    for p in plans:
        markup.add(types.InlineKeyboardButton(f"🗑️ Delete {p[1]}", callback_data=f"del_subplan_{p[0]}"))
    bot.send_message(message.chat.id, "🗑️ *Select a Subscription Plan to Delete:*", reply_markup=markup, parse_mode="Markdown")


def _logic_grant_subscription_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "🎟️ *Grant Subscription (Manual):*\nFormat: `UserID PlanName StorageMB Days`\n_Example:_ `123456789 Pro 500 30`\n\nℹ️ একটা ইউনিক 🆔 ID অটোমেটিক তৈরি হবে।", process_grant_subscription, admin_flow=True)


def process_grant_subscription(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, pname, storage_mb, days = int(parts[0]), parts[1], int(parts[2]), int(parts[3])
        exp = datetime.now() + timedelta(days=days)
        grant_id = save_storage_subscription(target, pname, storage_mb, exp, source="admin_grant")
        bot.reply_to(message, f"✅ *Subscription active for User `{target}` — {esc_md(pname)} ({storage_mb}MB, {days} days)!*\n🆔 *Grant ID:* `#{grant_id}`",
                     reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        try:
            bot.send_message(target, f"🎉 *আপনার `{esc_md(pname)}` Subscription এক্টিভ করা হয়েছে!*\n💾 Storage: {storage_mb} MB\n⏱️ Duration: {days} Days\n🤖 Bot Deploy: Unlimited\n🆔 *আপনার Subscription ID:* `#{grant_id}` (প্রোফাইলে দেখা যাবে)", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_revoke_subscription_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "❎ *Revoke Subscription*\n\nFormat: `UserID GrantID`\n_Example:_ `123456789 7`", process_revoke_subscription, admin_flow=True)


def process_revoke_subscription(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, grant_id = int(parts[0]), int(parts[1])
        active = user_storage_subs.get(target)
        if active and active.get("grant_id") == grant_id:
            remove_storage_subscription_db(target)
            bot.reply_to(message, f"✅ *User `{target}` এর Subscription `#{grant_id}` বাতিল করা হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
            try:
                bot.send_message(target, f"❌ *আপনার Subscription `#{grant_id}` এডমিন কর্তৃক বাতিল করা হয়েছে।*", parse_mode="Markdown")
            except Exception:
                pass
        else:
            bot.reply_to(message, f"ℹ️ *User `{target}` এর সাথে Subscription ID `#{grant_id}` মিলছে না, অথবা কোনো এক্টিভ Subscription নেই। সঠিক ID-র জন্য ইউজারের প্রোফাইল চেক করুন।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Use: `UserID GrantID`. Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_add_admin_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "👑 *Add New Admin*\n\nUser ID পাঠান:", process_add_admin, admin_flow=True)


def process_add_admin(message):
    uid = message.from_user.id
    try:
        target = int(message.text.strip())
        add_admin_db(target)
        bot.reply_to(message, f"✅ *User `{target}` কে সফলভাবে Admin বানানো হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        try:
            bot.send_message(target, "🎉 *অভিনন্দন! আপনাকে এই বটের Admin বানানো হয়েছে।*", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid User ID! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_remove_admin_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "➖ *Remove Admin*\n\nUser ID পাঠান:", process_remove_admin, admin_flow=True)


def process_remove_admin(message):
    uid = message.from_user.id
    try:
        target = int(message.text.strip())
        if target == OWNER_ID:
            bot.reply_to(message, "❌ *Owner কে Admin থেকে বাদ দেওয়া যাবে না!*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
            return
        remove_admin_db(target)
        bot.reply_to(message, f"✅ *User `{target}` কে Admin থেকে বাদ দেওয়া হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid User ID! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


# --- Settings actions ---
def _logic_change_api_key_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "🔑 *নতুন Binance API Key পাঠান:*", process_change_api_key, admin_flow=True)


def process_change_api_key(message):
    global BINANCE_API_KEY
    uid = message.from_user.id
    BINANCE_API_KEY = message.text.strip()
    set_setting("binance_api_key", BINANCE_API_KEY)
    bot.reply_to(message, "✅ *Binance API Key সফলভাবে পরিবর্তন করা হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_change_secret_key_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "🔒 *নতুন Binance Secret Key পাঠান:*", process_change_secret_key, admin_flow=True)


def process_change_secret_key(message):
    global BINANCE_SECRET_KEY
    uid = message.from_user.id
    BINANCE_SECRET_KEY = message.text.strip()
    set_setting("binance_secret_key", BINANCE_SECRET_KEY)
    bot.reply_to(message, "✅ *Binance Secret Key সফলভাবে পরিবর্তন করা হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_change_pay_id_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "🆔 *নতুন Binance Pay ID পাঠান:*", process_change_pay_id, admin_flow=True)


def process_change_pay_id(message):
    global BINANCE_PAY_ID
    uid = message.from_user.id
    BINANCE_PAY_ID = message.text.strip()
    set_setting("binance_pay_id", BINANCE_PAY_ID)
    bot.reply_to(message, f"✅ *Binance Pay ID পরিবর্তন করা হয়েছে:* `{BINANCE_PAY_ID}`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_contact_owner_edit_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, f"👑 *বর্তমান:* `{YOUR_USERNAME}`\n\nনতুন Owner Username/Link পাঠান:", process_contact_owner_edit, admin_flow=True)


def process_contact_owner_edit(message):
    global YOUR_USERNAME
    uid = message.from_user.id
    YOUR_USERNAME = message.text.strip()
    set_setting("contact_owner_username", YOUR_USERNAME)
    bot.reply_to(message, f"✅ *Contact Owner পরিবর্তন করা হয়েছে:* `{YOUR_USERNAME}`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_channel_edit_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, f"📢 *বর্তমান:* `{UPDATE_CHANNEL}`\n\nনতুন Channel Link পাঠান:", process_channel_edit, admin_flow=True)


def process_channel_edit(message):
    global UPDATE_CHANNEL
    uid = message.from_user.id
    UPDATE_CHANNEL = message.text.strip()
    set_setting("update_channel_link", UPDATE_CHANNEL)
    bot.reply_to(message, f"✅ *Update Channel পরিবর্তন করা হয়েছে:* `{UPDATE_CHANNEL}`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_usdt_rate_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, f"💱 *বর্তমান রেট:* `1 USDT = {USDT_BDT_RATE} BDT`\n\nনতুন রেট (শুধু সংখ্যা) পাঠান:", process_usdt_rate, admin_flow=True)


def process_usdt_rate(message):
    global USDT_BDT_RATE
    uid = message.from_user.id
    try:
        USDT_BDT_RATE = float(message.text.strip())
        set_setting("usdt_bdt_rate", str(USDT_BDT_RATE))
        bot.reply_to(message, f"✅ *নতুন রেট:* `1 USDT = {USDT_BDT_RATE} BDT`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ সঠিক সংখ্যা দিন!", reply_markup=return_keyboard(uid, True))


def _logic_deposit_group_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, f"👥 *বর্তমান Deposit Group ID:* `{DEPOSIT_GROUP_ID or 'Not set'}`\n\nনতুন Group ID পাঠান (বটকে ঐ গ্রুপে Admin বানাতে ভুলবেন না):", process_deposit_group, admin_flow=True)


def process_deposit_group(message):
    global DEPOSIT_GROUP_ID
    uid = message.from_user.id
    DEPOSIT_GROUP_ID = message.text.strip()
    set_setting("deposit_group_id", DEPOSIT_GROUP_ID)
    bot.reply_to(message, f"✅ *Deposit Group ID সেট করা হয়েছে:* `{DEPOSIT_GROUP_ID}`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_deposit_methods_menu(message):
    uid = message.from_user.id
    lines = ["🏦 *Deposit Methods:*\n"]
    if not deposit_methods:
        lines.append("_(কোনো method নেই)_")
    else:
        for mid, info in deposit_methods.items():
            lines.append(f"`#{mid}` {info['name']} — `{info['number']}` — Currency: `{info.get('currency','BDT')}` — _{info.get('note','')}_")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Add Method", callback_data="depmeth_add"))
    for mid in deposit_methods:
        markup.add(types.InlineKeyboardButton(f"🗑️ Delete #{mid}", callback_data=f"depmeth_del_{mid}"))
    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=markup, parse_mode="Markdown")


def _logic_add_deposit_method_init(chat_id, uid):
    step_prompt(chat_id, uid,
                "🏦 *Format:* `MethodName WalletNumber Currency Note`\n"
                "_Example:_ `Bkash 01700000000 BDT Send Money`\n\n"
                "ℹ️ Currency = ইউজারকে amount চাওয়ার সময় যেই কারেন্সিতে দেখানো হবে (যেমন `BDT`, `USD`, `INR`)। "
                "Note ঐচ্ছিক, একাধিক শব্দ হতে পারে।",
                process_add_deposit_method, admin_flow=True)


def process_add_deposit_method(message):
    uid = message.from_user.id
    try:
        parts = message.text.split(maxsplit=3)
        name, number = parts[0], parts[1]
        currency = parts[2] if len(parts) > 2 else "BDT"
        note = parts[3] if len(parts) > 3 else ""
        mid = add_deposit_method_db(name, number, note, currency)
        bot.reply_to(message, f"✅ *Deposit Method `#{mid} {esc_md(name)}` ({currency}) যোগ করা হয়েছে!*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


# --- Broadcast ---
def _logic_broadcast_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid,
                "📣 *Broadcast Message*\n\nযেভাবে খুশি মেসেজটি পাঠান — Text, Photo, Video, Document, Audio, Voice, Sticker ইত্যাদি:",
                process_broadcast, admin_flow=True)


def process_broadcast(message):
    uid = message.from_user.id
    status = bot.reply_to(message, "📣 *Broadcasting... please wait*", parse_mode="Markdown")
    sent, failed = 0, 0
    for target in list(active_users):
        try:
            bot.copy_message(target, message.chat.id, message.message_id)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    bot.edit_message_text(f"✅ *Broadcast Complete!*\n\n📨 Sent: `{sent}`\n❌ Failed: `{failed}`", status.chat.id, status.message_id, parse_mode="Markdown")
    bot.send_message(message.chat.id, "🔙", reply_markup=return_keyboard(uid, True))


def _logic_toggle_lock(message):
    global bot_locked
    uid = message.from_user.id
    bot_locked = not bot_locked
    bot.reply_to(message, f"🔐 *Bot status changed to:* `{'Locked' if bot_locked else 'Unlocked'}`", parse_mode="Markdown")


def _logic_run_all_scripts(message):
    started, already_running = 0, 0
    for uid, files in list(user_files.items()):
        ufolder = get_user_folder(uid)
        for file_name, file_type, display_name in files:
            if is_bot_running(uid, file_name):
                already_running += 1
                continue
            fpath = os.path.join(ufolder, file_name)
            if os.path.exists(fpath):
                if file_type == "js":
                    threading.Thread(target=run_js_script, args=(fpath, uid, ufolder, file_name, message)).start()
                else:
                    threading.Thread(target=run_script, args=(fpath, uid, ufolder, file_name, message)).start()
                started += 1
    bot.reply_to(message, f"⚙️ *Run All Scripts Complete!*\n\n🚀 Newly Started: `{started}`\n🟢 Already Running: `{already_running}`", parse_mode="Markdown")


def _logic_stop_all_scripts(message):
    """🆕 Stops every currently-running deployed script across all users."""
    stopped = 0
    for skey in list(bot_scripts.keys()):
        kill_process_tree(bot_scripts[skey])
        del bot_scripts[skey]
        manually_stopped_scripts.add(skey)
        stopped += 1
    bot.reply_to(message, f"🛑 *Stop All Scripts Complete!*\n\n🔴 Stopped: `{stopped}` running bot(s).", parse_mode="Markdown")


def _logic_bot_stats(message):
    running_count = sum(1 for key in list(bot_scripts.keys()) if is_bot_running(bot_scripts[key]["script_owner_id"], bot_scripts[key]["file_name"]))
    total_files = sum(len(v) for v in user_files.values())
    active_pkgs = len([u for u, s in user_subscriptions.items() if s["expiry"] > datetime.now()])
    active_subs = len([u for u, s in user_storage_subs.items() if s["expiry"] > datetime.now()])
    p_stats = get_purchase_stats()
    dep_counts = count_deposit_requests()
    total_revenue = round(p_stats["pkg_total_usdt"] + p_stats["sub_total_usdt"], 2)
    stats_msg = (f"📊 *𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀*\n━━━━━━━━━━━━━━━━━━━\n"
                 f"👥 *Active Users:* `{len(active_users)}`\n🛡️ *Total Admins:* `{len(admin_ids)}`\n"
                 f"💎 *Active Packages:* `{active_pkgs}`\n🎫 *Active Subscriptions:* `{active_subs}`\n"
                 f"📁 *Total Uploaded Files:* `{total_files}`\n🟢 *Currently Running Scripts:* `{running_count}`\n"
                 f"🔐 *Bot Locked:* `{bot_locked}`\n💾 *Auto-Backup:* `{'ON ✅' if AUTO_BACKUP_ENABLED else 'OFF ❌'}`\n"
                 f"🚫 *Backups Force-Stopped:* `{'YES' if BACKUP_FORCE_STOPPED else 'NO'}`\n"
                 f"🌐 *Free Pool Mode:* `{'ON ✅' if FREE_POOL_MODE else 'OFF ❌'}`\n"
                 f"━━━━━━━━━━━━━━━━━━━\n"
                 f"💰 *𝗦𝗮𝗹𝗲𝘀 𝗢𝘃𝗲𝗿𝘃𝗶𝗲𝘄*\n"
                 f"📦 Package Sold: `{p_stats['pkg_count']}` — Earned: `{p_stats['pkg_total_usdt']} USDT`\n"
                 f"🎫 Subscription Sold: `{p_stats['sub_count']}` — Earned: `{p_stats['sub_total_usdt']} USDT`\n"
                 f"🏆 Total Revenue: `{total_revenue} USDT` (~{round(total_revenue * USDT_BDT_RATE, 2)} BDT)\n"
                 f"━━━━━━━━━━━━━━━━━━━\n"
                 f"📥 *𝗗𝗲𝗽𝗼𝘀𝗶𝘁 𝗥𝗲𝗾𝘂𝗲𝘀𝘁𝘀*\n"
                 f"⏳ Pending: `{dep_counts['pending']}` | ✅ Approved: `{dep_counts['approved']}` | ❌ Rejected: `{dep_counts['rejected']}`\n"
                 f"━━━━━━━━━━━━━━━━━━━")
    bot.reply_to(message, stats_msg, parse_mode="Markdown")


def _logic_open_backup_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id,
                      f"💾 *𝗕𝗮𝗰𝗸𝘂𝗽 𝗖𝗼𝗻𝘁𝗿𝗼𝗹 𝗣𝗮𝗻𝗲𝗹*\n\n🔁 *Global Auto-Backup:* `{'ON ✅' if AUTO_BACKUP_ENABLED else 'OFF ❌'}`\n"
                      f"⏱️ প্রতি ১২ ঘন্টা পর পর\n\nℹ️ Package/Subscription ইউজাররা এমনিতেই পান।",
                      reply_markup=_kb_from_keys(uid, BACKUP_PANEL_KEYS), parse_mode="Markdown")


def _logic_grant_backup_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "➕ *Grant Backup Access*\n\nFormat: `UserID Days`\n(0 = Lifetime)\n_Example:_ `123456789 30`", process_grant_backup, admin_flow=True)


def process_grant_backup(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, days = int(parts[0]), int(parts[1])
        grant_backup_access_db(target, days)
        duration_text = "Lifetime" if days == 0 else f"{days} Days"
        bot.reply_to(message, f"✅ *User `{target}` কে Backup Access দেওয়া হয়েছে।* ({duration_text})", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        try:
            bot.send_message(target, f"💾 *আপনাকে Auto-Backup Access দেওয়া হয়েছে!* ({duration_text})", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_revoke_backup_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "➖ *Revoke Backup Access*\n\nUser ID পাঠান:", process_revoke_backup, admin_flow=True)


def process_revoke_backup(message):
    uid = message.from_user.id
    try:
        target = int(message.text.strip())
        revoke_backup_access_db(target)
        bot.reply_to(message, f"✅ *User `{target}` এর ম্যানুয়াল Backup Access বাতিল করা হয়েছে।*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_list_backup_users(message):
    rows = list_backup_access_db()
    if not rows:
        bot.reply_to(message, "ℹ️ কোনো ম্যানুয়াল Backup Access গ্রাহক নেই।", parse_mode="Markdown")
        return
    lines = ["📋 *Manual Backup Access List:*\n"]
    for target, enabled, expiry in rows:
        exp_text = "Lifetime" if not expiry else expiry.split("T")[0]
        lines.append(f"👤 `{target}` — {'✅ Active' if enabled else '❌ Disabled'} — Expiry: `{exp_text}`")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


def _logic_toggle_auto_backup(message):
    global AUTO_BACKUP_ENABLED
    AUTO_BACKUP_ENABLED = not AUTO_BACKUP_ENABLED
    bot.reply_to(message, f"🔁 *Global Auto-Backup:* `{'ON ✅' if AUTO_BACKUP_ENABLED else 'OFF ❌'}`", parse_mode="Markdown")


def _logic_toggle_stop_all_backup(message):
    """🆕 Force-stops backup access for every user except Admin/Owner (they keep it always)."""
    global BACKUP_FORCE_STOPPED
    BACKUP_FORCE_STOPPED = not BACKUP_FORCE_STOPPED
    set_setting("backup_force_stopped", "1" if BACKUP_FORCE_STOPPED else "0")
    state = "🛑 STOPPED for all users (Admin/Owner unaffected)" if BACKUP_FORCE_STOPPED else "✅ Resumed as normal"
    bot.reply_to(message, f"💾 *All User Backups:* `{state}`", parse_mode="Markdown")


def _logic_open_wallet_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, "💰 *𝗪𝗮𝗹𝗹𝗲𝘁 𝗖𝗼𝗻𝘁𝗿𝗼𝗹 𝗣𝗮𝗻𝗲𝗹*", reply_markup=_kb_from_keys(uid, WALLET_PANEL_KEYS), parse_mode="Markdown")


def _logic_add_balance_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "➕ *Add Balance*\n\nFormat: `UserID Amount`\n_Example:_ `123456789 500`", process_add_balance, admin_flow=True)


def process_add_balance(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, amount = int(parts[0]), float(parts[1])
        new_bal = add_balance(target, amount)
        bot.reply_to(message, f"✅ *User `{target}` এর ব্যালেন্সে `{amount} BDT` যোগ হয়েছে। নতুন Balance:* `{new_bal} BDT`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        try:
            bot.send_message(target, f"💰 *আপনার ওয়ালেটে `{amount} BDT` যোগ করা হয়েছে!*\nবর্তমান ব্যালেন্স: `{new_bal} BDT`", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_deduct_balance_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "➖ *Deduct Balance*\n\nFormat: `UserID Amount`\n_Example:_ `123456789 100`", process_deduct_balance, admin_flow=True)


def process_deduct_balance(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, amount = int(parts[0]), float(parts[1])
        new_bal = deduct_balance(target, amount)
        bot.reply_to(message, f"✅ *User `{target}` এর ব্যালেন্স থেকে `{amount} BDT` কাটা হয়েছে। নতুন Balance:* `{new_bal} BDT`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_check_balance_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "📋 *Check Balance*\n\nUser ID পাঠান:", process_check_balance, admin_flow=True)


def process_check_balance(message):
    uid = message.from_user.id
    try:
        target = int(message.text.strip())
        bot.reply_to(message, f"💰 *User `{target}` এর Balance:* `{get_balance(target)} BDT`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_pending_deposits(message):
    """🆕 Shows how many deposit requests are still awaiting approve/reject, plus a quick list."""
    counts = count_deposit_requests()
    pending_rows = list_pending_deposit_requests()
    lines = [
        "📥 *𝗔𝗹𝗹 𝗗𝗲𝗽𝗼𝘀𝗶𝘁 𝗥𝗲𝗾𝘂𝗲𝘀𝘁𝘀*",
        "━━━━━━━━━━━━━━━━━━━",
        f"⏳ *Pending (not yet approved/rejected):* `{counts['pending']}`",
        f"✅ *Approved so far:* `{counts['approved']}`",
        f"❌ *Rejected so far:* `{counts['rejected']}`",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if pending_rows:
        lines.append("*Pending requests:*")
        for rid, uid_r, method_name, amount, tx_id in pending_rows:
            lines.append(f"`#{rid}` 👤 `{uid_r}` — {esc_md(method_name)} — `{amount}` — TxID `{esc_md(tx_id)}`")
    else:
        lines.append("_(কোনো Pending Request নেই)_")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


def _logic_open_free_hours_panel(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id,
                      f"🎁 *𝗙𝗿𝗲𝗲 𝗛𝗼𝘂𝗿𝘀 𝗖𝗼𝗻𝘁𝗿𝗼𝗹 𝗣𝗮𝗻𝗲𝗹*\n\n"
                      f"🌐 Pool Mode: `{'ON ✅ (' + str(FREE_POOL_HOURS) + 'h shared)' if FREE_POOL_MODE else 'OFF ❌ (per-user)'}`\n"
                      f"🎯 Referral Milestone: `{REFERRAL_MILESTONE}` referrals → `{REFERRAL_REWARD_HOURS}h`\n",
                      reply_markup=_kb_from_keys(uid, FREE_HOURS_PANEL_KEYS), parse_mode="Markdown")


def _logic_grant_free_hours_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "🎁 *Grant Free Hours (1 user)*\n\nFormat: `UserID Hours`\n_Example:_ `123456789 5`", process_grant_free_hours, admin_flow=True)


def process_grant_free_hours(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, hours = int(parts[0]), float(parts[1])
        new_val = grant_free_hours(target, hours)
        bot.reply_to(message, f"✅ *User `{target}` কে `{hours}h` Free Hours দেওয়া হয়েছে। মোট:* `{new_val}h`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        try:
            bot.send_message(target, f"🎁 *আপনাকে `{hours}` Free Hours দেওয়া হয়েছে!*\nমোট ফ্রি সময়: `{new_val}h`", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_grant_all_free_hours_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid,
                "🎁 *Grant Free Hours to ALL Users*\n\n"
                "প্রত্যেক (active, non-admin, non-paid) ইউজার এতগুলো ঘন্টা করে পাবে।\n"
                "শুধু একটা সংখ্যা লিখুন (Hours):\n_Example:_ `1`\n\n"
                "ℹ️ Pool Mode ON থাকলে এটি shared pool-এই যোগ হবে।",
                process_grant_all_free_hours, admin_flow=True)


def process_grant_all_free_hours(message):
    uid = message.from_user.id
    try:
        hours = float(message.text.strip())
        if FREE_POOL_MODE:
            new_val = grant_free_hours(uid, hours)  # user_id arg unused in pool mode
            bot.reply_to(message, f"✅ *Shared Pool-এ `{hours}h` যোগ করা হয়েছে। মোট Pool:* `{new_val}h`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
        else:
            count = grant_free_hours_to_all(hours)
            bot.reply_to(message, f"✅ *`{count}` জন ইউজারকে `{hours}h` করে Free Hours দেওয়া হয়েছে!*", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ সঠিক সংখ্যা দিন!", reply_markup=return_keyboard(uid, True))


def _logic_remove_free_hours_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, "➖ *Remove Free Hours*\n\nFormat: `UserID Hours` (Pool mode হলে UserID যেকোনো কিছু দিন)\n_Example:_ `123456789 2`", process_remove_free_hours, admin_flow=True)


def process_remove_free_hours(message):
    uid = message.from_user.id
    try:
        parts = message.text.split()
        target, hours = int(parts[0]), float(parts[1])
        new_val = remove_free_hours(target, hours)
        bot.reply_to(message, f"✅ *`{hours}h` Free Hours কাটা হয়েছে। বাকি আছে:* `{new_val}h`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_check_free_hours_init(message):
    uid = message.from_user.id
    if FREE_POOL_MODE:
        bot.reply_to(message, f"⏳ *Shared Pool Hours:* `{FREE_POOL_HOURS}h`", parse_mode="Markdown")
        return
    step_prompt(message.chat.id, uid, "📋 *Check Free Hours*\n\nUser ID পাঠান:", process_check_free_hours, admin_flow=True)


def process_check_free_hours(message):
    uid = message.from_user.id
    try:
        target = int(message.text.strip())
        bot.reply_to(message, f"⏳ *User `{target}` এর Free Hours:* `{get_free_hours_remaining(target)}h`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


def _logic_toggle_pool_mode(message):
    uid = message.from_user.id
    set_free_pool_mode(not FREE_POOL_MODE)
    if FREE_POOL_MODE and FREE_POOL_HOURS <= 0:
        set_free_pool_hours(FREE_TRIAL_HOURS)
    bot.reply_to(message, f"🌐 *Free Hours Pool Mode:* `{'ON ✅' if FREE_POOL_MODE else 'OFF ❌'}`", parse_mode="Markdown")


def _logic_set_pool_hours_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid, f"⏳ *বর্তমান Shared Pool:* `{FREE_POOL_HOURS}h`\n\nনতুন Pool Hours লিখুন:", process_set_pool_hours, admin_flow=True)


def process_set_pool_hours(message):
    uid = message.from_user.id
    try:
        hours = float(message.text.strip())
        set_free_pool_hours(hours)
        bot.reply_to(message, f"✅ *Shared Pool Hours সেট করা হয়েছে:* `{FREE_POOL_HOURS}h`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ সঠিক সংখ্যা দিন!", reply_markup=return_keyboard(uid, True))


def _logic_set_referral_config_init(message):
    uid = message.from_user.id
    step_prompt(message.chat.id, uid,
                f"⚙️ *বর্তমান:* `{REFERRAL_MILESTONE}` referrals → `{REFERRAL_REWARD_HOURS}h`\n\n"
                "Format: `Milestone Hours`\n_Example:_ `3 5`", process_set_referral_config, admin_flow=True)


def process_set_referral_config(message):
    global REFERRAL_MILESTONE, REFERRAL_REWARD_HOURS
    uid = message.from_user.id
    try:
        parts = message.text.split()
        milestone, hours = int(parts[0]), float(parts[1])
        REFERRAL_MILESTONE = milestone
        REFERRAL_REWARD_HOURS = hours
        set_setting("referral_milestone", str(milestone))
        set_setting("referral_reward_hours", str(hours))
        bot.reply_to(message, f"✅ *Referral Config আপডেট হয়েছে:* `{milestone}` referrals → `{hours}h`", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid Format! Error: {esc_md(str(e))}", reply_markup=return_keyboard(uid, True), parse_mode="Markdown")


# ==========================================================================
# --- Unified action dispatch tables ---
# ==========================================================================
def _logic_speed_ping(message):
    """🆕 Real, live latency — measured as the actual round-trip time of a Telegram API call."""
    t0 = time.time()
    sent = bot.send_message(message.chat.id, "⚡ Pinging...")
    elapsed_ms = round((time.time() - t0) * 1000)
    try:
        bot.edit_message_text(f"⚡ *Bot Latency:* `{elapsed_ms} ms` (Live, Telegram API round-trip)",
                               sent.chat.id, sent.message_id, parse_mode="Markdown")
    except Exception:
        pass


USER_ACTIONS = {
    "upload": _logic_upload_file, "files": _logic_check_files, "plans": _logic_view_plans,
    "subplans": _logic_view_subscription_plans, "profile": _logic_my_profile, "referral": _logic_referral_menu,
    "deposit": _logic_deposit_menu, "language": _logic_language_menu,
    "speed": _logic_speed_ping,
    "stats": lambda m: bot.reply_to(m, f"📊 *Active Users:* `{len(active_users)}`", parse_mode="Markdown"),
    "channel": lambda m: safe_reply(m, f"📢 *Join channel:* {esc_md(UPDATE_CHANNEL)}", parse_mode="Markdown"),
    "contact": lambda m: safe_reply(m, f"👑 *Owner:* {esc_md(YOUR_USERNAME)}", parse_mode="Markdown"),
    "admin_panel": _logic_open_admin_panel,
}

ADMIN_PANEL_ACTIONS = {
    "pkg_panel": _logic_open_package_panel, "sub_panel": _logic_open_subscription_panel,
    "admin_mgmt": _logic_open_admin_mgmt_panel, "broadcast": _logic_broadcast_init,
    "run_all": _logic_run_all_scripts, "stop_all": _logic_stop_all_scripts, "bot_stats_a": _logic_bot_stats,
    "backup_panel": _logic_open_backup_panel, "wallet_panel": _logic_open_wallet_panel,
    "freehours_panel": _logic_open_free_hours_panel, "settings_panel": _logic_open_settings_panel,
    "back_main2": _logic_back_to_main_menu,
}
PACKAGE_PANEL_ACTIONS = {
    "add_plan": _logic_add_plan_init, "manage_plans": _logic_manage_plans,
    "grant_pkg": _logic_add_subscription_init, "revoke_pkg": _logic_remove_subscription_init,
    "total_pkg": _logic_total_packages, "back_admin2": _logic_back_to_admin_panel,
}
SUBSCRIPTION_PANEL_ACTIONS = {
    "add_subplan": _logic_add_sub_plan_init, "manage_subplans": _logic_manage_sub_plans,
    "grant_sub": _logic_grant_subscription_init, "revoke_sub": _logic_revoke_subscription_init,
    "total_sub": _logic_total_subscriptions, "back_admin2": _logic_back_to_admin_panel,
}
ADMIN_MGMT_ACTIONS = {
    "add_admin": _logic_add_admin_init, "remove_admin": _logic_remove_admin_init,
    "back_admin2": _logic_back_to_admin_panel,
}
SETTINGS_PANEL_ACTIONS = {
    "api_settings_sub": _logic_open_api_settings_sub, "deposit_methods_sub": _logic_deposit_methods_menu,
    "deposit_group_sub": _logic_deposit_group_init, "usdt_rate_sub": _logic_usdt_rate_init,
    "lock_toggle": _logic_toggle_lock, "contact_owner_edit": _logic_contact_owner_edit_init,
    "channel_edit": _logic_channel_edit_init, "back_admin2": _logic_back_to_admin_panel,
}
API_SETTINGS_ACTIONS = {
    "change_api_key": _logic_change_api_key_init, "change_secret": _logic_change_secret_key_init,
    "change_payid": _logic_change_pay_id_init, "back_settings2": _logic_back_to_settings,
}
BACKUP_PANEL_ACTIONS = {
    "grant_backup": _logic_grant_backup_init, "revoke_backup": _logic_revoke_backup_init,
    "list_backup": _logic_list_backup_users, "toggle_autobackup": _logic_toggle_auto_backup,
    "stop_all_backup": _logic_toggle_stop_all_backup, "back_admin2": _logic_back_to_admin_panel,
}
WALLET_PANEL_ACTIONS = {
    "add_balance": _logic_add_balance_init, "deduct_balance": _logic_deduct_balance_init,
    "check_balance": _logic_check_balance_init, "pending_deposits": _logic_pending_deposits,
    "back_admin2": _logic_back_to_admin_panel,
}
FREE_HOURS_PANEL_ACTIONS = {
    "grant_freehours": _logic_grant_free_hours_init, "grant_all_freehours": _logic_grant_all_free_hours_init,
    "remove_freehours": _logic_remove_free_hours_init, "check_freehours": _logic_check_free_hours_init,
    "toggle_pool_mode": _logic_toggle_pool_mode, "set_pool_hours": _logic_set_pool_hours_init,
    "set_referral_config": _logic_set_referral_config_init, "back_admin2": _logic_back_to_admin_panel,
}

ALL_ADMIN_PANELS = [
    (ADMIN_PANEL_REV, ADMIN_PANEL_ACTIONS), (PACKAGE_PANEL_REV, PACKAGE_PANEL_ACTIONS),
    (SUBSCRIPTION_PANEL_REV, SUBSCRIPTION_PANEL_ACTIONS), (ADMIN_MGMT_REV, ADMIN_MGMT_ACTIONS),
    (SETTINGS_PANEL_REV, SETTINGS_PANEL_ACTIONS), (API_SETTINGS_REV, API_SETTINGS_ACTIONS),
    (BACKUP_PANEL_REV, BACKUP_PANEL_ACTIONS), (WALLET_PANEL_REV, WALLET_PANEL_ACTIONS),
    (FREE_HOURS_PANEL_REV, FREE_HOURS_PANEL_ACTIONS),
]


LOCK_EXEMPT_ACTIONS = {"contact", "channel"}  # 🆕 only these two buttons stay active while bot_locked


@bot.message_handler(func=lambda m: (m.text in all_label_to_action())
                      or (m.from_user.id in admin_ids and any(m.text in rev for rev, _ in ALL_ADMIN_PANELS)))
def handle_text_router(message):
    """🆕 Single, deterministic text-button router (replaces the old two-handler setup).
    Admin sub-panel buttons are checked first (they're only ever shown to admins anyway),
    then the main user-menu buttons. This removes any ambiguity about which handler 'wins'
    when two labels happen to be identical, and is where the maintenance-lock is enforced."""
    uid = message.from_user.id
    text = message.text
    update_user_info(uid, message.from_user.username, message.from_user.first_name)  # 🆕 keep username fresh

    # Admin sub-panel navigation (Package/Subscription/Settings/Backup/Wallet/Free-Hours/etc.)
    if uid in admin_ids:
        for rev, actions in ALL_ADMIN_PANELS:
            if text in rev:
                actions[rev[text]](message)
                return

    # Main user-menu buttons
    if text in all_label_to_action():
        action = all_label_to_action()[text]
        if action == "admin_panel" and uid not in admin_ids:
            return
        if bot_locked and uid not in admin_ids and action not in LOCK_EXEMPT_ACTIONS:
            bot.reply_to(message, tr(uid, "bot_locked_button"), parse_mode="Markdown")
            return
        USER_ACTIONS[action](message)


# ==========================================================================
# --- Document Upload Processing ---
# ==========================================================================
@bot.message_handler(content_types=["document"])
def handle_file_upload_doc(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc = message.document

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, tr(user_id, "bot_locked_button"), parse_mode="Markdown")
        return

    if user_id not in admin_ids and user_id != OWNER_ID:
        has_pkg = user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now()
        has_sub = user_id in user_storage_subs and user_storage_subs[user_id]["expiry"] > datetime.now()
        has_trial = get_free_hours_remaining(user_id) > 0
        if not has_pkg and not has_sub and not has_trial:
            bot.reply_to(message, tr(user_id, "no_plan_upload"), parse_mode="Markdown")
            return
        if has_trial and not has_pkg and not has_sub and get_user_file_count(user_id) >= 1:
            bot.reply_to(message, "❌ *Free Trial-এ শুধুমাত্র ১টি বট ডিপ্লয় করা যাবে। আরও বট চালাতে একটি প্ল্যান কিনুন।*", parse_mode="Markdown")
            return

    file_name = doc.file_name
    file_ext = os.path.splitext(file_name)[1].lower()
    if file_ext not in [".py", ".js", ".zip"]:
        bot.reply_to(message, "⚠️ *Only `.py`, `.js`, and `.zip` files are supported!*", parse_mode="Markdown")
        return

    try:
        download_wait_msg = bot.reply_to(message, f"⏳ *Downloading `{esc_md(file_name)}`...*", parse_mode="Markdown")
        file_info_tg_doc = bot.get_file(doc.file_id)
        downloaded_file_content = bot.download_file(file_info_tg_doc.file_path)

        if user_id != OWNER_ID:
            is_safe, reason = scan_file_for_malware(downloaded_file_content, file_name, user_id)
            if not is_safe:
                bot.edit_message_text(f"🚨 *Security Alert:* {esc_md(reason)}", chat_id, download_wait_msg.message_id, parse_mode="Markdown")
                return

        storage_limit_mb = get_user_storage_limit_mb(user_id)
        if storage_limit_mb is not None:
            current_usage_mb = get_user_storage_usage_mb(user_id)
            incoming_mb = len(downloaded_file_content) / (1024 * 1024)
            if current_usage_mb + incoming_mb > storage_limit_mb:
                bot.edit_message_text(
                    f"🚫 *Storage Limit Exceeded!*\n\n💾 Used: `{current_usage_mb:.2f} MB` / `{storage_limit_mb} MB`\n"
                    f"📦 এই ফাইলের সাইজ: `{incoming_mb:.2f} MB`\n\nঅনুগ্রহ করে পুরাতন ফাইল ডিলিট করুন অথবা প্ল্যান আপগ্রেড করুন।",
                    chat_id, download_wait_msg.message_id, parse_mode="Markdown")
                return

        user_folder = get_user_folder(user_id)
        file_path = os.path.join(user_folder, file_name)
        with open(file_path, "wb") as f:
            f.write(downloaded_file_content)

        bot.edit_message_text(f"✅ *File `{esc_md(file_name)}` uploaded successfully!*", chat_id, download_wait_msg.message_id, parse_mode="Markdown")

        if file_ext in (".js", ".py"):
            file_type = "js" if file_ext == ".js" else "py"
            step_prompt(chat_id, user_id,
                        "📝 *এই বটের জন্য একটি সহজ Title/Name দিন* (যেমন: `PC Help Bot`)\n⏭️ Skip করতে `skip` লিখুন।",
                        process_file_title, user_id, file_name, file_path, file_type)
        else:
            check_and_notify_storage_usage(user_id)

    except Exception as e:
        bot.reply_to(message, f"❌ *Error:* {esc_md(str(e))}", parse_mode="Markdown")


def process_file_title(message, user_id, file_name, file_path, file_type):
    title = message.text.strip() if message.text else ""
    if not title or title.lower() == "skip":
        title = file_name
    title = title[:60]

    save_user_file(user_id, file_name, file_type, title)
    bot.reply_to(message, f"✅ *Title সেট হয়েছে:* `{esc_md(title)}`\n🚀 ফাইলটি এখন চালু করা হচ্ছে...",
                 reply_markup=create_reply_keyboard_main_menu(user_id), parse_mode="Markdown")

    check_and_notify_storage_usage(user_id)

    user_folder = get_user_folder(user_id)
    if file_type == "js":
        threading.Thread(target=run_js_script, args=(file_path, user_id, user_folder, file_name, message)).start()
    else:
        threading.Thread(target=run_script, args=(file_path, user_id, user_folder, file_name, message)).start()


# ==========================================================================
# --- Callback Routing ---
# ==========================================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    # 🆕 Maintenance-lock guard: block purchase/file/deposit actions for non-admins while locked
    # (deposit approve/reject in the admin group must still work, so it's excluded here).
    _LOCK_BLOCKED_PREFIXES = ("buy_", "submit_txid_", "walletpay_", "depmethod_", "file_", "start_", "stop_", "del_")
    if bot_locked and user_id not in admin_ids and (data in ("view_plans_cb", "view_subplans_cb", "confirm_plan_upload", "deposit_final_confirm")
                                                      or data.startswith(_LOCK_BLOCKED_PREFIXES)):
        bot.answer_callback_query(call.id, tr(user_id, "bot_locked_button"), show_alert=True)
        return

    if data == "view_plans_cb":
        bot.answer_callback_query(call.id)
        _logic_view_plans(call)

    elif data == "view_subplans_cb":
        bot.answer_callback_query(call.id)
        _logic_view_subscription_plans(call)

    elif data == "confirm_plan_upload":
        bot.answer_callback_query(call.id, "✅ Plan Verified!")
        bot.send_message(call.message.chat.id, tr(user_id, "ask_send_file"), parse_mode="Markdown")

    elif data == "toggle_autorestart":
        new_state = not is_auto_restart_enabled(user_id)
        set_auto_restart_db(user_id, new_state)
        bot.answer_callback_query(call.id, f"Auto-Restart {'ON ✅' if new_state else 'OFF ❌'}")
        bot.send_message(call.message.chat.id, f"🔄 *Auto-Restart:* `{'✅ ON' if new_state else '❌ OFF'}`", parse_mode="Markdown")

    elif data == "get_referral_link":
        bot.answer_callback_query(call.id)
        _logic_referral_menu(call.message)

    elif data.startswith("setlang_"):
        lang = data.split("_")[1]
        set_lang(user_id, lang)
        bot.answer_callback_query(call.id, "✅")
        bot.send_message(call.message.chat.id, tr(user_id, "lang_set"), reply_markup=create_reply_keyboard_main_menu(user_id), parse_mode="Markdown")

    elif data.startswith("depmethod_"):
        mid = int(data.split("_")[1])
        bot.answer_callback_query(call.id)
        _deposit_ask_sender(call.message, user_id, mid)

    elif data == "deposit_final_confirm":
        _deposit_finalize(call)

    elif data.startswith("dep_appr_"):
        _handle_deposit_decision(call, approve=True)

    elif data.startswith("dep_rej_"):
        _handle_deposit_decision(call, approve=False)

    elif data == "depmeth_add":
        if user_id not in admin_ids:
            bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        _logic_add_deposit_method_init(call.message.chat.id, user_id)

    elif data.startswith("depmeth_del_"):
        if user_id not in admin_ids:
            bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
            return
        mid = int(data.split("_")[2])
        delete_deposit_method_db(mid)
        bot.answer_callback_query(call.id, "Deleted!")
        bot.send_message(call.message.chat.id, f"✅ Deposit Method #{mid} deleted.")

    elif data.startswith("walletpay_"):
        _, plan_type, plan_id = data.split("_")
        plan_id = int(plan_id)
        if plan_type == "pkg":
            plan_row = get_plan_by_id(plan_id)
            price = plan_row[4] if plan_row else "0"
            label = plan_row[1] if plan_row else "Package"
        else:
            plan_row = get_subscription_plan_by_id(plan_id)
            price = plan_row[3] if plan_row else "0"
            label = plan_row[1] if plan_row else "Subscription"
        if not plan_row:
            bot.answer_callback_query(call.id, "Plan not found.", show_alert=True)
            return
        usdt_price, _ = parse_price_to_usdt(price)
        already_paid = get_pending_payment(user_id, plan_type, plan_id)
        due_amount = max(0.0, round(usdt_price - already_paid, 2))
        due_bdt = round(due_amount * USDT_BDT_RATE, 2)
        balance = get_balance(user_id)
        if balance < due_bdt:
            bot.answer_callback_query(call.id, tr(user_id, "wallet_insufficient", bal=balance, usdt=round(balance / USDT_BDT_RATE, 2)), show_alert=True)
            return
        deduct_balance(user_id, due_bdt)
        clear_pending_payment(user_id, plan_type, plan_id)
        name, expiry, grant_id = _activate_plan(user_id, plan_type, plan_id, plan_row, source="purchase_wallet")
        log_purchase(user_id, plan_type, name, due_amount + already_paid, "wallet")
        bot.answer_callback_query(call.id, "✅ Purchased!")
        bot.send_message(call.message.chat.id,
                          tr(user_id, "wallet_pay_success", label=esc_md(name), deducted=due_bdt) + f"\n🆔 *ID:* `#{grant_id}`",
                          parse_mode="Markdown")
        try:
            bot.send_message(OWNER_ID, f"🔔 *New {plan_type} Purchase via Wallet!*\n👤 User: `{user_id}`\n🏷️ Plan: `{esc_md(name)}`\n🆔 ID: `#{grant_id}`\n💰 Deducted: `{due_bdt} BDT`", parse_mode="Markdown")
        except Exception:
            pass

    elif data.startswith("instmod_"):
        _, owner_id, mod_name, fname = data.split("_", 3)
        if user_id != int(owner_id) and user_id not in admin_ids:
            bot.answer_callback_query(call.id, "❌ আপনি অন্য ইউজারের ফাইল কাস্টমাইজ করতে পারবেন না!", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        pkg_name = TELEGRAM_MODULES.get(mod_name.lower(), mod_name)
        ext = os.path.splitext(fname)[1].lower()
        status_msg = bot.send_message(call.message.chat.id, f"⏳ *`{esc_md(pkg_name)}` মডিউলটি ইনস্টল করা হচ্ছে...*", parse_mode="Markdown")

        def do_pip_install():
            cmd = ["npm", "install", pkg_name] if ext == ".js" else [sys.executable, "-m", "pip", "install", pkg_name]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                bot.edit_message_text(f"✅ *`{esc_md(pkg_name)}` মডিউলটি সফলভাবে ইনস্টল হয়েছে!*\n🚀 ফাইলটি পুনরায় চালু করা হচ্ছে...",
                                       call.message.chat.id, status_msg.message_id, parse_mode="Markdown")
                time.sleep(1)
                ufolder = get_user_folder(int(owner_id))
                fpath = os.path.join(ufolder, fname)
                if ext == ".js":
                    run_js_script(fpath, int(owner_id), ufolder, fname, call.message)
                else:
                    run_script(fpath, int(owner_id), ufolder, fname, call.message)
            else:
                bot.edit_message_text(f"❌ *ইনস্টলেশন ব্যর্থ হয়েছে!*\n\n```\n{res.stderr[:300]}\n```", call.message.chat.id, status_msg.message_id, parse_mode="Markdown")

        threading.Thread(target=do_pip_install).start()

    elif data.startswith("viewlog_"):
        _, owner_id, fname = data.split("_", 2)
        ufolder = get_user_folder(int(owner_id))
        log_fpath = os.path.join(ufolder, f"{os.path.splitext(fname)[0]}.log")
        if os.path.exists(log_fpath):
            with open(log_fpath, "r", encoding="utf-8", errors="ignore") as f:
                logs = f.read()[-2000:]
            bot.send_message(call.message.chat.id, f"📜 *Error Log for `{esc_md(fname)}`:*\n\n```\n{logs if logs else 'No logs recorded.'}\n```", parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, "No log file found!", show_alert=True)

    elif data.startswith("buy_pkg_"):
        plan_id = int(data.split("_")[2])
        bot.answer_callback_query(call.id)
        _initiate_binance_purchase(call.message.chat.id, user_id, "pkg", plan_id)

    elif data.startswith("buy_sub_"):
        plan_id = int(data.split("_")[2])
        bot.answer_callback_query(call.id)
        _initiate_binance_purchase(call.message.chat.id, user_id, "sub", plan_id)

    elif data.startswith("submit_txid_pkg_"):
        plan_id = int(data.split("_")[3])
        bot.answer_callback_query(call.id)
        step_prompt(call.message.chat.id, user_id, "📩 *আপনার Binance Pay এর Order ID / Transaction ID টি লিখুন:*", process_binance_txid, "pkg", plan_id)

    elif data.startswith("submit_txid_sub_"):
        plan_id = int(data.split("_")[3])
        bot.answer_callback_query(call.id)
        step_prompt(call.message.chat.id, user_id, "📩 *আপনার Binance Pay এর Order ID / Transaction ID টি লিখুন:*", process_binance_txid, "sub", plan_id)

    elif data.startswith("del_plan_") and user_id in admin_ids:
        pid = int(data.split("_")[2])
        delete_plan_db(pid)
        bot.answer_callback_query(call.id, "Plan Deleted!")
        bot.send_message(call.message.chat.id, "✅ Package Plan successfully deleted.")

    elif data.startswith("del_subplan_") and user_id in admin_ids:
        pid = int(data.split("_")[2])
        delete_subscription_plan_db(pid)
        bot.answer_callback_query(call.id, "Subscription Plan Deleted!")
        bot.send_message(call.message.chat.id, "✅ Subscription Plan successfully deleted.")

    elif data.startswith("file_"):
        _, owner_id, fname = data.split("_", 2)
        is_running = is_bot_running(int(owner_id), fname)
        display_name = fname
        for fn, ft, dn in user_files.get(int(owner_id), []):
            if fn == fname:
                display_name = dn if dn else fname
                break
        markup = types.InlineKeyboardMarkup(row_width=2)
        if is_running:
            markup.add(types.InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{owner_id}_{fname}"))
        else:
            markup.add(types.InlineKeyboardButton("▶️ Start", callback_data=f"start_{owner_id}_{fname}"))
        markup.add(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{owner_id}_{fname}"))
        bot.send_message(call.message.chat.id,
                          f"🤖 *Bot Name:* `{esc_md(display_name)}`\n📄 *File:* `{esc_md(fname)}`\n🚦 *Status:* `{'Running' if is_running else 'Stopped'}`",
                          reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("start_"):
        _, owner_id, fname = data.split("_", 2)
        owner_id = int(owner_id)
        if is_bot_running(owner_id, fname):
            bot.answer_callback_query(call.id, "Already running!", show_alert=True)
            return
        if not can_deploy_now(owner_id):
            bot.answer_callback_query(call.id, "কোনো এক্টিভ প্ল্যান/ফ্রি-আওয়ার নেই!", show_alert=True)
            return
        ufolder = get_user_folder(owner_id)
        fpath = os.path.join(ufolder, fname)
        if not os.path.exists(fpath):
            bot.answer_callback_query(call.id, "File not found!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Starting...")
        ext = os.path.splitext(fname)[1].lower()
        if ext == ".js":
            threading.Thread(target=run_js_script, args=(fpath, owner_id, ufolder, fname, call.message)).start()
        else:
            threading.Thread(target=run_script, args=(fpath, owner_id, ufolder, fname, call.message)).start()

    elif data.startswith("stop_"):
        _, owner_id, fname = data.split("_", 2)
        skey = f"{owner_id}_{fname}"
        if skey in bot_scripts:
            kill_process_tree(bot_scripts[skey])
            del bot_scripts[skey]
        manually_stopped_scripts.add(skey)
        bot.answer_callback_query(call.id, "Stopped!")
        bot.send_message(call.message.chat.id, f"🛑 Script `{esc_md(fname)}` stopped.", parse_mode="Markdown")

    elif data.startswith("del_"):
        _, owner_id, fname = data.split("_", 2)
        skey = f"{owner_id}_{fname}"
        if skey in bot_scripts:
            kill_process_tree(bot_scripts[skey])
            del bot_scripts[skey]
        manually_stopped_scripts.add(skey)
        remove_user_file_db(int(owner_id), fname)
        ufolder = get_user_folder(int(owner_id))
        fpath = os.path.join(ufolder, fname)
        if os.path.exists(fpath):
            os.remove(fpath)
        bot.answer_callback_query(call.id, "Deleted!")
        bot.send_message(call.message.chat.id, f"🗑️ File `{esc_md(fname)}` deleted.", parse_mode="Markdown")


def process_binance_txid(message, plan_type, plan_id):
    pay_order_id = message.text.strip()
    user_id = message.from_user.id

    if plan_type == "pkg":
        plan = get_plan_by_id(plan_id)
        if not plan:
            bot.reply_to(message, "❌ প্যাকেজ পাওয়া যায়নি!", reply_markup=create_reply_keyboard_main_menu(user_id))
            return
        _, name, file_limit, storage_mb, price, duration, _ = plan
        plan_label = "Package"
    else:
        plan = get_subscription_plan_by_id(plan_id)
        if not plan:
            bot.reply_to(message, "❌ সাবস্ক্রিপশন প্ল্যান পাওয়া যায়নি!", reply_markup=create_reply_keyboard_main_menu(user_id))
            return
        _, name, storage_mb, price, duration = plan
        file_limit = None
        plan_label = "Subscription"

    usdt_price, formatted_price = parse_price_to_usdt(price)

    if is_txid_used(pay_order_id):
        bot.reply_to(message, "❌ *এই Order ID / Transaction ID টি ইতিপূর্বেই ব্যবহার করা হয়েছে!*", reply_markup=create_reply_keyboard_main_menu(user_id), parse_mode="Markdown")
        return

    wait_msg = bot.reply_to(message, "⏳ *ভেরিফাই করা হচ্ছে, অনুগ্রহ করে অপেক্ষা করুন...*", parse_mode="Markdown")
    is_valid, paid_amount_new, amount_or_error = check_binance_payment(pay_order_id)

    if is_valid:
        add_used_txid(pay_order_id)
        already_paid = get_pending_payment(user_id, plan_type, plan_id)
        total_paid = round(already_paid + paid_amount_new, 2)

        if total_paid < usdt_price:
            remaining = round(usdt_price - total_paid, 2)
            update_pending_payment(user_id, plan_type, plan_id, total_paid)
            bot.edit_message_text(
                f"⚠️ *পেমেন্ট অসম্পূর্ণ!*\n\n📌 *Plan:* `{esc_md(name)}`\n💰 *মোট দাম:* `{usdt_price} USDT`\n"
                f"✅ *জমা হয়েছে:* `{total_paid} USDT`\n❌ *বাকি:* `{remaining} USDT`\n\n"
                f"বাকি টাকা পাঠিয়ে নতুন Order ID পুনরায় জমা দিন।",
                message.chat.id, wait_msg.message_id, parse_mode="Markdown")
            bot.send_message(message.chat.id, "🔙", reply_markup=create_reply_keyboard_main_menu(user_id))
            return

        clear_pending_payment(user_id, plan_type, plan_id)
        name, expiry, grant_id = _activate_plan(user_id, plan_type, plan_id, plan, source="purchase_binance")
        log_purchase(user_id, plan_type, name, total_paid, "binance")

        bot.edit_message_text(
            f"🎉 *পেমেন্ট সফলভাবে ভেরিফাই হয়েছে!*\n\n👤 *User ID:* `{user_id}`\n🏷️ *{plan_label}:* `{esc_md(name)}`\n"
            f"🆔 *ID:* `#{grant_id}`\n💰 *Total Paid:* `{total_paid} USDT`\n📅 *Expiry:* `{expiry.strftime('%Y-%m-%d %H:%M')}`\n\n🚀 চালু হয়েছে!",
            message.chat.id, wait_msg.message_id, parse_mode="Markdown")
        bot.send_message(message.chat.id, "🔙", reply_markup=create_reply_keyboard_main_menu(user_id))

        bot.send_message(OWNER_ID, f"🔔 *New {plan_label} Purchase via Binance Pay!*\n👤 User: `{user_id}`\n🏷️ {plan_label}: `{esc_md(name)}`\n🆔 ID: `#{grant_id}`\n📑 Order ID: `{esc_md(pay_order_id)}`\n💰 Amount: `{total_paid} USDT`", parse_mode="Markdown")
    else:
        bot.edit_message_text(f"❌ *পেমেন্ট ভেরিফাই করা সম্ভব হয়নি!*\n\n⚠️ *কারণ:* `{esc_md(amount_or_error)}`", message.chat.id, wait_msg.message_id, parse_mode="Markdown")
        bot.send_message(message.chat.id, "🔙", reply_markup=create_reply_keyboard_main_menu(user_id))


@bot.message_handler(commands=["start"])
def start_cmd(message):
    referrer_id = None
    if message.text and len(message.text.split()) > 1:
        payload = message.text.split(maxsplit=1)[1].strip()
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload.replace("ref_", "", 1))
            except ValueError:
                referrer_id = None
    _logic_send_welcome(message, referrer_id=referrer_id)


@bot.message_handler(commands=["language"])
def language_cmd(message):
    _logic_language_menu(message)


# --- Cleanup & Start ---
def cleanup():
    for key in list(bot_scripts.keys()):
        kill_process_tree(bot_scripts[key])


atexit.register(cleanup)

if __name__ == "__main__":
    logger.info("🤖 Starting Bot (i18n, Deposit System, Wallet-Pay, Settings, Free-Hours Pool)...")
    try:
        BOT_USERNAME = bot.get_me().username
    except Exception as e:
        logger.error(f"❌ Could not fetch bot username for referral links: {e}")
    keep_alive()
    threading.Thread(target=backup_scheduler_loop, daemon=True).start()
    threading.Thread(target=free_hours_scheduler_loop, daemon=True).start()
    threading.Thread(target=auto_restart_scheduler_loop, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)

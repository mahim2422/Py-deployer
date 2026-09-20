"""
Telegram Earn & Withdraw Bot — single-file build.

Python 3.12+ / aiogram 3.x / MongoDB (auto-fallback to SQLite) / xRocket Pay API.
Everything is controlled from Telegram - no web admin panel.

Run:
    pip install -r requirements.txt
    cp .env.example .env   (or just set the environment variables below manually)
    python bot.py

Required environment variables (put them in a .env file next to this script):
    BOT_TOKEN=...              Telegram bot token from @BotFather
    ADMIN_ID=...                Your Telegram numeric user ID
    XROCKET_API_TOKEN=...       From @xrocket bot -> Rocket Pay -> Create App -> API token

Optional environment variables (sensible defaults are used if omitted):
    ADMIN_IDS, MONGO_URI, DB_NAME, SQLITE_PATH, XROCKET_WEBHOOK_TOKEN,
    XROCKET_BASE_URL, ENABLE_WEBHOOK_SERVER, WEBHOOK_LISTEN_HOST,
    WEBHOOK_LISTEN_PORT, WEBHOOK_PATH, BACKUP_INTERVAL_HOURS, BACKUP_DIR,
    DEFAULT_MIN_WITHDRAW_POINTS, DEFAULT_CONVERSION_POINTS_UNIT,
    DEFAULT_CONVERSION_USDT_UNIT, DEFAULT_WITHDRAW_CURRENCY,
    DEFAULT_WITHDRAW_NETWORK, DEFAULT_REFERRAL_REWARD, LOG_DIR

NOTE ON THE xROCKET INTEGRATION:
Direct access to xRocket's live Swagger JSON (https://pay.xrocket.tg/api-json)
was blocked by robots.txt while this file was generated. The base URL, the
"Rocket-Pay-Key" auth header, and every request/response field used below
(currency, amount, withdrawalId, network, address, comment, tgUserId,
transferId, minWithdraw, feeWithdraw...) are confirmed directly from
xRocket's own official SDK docs and PyPI package descriptions. The exact
REST *paths* in XROCKET_ENDPOINTS below are a best-effort mapping onto
that confirmed method set - open https://pay.xrocket.tg/api#/ once and
confirm them before processing real payouts (it's a single dict, one place
to fix). Same caveat for the webhook signature header name, assumed to be
`Rocket-Pay-Signature`.

This file is organized top to bottom as:
    1. Imports & environment config
    2. Small shared helpers (logging, formatting)
    3. Database layer (MongoDB / SQLite)
    4. xRocket Pay API client
    5. Keyboards
    6. FSM states
    7. Backup system
    8. User-facing handlers (Tasks, Balance, Referral, Withdraw, History, Help)
    9. Admin panel & admin commands
    10. Application entry point (main)
"""

import asyncio
import json
import logging
import os
import secrets
import signal
import string
import time
import uuid
import zipfile
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()


# ==============================================================================
# CONFIG - environment variables
# ==============================================================================
def _get(key: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _get_decimal(key: str, default: str) -> Decimal:
    raw = _get(key, default)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise RuntimeError(f"Environment variable {key} must be a valid number, got: {raw!r}") from exc


def _get_bool(key: str, default: str = "false") -> bool:
    return _get(key, default).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
BOT_TOKEN: str = _get("BOT_TOKEN", required=True)

_admin_id_raw = _get("ADMIN_ID", required=True)
ADMIN_ID: int = int(_admin_id_raw)

_extra_admins_raw = _get("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {ADMIN_ID}
if _extra_admins_raw:
    for part in _extra_admins_raw.split(","):
        part = part.strip()
        if part:
            ADMIN_IDS.add(int(part))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
MONGO_URI: str = _get("MONGO_URI", "")
DB_NAME: str = _get("DB_NAME", "earn_withdraw_bot")
SQLITE_PATH: str = _get("SQLITE_PATH", "bot_database.sqlite3")

# ---------------------------------------------------------------------------
# xRocket Pay API
# ---------------------------------------------------------------------------
# NOTE ON ENDPOINT PATHS:
# Direct access to the live Swagger/OpenAPI JSON (https://pay.xrocket.tg/api-json)
# was blocked at generation time. The base URL, the auth header name
# ("Rocket-Pay-Key"), and every request/response field used below come
# straight from xRocket's own published client libraries (the official
# TypeScript SDK "xrocket-pay-api-sdk" and the community "aiorocket2" /
# "xrocket" Python packages, whose method signatures mirror the API 1:1).
# Before running this in production, open https://pay.xrocket.tg/api#/
# once and confirm the exact paths in XROCKET_ENDPOINTS below still match —
# they are kept as a single dict specifically so a mismatch is a one-line fix.
XROCKET_API_TOKEN: str = _get("XROCKET_API_TOKEN", required=True)
XROCKET_WEBHOOK_TOKEN: str = _get("XROCKET_WEBHOOK_TOKEN", "")
XROCKET_BASE_URL: str = _get("XROCKET_BASE_URL", "https://pay.xrocket.tg")

XROCKET_ENDPOINTS = {
    "version": "/version",
    "app_info": "/app/info",
    "currencies": "/currencies/available",
    "transfer": "/app/transfer",
    "withdrawal_create": "/app/withdrawal",
    "withdrawal_status": "/app/withdrawal/status/{id}",
}

# ---------------------------------------------------------------------------
# Webhook receiver (NOT an admin panel - just an inbound HTTP listener so
# xRocket can push withdrawal status updates instead of us only polling).
# Leave ENABLE_WEBHOOK_SERVER=false to run the bot in pure long-polling mode
# with zero open ports.
# ---------------------------------------------------------------------------
ENABLE_WEBHOOK_SERVER: bool = _get_bool("ENABLE_WEBHOOK_SERVER", "false")
WEBHOOK_LISTEN_HOST: str = _get("WEBHOOK_LISTEN_HOST", "0.0.0.0")
WEBHOOK_LISTEN_PORT: int = int(_get("WEBHOOK_LISTEN_PORT", "8080"))
WEBHOOK_PATH: str = _get("WEBHOOK_PATH", "/xrocket/webhook")

# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
BACKUP_INTERVAL_HOURS: int = int(_get("BACKUP_INTERVAL_HOURS", "12"))
BACKUP_DIR: str = _get("BACKUP_DIR", "backups")

# ---------------------------------------------------------------------------
# Business defaults (all changeable at runtime from Telegram, these are only
# the values used the very first time the bot starts and creates its
# "settings" document/row).
# ---------------------------------------------------------------------------
DEFAULT_MIN_WITHDRAW_POINTS: Decimal = _get_decimal("DEFAULT_MIN_WITHDRAW_POINTS", "15")
DEFAULT_CONVERSION_POINTS_UNIT: Decimal = _get_decimal("DEFAULT_CONVERSION_POINTS_UNIT", "15")
DEFAULT_CONVERSION_USDT_UNIT: Decimal = _get_decimal("DEFAULT_CONVERSION_USDT_UNIT", "0.01")
DEFAULT_WITHDRAW_CURRENCY: str = _get("DEFAULT_WITHDRAW_CURRENCY", "USDT")
DEFAULT_WITHDRAW_NETWORK: str = _get("DEFAULT_WITHDRAW_NETWORK", "TON")
DEFAULT_REFERRAL_REWARD: Decimal = _get_decimal("DEFAULT_REFERRAL_REWARD", "1")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR: str = _get("LOG_DIR", "logs")


# ==============================================================================
# SHARED HELPERS - logging, referral codes, decimal formatting
# ==============================================================================
_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger that writes to logs/<name>.log (rotating, 5MB x 3) and
    to stdout. Valid names used across the project: errors, withdrawals,
    webhooks, backups, bot.
    """
    if name in _LOGGERS:
        return _LOGGERS[name]

    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(f"earnbot.{name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_path = os.path.join(LOG_DIR, f"{name}.log")
        file_handler = RotatingFileHandler(
            file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        error_path = os.path.join(LOG_DIR, "errors.log")
        error_handler = RotatingFileHandler(
            error_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        error_handler.setFormatter(fmt)
        error_handler.setLevel(logging.ERROR)
        logger.addHandler(error_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

    _LOGGERS[name] = logger
    return logger


_ALPHABET = string.ascii_uppercase + string.digits


def generate_referral_code(length: int = 8) -> str:
    """Short, URL-safe, human-typeable referral code."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def to_decimal(value) -> Decimal:
    """Safely coerce a value (str/int/float/Decimal/None) into a Decimal."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def format_points(value) -> str:
    d = to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if d == d.to_integral_value():
        return str(d.to_integral_value())
    return str(d)


def format_amount(value, digits: int = 8) -> str:
    quant = Decimal(1).scaleb(-digits)
    d = to_decimal(value).quantize(quant, rounding=ROUND_DOWN)
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def build_referral_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{code}"


# ==============================================================================
# DATABASE LAYER - MongoDB (auto) / SQLite (fallback)
# ==============================================================================
log = get_logger("bot")

SCALE = 100  # 2 decimal places of precision for points/amounts


def scale(value) -> int:
    if value is None:
        return 0
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return int((value * SCALE).to_integral_value())


def unscale(value: int) -> Decimal:
    return Decimal(int(value or 0)) / SCALE


class Database:
    def __init__(self, mongo_uri: str, db_name: str, sqlite_path: str):
        self._mongo_uri = mongo_uri
        self._db_name = db_name
        self._sqlite_path = sqlite_path
        self.backend: str = "sqlite"
        self._mongo_client = None
        self._db = None  # motor database handle
        self._sqlite: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # connection / bootstrap
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        if self._mongo_uri:
            connected = await self._try_connect_mongo()
            if connected:
                self.backend = "mongo"
                await self._init_mongo_indexes()
                await self._ensure_settings_mongo()
                log.info("Database backend: MongoDB (%s)", self._db_name)
                return
            log.info("MongoDB unavailable, falling back to SQLite.")

        self.backend = "sqlite"
        self._sqlite = await aiosqlite.connect(self._sqlite_path)
        self._sqlite.row_factory = aiosqlite.Row
        await self._sqlite.execute("PRAGMA journal_mode=WAL;")
        await self._sqlite.execute("PRAGMA foreign_keys=ON;")
        await self._init_sqlite_schema()
        await self._ensure_settings_sqlite()
        log.info("Database backend: SQLite (%s)", self._sqlite_path)

    async def _try_connect_mongo(self) -> bool:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            client = AsyncIOMotorClient(self._mongo_uri, serverSelectionTimeoutMS=3000)
            await client.admin.command("ping")
            self._mongo_client = client
            self._db = client[self._db_name]
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "use sqlite"
            log.warning("MongoDB connection failed (%s): %s", type(exc).__name__, exc)
            return False

    async def close(self) -> None:
        if self.backend == "mongo" and self._mongo_client:
            self._mongo_client.close()
        elif self.backend == "sqlite" and self._sqlite:
            await self._sqlite.close()

    async def _init_mongo_indexes(self) -> None:
        db = self._db
        await db.users.create_index("user_id", unique=True)
        await db.users.create_index("referral_code", unique=True)
        await db.tasks.create_index("task_id", unique=True)
        await db.task_completions.create_index([("user_id", 1), ("task_id", 1)], unique=True)
        await db.transactions.create_index([("user_id", 1), ("created_at", -1)])
        await db.withdrawals.create_index("xrocket_withdrawal_id", unique=True)
        await db.withdrawals.create_index([("user_id", 1), ("created_at", -1)])
        await db.counters.create_index("name", unique=True)

    async def _init_sqlite_schema(self) -> None:
        await self._sqlite.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                referral_code TEXT UNIQUE NOT NULL,
                referred_by INTEGER,
                referral_count INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                joined_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                title TEXT,
                reward INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_completions (
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                completed_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, task_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tx_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                description TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_points INTEGER NOT NULL,
                amount_currency TEXT NOT NULL,
                currency TEXT NOT NULL,
                network TEXT NOT NULL,
                address TEXT NOT NULL,
                comment TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                xrocket_withdrawal_id TEXT UNIQUE NOT NULL,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                min_withdraw_points INTEGER NOT NULL,
                conversion_points_unit INTEGER NOT NULL,
                conversion_usdt_unit INTEGER NOT NULL,
                withdrawals_enabled INTEGER NOT NULL DEFAULT 1,
                referral_reward INTEGER NOT NULL,
                withdraw_currency TEXT NOT NULL,
                withdraw_network TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_wd_user ON withdrawals(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_wd_status ON withdrawals(status);
            """
        )
        await self._sqlite.commit()

    async def _ensure_settings_sqlite(self) -> None:

        cur = await self._sqlite.execute("SELECT 1 FROM settings WHERE id = 1")
        row = await cur.fetchone()
        if row is None:
            await self._sqlite.execute(
                """INSERT INTO settings
                   (id, min_withdraw_points, conversion_points_unit, conversion_usdt_unit,
                    withdrawals_enabled, referral_reward, withdraw_currency, withdraw_network)
                   VALUES (1, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    scale(DEFAULT_MIN_WITHDRAW_POINTS),
                    scale(DEFAULT_CONVERSION_POINTS_UNIT),
                    scale(DEFAULT_CONVERSION_USDT_UNIT),
                    scale(DEFAULT_REFERRAL_REWARD),
                    DEFAULT_WITHDRAW_CURRENCY,
                    DEFAULT_WITHDRAW_NETWORK,
                ),
            )
            await self._sqlite.commit()

    async def _ensure_settings_mongo(self) -> None:

        existing = await self._db.settings.find_one({"_id": "singleton"})
        if existing is None:
            await self._db.settings.insert_one(
                {
                    "_id": "singleton",
                    "min_withdraw_points": scale(DEFAULT_MIN_WITHDRAW_POINTS),
                    "conversion_points_unit": scale(DEFAULT_CONVERSION_POINTS_UNIT),
                    "conversion_usdt_unit": scale(DEFAULT_CONVERSION_USDT_UNIT),
                    "withdrawals_enabled": True,
                    "referral_reward": scale(DEFAULT_REFERRAL_REWARD),
                    "withdraw_currency": DEFAULT_WITHDRAW_CURRENCY,
                    "withdraw_network": DEFAULT_WITHDRAW_NETWORK,
                }
            )

    async def _next_counter(self, name: str) -> int:
        """Mongo-only helper: auto-increment style integer IDs for tasks/withdrawals."""
        doc = await self._db.counters.find_one_and_update(
            {"name": name},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=True,
        )
        return int(doc["value"])

    # ------------------------------------------------------------------
    # USERS
    # ------------------------------------------------------------------
    async def get_user(self, user_id: int) -> Optional[dict]:
        if self.backend == "mongo":
            return await self._db.users.find_one({"user_id": user_id})
        cur = await self._sqlite.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_user_by_referral_code(self, code: str) -> Optional[dict]:
        if self.backend == "mongo":
            return await self._db.users.find_one({"referral_code": code})
        cur = await self._sqlite.execute("SELECT * FROM users WHERE referral_code = ?", (code,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_or_create_user(
        self, user_id: int, username: str | None, first_name: str | None, referral_code: str
    ) -> tuple[dict, bool]:
        """Returns (user_doc, created). Does not set referred_by - call set_referrer separately."""
        existing = await self.get_user(user_id)
        if existing:
            await self.update_profile(user_id, username, first_name)
            existing = await self.get_user(user_id)
            return existing, False

        now = int(time.time())
        if self.backend == "mongo":
            doc = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "balance": 0,
                "referral_code": referral_code,
                "referred_by": None,
                "referral_count": 0,
                "is_banned": False,
                "joined_at": now,
            }
            await self._db.users.insert_one(doc)
            return doc, True

        await self._sqlite.execute(
            """INSERT INTO users (user_id, username, first_name, balance, referral_code,
                                   referred_by, referral_count, is_banned, joined_at)
               VALUES (?, ?, ?, 0, ?, NULL, 0, 0, ?)""",
            (user_id, username, first_name, referral_code, now),
        )
        await self._sqlite.commit()
        return await self.get_user(user_id), True

    async def update_profile(self, user_id: int, username: str | None, first_name: str | None) -> None:
        if self.backend == "mongo":
            await self._db.users.update_one(
                {"user_id": user_id}, {"$set": {"username": username, "first_name": first_name}}
            )
        else:
            await self._sqlite.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
            await self._sqlite.commit()

    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """Sets referred_by only if not already set. Returns True if it was set now."""
        if self.backend == "mongo":
            result = await self._db.users.update_one(
                {"user_id": user_id, "referred_by": None},
                {"$set": {"referred_by": referrer_id}},
            )
            return result.modified_count == 1

        cur = await self._sqlite.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
            (referrer_id, user_id),
        )
        await self._sqlite.commit()
        return cur.rowcount == 1

    async def increment_referral_count(self, user_id: int) -> None:
        if self.backend == "mongo":
            await self._db.users.update_one({"user_id": user_id}, {"$inc": {"referral_count": 1}})
        else:
            await self._sqlite.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (user_id,)
            )
            await self._sqlite.commit()

    async def set_ban(self, user_id: int, banned: bool) -> None:
        if self.backend == "mongo":
            await self._db.users.update_one({"user_id": user_id}, {"$set": {"is_banned": banned}})
        else:
            await self._sqlite.execute(
                "UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id)
            )
            await self._sqlite.commit()

    async def count_users(self) -> int:
        if self.backend == "mongo":
            return await self._db.users.count_documents({})
        cur = await self._sqlite.execute("SELECT COUNT(*) AS c FROM users")
        row = await cur.fetchone()
        return int(row["c"])

    async def list_all_user_ids(self) -> list[int]:
        if self.backend == "mongo":
            cursor = self._db.users.find({}, {"user_id": 1, "_id": 0})
            return [doc["user_id"] async for doc in cursor]
        cur = await self._sqlite.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    async def sum_balances(self) -> Decimal:
        if self.backend == "mongo":
            pipeline = [{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]
            result = await self._db.users.aggregate(pipeline).to_list(length=1)
            return unscale(result[0]["total"]) if result else Decimal("0")
        cur = await self._sqlite.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users")
        row = await cur.fetchone()
        return unscale(row["s"])

    # ------------------------------------------------------------------
    # BALANCE / TRANSACTIONS (atomic, never goes negative)
    # ------------------------------------------------------------------
    async def adjust_balance(
        self, user_id: int, delta: Decimal, tx_type: str, description: str = ""
    ) -> Optional[Decimal]:
        """
        Atomically applies `delta` (may be negative) to the user's balance.
        Returns the new balance on success, or None if the update would have
        made the balance negative (insufficient funds - nothing is changed).
        """
        delta_scaled = scale(delta)
        now = int(time.time())

        if self.backend == "mongo":
            doc = await self._db.users.find_one_and_update(
                {
                    "user_id": user_id,
                    "$expr": {"$gte": [{"$add": ["$balance", delta_scaled]}, 0]},
                },
                {"$inc": {"balance": delta_scaled}},
                return_document=True,
            )
            if doc is None:
                return None
            new_balance = doc["balance"]
            await self._db.transactions.insert_one(
                {
                    "user_id": user_id,
                    "tx_type": tx_type,
                    "amount": delta_scaled,
                    "balance_after": new_balance,
                    "description": description,
                    "created_at": now,
                }
            )
            return unscale(new_balance)

        cur = await self._sqlite.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ? AND balance + ? >= 0",
            (delta_scaled, user_id, delta_scaled),
        )
        await self._sqlite.commit()
        if cur.rowcount == 0:
            return None
        row = await (await self._sqlite.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )).fetchone()
        new_balance = row["balance"]
        await self._sqlite.execute(
            """INSERT INTO transactions (user_id, tx_type, amount, balance_after, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, tx_type, delta_scaled, new_balance, description, now),
        )
        await self._sqlite.commit()
        return unscale(new_balance)

    async def get_transactions(self, user_id: int, limit: int = 15) -> list[dict]:
        if self.backend == "mongo":
            cursor = self._db.transactions.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
            return [self._tx_out(doc) async for doc in cursor]
        cur = await self._sqlite.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [self._tx_out(dict(r)) for r in rows]

    @staticmethod
    def _tx_out(doc: dict) -> dict:
        return {
            "tx_type": doc["tx_type"],
            "amount": unscale(doc["amount"]),
            "balance_after": unscale(doc["balance_after"]),
            "description": doc.get("description") or "",
            "created_at": doc["created_at"],
        }

    # ------------------------------------------------------------------
    # TASKS
    # ------------------------------------------------------------------
    async def add_task(self, task_type: str, chat_id: str, title: str, reward: Decimal) -> int:
        now = int(time.time())
        if self.backend == "mongo":
            task_id = await self._next_counter("tasks")
            await self._db.tasks.insert_one(
                {
                    "task_id": task_id,
                    "task_type": task_type,
                    "chat_id": chat_id,
                    "title": title,
                    "reward": scale(reward),
                    "is_active": True,
                    "created_at": now,
                }
            )
            return task_id
        cur = await self._sqlite.execute(
            """INSERT INTO tasks (task_type, chat_id, title, reward, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (task_type, chat_id, title, scale(reward), now),
        )
        await self._sqlite.commit()
        return cur.lastrowid

    async def remove_task(self, task_id: int) -> None:
        if self.backend == "mongo":
            await self._db.tasks.delete_one({"task_id": task_id})
        else:
            await self._sqlite.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            await self._sqlite.commit()

    async def set_task_active(self, task_id: int, active: bool) -> None:
        if self.backend == "mongo":
            await self._db.tasks.update_one({"task_id": task_id}, {"$set": {"is_active": active}})
        else:
            await self._sqlite.execute(
                "UPDATE tasks SET is_active = ? WHERE task_id = ?", (1 if active else 0, task_id)
            )
            await self._sqlite.commit()

    async def set_task_reward(self, task_id: int, reward: Decimal) -> None:
        if self.backend == "mongo":
            await self._db.tasks.update_one({"task_id": task_id}, {"$set": {"reward": scale(reward)}})
        else:
            await self._sqlite.execute(
                "UPDATE tasks SET reward = ? WHERE task_id = ?", (scale(reward), task_id)
            )
            await self._sqlite.commit()

    async def get_task(self, task_id: int) -> Optional[dict]:
        if self.backend == "mongo":
            return await self._db.tasks.find_one({"task_id": task_id})
        cur = await self._sqlite.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_tasks(self, active_only: bool = False) -> list[dict]:
        if self.backend == "mongo":
            query = {"is_active": True} if active_only else {}
            cursor = self._db.tasks.find(query).sort("task_id", 1)
            return [doc async for doc in cursor]
        query = "SELECT * FROM tasks"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY task_id"
        cur = await self._sqlite.execute(query)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # TASK COMPLETIONS
    # ------------------------------------------------------------------
    async def has_completed_task(self, user_id: int, task_id: int) -> bool:
        if self.backend == "mongo":
            doc = await self._db.task_completions.find_one({"user_id": user_id, "task_id": task_id})
            return doc is not None
        cur = await self._sqlite.execute(
            "SELECT 1 FROM task_completions WHERE user_id = ? AND task_id = ?", (user_id, task_id)
        )
        row = await cur.fetchone()
        return row is not None

    async def mark_task_completed(self, user_id: int, task_id: int) -> bool:
        """Race-safe: returns False if this (user, task) pair was already rewarded."""
        now = int(time.time())
        if self.backend == "mongo":
            try:
                await self._db.task_completions.insert_one(
                    {"user_id": user_id, "task_id": task_id, "completed_at": now}
                )
                return True
            except Exception as exc:  # noqa: BLE001
                if "duplicate key" in str(exc).lower():
                    return False
                raise
        try:
            await self._sqlite.execute(
                "INSERT INTO task_completions (user_id, task_id, completed_at) VALUES (?, ?, ?)",
                (user_id, task_id, now),
            )
            await self._sqlite.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    # ------------------------------------------------------------------
    # WITHDRAWALS
    # ------------------------------------------------------------------
    async def has_pending_withdrawal(self, user_id: int) -> bool:
        if self.backend == "mongo":
            doc = await self._db.withdrawals.find_one(
                {"user_id": user_id, "status": {"$in": ["pending", "processing"]}}
            )
            return doc is not None
        cur = await self._sqlite.execute(
            "SELECT 1 FROM withdrawals WHERE user_id = ? AND status IN ('pending','processing')",
            (user_id,),
        )
        row = await cur.fetchone()
        return row is not None

    async def create_withdrawal(
        self,
        user_id: int,
        amount_points: Decimal,
        amount_currency: str,
        currency: str,
        network: str,
        address: str,
        comment: str,
        xrocket_withdrawal_id: str,
    ) -> int:
        now = int(time.time())
        if self.backend == "mongo":
            wid = await self._next_counter("withdrawals")
            await self._db.withdrawals.insert_one(
                {
                    "id": wid,
                    "user_id": user_id,
                    "amount_points": scale(amount_points),
                    "amount_currency": amount_currency,
                    "currency": currency,
                    "network": network,
                    "address": address,
                    "comment": comment,
                    "status": "pending",
                    "xrocket_withdrawal_id": xrocket_withdrawal_id,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            return wid
        cur = await self._sqlite.execute(
            """INSERT INTO withdrawals
               (user_id, amount_points, amount_currency, currency, network, address, comment,
                status, xrocket_withdrawal_id, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?)""",
            (
                user_id,
                scale(amount_points),
                amount_currency,
                currency,
                network,
                address,
                comment,
                xrocket_withdrawal_id,
                now,
                now,
            ),
        )
        await self._sqlite.commit()
        return cur.lastrowid

    async def update_withdrawal_status(
        self, xrocket_withdrawal_id: str, status: str, error: str | None = None
    ) -> None:
        now = int(time.time())
        if self.backend == "mongo":
            await self._db.withdrawals.update_one(
                {"xrocket_withdrawal_id": xrocket_withdrawal_id},
                {"$set": {"status": status, "error": error, "updated_at": now}},
            )
        else:
            await self._sqlite.execute(
                "UPDATE withdrawals SET status = ?, error = ?, updated_at = ? WHERE xrocket_withdrawal_id = ?",
                (status, error, now, xrocket_withdrawal_id),
            )
            await self._sqlite.commit()

    async def get_withdrawal_by_xrocket_id(self, xrocket_withdrawal_id: str) -> Optional[dict]:
        if self.backend == "mongo":
            return await self._db.withdrawals.find_one({"xrocket_withdrawal_id": xrocket_withdrawal_id})
        cur = await self._sqlite.execute(
            "SELECT * FROM withdrawals WHERE xrocket_withdrawal_id = ?", (xrocket_withdrawal_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_withdrawals(self, status: str | None = None, limit: int = 20) -> list[dict]:
        if self.backend == "mongo":
            query = {"status": status} if status else {}
            cursor = self._db.withdrawals.find(query).sort("created_at", -1).limit(limit)
            return [doc async for doc in cursor]
        if status:
            cur = await self._sqlite.execute(
                "SELECT * FROM withdrawals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = await self._sqlite.execute(
                "SELECT * FROM withdrawals ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_withdrawal_stats(self) -> dict:
        statuses = ["pending", "processing", "completed", "failed"]
        out = {}
        if self.backend == "mongo":
            for s in statuses:
                out[s] = await self._db.withdrawals.count_documents({"status": s})
            return out
        for s in statuses:
            cur = await self._sqlite.execute(
                "SELECT COUNT(*) AS c FROM withdrawals WHERE status = ?", (s,)
            )
            row = await cur.fetchone()
            out[s] = int(row["c"])
        return out

    # ------------------------------------------------------------------
    # SETTINGS
    # ------------------------------------------------------------------
    async def get_settings(self) -> dict:
        if self.backend == "mongo":
            doc = await self._db.settings.find_one({"_id": "singleton"})
            return self._settings_out(doc)
        cur = await self._sqlite.execute("SELECT * FROM settings WHERE id = 1")
        row = await cur.fetchone()
        return self._settings_out(dict(row))

    @staticmethod
    def _settings_out(doc: dict) -> dict:
        return {
            "min_withdraw_points": unscale(doc["min_withdraw_points"]),
            "conversion_points_unit": unscale(doc["conversion_points_unit"]),
            "conversion_usdt_unit": unscale(doc["conversion_usdt_unit"]),
            "withdrawals_enabled": bool(doc["withdrawals_enabled"]),
            "referral_reward": unscale(doc["referral_reward"]),
            "withdraw_currency": doc["withdraw_currency"],
            "withdraw_network": doc["withdraw_network"],
        }

    async def update_settings(self, **kwargs: Any) -> None:
        scaled_fields = {
            "min_withdraw_points",
            "conversion_points_unit",
            "conversion_usdt_unit",
            "referral_reward",
        }
        to_set: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in scaled_fields:
                to_set[key] = scale(value)
            elif key == "withdrawals_enabled":
                to_set[key] = bool(value)
            else:
                to_set[key] = value

        if self.backend == "mongo":
            await self._db.settings.update_one({"_id": "singleton"}, {"$set": to_set})
            return
        if not to_set:
            return
        columns = ", ".join(f"{k} = ?" for k in to_set)
        values = [1 if isinstance(v, bool) else v for v in to_set.values()]
        await self._sqlite.execute(f"UPDATE settings SET {columns} WHERE id = 1", values)
        await self._sqlite.commit()

    # ------------------------------------------------------------------
    # Raw export for backups
    # ------------------------------------------------------------------
    async def export_all(self) -> dict:
        """Dumps every collection/table as plain lists of dicts, for backup.py."""
        if self.backend == "mongo":
            out = {}
            for name in ("users", "tasks", "task_completions", "transactions", "withdrawals"):
                out[name] = await self._db[name].find({}, {"_id": 0}).to_list(length=None)
            settings_doc = await self._db.settings.find_one({"_id": "singleton"}, {"_id": 0})
            out["settings"] = [settings_doc] if settings_doc else []
            return out

        out = {}
        for name in ("users", "tasks", "task_completions", "transactions", "withdrawals", "settings"):
            cur = await self._sqlite.execute(f"SELECT * FROM {name}")
            rows = await cur.fetchall()
            out[name] = [dict(r) for r in rows]
        return out


# ==============================================================================
# XROCKET PAY API CLIENT
# ==============================================================================
log = get_logger("bot")


class XRocketAPIError(Exception):
    """Raised for any non-2xx response from the xRocket Pay API."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"xRocket API error {status}: {payload}")


class XRocketClient:
    def __init__(
        self,
        api_token: str,
        base_url: str = XROCKET_BASE_URL,
        timeout_seconds: int = 30,
        max_retries: int = 3,
    ):
        self._token = api_token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        auth: bool = True,
        idempotent: bool = True,
    ) -> Any:
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Rocket-Pay-Key"] = self._token

        attempt = 0
        last_exc: Exception | None = None
        while attempt < self._max_retries:
            attempt += 1
            try:
                async with session.request(
                    method, url, json=json_body, params=params, headers=headers
                ) as resp:
                    text = await resp.text()
                    data = None
                    if text:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:  # noqa: BLE001
                            data = text
                    if resp.status >= 400:
                        raise XRocketAPIError(resp.status, data)
                    return data
            except XRocketAPIError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                # Financial POST calls (idempotent=False by caller choice) are
                # never silently retried - a timed-out payout must be
                # reconciled via get_payout(), not resent.
                if not idempotent or attempt >= self._max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 8))
        log.error("xRocket request failed after %s attempts: %s %s -> %s", attempt, method, url, last_exc)
        raise XRocketAPIError(0, str(last_exc))

    # ------------------------------------------------------------------
    # Read-only / informational
    # ------------------------------------------------------------------
    async def get_version(self) -> Any:
        return await self._request("GET", XROCKET_ENDPOINTS["version"], auth=False)

    async def get_app_info(self) -> Any:
        return await self._request("GET", XROCKET_ENDPOINTS["app_info"])

    async def get_available_currencies(self) -> list[dict]:
        data = await self._request("GET", XROCKET_ENDPOINTS["currencies"], auth=False)
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        return data or []

    async def get_currency_limits(self, currency: str) -> dict:
        """
        Returns {"min_withdraw": Decimal, "fees": {network_code: (fee_amount, fee_currency)}}
        for one currency, derived from get_available_currencies() (no separate
        fee endpoint is called since its exact path could not be confirmed -
        see module docstring).
        """
        currencies = await self.get_available_currencies()
        for entry in currencies:
            code = entry.get("currency") or entry.get("code")
            if code == currency:
                fees: dict[str, tuple[Decimal, str]] = {}
                fee_withdraw = entry.get("feeWithdraw") or {}
                for net in fee_withdraw.get("networks", []) or []:
                    fw = net.get("feeWithdraw", {})
                    fees[net.get("networkCode")] = (to_decimal(fw.get("fee")), fw.get("currency", currency))
                return {
                    "min_withdraw": to_decimal(entry.get("minWithdraw")),
                    "fees": fees,
                }
        raise XRocketAPIError(0, f"Currency {currency} not found in available currencies")

    # ------------------------------------------------------------------
    # Transfers (app -> Telegram user, internal balance transfer)
    # ------------------------------------------------------------------
    async def create_transfer(
        self, tg_user_id: int, currency: str, amount: Decimal, transfer_id: str, description: str = ""
    ) -> Any:
        body = {
            "tgUserId": tg_user_id,
            "currency": currency,
            "amount": float(amount),
            "transferId": transfer_id,
            "description": description,
        }
        return await self._request(
            "POST", XROCKET_ENDPOINTS["transfer"], json_body=body, idempotent=False
        )

    # ------------------------------------------------------------------
    # Withdrawals (payouts to an external wallet address)
    # ------------------------------------------------------------------
    async def create_payout(
        self,
        currency: str,
        amount: Decimal,
        withdrawal_id: str,
        network: str,
        address: str,
        comment: str = "",
    ) -> Any:
        """
        Creates a withdrawal (payout) to an external address.
        `withdrawal_id` is our own idempotency key (client_withdrawal_id) -
        always generate a fresh, unique one per attempt and persist it
        BEFORE calling this, so a network timeout can be reconciled with
        get_payout() instead of accidentally double-paying.
        """
        body = {
            "currency": currency,
            "amount": float(amount),
            "withdrawalId": withdrawal_id,
            "network": network,
            "address": address,
            "comment": comment,
        }
        return await self._request(
            "POST",
            XROCKET_ENDPOINTS["withdrawal_create"],
            json_body=body,
            idempotent=False,
        )

    async def get_payout(self, withdrawal_id: str) -> Any:
        path = XROCKET_ENDPOINTS["withdrawal_status"].format(id=withdrawal_id)
        return await self._request("GET", path)

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------
    @staticmethod
    def verify_webhook(raw_body: bytes, signature_header: str | None, webhook_token: str) -> bool:
        """
        Verifies an inbound xRocket webhook using HMAC-SHA256 over the raw
        request body, keyed with XROCKET_WEBHOOK_TOKEN - the standard scheme
        xRocket documents for its webhook signature. Confirm the exact
        header name your dashboard sends (commonly `Rocket-Pay-Signature`)
        against your app's webhook settings before relying on this in
        production; the header name is configurable via the caller.
        """
        if not webhook_token or not signature_header:
            return False
        import hashlib
        import hmac

        expected = hmac.new(webhook_token.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header.strip().lower())


# ==============================================================================
# KEYBOARDS
# ==============================================================================
# ---------------------------------------------------------------------------
# USER: main menu
# ---------------------------------------------------------------------------
BTN_TASKS = "📢 Tasks"
BTN_BALANCE = "💰 Balance"
BTN_REFERRAL = "🔗 Referral"
BTN_WITHDRAW = "💸 Withdraw"
BTN_HISTORY = "📜 History"
BTN_HELP = "ℹ️ Help"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_BALANCE)],
            [KeyboardButton(text=BTN_REFERRAL), KeyboardButton(text=BTN_WITHDRAW)],
            [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def task_item_kb(task_id: int, chat_username: str, already_done: bool) -> InlineKeyboardMarkup:
    rows = []
    if not already_done:
        link = f"https://t.me/{chat_username.lstrip('@')}"
        rows.append([InlineKeyboardButton(text="➡️ Open", url=link)])
        rows.append(
            [InlineKeyboardButton(text="✅ Check / Claim", callback_data=f"task_check:{task_id}")]
        )
    else:
        rows.append([InlineKeyboardButton(text="✅ Completed", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_cancel_kb(confirm_data: str, cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data=confirm_data),
                InlineKeyboardButton(text="❌ Cancel", callback_data=cancel_data),
            ]
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]]
    )


# ---------------------------------------------------------------------------
# ADMIN: root panel
# ---------------------------------------------------------------------------
def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
                InlineKeyboardButton(text="📢 Tasks", callback_data="admin:tasks"),
            ],
            [
                InlineKeyboardButton(text="💰 Withdrawals", callback_data="admin:withdrawals"),
                InlineKeyboardButton(text="⚙ Settings", callback_data="admin:settings"),
            ],
            [
                InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats"),
                InlineKeyboardButton(text="💳 xRocket", callback_data="admin:xrocket"),
            ],
            [InlineKeyboardButton(text="💾 Backup", callback_data="admin:backup")],
        ]
    )


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin:root")]]
    )


def admin_tasks_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Add Channel", callback_data="admin:add_channel"),
                InlineKeyboardButton(text="➕ Add Group", callback_data="admin:add_group"),
            ],
            [
                InlineKeyboardButton(text="📋 List Tasks", callback_data="admin:list_tasks"),
                InlineKeyboardButton(text="✏ Change Reward", callback_data="admin:set_reward"),
            ],
            [InlineKeyboardButton(text="❌ Remove Task", callback_data="admin:remove_task")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:root")],
        ]
    )


def admin_task_row_kb(task_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Disable" if is_active else "🟢 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f"admin:toggle_task:{task_id}")],
        ]
    )


def admin_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 Min withdraw", callback_data="admin:set_min_withdraw")],
            [InlineKeyboardButton(text="🔁 Conversion rate", callback_data="admin:set_rate")],
            [InlineKeyboardButton(text="🎁 Referral reward", callback_data="admin:set_referral_reward")],
            [InlineKeyboardButton(text="🔀 Toggle withdrawals", callback_data="admin:toggle_withdrawals")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:root")],
        ]
    )


def admin_users_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚫 Ban user", callback_data="admin:ban_user"),
                InlineKeyboardButton(text="✅ Unban user", callback_data="admin:unban_user"),
            ],
            [InlineKeyboardButton(text="📣 Broadcast", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:root")],
        ]
    )


# ==============================================================================
# FSM STATES
# ==============================================================================
class AdminAddChannel(StatesGroup):
    waiting_chat_id = State()
    waiting_reward = State()


class AdminAddGroup(StatesGroup):
    waiting_chat_id = State()
    waiting_reward = State()


class AdminRemoveTask(StatesGroup):
    waiting_task_id = State()


class AdminSetReward(StatesGroup):
    waiting_task_id = State()
    waiting_value = State()


class AdminSetWithdraw(StatesGroup):
    waiting_min_points = State()


class AdminSetRate(StatesGroup):
    waiting_points_unit = State()
    waiting_usdt_unit = State()


class AdminSetReferralReward(StatesGroup):
    waiting_value = State()


class AdminBanUser(StatesGroup):
    waiting_user_id = State()


class AdminUnbanUser(StatesGroup):
    waiting_user_id = State()


class AdminBroadcast(StatesGroup):
    waiting_message = State()


class WithdrawFlow(StatesGroup):
    waiting_address = State()
    waiting_confirmation = State()


# ==============================================================================
# BACKUP SYSTEM
# ==============================================================================
log = get_logger("backups")

_PROJECT_FILES = [
    "bot.py",
    "requirements.txt",
    ".env.example",
]


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):  # noqa: D102
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


async def _build_backup_zip(db: Database) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M")
    zip_path = os.path.join(BACKUP_DIR, f"backup_{timestamp}.zip")

    data = await db.export_all()
    export_json = json.dumps(data, indent=2, cls=_DecimalEncoder, default=str)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"database_export_{timestamp}.json", export_json)

        for filename in _PROJECT_FILES:
            if os.path.exists(filename):
                zf.write(filename, arcname=f"project/{filename}")

        if os.path.isdir(LOG_DIR):
            for fname in os.listdir(LOG_DIR):
                full = os.path.join(LOG_DIR, fname)
                if os.path.isfile(full):
                    zf.write(full, arcname=f"logs/{fname}")

    return zip_path


async def run_backup_now(bot: Bot, db: Database) -> None:
    try:
        zip_path = await _build_backup_zip(db)
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        log.info("Backup created: %s (%.2f MB)", zip_path, size_mb)

        if size_mb > 49:
            log.warning("Backup %s exceeds Telegram's 50MB limit, sending anyway may fail.", zip_path)

        await bot.send_document(
            ADMIN_ID,
            FSInputFile(zip_path),
            caption=f"💾 Backup — {os.path.basename(zip_path)} ({size_mb:.2f} MB)",
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Backup failed: %s", exc)
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ Automatic backup FAILED: {exc}")
        except Exception:  # noqa: BLE001
            pass


def setup_scheduler(bot: Bot, db: Database) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_backup_now,
        "interval",
        hours=BACKUP_INTERVAL_HOURS,
        args=[bot, db],
        id="auto_backup",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Backup scheduler started: every %s hour(s)", BACKUP_INTERVAL_HOURS)
    return scheduler


# ==============================================================================
# USER HANDLERS
# ==============================================================================
log = get_logger("bot")
user_router = Router(name="user")


def _display_name(message: Message) -> str:
    return message.from_user.full_name or message.from_user.username or str(message.from_user.id)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@user_router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    user_id = message.from_user.id
    referral_code = generate_referral_code()

    payload = None
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()

    user, created = await db.get_or_create_user(
        user_id, message.from_user.username, message.from_user.first_name, referral_code
    )

    if created and payload and payload.startswith("ref_"):
        ref_code = payload[len("ref_"):]
        referrer = await db.get_user_by_referral_code(ref_code)
        if referrer and referrer["user_id"] != user_id:
            was_set = await db.set_referrer(user_id, referrer["user_id"])
            if was_set:
                settings = await db.get_settings()
                reward = settings["referral_reward"]
                new_balance = await db.adjust_balance(
                    referrer["user_id"], reward, "referral", f"Referral bonus for user {user_id}"
                )
                if new_balance is not None:
                    await db.increment_referral_count(referrer["user_id"])
                    try:
                        await message.bot.send_message(
                            referrer["user_id"],
                            f"🎉 Someone joined using your referral link!\n"
                            f"+{format_points(reward)} points added to your balance.",
                        )
                    except Exception:  # noqa: BLE001 - user may have blocked the bot
                        pass

    if user.get("is_banned"):
        await message.answer("🚫 Your account has been banned. Contact the administrator for details.")
        return

    await message.answer(
        f"👋 Welcome, {_display_name(message)}!\n\n"
        "Complete tasks, invite friends, and earn points you can withdraw as crypto.\n"
        "Use the menu below to get started.",
        reply_markup=main_menu_kb(),
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@user_router.message(F.text == BTN_TASKS)
@user_router.message(Command("tasks"))
async def show_tasks(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)
    if user and user.get("is_banned"):
        return

    tasks = await db.list_tasks(active_only=True)
    if not tasks:
        await message.answer("📢 There are no tasks available right now. Check back later!")
        return

    rows = []
    lines = ["📢 <b>Available Tasks</b>\n"]
    for task in tasks:
        done = await db.has_completed_task(message.from_user.id, task["task_id"])
        reward_text = format_points(unscale(task["reward"]))
        status = "✅ done" if done else f"+{reward_text} pts"
        title = task.get("title") or task["chat_id"]
        lines.append(f"• {title} — {status}")

        if not done:
            chat_username = task["chat_id"].lstrip("@")
            rows.append(
                [
                    {"text": f"➡️ Open {title}", "url": f"https://t.me/{chat_username}"},
                    {"text": "✅ Check", "callback_data": f"task_check:{task['task_id']}"},
                ]
            )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(**btn) for btn in row] for row in rows
        ]
    ) if rows else None

    await message.answer("\n".join(lines), reply_markup=keyboard)


@user_router.callback_query(F.data.startswith("task_check:"))
async def check_task(callback: CallbackQuery, db: Database) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    task = await db.get_task(task_id)
    if not task or not task["is_active"]:
        await callback.answer("This task is no longer available.", show_alert=True)
        return

    already = await db.has_completed_task(callback.from_user.id, task_id)
    if already:
        await callback.answer("You already claimed this task.", show_alert=True)
        return

    try:
        member = await callback.bot.get_chat_member(task["chat_id"], callback.from_user.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("get_chat_member failed for task %s: %s", task_id, exc)
        await callback.answer(
            "Could not verify your membership. Make sure you joined and try again.", show_alert=True
        )
        return

    if member.status not in ("member", "administrator", "creator"):
        await callback.answer("Please join first, then tap Check again.", show_alert=True)
        return

    newly_marked = await db.mark_task_completed(callback.from_user.id, task_id)
    if not newly_marked:
        await callback.answer("You already claimed this task.", show_alert=True)
        return

    reward = unscale(task["reward"])
    title = task.get("title") or task["chat_id"]
    await db.adjust_balance(callback.from_user.id, reward, "task", f"Reward for task: {title}")
    await callback.answer(f"✅ +{format_points(reward)} points added!", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------
@user_router.message(F.text == BTN_BALANCE)
@user_router.message(Command("balance"))
async def show_balance(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Please send /start first.")
        return

    balance = unscale(user["balance"])
    settings = await db.get_settings()
    await message.answer(
        f"💰 <b>Your Balance</b>\n\n"
        f"Points: <b>{format_points(balance)}</b>\n"
        f"Minimum withdrawal: {format_points(settings['min_withdraw_points'])} points\n"
        f"Rate: {format_points(settings['conversion_points_unit'])} points = "
        f"{format_amount(settings['conversion_usdt_unit'])} {settings['withdraw_currency']}"
    )


# ---------------------------------------------------------------------------
# Referral
# ---------------------------------------------------------------------------
@user_router.message(F.text == BTN_REFERRAL)
@user_router.message(Command("referral"))
async def show_referral(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Please send /start first.")
        return
    bot_info = await message.bot.get_me()
    link = build_referral_link(bot_info.username, user["referral_code"])
    settings = await db.get_settings()
    await message.answer(
        f"🔗 <b>Your Referral Link</b>\n\n{link}\n\n"
        f"You earn <b>{format_points(settings['referral_reward'])} points</b> for every friend who joins.\n"
        f"Total referrals: <b>{user.get('referral_count', 0)}</b>"
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
_TX_LABELS = {
    "task": "📢 Task reward",
    "referral": "🔗 Referral bonus",
    "withdraw": "💸 Withdrawal",
    "withdraw_refund": "↩️ Withdrawal refund",
    "admin_adjust": "⚙ Admin adjustment",
}


@user_router.message(F.text == BTN_HISTORY)
@user_router.message(Command("history"))
async def show_history(message: Message, db: Database) -> None:
    txs = await db.get_transactions(message.from_user.id, limit=15)
    if not txs:
        await message.answer("📜 No transactions yet.")
        return
    lines = ["📜 <b>Recent Transactions</b>\n"]
    for tx in txs:
        sign = "+" if tx["amount"] >= 0 else ""
        label = _TX_LABELS.get(tx["tx_type"], tx["tx_type"])
        lines.append(f"{label}: {sign}{format_points(tx['amount'])} pts (bal: {format_points(tx['balance_after'])})")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
@user_router.message(F.text == BTN_HELP)
@user_router.message(Command("help"))
async def show_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>How this bot works</b>\n\n"
        "📢 <b>Tasks</b> — join channels/groups to earn points\n"
        "🔗 <b>Referral</b> — invite friends and earn per signup\n"
        "💰 <b>Balance</b> — check your current points\n"
        "💸 <b>Withdraw</b> — convert points to crypto once you hit the minimum\n"
        "📜 <b>History</b> — see your recent point changes\n\n"
        "Need more help? Contact the administrator."
    )


# ---------------------------------------------------------------------------
# Withdraw flow
# ---------------------------------------------------------------------------
@user_router.message(F.text == BTN_WITHDRAW)
@user_router.message(Command("withdraw"))
async def withdraw_start(message: Message, db: Database, state: FSMContext) -> None:
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Please send /start first.")
        return
    if user.get("is_banned"):
        await message.answer("🚫 Your account is banned and cannot withdraw.")
        return

    settings = await db.get_settings()
    if not settings["withdrawals_enabled"]:
        await message.answer("💸 Withdrawals are currently disabled by the administrator. Please try later.")
        return

    balance = unscale(user["balance"])
    if balance < settings["min_withdraw_points"]:
        await message.answer(
            f"💸 Minimum withdrawal is {format_points(settings['min_withdraw_points'])} points.\n"
            f"Your balance: {format_points(balance)} points."
        )
        return

    if await db.has_pending_withdrawal(message.from_user.id):
        await message.answer("⏳ You already have a withdrawal in progress. Please wait for it to complete.")
        return

    await state.set_state(WithdrawFlow.waiting_address)
    await state.update_data(balance_points=str(balance))
    await message.answer(
        f"💸 Withdrawing <b>{format_points(balance)} points</b> "
        f"({settings['withdraw_network']} network, {settings['withdraw_currency']}).\n\n"
        f"Please send your {settings['withdraw_network']} wallet address, or /cancel to abort.",
        reply_markup=cancel_kb(),
    )


@user_router.message(StateFilter(WithdrawFlow.waiting_address), Command("cancel"))
async def withdraw_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Withdrawal cancelled.", reply_markup=main_menu_kb())


@user_router.message(StateFilter(WithdrawFlow.waiting_address))
async def withdraw_get_address(message: Message, db: Database, state: FSMContext) -> None:
    address = (message.text or "").strip()
    if not address or len(address) < 6 or " " in address:
        await message.answer("That doesn't look like a valid wallet address. Please try again, or /cancel.")
        return

    data = await state.get_data()
    balance = Decimal(data["balance_points"])
    settings = await db.get_settings()

    amount_currency = (balance / settings["conversion_points_unit"]) * settings["conversion_usdt_unit"]

    await state.update_data(address=address, amount_currency=str(amount_currency))
    await state.set_state(WithdrawFlow.waiting_confirmation)
    await message.answer(
        "📋 <b>Confirm withdrawal</b>\n\n"
        f"Amount: {format_points(balance)} points\n"
        f"You'll receive: ~{format_amount(amount_currency)} {settings['withdraw_currency']}\n"
        f"Network: {settings['withdraw_network']}\n"
        f"Address: <code>{address}</code>\n\n"
        "Double-check the address — crypto withdrawals cannot be reversed.",
        reply_markup=confirm_cancel_kb("withdraw_confirm"),
    )


@user_router.callback_query(StateFilter(WithdrawFlow.waiting_confirmation), F.data == "cancel")
async def withdraw_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Withdrawal cancelled.")
    await callback.answer()


@user_router.callback_query(StateFilter(WithdrawFlow.waiting_confirmation), F.data == "withdraw_confirm")
async def withdraw_confirm(
    callback: CallbackQuery, db: Database, xrocket: XRocketClient, state: FSMContext
) -> None:
    data = await state.get_data()
    await state.clear()

    user_id = callback.from_user.id
    balance = Decimal(data["balance_points"])
    amount_currency = Decimal(data["amount_currency"])
    address = data["address"]
    settings = await db.get_settings()

    if await db.has_pending_withdrawal(user_id):
        await callback.message.edit_text("⏳ You already have a withdrawal in progress.")
        await callback.answer()
        return

    new_balance = await db.adjust_balance(user_id, -balance, "withdraw", "Withdrawal request")
    if new_balance is None:
        await callback.message.edit_text("❌ Insufficient balance (it may have changed). Please try again.")
        await callback.answer()
        return

    withdrawal_id = f"wd-{user_id}-{uuid.uuid4().hex[:12]}"
    await db.create_withdrawal(
        user_id=user_id,
        amount_points=balance,
        amount_currency=format_amount(amount_currency),
        currency=settings["withdraw_currency"],
        network=settings["withdraw_network"],
        address=address,
        comment=f"Payout for Telegram user {user_id}",
        xrocket_withdrawal_id=withdrawal_id,
    )

    await callback.message.edit_text("⏳ Processing your withdrawal, please wait...")
    await callback.answer()

    try:
        await xrocket.create_payout(
            currency=settings["withdraw_currency"],
            amount=amount_currency,
            withdrawal_id=withdrawal_id,
            network=settings["withdraw_network"],
            address=address,
            comment=f"Payout for Telegram user {user_id}",
        )
        await db.update_withdrawal_status(withdrawal_id, "processing")
        await callback.message.answer(
            "✅ Withdrawal submitted successfully!\n"
            f"Amount: {format_amount(amount_currency)} {settings['withdraw_currency']}\n"
            "It will arrive shortly depending on network conditions."
        )
    except XRocketAPIError as exc:
        log.error("Withdrawal %s failed: %s", withdrawal_id, exc)
        await db.update_withdrawal_status(withdrawal_id, "failed", error=str(exc))
        await db.adjust_balance(user_id, balance, "withdraw_refund", f"Refund for failed withdrawal {withdrawal_id}")
        await callback.message.answer(
            "❌ The withdrawal could not be processed and your points have been refunded.\n"
            "Please try again later or contact the administrator."
        )
        try:
            await callback.bot.send_message(
                ADMIN_ID, f"⚠️ Withdrawal FAILED for user {user_id}: {withdrawal_id}\n{exc}"
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
@user_router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


# ==============================================================================
# ADMIN HANDLERS
# ==============================================================================
log = get_logger("bot")
admin_router = Router(name="admin")
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


def _parse_decimal(text: str) -> Decimal | None:
    try:
        value = Decimal((text or "").strip())
        if value < 0:
            return None
        return value
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Root panel
# ---------------------------------------------------------------------------
@admin_router.message(Command("admin"))
async def admin_root(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>Admin Panel</b>", reply_markup=admin_menu_kb())


@admin_router.callback_query(F.data == "admin:root")
async def admin_root_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("🛠 <b>Admin Panel</b>", reply_markup=admin_menu_kb())
    await callback.answer()


@admin_router.callback_query(F.data == "cancel")
async def admin_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Cancelled.", reply_markup=back_to_admin_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# TASKS section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:tasks")
async def admin_tasks_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📢 <b>Task Management</b>", reply_markup=admin_tasks_menu_kb())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:add_channel")
async def admin_add_channel_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminAddChannel.waiting_chat_id)
    await state.update_data(task_type="channel")
    await callback.message.edit_text(
        "➕ Send the channel's @username or numeric chat ID (bot must be an admin there).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:add_group")
async def admin_add_group_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminAddChannel.waiting_chat_id)
    await state.update_data(task_type="group")
    await callback.message.edit_text(
        "➕ Send the group's @username or numeric chat ID (bot must be an admin there).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@admin_router.message(StateFilter(AdminAddChannel.waiting_chat_id))
async def admin_add_task_chat_id(message: Message, state: FSMContext) -> None:
    chat_id = (message.text or "").strip()
    if not chat_id:
        await message.answer("Please send a valid @username or chat ID.")
        return
    await state.update_data(chat_id=chat_id)
    await state.set_state(AdminAddChannel.waiting_reward)
    await message.answer("💰 Now send the reward in points for joining (e.g. 1 or 2.5).")


@admin_router.message(StateFilter(AdminAddChannel.waiting_reward))
async def admin_add_task_reward(message: Message, db: Database, state: FSMContext) -> None:
    reward = _parse_decimal(message.text)
    if reward is None:
        await message.answer("Please send a valid non-negative number.")
        return
    data = await state.get_data()
    await state.clear()

    chat_id = data["chat_id"]
    task_type = data["task_type"]
    title = chat_id
    try:
        chat = await message.bot.get_chat(chat_id)
        title = chat.title or chat.username or chat_id
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch chat info for %s: %s", chat_id, exc)

    task_id = await db.add_task(task_type, chat_id, title, reward)
    await message.answer(
        f"✅ Task #{task_id} created: <b>{title}</b> ({task_type}) — +{format_points(reward)} pts",
        reply_markup=back_to_admin_kb(),
    )


@admin_router.callback_query(F.data == "admin:list_tasks")
async def admin_list_tasks(callback: CallbackQuery, db: Database) -> None:
    tasks = await db.list_tasks()
    if not tasks:
        await callback.message.edit_text("No tasks configured yet.", reply_markup=back_to_admin_kb())
        await callback.answer()
        return
    await callback.message.edit_text("📋 <b>Tasks</b>", reply_markup=back_to_admin_kb())
    for task in tasks:
        status = "🟢 active" if task["is_active"] else "🔴 disabled"
        text = (
            f"#{task['task_id']} <b>{task.get('title') or task['chat_id']}</b>\n"
            f"Type: {task['task_type']} | Chat: {task['chat_id']}\n"
            f"Reward: {format_points(unscale(task['reward']))} pts | {status}"
        )
        await callback.message.answer(text, reply_markup=admin_task_row_kb(task["task_id"], task["is_active"]))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:toggle_task:"))
async def admin_toggle_task(callback: CallbackQuery, db: Database) -> None:
    task_id = int(callback.data.split(":")[-1])
    task = await db.get_task(task_id)
    if not task:
        await callback.answer("Task not found.", show_alert=True)
        return
    new_state = not task["is_active"]
    await db.set_task_active(task_id, new_state)
    await callback.answer("Updated." if new_state else "Disabled.")
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_task_row_kb(task_id, new_state))
    except Exception:  # noqa: BLE001
        pass


@admin_router.callback_query(F.data == "admin:remove_task")
async def admin_remove_task_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminRemoveTask.waiting_task_id)
    await callback.message.edit_text("❌ Send the task ID to remove.", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminRemoveTask.waiting_task_id))
async def admin_remove_task_finish(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    try:
        task_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please send a numeric task ID.")
        return
    task = await db.get_task(task_id)
    if not task:
        await message.answer("Task not found.", reply_markup=back_to_admin_kb())
        return
    await db.remove_task(task_id)
    await message.answer(f"✅ Task #{task_id} removed.", reply_markup=back_to_admin_kb())


@admin_router.callback_query(F.data == "admin:set_reward")
async def admin_set_reward_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSetReward.waiting_task_id)
    await callback.message.edit_text("✏ Send the task ID to change the reward for.", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminSetReward.waiting_task_id))
async def admin_set_reward_task(message: Message, db: Database, state: FSMContext) -> None:
    try:
        task_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please send a numeric task ID.")
        return
    task = await db.get_task(task_id)
    if not task:
        await message.answer("Task not found.")
        return
    await state.update_data(task_id=task_id)
    await state.set_state(AdminSetReward.waiting_value)
    await message.answer("Send the new reward value in points.")


@admin_router.message(StateFilter(AdminSetReward.waiting_value))
async def admin_set_reward_value(message: Message, db: Database, state: FSMContext) -> None:
    reward = _parse_decimal(message.text)
    if reward is None:
        await message.answer("Please send a valid non-negative number.")
        return
    data = await state.get_data()
    await state.clear()
    await db.set_task_reward(data["task_id"], reward)
    await message.answer(
        f"✅ Task #{data['task_id']} reward updated to {format_points(reward)} pts.",
        reply_markup=back_to_admin_kb(),
    )


# ---------------------------------------------------------------------------
# WITHDRAWALS section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:withdrawals")
async def admin_withdrawals(callback: CallbackQuery, db: Database) -> None:
    stats = await db.get_withdrawal_stats()
    recent = await db.list_withdrawals(limit=10)
    lines = [
        "💰 <b>Withdrawals</b>\n",
        f"Pending: {stats['pending']} | Processing: {stats['processing']}",
        f"Completed: {stats['completed']} | Failed: {stats['failed']}\n",
        "<b>Recent:</b>",
    ]
    if not recent:
        lines.append("(none yet)")
    for w in recent:
        lines.append(
            f"#{w.get('id', w.get('_id',''))} user {w['user_id']}: "
            f"{w['amount_currency']} {w['currency']} — {w['status']}"
        )
    await callback.message.edit_text("\n".join(lines), reply_markup=back_to_admin_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# SETTINGS section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery, db: Database) -> None:
    s = await db.get_settings()
    text = (
        "⚙ <b>Settings</b>\n\n"
        f"Min withdraw: {format_points(s['min_withdraw_points'])} pts\n"
        f"Rate: {format_points(s['conversion_points_unit'])} pts = "
        f"{format_amount(s['conversion_usdt_unit'])} {s['withdraw_currency']}\n"
        f"Referral reward: {format_points(s['referral_reward'])} pts\n"
        f"Withdrawals enabled: {'✅' if s['withdrawals_enabled'] else '🔴'}\n"
        f"Network: {s['withdraw_network']}"
    )
    await callback.message.edit_text(text, reply_markup=admin_settings_kb())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:toggle_withdrawals")
async def admin_toggle_withdrawals(callback: CallbackQuery, db: Database) -> None:
    s = await db.get_settings()
    await db.update_settings(withdrawals_enabled=not s["withdrawals_enabled"])
    await admin_settings(callback, db)


@admin_router.callback_query(F.data == "admin:set_min_withdraw")
async def admin_set_min_withdraw_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSetWithdraw.waiting_min_points)
    await callback.message.edit_text("💵 Send the new minimum withdrawal (points).", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminSetWithdraw.waiting_min_points))
async def admin_set_min_withdraw_finish(message: Message, db: Database, state: FSMContext) -> None:
    value = _parse_decimal(message.text)
    if value is None:
        await message.answer("Please send a valid non-negative number.")
        return
    await state.clear()
    await db.update_settings(min_withdraw_points=value)
    await message.answer(f"✅ Minimum withdrawal set to {format_points(value)} pts.", reply_markup=back_to_admin_kb())


@admin_router.callback_query(F.data == "admin:set_rate")
async def admin_set_rate_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSetRate.waiting_points_unit)
    await callback.message.edit_text(
        "🔁 Send the points side of the rate, e.g. for '15 points = 0.01 USDT' send: 15",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@admin_router.message(StateFilter(AdminSetRate.waiting_points_unit))
async def admin_set_rate_points(message: Message, state: FSMContext) -> None:
    value = _parse_decimal(message.text)
    if value is None or value == 0:
        await message.answer("Please send a valid positive number.")
        return
    await state.update_data(points_unit=str(value))
    await state.set_state(AdminSetRate.waiting_usdt_unit)
    await message.answer("Now send the currency side, e.g. for '15 points = 0.01 USDT' send: 0.01")


@admin_router.message(StateFilter(AdminSetRate.waiting_usdt_unit))
async def admin_set_rate_usdt(message: Message, db: Database, state: FSMContext) -> None:
    value = _parse_decimal(message.text)
    if value is None:
        await message.answer("Please send a valid non-negative number.")
        return
    data = await state.get_data()
    await state.clear()
    points_unit = Decimal(data["points_unit"])
    await db.update_settings(conversion_points_unit=points_unit, conversion_usdt_unit=value)
    await message.answer(
        f"✅ Rate updated: {format_points(points_unit)} points = {format_amount(value)}.",
        reply_markup=back_to_admin_kb(),
    )


@admin_router.callback_query(F.data == "admin:set_referral_reward")
async def admin_set_referral_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSetReferralReward.waiting_value)
    await callback.message.edit_text("🎁 Send the new referral reward (points).", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminSetReferralReward.waiting_value))
async def admin_set_referral_finish(message: Message, db: Database, state: FSMContext) -> None:
    value = _parse_decimal(message.text)
    if value is None:
        await message.answer("Please send a valid non-negative number.")
        return
    await state.clear()
    await db.update_settings(referral_reward=value)
    await message.answer(f"✅ Referral reward set to {format_points(value)} pts.", reply_markup=back_to_admin_kb())


# ---------------------------------------------------------------------------
# USERS section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:users")
async def admin_users_menu(callback: CallbackQuery, db: Database) -> None:
    total = await db.count_users()
    await callback.message.edit_text(
        f"👥 <b>Users</b>\n\nTotal users: {total}", reply_markup=admin_users_menu_kb()
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:ban_user")
async def admin_ban_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminBanUser.waiting_user_id)
    await callback.message.edit_text("🚫 Send the Telegram user ID to ban.", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminBanUser.waiting_user_id))
async def admin_ban_finish(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please send a numeric user ID.")
        return
    await db.set_ban(user_id, True)
    await message.answer(f"🚫 User {user_id} banned.", reply_markup=back_to_admin_kb())


@admin_router.callback_query(F.data == "admin:unban_user")
async def admin_unban_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUnbanUser.waiting_user_id)
    await callback.message.edit_text("✅ Send the Telegram user ID to unban.", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminUnbanUser.waiting_user_id))
async def admin_unban_finish(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please send a numeric user ID.")
        return
    await db.set_ban(user_id, False)
    await message.answer(f"✅ User {user_id} unbanned.", reply_markup=back_to_admin_kb())


@admin_router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminBroadcast.waiting_message)
    await callback.message.edit_text("📣 Send the message to broadcast to all users.", reply_markup=cancel_kb())
    await callback.answer()


@admin_router.message(StateFilter(AdminBroadcast.waiting_message))
async def admin_broadcast_finish(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    text = message.text or ""
    if not text:
        await message.answer("Please send a text message.")
        return
    user_ids = await db.list_all_user_ids()
    await message.answer(f"📣 Broadcasting to {len(user_ids)} users...")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await message.bot.send_message(uid, text)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits

    await message.answer(f"✅ Broadcast complete. Sent: {sent}, Failed: {failed}", reply_markup=back_to_admin_kb())


# ---------------------------------------------------------------------------
# STATS section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery, db: Database) -> None:
    total_users = await db.count_users()
    total_balance = await db.sum_balances()
    wd_stats = await db.get_withdrawal_stats()
    tasks = await db.list_tasks()
    active_tasks = sum(1 for t in tasks if t["is_active"])

    text = (
        "📊 <b>Stats</b>\n\n"
        f"Total users: {total_users}\n"
        f"Total outstanding balance: {format_points(total_balance)} pts\n"
        f"Active tasks: {active_tasks} / {len(tasks)}\n"
        f"Withdrawals — pending: {wd_stats['pending']}, processing: {wd_stats['processing']}, "
        f"completed: {wd_stats['completed']}, failed: {wd_stats['failed']}\n"
        f"Database backend: {db.backend}"
    )
    await callback.message.edit_text(text, reply_markup=back_to_admin_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# xROCKET section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:xrocket")
async def admin_xrocket(callback: CallbackQuery, xrocket: XRocketClient) -> None:
    await callback.answer()
    try:
        info = await xrocket.get_app_info()
        payload = info.get("data", info) if isinstance(info, dict) else info
        name = payload.get("name", "N/A") if isinstance(payload, dict) else "N/A"
        fee = payload.get("feePercents", "N/A") if isinstance(payload, dict) else "N/A"
        balances = payload.get("balances", []) if isinstance(payload, dict) else []
        bal_lines = "\n".join(f"  {b.get('currency')}: {b.get('balance')}" for b in balances) or "  (none)"
        text = f"💳 <b>xRocket App</b>\n\nName: {name}\nFee: {fee}%\nBalances:\n{bal_lines}"
    except XRocketAPIError as exc:
        text = f"💳 <b>xRocket App</b>\n\n⚠️ Could not reach xRocket API: {exc}"
    await callback.message.edit_text(text, reply_markup=back_to_admin_kb())


# ---------------------------------------------------------------------------
# BACKUP section
# ---------------------------------------------------------------------------
@admin_router.callback_query(F.data == "admin:backup")
async def admin_backup_cb(callback: CallbackQuery, db: Database) -> None:
    await callback.answer("Creating backup...")
    await callback.message.edit_text("💾 Creating backup, this may take a moment...", reply_markup=back_to_admin_kb())

    await run_backup_now(callback.bot, db)


@admin_router.message(Command("backup"))
async def admin_backup_cmd(message: Message, db: Database) -> None:
    await message.answer("💾 Creating backup, this may take a moment...")

    await run_backup_now(message.bot, db)


# ---------------------------------------------------------------------------
# Direct slash-command shortcuts (spec explicitly requests these in addition
# to the inline menu above)
# ---------------------------------------------------------------------------
@admin_router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminAddChannel.waiting_chat_id)
    await state.update_data(task_type="channel")
    await message.answer("➕ Send the channel's @username or numeric chat ID.", reply_markup=cancel_kb())


@admin_router.message(Command("addgroup"))
async def cmd_addgroup(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminAddChannel.waiting_chat_id)
    await state.update_data(task_type="group")
    await message.answer("➕ Send the group's @username or numeric chat ID.", reply_markup=cancel_kb())


@admin_router.message(Command("removechannel"))
@admin_router.message(Command("removegroup"))
async def cmd_remove_task(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminRemoveTask.waiting_task_id)
    await message.answer("❌ Send the task ID to remove (see 📋 List Tasks in /admin).", reply_markup=cancel_kb())


@admin_router.message(Command("setreward"))
async def cmd_setreward(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminSetReward.waiting_task_id)
    await message.answer("✏ Send the task ID to change the reward for.", reply_markup=cancel_kb())


@admin_router.message(Command("setwithdraw"))
async def cmd_setwithdraw(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminSetWithdraw.waiting_min_points)
    await message.answer("💵 Send the new minimum withdrawal (points).", reply_markup=cancel_kb())


@admin_router.message(Command("setrate"))
async def cmd_setrate(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminSetRate.waiting_points_unit)
    await message.answer(
        "🔁 Send the points side of the rate, e.g. for '15 points = 0.01 USDT' send: 15",
        reply_markup=cancel_kb(),
    )


_ADMIN_FLOW_STATES = (
    AdminAddChannel.waiting_chat_id,
    AdminAddChannel.waiting_reward,
    AdminRemoveTask.waiting_task_id,
    AdminSetReward.waiting_task_id,
    AdminSetReward.waiting_value,
    AdminSetWithdraw.waiting_min_points,
    AdminSetRate.waiting_points_unit,
    AdminSetRate.waiting_usdt_unit,
    AdminSetReferralReward.waiting_value,
    AdminBanUser.waiting_user_id,
    AdminUnbanUser.waiting_user_id,
    AdminBroadcast.waiting_message,
)


@admin_router.message(StateFilter(*_ADMIN_FLOW_STATES), Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Cancelled.", reply_markup=back_to_admin_kb())


@admin_router.message(StateFilter(None), Command("cancel"))
async def cmd_cancel_noop(message: Message) -> None:
    await message.answer("Nothing to cancel.")


# ==============================================================================
# APPLICATION ENTRY POINT
# ==============================================================================
log = get_logger("bot")


async def _handle_xrocket_webhook(request):
    """Minimal inbound listener - NOT an admin panel, just a status callback."""
    from aiohttp import web

    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]

    raw_body = await request.read()
    signature = request.headers.get("Rocket-Pay-Signature")

    if XROCKET_WEBHOOK_TOKEN:
        valid = XRocketClient.verify_webhook(raw_body, signature, XROCKET_WEBHOOK_TOKEN)
        if not valid:
            log.warning("Rejected xRocket webhook with invalid signature.")
            return web.Response(status=401, text="invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="invalid json")

    withdrawal_id = (
        payload.get("withdrawalId") or payload.get("data", {}).get("withdrawalId")
    )
    status = payload.get("status") or payload.get("data", {}).get("status")

    if not withdrawal_id or not status:
        log.info("Ignoring xRocket webhook without withdrawalId/status: %s", payload)
        return web.Response(status=200, text="ok")

    record = await db.get_withdrawal_by_xrocket_id(withdrawal_id)
    if not record:
        log.warning("Webhook for unknown withdrawal_id=%s", withdrawal_id)
        return web.Response(status=200, text="ok")

    normalized = status.upper()
    await db.update_withdrawal_status(withdrawal_id, normalized.lower())

    if normalized in ("FAIL", "FAILED", "CANCELLED", "CANCELED"):
        from decimal import Decimal


        amount_points = unscale(record["amount_points"])
        already_refunded = record["status"] in ("failed",)
        if not already_refunded:
            await db.adjust_balance(
                record["user_id"], amount_points, "withdraw_refund", f"Refund for {withdrawal_id}"
            )
        try:
            await bot.send_message(
                record["user_id"],
                f"❌ Your withdrawal failed and {amount_points} points were refunded.",
            )
        except Exception:  # noqa: BLE001
            pass
    elif normalized in ("COMPLETED", "SUCCESS", "DONE"):
        try:
            await bot.send_message(record["user_id"], "✅ Your withdrawal has completed successfully!")
        except Exception:  # noqa: BLE001
            pass

    log.info("Webhook processed: withdrawal_id=%s status=%s", withdrawal_id, status)
    return web.Response(status=200, text="ok")


async def _run_webhook_server(bot: Bot, db: Database):
    from aiohttp import web

    app = web.Application()
    app["bot"] = bot
    app["db"] = db
    app.router.add_post(WEBHOOK_PATH, _handle_xrocket_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_LISTEN_HOST, WEBHOOK_LISTEN_PORT)
    await site.start()
    log.info(
        "Webhook listener running on %s:%s%s",
        WEBHOOK_LISTEN_HOST,
        WEBHOOK_LISTEN_PORT,
        WEBHOOK_PATH,
    )
    return runner


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    db = Database(MONGO_URI, DB_NAME, SQLITE_PATH)
    await db.connect()

    xrocket = XRocketClient(XROCKET_API_TOKEN)

    # Admin router first so its stricter admin-only filters get first refusal
    # on admin-only commands like /admin, /addchannel, /backup, etc.
    dp.include_router(admin_router)
    dp.include_router(user_router)

    scheduler = setup_scheduler(bot, db)

    webhook_runner = None
    if ENABLE_WEBHOOK_SERVER:
        webhook_runner = await _run_webhook_server(bot, db)

    stop_event = asyncio.Event()

    def _signal_handler(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for these

    log.info("Bot starting (backend=%s)...", db.backend)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        polling_task = asyncio.create_task(
            dp.start_polling(bot, db=db, xrocket=xrocket, handle_signals=False)
        )
        await stop_event.wait()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    finally:
        scheduler.shutdown(wait=False)
        if webhook_runner:
            await webhook_runner.cleanup()
        await xrocket.close()
        await db.close()
        await bot.session.close()
        log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

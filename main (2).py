import os
import re
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import openai
from openai import AsyncOpenAI
from collections import defaultdict, deque
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, MenuButtonCommands, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
CARD_DETAILS = os.getenv("CARD_DETAILS", "Admin bilan bog'laning")
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
AI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
AI_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=40.0, max_retries=2) if OPENAI_API_KEY else None
AI_HISTORY = defaultdict(lambda: deque(maxlen=12))

TASHKENT = ZoneInfo("Asia/Tashkent")
# 0 = tungi tarif; tugash vaqti keyingi tegishli 09:00.
TARIFF_TITLES = {
    3: "3 SOAT - 150000",
    6: "6 SOAT - 300000",
    12: "12 SOAT - 400000",
    24: "24 SOAT - 600000",
    0: "TUNGI TARIF - 350000",
}
DEFAULT_PRICES = {3: 150000, 6: 300000, 12: 400000, 24: 600000, 0: 350000}

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool = None


class ReceiptState(StatesGroup):
    waiting_receipt = State()


class CashbackState(StatesGroup):
    waiting_full_name = State()
    waiting_card_number = State()


class KYCState(StatesGroup):
    waiting_full_name = State()
    waiting_passport = State()
    waiting_location = State()
    waiting_phone = State()
    waiting_video = State()


class AdminState(StatesGroup):
    waiting_price = State()
    waiting_toggle = State()


account_views = set()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_reply_keyboard(user_id: int):
    rows = [
        [KeyboardButton(text="🎮 Akkauntlar"), KeyboardButton(text="🎁 BONUS")],
        [KeyboardButton(text="💬 Admin bilan aloqa")],
        [KeyboardButton(text="📜 Qoidalar")],
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(text="⚙️ Admin paneli")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Kerakli bo'limni tanlang"
    )


def tariff_title(hours: int, price: int) -> str:
    if hours == 0:
        return f"TUNGI TARIF - {price}"
    return f"{hours} SOAT - {price}"


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS accounts(
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                hourly_price BIGINT NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS account_prices(
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                hours INTEGER NOT NULL,
                price BIGINT NOT NULL CHECK(price >= 0),
                PRIMARY KEY(account_id, hours)
            );

            CREATE TABLE IF NOT EXISTS bookings(
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                hours INTEGER NOT NULL,
                amount BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'awaiting_payment',
                receipt_file_id TEXT,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS receipt_file_id TEXT;

            ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS qr_file_id TEXT;

            ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

            ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

            ALTER TABLE bookings ADD COLUMN IF NOT EXISTS user_username TEXT;
            ALTER TABLE bookings ADD COLUMN IF NOT EXISTS user_first_name TEXT;
            ALTER TABLE bookings ADD COLUMN IF NOT EXISTS user_last_name TEXT;
            ALTER TABLE bookings ADD COLUMN IF NOT EXISTS original_amount BIGINT;
            ALTER TABLE bookings ADD COLUMN IF NOT EXISTS bonus_used BIGINT NOT NULL DEFAULT 0;

            CREATE INDEX IF NOT EXISTS idx_bookings_account_status
            ON bookings(account_id, status, expires_at);

            CREATE TABLE IF NOT EXISTS manual_account_blocks(
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                admin_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bonus_rewards(
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                milestone_number INTEGER NOT NULL,
                amount BIGINT NOT NULL DEFAULT 100000,
                status TEXT NOT NULL DEFAULT 'pending',
                full_name TEXT,
                card_number TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                requested_at TIMESTAMPTZ,
                paid_at TIMESTAMPTZ,
                UNIQUE(user_id, milestone_number)
            );

            ALTER TABLE bonus_rewards
            ADD COLUMN IF NOT EXISTS full_name TEXT;
            ALTER TABLE bonus_rewards
            ADD COLUMN IF NOT EXISTS card_number TEXT;
            ALTER TABLE bonus_rewards
            ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;
            ALTER TABLE bonus_rewards
            ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;
            ALTER TABLE bonus_rewards ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMPTZ;
            ALTER TABLE bonus_rewards ADD COLUMN IF NOT EXISTS used_at TIMESTAMPTZ;
            ALTER TABLE bookings ADD COLUMN IF NOT EXISTS bonus_reward_id INTEGER REFERENCES bonus_rewards(id);

            CREATE TABLE IF NOT EXISTS rental_verifications(
                booking_id INTEGER PRIMARY KEY REFERENCES bookings(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                full_name TEXT NOT NULL,
                passport_photo_file_id TEXT NOT NULL,
                location_text TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                phone_number TEXT NOT NULL,
                video_file_id TEXT,
                video_note_file_id TEXT,
                consented_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_at TIMESTAMPTZ,
                reviewed_by BIGINT
            );
        """)

        count = await c.fetchval("SELECT COUNT(*) FROM accounts")
        if count == 0:
            for i in range(1, 31):
                await c.execute(
                    "INSERT INTO accounts(title, hourly_price) VALUES($1, 0)",
                    f"PUBG akkaunt {i:02d}"
                )

        # Account 1 uchun tariflar faqat birinchi marta qo'shiladi.
        for hours, price in DEFAULT_PRICES.items():
            await c.execute("""
                INSERT INTO account_prices(account_id, hours, price)
                SELECT 1, $1, $2
                WHERE EXISTS (SELECT 1 FROM accounts WHERE id=1)
                ON CONFLICT(account_id, hours) DO NOTHING
            """, hours, price)


async def reset_user_flow(user_id: int, state: FSMContext):
    """Clear unfinished conversation state; return a reserved KYC bonus once."""
    data = await state.get_data()
    booking_id = data.get("kyc_booking_id")
    if booking_id:
        await cancel_pending_kyc(user_id, booking_id)
    await state.clear()


@dp.message(
    F.text.in_({
        "🎮 Akkauntlar", "🎁 BONUS", "💬 Admin bilan aloqa",
        "📜 Qoidalar", "🧾 Qoidalar", "⚙️ Admin paneli"
    })
)
async def universal_main_menu_router(m: Message, state: FSMContext):
    # Menu buttons must work even if the user is stuck in KYC, cashback,
    # receipt, or admin FSM state. Clear that state before opening a section.
    await reset_user_flow(m.from_user.id, state)

    if m.text == "🎮 Akkauntlar":
        await reply_menu_accounts(m)
    elif m.text == "🎁 BONUS":
        await reply_menu_bonus(m)
    elif m.text == "💬 Admin bilan aloqa":
        await reply_menu_contact(m)
    elif m.text in ("📜 Qoidalar", "🧾 Qoidalar"):
        await reply_menu_rules(m)
    elif m.text == "⚙️ Admin paneli":
        await reply_menu_admin(m)


@dp.message(Command("menu"))
async def show_menu(m: Message, state: FSMContext):
    await reset_user_flow(m.from_user.id, state)
    await m.answer(
        "Asosiy menyu:",
        reply_markup=main_reply_keyboard(m.from_user.id)
    )


@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await reset_user_flow(m.from_user.id, state)
    await m.answer(
        "Assalomu alaykum! KHOJA RENT AI botiga xush kelibsiz.\n"
        "Pastdagi menyudan kerakli bo'limni tanlang:",
        reply_markup=main_reply_keyboard(m.from_user.id)
    )


async def get_bonus_stats(user_id: int):
    async with pool.acquire() as c:
        total = await c.fetchval("""
            SELECT COUNT(*) FROM bookings
            WHERE user_id=$1 AND status IN ('paid','active','expired')
        """, user_id)
        available = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status='pending'
        """, user_id)
        requested = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status='requested'
        """, user_id)
        reserved = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status='reserved'
        """, user_id)
        paid = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status='paid'
        """, user_id)
        used = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status='redeemed'
        """, user_id)
        claimable = await c.fetchval("""
            SELECT COUNT(*) FROM bonus_rewards
            WHERE user_id=$1 AND status='pending'
        """, user_id)
    return tuple(int(v or 0) for v in (total, available, requested, reserved, paid, used, claimable))


async def bonus_screen(user_id: int):
    total, available, requested, reserved, paid, used, claimable = await get_bonus_stats(user_id)
    progress = total % 10
    remaining = 10 - progress if progress else 10
    text = (
        f"🎁 KHOJA RENT BONUS\n\n"
        f"📊 Jami tasdiqlangan ijaralar: {total} ta\n"
        f"🎯 Keyingi bonusgacha: {progress}/10 ta\n"
        f"🔥 Yana kerak: {remaining} ta ({progress * 10}%)\n\n"
        "Har 10 ta tasdiqlangan ijara uchun 100 000 so'm bonus!\n\n"
        f"💰 Mavjud bonus: {available:,} so'm\n"
        f"⏳ Kartaga o'tkazish so'rovi: {requested:,} so'm\n"
        f"🛒 Buyurtmada band qilingan bonus: {reserved:,} so'm\n"
        f"🎮 Akkauntga ishlatilgan bonus: {used:,} so'm\n"
        f"✅ Oldin kartaga to'langan: {paid:,} so'm"
    )
    buttons = []
    if claimable:
        buttons.append([InlineKeyboardButton(
            text=f"💳 Kartaga o'tkazish ({claimable} ta yutuq)",
            callback_data="bonus:claim"
        )])
        buttons.append([InlineKeyboardButton(
            text="🎮 Yutuqqa akkaunt olish",
            callback_data="bonus:account"
        )])
    else:
        text += "\n\nHozircha ishlatish mumkin bo'lgan bonus yo'q. 10 ta tasdiqlangan ijaraga yetganda bonus beriladi."
    buttons.append([InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")])
    return text, kb(buttons)



@dp.callback_query(F.data == "bonus")
async def customer_bonus(q: CallbackQuery):
    text, markup = await bonus_screen(q.from_user.id)
    await q.message.answer(text, reply_markup=markup)
    await q.answer()



@dp.callback_query(F.data == "bonus:claim")
async def cashback_claim_start(q: CallbackQuery, state: FSMContext):
    async with pool.acquire() as c:
        reward = await c.fetchrow("""
            SELECT id, amount FROM bonus_rewards
            WHERE user_id=$1 AND status='pending'
            ORDER BY milestone_number LIMIT 1
        """, q.from_user.id)
    if not reward:
        await q.answer("Hozircha olish mumkin bo'lgan yutuq yo'q.", show_alert=True)
        return
    await state.update_data(cashback_reward_id=reward["id"])
    await state.set_state(CashbackState.waiting_full_name)
    await q.message.answer(
        f"🎉 Siz {reward['amount']:,} so'm cashback yutdingiz!\n\n"
        "Pulni o'tkazishimiz uchun ism va familiyangizni yuboring.",
        reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]])
    )
    await q.answer()


@dp.message(CashbackState.waiting_full_name)
async def cashback_receive_name(m: Message, state: FSMContext):
    full_name = " ".join((m.text or "").strip().split())
    if not full_name or len(full_name) < 5 or len(full_name) > 100 or len(full_name.split()) < 2:
        await m.answer("Iltimos, ism va familiyangizni to'liq yozing. Masalan: Ali Valiyev", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
        return
    await state.update_data(cashback_full_name=full_name)
    await state.set_state(CashbackState.waiting_card_number)
    await m.answer("Endi cashback tushiriladigan 16 xonali karta raqamini yuboring.\nCVV, amal qilish muddati yoki SMS-kodni yubormang.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))


@dp.message(CashbackState.waiting_card_number)
async def cashback_receive_card(m: Message, state: FSMContext):
    card_number = re.sub(r"[\s-]", "", (m.text or ""))
    if not re.fullmatch(r"\d{16}", card_number):
        await m.answer("Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qayta yuboring.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
        return

    data = await state.get_data()
    reward_id = data.get("cashback_reward_id")
    full_name = data.get("cashback_full_name")
    async with pool.acquire() as c:
        reward = await c.fetchrow("""
            UPDATE bonus_rewards
            SET full_name=$1, card_number=$2, status='requested', requested_at=NOW()
            WHERE id=$3 AND user_id=$4 AND status='pending'
            RETURNING id, amount, milestone_number
        """, full_name, card_number, reward_id, m.from_user.id)

    if not reward:
        await m.answer("Bu yutuq allaqachon so'ralgan yoki mavjud emas. BONUS bo'limini yangilang.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
        await state.clear()
        return

    await bot.send_message(
        ADMIN_ID,
        "🎁 CASHBACK TO'LASH SO'ROVI\n"
        f"Yutuq ID: {reward['id']}\n"
        f"Mijoz ID: {m.from_user.id}\n"
        f"Ism-familiya: {full_name}\n"
        f"Karta raqami: {card_number}\n"
        f"Summa: {reward['amount']:,} so'm\n"
        f"Bosqich: {reward['milestone_number']} (har 10 ta ijara)",
        reply_markup=kb([[
            InlineKeyboardButton(
                text="✅ Cashback to'landi",
                callback_data=f"bonus_paid:{reward['id']}"
            ),
            InlineKeyboardButton(
                text="❌ So'rovni rad etish",
                callback_data=f"bonus_reject:{reward['id']}"
            )
        ]])
    )
    await m.answer(
        "✅ Ma'lumotlaringiz adminga yuborildi. "
        "Cashback kartangizga o'tkazilgach sizga xabar beramiz.",
        reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]])
    )
    await state.clear()


@dp.callback_query(F.data.startswith("bonus_paid:"))
async def cashback_mark_paid(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    reward_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        reward = await c.fetchrow("""
            UPDATE bonus_rewards
            SET status='paid', paid_at=NOW()
            WHERE id=$1 AND status='requested'
            RETURNING user_id, amount
        """, reward_id)
    if not reward:
        await q.answer("So'rov topilmadi yoki allaqachon to'langan.", show_alert=True)
        return
    await bot.send_message(
        reward["user_id"],
        f"🎉 {reward['amount']:,} so'm cashback kartangizga o'tkazildi. Rahmat!"
    )
    await q.message.edit_text((q.message.text or "") + "\n\n✅ CASHBACK TO'LANDI")
    await q.answer("To'lov belgilandi.")


@dp.callback_query(F.data == "bonus:account")
async def bonus_account_start(q: CallbackQuery):
    async with pool.acquire() as c:
        reward = await c.fetchrow("""
            SELECT id, amount FROM bonus_rewards
            WHERE user_id=$1 AND status='pending'
            ORDER BY milestone_number LIMIT 1
        """, q.from_user.id)
        accounts = await c.fetch("""
            SELECT a.id, a.title FROM accounts a
            WHERE a.active=TRUE
              AND NOT EXISTS (SELECT 1 FROM manual_account_blocks mb WHERE mb.account_id=a.id)
              AND NOT EXISTS (
                  SELECT 1 FROM bookings b
                  WHERE b.account_id=a.id AND b.status IN ('awaiting_verification','paid','active')
                    AND (b.status IN ('awaiting_verification','paid') OR b.expires_at > NOW())
              )
            ORDER BY a.id
        """)
    if not reward:
        await q.answer("Ishlatish mumkin bo'lgan bonus topilmadi.", show_alert=True)
        return
    if not accounts:
        await q.answer("Hozircha bo'sh akkaunt yo'q. Keyinroq urinib ko'ring.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=f"🟢 {a['id']} AKKAUNT — BO'SH",
        callback_data=f"bonus:pickacc:{reward['id']}:{a['id']}"
    )] for a in accounts]
    buttons += [
        [InlineKeyboardButton(text="⬅️ BONUSGA QAYTISH", callback_data="bonus")],
        [InlineKeyboardButton(text="🏠 ASOSIY MENYU", callback_data="home")]
    ]
    await q.message.answer(
        f"🎮 BONUS ORQALI AKKAUNT OLISH\n"
        f"Bonus balansingiz: {reward['amount']:,} so'm\n\n"
        "Ijaraga olmoqchi bo'lgan bo'sh akkauntni tanlang:",
        reply_markup=kb(buttons)
    )
    await q.answer()


@dp.callback_query(F.data.startswith("bonus:pickacc:"))
async def bonus_pick_account(q: CallbackQuery):
    _, _, reward_id_text, account_id_text = q.data.split(":")
    reward_id, account_id = int(reward_id_text), int(account_id_text)
    async with pool.acquire() as c:
        reward = await c.fetchrow("""
            SELECT amount FROM bonus_rewards
            WHERE id=$1 AND user_id=$2 AND status='pending'
        """, reward_id, q.from_user.id)
        account = await c.fetchrow(
            "SELECT id, title FROM accounts WHERE id=$1 AND active=TRUE", account_id
        )
        busy = await c.fetchval("""
            SELECT (
                EXISTS(SELECT 1 FROM manual_account_blocks WHERE account_id=$1)
                OR EXISTS(SELECT 1 FROM bookings
                          WHERE account_id=$1 AND status IN ('paid','active')
                            AND (status='paid' OR expires_at > NOW()))
            )
        """, account_id)
        prices = await c.fetch("""
            SELECT hours, price FROM account_prices
            WHERE account_id=$1 AND price > 0
            ORDER BY CASE WHEN hours=0 THEN 99 ELSE hours END
        """, account_id)
    if not reward:
        await q.answer("Bu bonus allaqachon ishlatilgan yoki mavjud emas.", show_alert=True)
        return
    if not account or busy:
        await q.answer("Bu akkaunt band bo'lib qoldi. Boshqasini tanlang.", show_alert=True)
        return
    if not prices:
        await q.answer("Bu akkaunt uchun tarif belgilanmagan.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=tariff_title(p["hours"], p["price"]),
        callback_data=f"bonus:picktariff:{reward_id}:{account_id}:{p['hours']}"
    )] for p in prices]
    buttons += [
        [InlineKeyboardButton(text="⬅️ AKKAUNTLARGA QAYTISH", callback_data="bonus:account")],
        [InlineKeyboardButton(text="🏠 ASOSIY MENYU", callback_data="home")]
    ]
    await q.message.answer(
        f"🎮 {account['title']}\nBonus balansingiz: {reward['amount']:,} so'm\nTarifni tanlang:",
        reply_markup=kb(buttons)
    )
    await q.answer()


@dp.callback_query(F.data.startswith("bonus:picktariff:"))
async def bonus_pick_tariff(q: CallbackQuery, state: FSMContext):
    _, _, reward_id_text, account_id_text, hours_text = q.data.split(":")
    reward_id, account_id, hours = int(reward_id_text), int(account_id_text), int(hours_text)
    async with pool.acquire() as c:
        async with c.transaction():
            account = await c.fetchrow(
                "SELECT id, title FROM accounts WHERE id=$1 AND active=TRUE FOR UPDATE",
                account_id
            )
            reward = await c.fetchrow("""
                SELECT id, amount FROM bonus_rewards
                WHERE id=$1 AND user_id=$2 AND status='pending' FOR UPDATE
            """, reward_id, q.from_user.id)
            price = await c.fetchrow("""
                SELECT price FROM account_prices
                WHERE account_id=$1 AND hours=$2 AND price > 0
            """, account_id, hours)
            busy = await c.fetchval("""
                SELECT (
                    EXISTS(SELECT 1 FROM manual_account_blocks WHERE account_id=$1)
                    OR EXISTS(SELECT 1 FROM bookings
                              WHERE account_id=$1 AND status IN ('paid','active')
                                AND (status='paid' OR expires_at > NOW()))
                )
            """, account_id)
            if not account or not reward or not price or busy:
                await q.answer("Akkaunt, tarif yoki bonus endi mavjud emas. Qaytadan tanlang.", show_alert=True)
                return
            original_amount = int(price["price"])
            bonus_used = min(int(reward["amount"]), original_amount)
            due = original_amount - bonus_used
            status = "awaiting_verification"
            booking_id = await c.fetchval("""
                INSERT INTO bookings(
                    user_id, account_id, hours, amount, status,
                    user_username, user_first_name, user_last_name,
                    original_amount, bonus_used, bonus_reward_id
                )
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                RETURNING id
            """, q.from_user.id, account_id, hours, due, status,
                 q.from_user.username, q.from_user.first_name, q.from_user.last_name,
                 original_amount, bonus_used, reward_id)
            await c.execute("""
                UPDATE bonus_rewards SET status='reserved', reserved_at=NOW()
                WHERE id=$1 AND status='pending'
            """, reward_id)


    await begin_kyc(q.message, state, booking_id)
    await q.answer("Buyurtma yaratildi.")


@dp.callback_query(F.data.startswith("bonus_reject:"))
async def cashback_reject(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    reward_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        reward = await c.fetchrow("""
            UPDATE bonus_rewards
            SET status='pending', full_name=NULL, card_number=NULL, requested_at=NULL
            WHERE id=$1 AND status='requested'
            RETURNING user_id, amount
        """, reward_id)
    if not reward:
        await q.answer("So'rov topilmadi yoki holati o'zgargan.", show_alert=True)
        return
    await bot.send_message(
        reward["user_id"],
        f"Cashback so'rovingiz rad etildi. {reward['amount']:,} so'm bonusingiz balansga qaytarildi."
    )
    await q.message.edit_text((q.message.text or "") + "\n\n❌ SO'ROV RAD ETILDI")
    await q.answer("Bonus mijoz balansiga qaytarildi.")


@dp.callback_query(F.data.startswith("bonus_cancel:"))
async def bonus_booking_cancel(q: CallbackQuery):
    booking_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        async with c.transaction():
            booking = await c.fetchrow("""
                SELECT id, bonus_reward_id FROM bookings
                WHERE id=$1 AND user_id=$2 AND status='awaiting_payment'
                FOR UPDATE
            """, booking_id, q.from_user.id)
            if not booking:
                await q.answer("Buyurtma topilmadi yoki bekor qilib bo'lmaydi.", show_alert=True)
                return
            await c.execute(
                "UPDATE bookings SET status='rejected' WHERE id=$1",
                booking_id
            )
            if booking["bonus_reward_id"] is not None:
                await c.execute("""
                    UPDATE bonus_rewards SET status='pending', reserved_at=NULL
                    WHERE id=$1 AND status='reserved'
                """, booking["bonus_reward_id"])
    await q.message.answer(
        "Buyurtma bekor qilindi. Ishlatilmagan bonus balansingizga qaytarildi.",
        reply_markup=kb([[InlineKeyboardButton(text="⬅️ ASOSIY MENYU", callback_data="home")]])
    )
    await q.answer("Buyurtma bekor qilindi.")


async def account_list_view():
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.id, a.title,
                   current_booking.status AS booking_status,
                   current_booking.expires_at AS expires_at,
                   EXISTS(
                       SELECT 1 FROM manual_account_blocks mb
                       WHERE mb.account_id=a.id
                   ) AS manually_blocked
            FROM accounts a
            LEFT JOIN LATERAL (
                SELECT b.status, b.expires_at
                FROM bookings b
                WHERE b.account_id=a.id
                  AND b.status IN ('awaiting_verification','paid','active')
                ORDER BY
                    CASE
                        WHEN b.status='active' AND b.expires_at IS NOT NULL THEN 0
                        WHEN b.status='paid' THEN 1
                        ELSE 2
                    END,
                    b.created_at DESC
                LIMIT 1
            ) AS current_booking ON TRUE
            WHERE a.active=TRUE
            ORDER BY a.id
        """)
    buttons = []
    now = datetime.now(timezone.utc)
    for a in rows:
        status = a["booking_status"]
        # Admin qo'lda band qilgan akkaunt: doim qizil, vaqt ko'rsatilmaydi.
        if a["manually_blocked"]:
            label = f"{a['id']} AKKAUNT — 🔴 BAND"
            buttons.append([InlineKeyboardButton(
                text=label,
                callback_data=f"account_busy:{a['id']}"
            )])
        elif status == "active" and a["expires_at"]:
            seconds = max(0, int((a["expires_at"] - now).total_seconds()))
            if seconds > 0:
                hh, rem = divmod(seconds, 3600)
                mm, ss = divmod(rem, 60)
                end_time = a["expires_at"].astimezone(TASHKENT).strftime("%H:%M")
                label = (
                    f"{a['id']} AKKAUNT — 🔴 BAND | BO'SHASH: {end_time} "
                    f"({hh:02d}:{mm:02d}:{ss:02d})"
                )
                buttons.append([InlineKeyboardButton(text=label, callback_data=f"account_busy:{a['id']}")])
            else:
                buttons.append([InlineKeyboardButton(text=f"{a['id']} AKKAUNT — 🟢 BO'SH", callback_data=f"account:{a['id']}")])
        elif status in ("awaiting_verification", "paid"):
            buttons.append([InlineKeyboardButton(
                text=f"{a['id']} AKKAUNT — 🔴 BAND",
                callback_data=f"account_busy:{a['id']}"
            )])
        else:
            buttons.append([InlineKeyboardButton(text=f"{a['id']} AKKAUNT — 🟢 BO'SH", callback_data=f"account:{a['id']}")])
    buttons.append([InlineKeyboardButton(text="🔄 Yangilash", callback_data="accounts")])
    buttons.append([InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")])
    text = "🎮 KHOJA RENT — AKKAUNTLAR"
    if not rows:
        text += "\n\nHozircha faol akkaunt yo'q."
    return text, kb(buttons)


async def update_account_views():
    stale = set()
    for chat_id, message_id in list(account_views):
        try:
            text, markup = await account_list_view()
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                stale.add((chat_id, message_id))
    account_views.difference_update(stale)


async def cancel_pending_kyc(user_id: int, booking_id):
    if not booking_id:
        return False
    async with pool.acquire() as c:
        async with c.transaction():
            booking = await c.fetchrow(
                """
                SELECT bonus_reward_id
                FROM bookings
                WHERE id=$1 AND user_id=$2 AND status='awaiting_verification'
                FOR UPDATE
                """,
                booking_id, user_id
            )
            if not booking:
                return False

            await c.execute(
                "UPDATE bookings SET status='rejected' "
                "WHERE id=$1 AND user_id=$2 AND status='awaiting_verification'",
                booking_id, user_id
            )
            if booking["bonus_reward_id"] is not None:
                await c.execute(
                    """
                    UPDATE bonus_rewards
                    SET status='pending', reserved_at=NULL
                    WHERE id=$1 AND status='reserved'
                    """,
                    booking["bonus_reward_id"]
                )
            return True


@dp.callback_query(F.data == "home")
async def home(q: CallbackQuery, state: FSMContext):
    fsm_data = await state.get_data()
    pending_kyc_bid = fsm_data.get("kyc_booking_id")
    if pending_kyc_bid:
        await cancel_pending_kyc(q.from_user.id, pending_kyc_bid)
    # Har qanday tugallanmagan FSM jarayonini tozalaymiz, shunda bot eski
    # bosqichda qolib ketmaydi. KYC buyurtmasi bo'lsa bonus yuqorida qaytariladi.
    await state.clear()
    # Eski inline menyu/xabarni olib tashlaymiz: asosiy menyu faqat pastdagi
    # doimiy ReplyKeyboard panelida qoladi.
    try:
        await q.message.delete()
    except Exception:
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await q.message.answer(
        "Asosiy menyu. Kerakli bo'limni pastdagi doimiy paneldan tanlang.",
        reply_markup=main_reply_keyboard(q.from_user.id)
    )
    await q.answer()


@dp.callback_query(F.data == "accounts")
async def accounts(q: CallbackQuery):
    text, markup = await account_list_view()
    msg = await q.message.answer(text, reply_markup=markup)
    account_views.add((msg.chat.id, msg.message_id))
    await q.answer()


@dp.callback_query(F.data.startswith("account_busy:"))
async def account_busy(q: CallbackQuery):
    account_id = int(q.data.split(":", 1)[1])
    async with pool.acquire() as c:
        row = await c.fetchrow("""
            SELECT b.status, b.expires_at,
                   EXISTS(SELECT 1 FROM manual_account_blocks mb
                          WHERE mb.account_id=$1) AS manually_blocked
            FROM accounts a
            LEFT JOIN LATERAL (
                SELECT status, expires_at
                FROM bookings
                WHERE account_id=a.id
                  AND status IN ('active','paid','awaiting_verification')
                ORDER BY CASE
                    WHEN status='active' AND expires_at IS NOT NULL THEN 0
                    WHEN status='paid' THEN 1
                    ELSE 2 END,
                    created_at DESC
                LIMIT 1
            ) b ON TRUE
            WHERE a.id=$1
        """, account_id)

    if not row:
        await q.answer("Akkaunt topilmadi.", show_alert=True)
        return

    # Faqat amalda boshlangan ijara uchun bo'shash vaqtini ko'rsatamiz.
    expires_at = row["expires_at"]
    if row["status"] == "active" and expires_at:
        remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
        if remaining > 0:
            hh, rem = divmod(remaining, 3600)
            mm, ss = divmod(rem, 60)
            end_time = expires_at.astimezone(TASHKENT).strftime("%H:%M")
            await q.answer(
                f"🔴 AKKAUNT BAND\nBo'shash vaqti: {end_time}\nQolgan vaqt: {hh:02d}:{mm:02d}:{ss:02d}",
                show_alert=True
            )
            return

    if row["manually_blocked"]:
        message = "🔴 AKKAUNT ADMIN TOMONIDAN QO'LDA BAND QILINGAN."
    elif row["status"] in ("paid", "awaiting_verification"):
        message = "🔴 AKKAUNT BAND. Ijara hali boshlanmagan, bo'shash vaqti belgilanmagan."
    else:
        message = "🔴 AKKAUNT HOZIR BAND. Bo'shash vaqtini aniqlab bo'lmadi."
    await q.answer(message, show_alert=True)


@dp.callback_query(F.data.startswith("account:"))
async def choose_account(q: CallbackQuery):
    account_id = int(q.data.split(":")[1])

    async with pool.acquire() as c:
        a = await c.fetchrow(
            "SELECT * FROM accounts WHERE id=$1 AND active=TRUE",
            account_id
        )
        busy = await c.fetchval("""
            SELECT (
                EXISTS(SELECT 1 FROM manual_account_blocks WHERE account_id=$1)
                OR EXISTS(SELECT 1 FROM bookings
                          WHERE account_id=$1 AND status IN ('paid','active')
                            AND (status='paid' OR expires_at > NOW()))
            )
        """, account_id)
        prices = await c.fetch("""
            SELECT hours, price
            FROM account_prices
            WHERE account_id=$1 AND price > 0
            ORDER BY CASE WHEN hours=0 THEN 99 ELSE hours END
        """, account_id)

    if not a:
        await q.answer("Akkaunt topilmadi.", show_alert=True)
        return
    if busy:
        await q.answer("Bu akkaunt band. Bo'sh akkauntni tanlang.", show_alert=True)
        return

    if not prices:
        await q.answer(
            "Bu akkaunt narxi hali belgilanmagan. Admin bilan bog'laning.",
            show_alert=True
        )
        return

    buttons = []
    for p in prices:
        label = tariff_title(p["hours"], p["price"])
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"duration:{account_id}:{p['hours']}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")])

    await q.message.answer(
        f"Tanlangan: {a['title']}\nTarifni tanlang:",
        reply_markup=kb(buttons)
    )
    await q.answer()



def kyc_text():
    return (
        "🔐 KHOJA RENT — IJARA XAVFSIZLIGI\n\n"
        "Buyurtmani davom ettirish uchun quyidagi ma'lumotlar so'raladi:\n"
        "1. Ism-familiya\n2. Pasport yoki ID-karta rasmi\n"
        "3. Jonli yoki oddiy lokatsiya\n4. Telefon raqami\n"
        "5. Yuzingiz ko'rinadigan video yoki video-xabar.\n\n"
        "Ma'lumotlar mijozni aniqlash va ijara xavfsizligi maqsadida bot bazasida saqlanadi. "
        "Ular ommaga chiqarilmaydi; faqat vakolatli KHOJA RENT administratori tekshirishi mumkin. "
        "Rozilik bermasangiz, davom etmang. Ma'lumotlarni saqlash va o'chirish bo'yicha so'rov uchun admin bilan bog'lanishingiz mumkin."
    )


async def begin_kyc(message: Message, state: FSMContext, booking_id: int):
    await state.clear()
    await state.update_data(kyc_booking_id=booking_id)
    await message.answer(
        kyc_text(),
        reply_markup=kb([
            [InlineKeyboardButton(text="✅ Roziman, davom etish", callback_data=f"kyc:consent:{booking_id}")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"kyc:cancel:{booking_id}")],
            [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]
        ])
    )


@dp.callback_query(F.data.startswith("kyc:consent:"))
async def kyc_consent(q: CallbackQuery, state: FSMContext):
    bid = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        ok = await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM bookings WHERE id=$1 AND user_id=$2 AND status='awaiting_verification')",
            bid, q.from_user.id
        )
    if not ok:
        await q.answer("Buyurtma topilmadi yoki holati o'zgargan.", show_alert=True)
        return
    await state.clear()
    await state.update_data(kyc_booking_id=bid)
    await state.set_state(KYCState.waiting_full_name)
    await q.message.answer("1/5. Ism-familiyangizni to'liq yuboring.")
    await q.answer()


@dp.callback_query(F.data.startswith("kyc:cancel:"))
async def kyc_cancel(q: CallbackQuery, state: FSMContext):
    bid = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        async with c.transaction():
            booking = await c.fetchrow(
                "SELECT bonus_reward_id FROM bookings WHERE id=$1 AND user_id=$2 AND status='awaiting_verification' FOR UPDATE",
                bid, q.from_user.id
            )
            if booking:
                await c.execute("UPDATE bookings SET status='rejected' WHERE id=$1", bid)
                if booking["bonus_reward_id"] is not None:
                    await c.execute(
                        "UPDATE bonus_rewards SET status='pending', reserved_at=NULL WHERE id=$1 AND status='reserved'",
                        booking["bonus_reward_id"]
                    )
    await state.clear()
    await q.message.answer("Buyurtma bekor qilindi.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ ASOSIY MENYU", callback_data="home")]]))
    await q.answer("Bekor qilindi.")


@dp.message(Command("cancel"))
async def cancel_current_flow(m: Message, state: FSMContext):
    data = await state.get_data()
    bid = data.get("kyc_booking_id")
    if bid:
        async with pool.acquire() as c:
            async with c.transaction():
                booking = await c.fetchrow(
                    "SELECT bonus_reward_id FROM bookings WHERE id=$1 AND user_id=$2 AND status='awaiting_verification' FOR UPDATE",
                    bid, m.from_user.id
                )
                if booking:
                    await c.execute("UPDATE bookings SET status='rejected' WHERE id=$1", bid)
                    if booking["bonus_reward_id"] is not None:
                        await c.execute(
                            "UPDATE bonus_rewards SET status='pending', reserved_at=NULL WHERE id=$1 AND status='reserved'",
                            booking["bonus_reward_id"]
                        )
        await state.clear()
        await m.answer("Jarayon bekor qilindi. Bonus ishlatilmagan bo'lsa balansingizga qaytarildi.",
                       reply_markup=main_reply_keyboard(m.from_user.id))
    else:
        await state.clear()
        await m.answer("Joriy jarayon bekor qilindi.", reply_markup=main_reply_keyboard(m.from_user.id))


@dp.message(
    StateFilter(
        KYCState.waiting_full_name, KYCState.waiting_passport,
        KYCState.waiting_location, KYCState.waiting_phone, KYCState.waiting_video
    ),
    F.text.in_({
        "🎮 Akkauntlar", "🎁 BONUS", "💬 Admin bilan aloqa",
        "📜 Qoidalar", "🧾 Qoidalar", "⚙️ Admin paneli",
        "⬅️ Asosiy menyu"
    })
)
async def kyc_main_menu_navigation(m: Message, state: FSMContext):
    await reset_user_flow(m.from_user.id, state)

    # Reuse the normal menu handlers after clearing the stuck KYC state.
    if m.text == "🎮 Akkauntlar":
        text, markup = await account_list_view()
        msg = await m.answer(text, reply_markup=markup)
        account_views.add((msg.chat.id, msg.message_id))
    elif m.text == "🎁 BONUS":
        text, markup = await bonus_screen(m.from_user.id)
        await m.answer(text, reply_markup=markup)
    elif m.text == "💬 Admin bilan aloqa":
        await reply_menu_contact(m)
    elif m.text in ("📜 Qoidalar", "🧾 Qoidalar"):
        await reply_menu_rules(m)
    elif m.text == "⚙️ Admin paneli":
        await reply_menu_admin(m)
    else:
        await m.answer(
            "⬅️ Asosiy menyuga qaytdingiz. Kerakli bo'limni pastdagi paneldan tanlang.",
            reply_markup=main_reply_keyboard(m.from_user.id)
        )


@dp.message(
    StateFilter(
        KYCState.waiting_full_name, KYCState.waiting_passport,
        KYCState.waiting_location, KYCState.waiting_phone, KYCState.waiting_video
    ),
    F.text == "⬅️ Asosiy menyu"
)
async def kyc_reply_home(m: Message, state: FSMContext):
    data = await state.get_data()
    booking_id = data.get("kyc_booking_id")
    if booking_id:
        await cancel_pending_kyc(m.from_user.id, booking_id)
    await state.clear()
    await m.answer(
        "⬅️ Asosiy menyuga qaytdingiz. Buyurtma bekor qilindi; "
        "band qilingan bonus balansingizga qaytarildi (agar bonus ishlatilgan bo'lsa).",
        reply_markup=main_reply_keyboard(m.from_user.id)
    )


@dp.message(KYCState.waiting_full_name)
async def kyc_name(m: Message, state: FSMContext):
    name = " ".join((m.text or "").split())
    if len(name) < 5 or len(name) > 100 or len(name.split()) < 2:
        await m.answer("Ism-familiyangizni to'liq yuboring. Masalan: Ali Valiyev.")
        return
    await state.update_data(kyc_name=name)
    await state.set_state(KYCState.waiting_passport)
    await m.answer("2/5. Pasport yoki ID-kartangiz rasmini foto ko'rinishida yuboring.")


@dp.message(KYCState.waiting_passport, F.photo)
async def kyc_passport(m: Message, state: FSMContext):
    await state.update_data(kyc_passport=m.photo[-1].file_id)
    await state.set_state(KYCState.waiting_location)
    await m.answer(
        "3/5. Jonli yoki oddiy lokatsiyangizni yuboring. "
        "Telegram lokatsiyasi yoki manzilni matn ko'rinishida yuborishingiz mumkin.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Lokatsiyani yuborish", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )


@dp.message(KYCState.waiting_passport)
async def kyc_passport_other(m: Message):
    await m.answer("Pasport yoki ID-karta rasmini foto ko'rinishida yuboring.")


@dp.message(KYCState.waiting_location, F.location)
async def kyc_location(m: Message, state: FSMContext):
    await state.update_data(
        kyc_lat=m.location.latitude, kyc_lon=m.location.longitude,
        kyc_location=("Jonli lokatsiya" if m.location.live_period else "Oddiy lokatsiya")
    )
    await state.set_state(KYCState.waiting_phone)
    await m.answer(
        "4/5. Telefon raqamingizni yuboring.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )


@dp.message(KYCState.waiting_location, F.text)
async def kyc_location_text(m: Message, state: FSMContext):
    loc = (m.text or "").strip()
    if len(loc) < 3:
        await m.answer("Lokatsiya yuboring yoki manzilni matn ko'rinishida yozing.")
        return
    await state.update_data(kyc_location=loc)
    await state.set_state(KYCState.waiting_phone)
    await m.answer(
        "4/5. Telefon raqamingizni yuboring.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )


async def kyc_phone_next(m: Message, state: FSMContext, phone: str):
    digits = re.sub(r"[^0-9]", "", phone)
    if not 9 <= len(digits) <= 15:
        await m.answer("Telefon raqamini to'g'ri formatda yuboring.")
        return
    await state.update_data(kyc_phone=phone)
    await state.set_state(KYCState.waiting_video)
    await m.answer(
        "5/5. Yuzingiz ko'rinadigan video yoki dumaloq video-xabar yuboring. "
        "Unda ism-familiyangiz, sana, akkaunt va ijara muddatini ayting. "
        "Shuningdek, qoidalarni buzib o'z aybingiz bilan akkauntga zarar yetkazsangiz, "
        "shartnomadagi tartibda zararni qoplashga roziligingizni bildiring.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Asosiy menyu")]],
            resize_keyboard=True
        )
    )


@dp.message(KYCState.waiting_phone, F.contact)
async def kyc_phone_contact(m: Message, state: FSMContext):
    if m.contact.user_id and m.contact.user_id != m.from_user.id:
        await m.answer("Iltimos, o'zingizning telefon raqamingizni yuboring.")
        return
    await kyc_phone_next(m, state, m.contact.phone_number)


@dp.message(KYCState.waiting_phone, F.text)
async def kyc_phone_text(m: Message, state: FSMContext):
    await kyc_phone_next(m, state, m.text or "")


async def kyc_finish(m: Message, state: FSMContext, video_id=None, video_note_id=None):
    data = await state.get_data()
    bid = data.get("kyc_booking_id")
    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO rental_verifications(
                booking_id,user_id,full_name,passport_photo_file_id,location_text,
                latitude,longitude,phone_number,video_file_id,video_note_file_id,status
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending')
            ON CONFLICT(booking_id) DO UPDATE SET
                full_name=EXCLUDED.full_name,
                passport_photo_file_id=EXCLUDED.passport_photo_file_id,
                location_text=EXCLUDED.location_text,
                latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
                phone_number=EXCLUDED.phone_number,
                video_file_id=EXCLUDED.video_file_id,
                video_note_file_id=EXCLUDED.video_note_file_id,
                status='pending', consented_at=NOW()
        """, bid, m.from_user.id, data["kyc_name"], data["kyc_passport"],
             data.get("kyc_location"), data.get("kyc_lat"), data.get("kyc_lon"),
             data["kyc_phone"], video_id, video_note_id)
    caption = (
        f"🔐 IJARA XAVFSIZLIK TEKSHIRUVI — BUYURTMA №{bid}\n"
        f"Ism-familiya: {data['kyc_name']}\n"
        f"Telegram ID: {m.from_user.id}\nTelefon: {data['kyc_phone']}\n"
        f"Lokatsiya: {data.get('kyc_location') or 'yuborilgan lokatsiya'}\n"
        "Maxfiy ma'lumot: faqat vakolatli admin tekshiruvi uchun."
    )
    await bot.send_photo(
        ADMIN_ID, data["kyc_passport"], caption=caption,
        reply_markup=kb([
            [InlineKeyboardButton(text="✅ Tasdiqlash — karta yuborish", callback_data=f"kyc:approve:{bid}")],
            [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"kyc:reject:{bid}")],
            [InlineKeyboardButton(text="👤 Mijoz lichkasi", url=f"tg://user?id={m.from_user.id}")]
        ])
    )
    if video_id:
        await bot.send_video(ADMIN_ID, video_id, caption=f"Video rozilik — buyurtma №{bid}")
    if video_note_id:
        await bot.send_video_note(ADMIN_ID, video_note_id)
    if data.get("kyc_lat") is not None:
        await bot.send_location(ADMIN_ID, data["kyc_lat"], data["kyc_lon"])
    await m.answer(
        "✅ Ma'lumotlaringiz admin tekshiruviga yuborildi. Tasdiqlangach, to'lov kartasi yuboriladi.",
        reply_markup=main_reply_keyboard(m.from_user.id)
    )
    await state.clear()


@dp.message(KYCState.waiting_video, F.video)
async def kyc_video(m: Message, state: FSMContext):
    await kyc_finish(m, state, video_id=m.video.file_id)


@dp.message(KYCState.waiting_video, F.video_note)
async def kyc_video_note(m: Message, state: FSMContext):
    await kyc_finish(m, state, video_note_id=m.video_note.file_id)


@dp.message(KYCState.waiting_video)
async def kyc_video_other(m: Message):
    await m.answer("Video yoki dumaloq video-xabar yuboring.")


@dp.callback_query(F.data.startswith("kyc:approve:"))
async def kyc_approve(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    bid = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        async with c.transaction():
            b = await c.fetchrow("""
                SELECT b.*, a.title FROM bookings b
                JOIN accounts a ON a.id=b.account_id
                JOIN rental_verifications v ON v.booking_id=b.id
                WHERE b.id=$1 AND b.status='awaiting_verification' AND v.status='pending'
                FOR UPDATE OF b
            """, bid)
            if not b:
                await q.answer("Buyurtma topilmadi yoki allaqachon ko'rib chiqilgan.", show_alert=True)
                return
            await c.execute(
                "UPDATE rental_verifications SET status='approved', reviewed_at=NOW(), reviewed_by=$2 WHERE booking_id=$1",
                bid, q.from_user.id
            )
            due = int(b["amount"])
            if due == 0:
                await c.execute("UPDATE bookings SET status='paid' WHERE id=$1", bid)
                if b["bonus_reward_id"] is not None:
                    await c.execute(
                        "UPDATE bonus_rewards SET status='redeemed', used_at=NOW() WHERE id=$1 AND status='reserved'",
                        b["bonus_reward_id"]
                    )
            else:
                await c.execute("UPDATE bookings SET status='awaiting_payment' WHERE id=$1", bid)
    if due == 0:
        await bot.send_message(
            b["user_id"],
            f"✅ Xavfsizlik tekshiruvi tasdiqlandi. Buyurtma №{bid} bonus bilan to'liq qoplangan. "
            "Qo'shimcha to'lov yo'q. Endi akkauntga kirish uchun kerakli QR-kodni yuboring. "
            "Admin mijoz kirganini tasdiqlagach ijara vaqti boshlanadi."
        )
        await bot.send_message(
            ADMIN_ID,
            f"🎮 BONUS BUYURTMASI №{bid} TASDIQLANDI. To'lov bonus bilan to'liq qoplandi. "
            "Mijozdan QR-kod kutilmoqda; admin kirishni tasdiqlaganda ijara boshlanadi."
        )
        await q.message.edit_caption((q.message.caption or "") + "\n\n✅ TEKSHIRUV TASDIQLANDI")
    else:
        await bot.send_message(
            b["user_id"],
            f"✅ Xavfsizlik tekshiruvi tasdiqlandi.\nBuyurtma №{bid}\nAkkaunt: {b['title']}\n"
            f"To'lov summasi: {due:,} so'm\n\n💳 To'lov kartasi: {CARD_DETAILS}\n"
            "To'lovdan keyin chekni yuboring.",
            reply_markup=kb([
                [InlineKeyboardButton(text="📸 Chek yuborish", callback_data=f"receipt:{bid}")],
                [InlineKeyboardButton(text="⬅️ ASOSIY MENYU", callback_data="home")]
            ])
        )
        await q.message.edit_caption((q.message.caption or "") + "\n\n✅ TEKSHIRUV TASDIQLANDI — KARTA YUBORILDI")
    await q.answer("Tekshiruv tasdiqlandi.")


@dp.callback_query(F.data.startswith("kyc:reject:"))
async def kyc_reject(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    bid = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        async with c.transaction():
            b = await c.fetchrow(
                "SELECT user_id, bonus_reward_id FROM bookings WHERE id=$1 AND status='awaiting_verification' FOR UPDATE",
                bid
            )
            if not b:
                await q.answer("Buyurtma topilmadi yoki holati o'zgargan.", show_alert=True)
                return
            await c.execute("UPDATE bookings SET status='rejected' WHERE id=$1", bid)
            await c.execute(
                "UPDATE rental_verifications SET status='rejected', reviewed_at=NOW(), reviewed_by=$2 WHERE booking_id=$1",
                bid, q.from_user.id
            )
            if b["bonus_reward_id"] is not None:
                await c.execute(
                    "UPDATE bonus_rewards SET status='pending', reserved_at=NULL WHERE id=$1 AND status='reserved'",
                    b["bonus_reward_id"]
                )
    await bot.send_message(b["user_id"], "❌ Xavfsizlik tekshiruvi rad etildi. Admin bilan bog'laning.")
    await q.message.edit_caption((q.message.caption or "") + "\n\n❌ TEKSHIRUV RAD ETILDI")
    await q.answer("Rad etildi.")


@dp.callback_query(F.data.startswith("duration:"))
async def choose_duration(q: CallbackQuery, state: FSMContext):
    _, aid_text, hours_text = q.data.split(":")
    aid = int(aid_text)
    hours = int(hours_text)

    async with pool.acquire() as c:
        a = await c.fetchrow(
            "SELECT * FROM accounts WHERE id=$1 AND active=TRUE",
            aid
        )
        p = await c.fetchrow(
            "SELECT price FROM account_prices WHERE account_id=$1 AND hours=$2",
            aid, hours
        )
        busy = await c.fetchval("""
            SELECT (
                EXISTS(SELECT 1 FROM manual_account_blocks WHERE account_id=$1)
                OR EXISTS(SELECT 1 FROM bookings
                          WHERE account_id=$1 AND status IN ('paid','active')
                            AND (status='paid' OR expires_at > NOW()))
            )
        """, aid)
        if busy:
            await q.answer("Bu akkaunt band bo'lib qoldi. Boshqasini tanlang.", show_alert=True)
            return

        if not a or not p or p["price"] <= 0:
            await q.answer("Bu tarif hozir mavjud emas.", show_alert=True)
            return

        amount = p["price"]
        booking_id = await c.fetchval("""
            INSERT INTO bookings(user_id, account_id, hours, amount, status, user_username, user_first_name, user_last_name)
            VALUES($1, $2, $3, $4, 'awaiting_verification', $5, $6, $7)
            RETURNING id
        """, q.from_user.id, aid, hours, amount, q.from_user.username,
             q.from_user.first_name, q.from_user.last_name)

    await begin_kyc(q.message, state, booking_id)
    await q.answer()


@dp.callback_query(F.data.startswith("receipt:"))
async def receipt_start(q: CallbackQuery, state: FSMContext):
    booking_id = int(q.data.split(":")[1])

    async with pool.acquire() as c:
        b = await c.fetchrow("""
            SELECT id FROM bookings
            WHERE id=$1 AND user_id=$2 AND status='awaiting_payment'
        """, booking_id, q.from_user.id)

    if not b:
        await q.answer(
            "Buyurtma topilmadi yoki allaqachon ko'rib chiqilmoqda.",
            show_alert=True
        )
        return

    await state.update_data(booking_id=booking_id)
    await state.set_state(ReceiptState.waiting_receipt)
    await q.message.answer("To'lov chekini rasm ko'rinishida yuboring.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
    await q.answer()


@dp.message(ReceiptState.waiting_receipt, F.photo)
async def receive_receipt(m: Message, state: FSMContext):
    data = await state.get_data()
    booking_id = data["booking_id"]
    file_id = m.photo[-1].file_id

    async with pool.acquire() as c:
        result = await c.execute("""
            UPDATE bookings
            SET receipt_file_id=$1, status='receipt_received'
            WHERE id=$2 AND user_id=$3 AND status='awaiting_payment'
        """, file_id, booking_id, m.from_user.id)

        if result.endswith("0"):
            await m.answer("Buyurtma topilmadi yoki chek oldin yuborilgan.")
            await state.clear()
            return

        b = await c.fetchrow(
            "SELECT * FROM bookings WHERE id=$1",
            booking_id
        )
        a = await c.fetchrow(
            "SELECT title FROM accounts WHERE id=$1",
            b["account_id"]
        )

    label = tariff_title(b["hours"], b["amount"])
    await bot.send_photo(
        ADMIN_ID,
        file_id,
        caption=(
            f"Chek — buyurtma №{booking_id}\n"
            f"Mijoz: {m.from_user.full_name}\n"
            f"Username: @{m.from_user.username or 'mavjud emas'}\n"
            f"Mijoz ID: {m.from_user.id}\n"
            f"Akkaunt: {a['title']}\n"
            f"Tarif: {label}\n"
            f"To'lanadigan summa: {b['amount']:,} so'm\n"
            + (f"Ijara narxi: {b['original_amount']:,} so'm\nBonus chegirmasi: {b['bonus_used']:,} so'm"
               if b["bonus_used"] else "")
        ),
        reply_markup=kb([
            [InlineKeyboardButton(text="👤 Mijoz lichkasi", url=(f"https://t.me/{m.from_user.username}" if m.from_user.username else f"tg://user?id={m.from_user.id}"))],
            [InlineKeyboardButton(text="To'lov tasdiqlandi", callback_data=f"approve:{booking_id}"),
             InlineKeyboardButton(text="Rad etish", callback_data=f"reject:{booking_id}")],
            [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]
        ])
    )

    await m.answer(
        "Chek adminga yuborildi. To'lov bank ilovasida tekshirilgach javob beramiz.",
        reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]])
    )
    await state.clear()


@dp.message(ReceiptState.waiting_receipt)
async def receipt_not_photo(m: Message):
    await m.answer("Iltimos, chekni rasm ko'rinishida yuboring.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))


@dp.callback_query(F.data.startswith("approve:"))
async def approve(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return

    bid = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        b = await c.fetchrow(
            "SELECT * FROM bookings WHERE id=$1",
            bid
        )
        if not b or b["status"] != "receipt_received":
            await q.answer("Buyurtma holati o'zgargan.", show_alert=True)
            return
        await c.execute(
            "UPDATE bookings SET status='paid' WHERE id=$1",
            bid
        )
        if b["bonus_reward_id"] is not None:
            await c.execute("""
                UPDATE bonus_rewards SET status='redeemed', used_at=NOW()
                WHERE id=$1 AND status='reserved'
            """, b["bonus_reward_id"])
        approved_count = await c.fetchval("""
            SELECT COUNT(*) FROM bookings
            WHERE user_id=$1 AND status IN ('paid','active','expired')
        """, b["user_id"])
        if approved_count and approved_count % 10 == 0:
            milestone = approved_count // 10
            await c.execute("""
                INSERT INTO bonus_rewards(user_id, milestone_number, amount, status)
                VALUES($1, $2, 100000, 'pending')
                ON CONFLICT(user_id, milestone_number) DO NOTHING
            """, b["user_id"], milestone)

    await bot.send_message(
        b["user_id"],
        f"Buyurtma №{bid}: to'lov tasdiqlandi.\n"
        "Endi akkauntga kirish uchun kerakli QR-kodni yuboring. "
        "Admin QR-kod orqali kirish jarayonini tekshiradi va mijoz kirganini tasdiqlagach "
        "ijara vaqti avtomatik boshlanadi."
        + ("\n\n🎉 Tabriklaymiz! Har 10 ta tasdiqlangan ijarangiz uchun 100 000 so'm cashback yutdingiz. BONUS bo'limidan yutuqni oling."
           if approved_count and approved_count % 10 == 0 else "")
    )
    await q.message.edit_caption(
        (q.message.caption or "") + "\n\nTO'LOV TASDIQLANDI"
    )
    await q.answer("Tasdiqlandi.")


@dp.callback_query(F.data.startswith("reject:"))
async def reject(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return

    bid = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        b = await c.fetchrow(
            "SELECT * FROM bookings WHERE id=$1",
            bid
        )
        if not b:
            await q.answer("Buyurtma topilmadi.", show_alert=True)
            return
        await c.execute(
            "UPDATE bookings SET status='rejected' WHERE id=$1",
            bid
        )
        if b["bonus_reward_id"] is not None:
            await c.execute("""
                UPDATE bonus_rewards SET status='pending', reserved_at=NULL
                WHERE id=$1 AND status='reserved'
            """, b["bonus_reward_id"])

    await bot.send_message(
        b["user_id"],
        f"Buyurtma №{bid}: to'lov tasdiqlanmadi. Admin bilan bog'laning."
    )
    await q.message.edit_caption(
        (q.message.caption or "") + "\n\nRAD ETILDI"
    )
    await q.answer("Rad etildi.")


@dp.message(F.photo)
async def receive_rental_qr(m: Message):
    """Receive the customer's PUBG login QR after payment and send it to admin for review."""
    async with pool.acquire() as c:
        booking = await c.fetchrow("""
            SELECT b.id, b.account_id, b.hours, b.amount, b.user_id,
                   a.title
            FROM bookings b
            JOIN accounts a ON a.id=b.account_id
            WHERE b.user_id=$1 AND b.status='paid'
              AND b.qr_file_id IS NULL
            ORDER BY b.created_at DESC
            LIMIT 1
        """, m.from_user.id)
        if not booking:
            return
        updated = await c.execute("""
            UPDATE bookings SET qr_file_id=$1
            WHERE id=$2 AND status='paid' AND qr_file_id IS NULL
        """, m.photo[-1].file_id, booking["id"])
        if updated.endswith("0"):
            await m.answer("QR-kod allaqachon yuborilgan yoki buyurtma holati o'zgargan.")
            return

    await bot.send_photo(
        ADMIN_ID,
        m.photo[-1].file_id,
        caption=(
            f"📲 MIJOZ QR-KODI — BUYURTMA №{booking['id']}\n"
            f"Mijoz: {m.from_user.full_name}\n"
            f"Telegram ID: {m.from_user.id}\n"
            f"Akkaunt: {booking['title']}\n"
            f"Tarif: {tariff_title(booking['hours'], booking['amount'])}\n\n"
            "QR-kodni tekshiring. Mijoz akkauntga muvaffaqiyatli kirganidan keyingina "
            "quyidagi tasdiqlash tugmasini bosing. Shu paytda ijara vaqti boshlanadi."
        ),
        reply_markup=kb([
            [InlineKeyboardButton(
                text="✅ MIJOZ KIRDI — IJARANI BOSHLASH",
                callback_data=f"qr_entered:{booking['id']}"
            )],
            [InlineKeyboardButton(
                text="❌ QR-KODNI RAD ETISH",
                callback_data=f"qr_reject:{booking['id']}"
            )]
        ])
    )
    await m.answer(
        "✅ QR-kodingiz adminga yuborildi. Admin akkauntga kirganingizni tekshirib tasdiqlaydi. "
        "Tasdiqlanmaguncha ijara vaqti boshlanmaydi."
    )


@dp.callback_query(F.data.startswith("qr_entered:"))
async def admin_confirm_customer_entered(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return

    booking_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        async with c.transaction():
            b = await c.fetchrow("""
                SELECT b.*, a.title
                FROM bookings b
                JOIN accounts a ON a.id=b.account_id
                WHERE b.id=$1 AND b.status='paid' AND b.qr_file_id IS NOT NULL
                FOR UPDATE OF b
            """, booking_id)
            if not b:
                await q.answer("Buyurtma topilmadi, QR yo'q yoki ijara allaqachon boshlangan.", show_alert=True)
                return

            now = datetime.now(TASHKENT)
            if b["hours"] == 0:
                expiry = now.replace(hour=9, minute=0, second=0, microsecond=0)
                if now >= expiry:
                    expiry += timedelta(days=1)
            else:
                expiry = now + timedelta(hours=b["hours"])

            await c.execute("""
                UPDATE bookings
                SET status='active', expires_at=$1
                WHERE id=$2 AND status='paid' AND expires_at IS NULL
            """, expiry.astimezone(timezone.utc), booking_id)

    await bot.send_message(
        b["user_id"],
        f"✅ Admin akkauntga kirganingizni tasdiqladi.\n"
        f"🎮 {b['title']}\n"
        f"⏱ Ijara boshlandi.\n"
        f"🕒 Bo'shash vaqti: {expiry.strftime('%d.%m.%Y %H:%M')}"
    )
    await q.message.edit_caption((q.message.caption or "") + "\n\n✅ MIJOZ KIRDI — IJARA BOSHLANDI")
    await q.answer("Mijoz kirgani tasdiqlandi. Ijara vaqti boshlandi.")


@dp.callback_query(F.data.startswith("qr_reject:"))
async def admin_reject_customer_qr(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return

    booking_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        b = await c.fetchrow("""
            SELECT user_id FROM bookings
            WHERE id=$1 AND status='paid' AND qr_file_id IS NOT NULL
        """, booking_id)
        if not b:
            await q.answer("QR so'rovi topilmadi yoki allaqachon ko'rib chiqilgan.", show_alert=True)
            return
        await c.execute("""
            UPDATE bookings SET qr_file_id=NULL
            WHERE id=$1 AND status='paid'
        """, booking_id)

    await bot.send_message(
        b["user_id"],
        "❌ QR-kod tasdiqlanmadi. Iltimos, akkauntga kirish uchun yangi QR-kod yuboring."
    )
    await q.message.edit_caption((q.message.caption or "") + "\n\n❌ QR-KOD RAD ETILDI")
    await q.answer("QR-kod rad etildi.")


@dp.message(Command("start_rental"))
async def start_rental(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "Ijara endi /start_rental orqali boshlanmaydi. "
        "Mijoz QR-kodini yuborgach, QR xabaridagi "
        "'✅ MIJOZ KIRDI — IJARANI BOSHLASH' tugmasini faqat mijoz akkauntga kirganini "
        "tekshirgandan keyin bosing."
    )


@dp.message(Command("setprice"))
async def set_price(m: Message):
    if m.from_user.id != ADMIN_ID:
        return

    parts = (m.text or "").split()
    if len(parts) != 4:
        await m.answer(
            "Foydalanish: /setprice AKKAUNT_ID SOAT NARX\n"
            "Masalan: /setprice 1 3 150000\n"
            "Tungi tarif uchun: /setprice 1 0 350000"
        )
        return

    if not all(p.isdigit() for p in parts[1:]):
        await m.answer("Barcha qiymatlar raqam bo'lishi kerak.")
        return

    aid, hours, price = map(int, parts[1:])
    if hours not in (0, 3, 6, 12, 24) or price <= 0:
        await m.answer("Soat 0, 3, 6, 12 yoki 24; narx esa 0 dan katta bo'lishi kerak.")
        return

    async with pool.acquire() as c:
        exists = await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM accounts WHERE id=$1)",
            aid
        )
        if exists:
            await c.execute("""
                INSERT INTO account_prices(account_id, hours, price)
                VALUES($1, $2, $3)
                ON CONFLICT(account_id, hours)
                DO UPDATE SET price=EXCLUDED.price
            """, aid, hours, price)

    if not exists:
        await m.answer("Akkaunt ID topilmadi.")
        return

    label = tariff_title(hours, price)
    await m.answer(f"Akkaunt {aid}: {label} narxi saqlandi.")


@dp.message(Command("accounts"))
async def admin_accounts(m: Message):
    if m.from_user.id != ADMIN_ID:
        return

    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.id, a.title, p.hours, p.price
            FROM accounts a
            LEFT JOIN account_prices p ON p.account_id=a.id
            ORDER BY a.id, CASE WHEN p.hours=0 THEN 99 ELSE p.hours END
        """)

    lines = []
    for r in rows:
        if r["hours"] is None:
            lines.append(f"{r['id']}. {r['title']} — tarif belgilanmagan")
        else:
            lines.append(
                f"{r['id']}. {r['title']} — "
                f"{tariff_title(r['hours'], r['price'])}"
            )

    await m.answer("\n".join(lines) or "Akkauntlar yo'q.")


@dp.callback_query(F.data == "admin:panel")
async def admin_panel(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await q.message.answer(
        "⚙️ ADMIN PANELI — amalni tanlang:",
        reply_markup=kb([
            [InlineKeyboardButton(text="💰 Narxni o'zgartirish", callback_data="admin:price")],
            [InlineKeyboardButton(text="🔄 Akkauntni yashirish/ochish", callback_data="admin:toggle")],
            [InlineKeyboardButton(text="🔴 Qo'lda band qilish / bo'shatish", callback_data="admin:manual_blocks")],
            [InlineKeyboardButton(text="📦 Buyurtmalar", callback_data="admin:bookings")],
            [InlineKeyboardButton(text="🎁 Cashback so'rovlari", callback_data="admin:cashbacks")],
            [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]
        ])
    )
    await q.answer()



@dp.callback_query(F.data == "admin:cashbacks")
async def admin_cashback_requests(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT id, user_id, amount, full_name, card_number, milestone_number
            FROM bonus_rewards
            WHERE status='requested'
            ORDER BY requested_at
            LIMIT 30
        """)
    if not rows:
        await q.message.answer("Hozircha cashback to'lash so'rovlari yo'q.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
    else:
        for r in rows:
            await q.message.answer(
                "🎁 CASHBACK SO'ROVI\n"
                f"Yutuq ID: {r['id']}\nMijoz ID: {r['user_id']}\n"
                f"Ism-familiya: {r['full_name']}\nKarta: {r['card_number']}\n"
                f"Summa: {r['amount']:,} so'm\nBosqich: {r['milestone_number']}",
                reply_markup=kb([
                    [InlineKeyboardButton(text="✅ Cashback to'landi", callback_data=f"bonus_paid:{r['id']}")],
                    [InlineKeyboardButton(text="❌ So'rovni rad etish", callback_data=f"bonus_reject:{r['id']}")],
                    [InlineKeyboardButton(text="👤 Mijoz lichkasiga o'tish", url=f"tg://user?id={r['user_id']}")],
                    [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]
                ])
            )
    await q.answer()


@dp.callback_query(F.data == "admin:price")
async def admin_price_start(q: CallbackQuery, state: FSMContext):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminState.waiting_price)
    await q.message.answer("Format: AKKAUNT_ID SOAT NARX\nMasalan: 1 3 150000\nTungi tarifda soat o'rniga 0 yozing.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
    await q.answer()


@dp.callback_query(F.data == "admin:toggle")
async def admin_toggle_start(q: CallbackQuery, state: FSMContext):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminState.waiting_toggle)
    await q.message.answer("Akkaunt ID sini yuboring. Faol akkaunt yashiriladi, yashirilgani qayta ochiladi.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
    await q.answer()


@dp.callback_query(F.data == "admin:manual_blocks")
async def admin_manual_blocks(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.id, a.title,
                   EXISTS(SELECT 1 FROM manual_account_blocks mb
                          WHERE mb.account_id=a.id) AS blocked
            FROM accounts a
            ORDER BY a.id
        """)
    buttons = []
    for row in rows:
        if row["blocked"]:
            label = f"🟢 {row['id']} — Bo'shatish"
            action = f"admin:unblock:{row['id']}"
        else:
            label = f"🔴 {row['id']} — Band qilish"
            action = f"admin:block:{row['id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=action)])
    buttons.append([InlineKeyboardButton(text="⬅️ Admin paneli", callback_data="admin:panel")])
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy menyu", callback_data="home")])
    await q.message.answer(
        "Qo'lda band qilish yoki bo'shatish uchun akkauntni tanlang. "
        "Qo'lda band qilingan akkauntda bo'shash vaqti ko'rsatilmaydi:",
        reply_markup=kb(buttons)
    )
    await q.answer()


@dp.callback_query(F.data.startswith("admin:block:"))
async def admin_manual_block(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    account_id = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        exists = await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM accounts WHERE id=$1)",
            account_id
        )
        if exists:
            await c.execute("""
                INSERT INTO manual_account_blocks(account_id, admin_id)
                VALUES($1, $2)
                ON CONFLICT(account_id) DO UPDATE
                SET admin_id=EXCLUDED.admin_id, created_at=NOW()
            """, account_id, q.from_user.id)
    if not exists:
        await q.answer("Akkaunt topilmadi.", show_alert=True)
        return
    await q.answer("Akkaunt qo'lda band qilindi.")
    await update_account_views()
    await admin_manual_blocks(q)


@dp.callback_query(F.data.startswith("admin:unblock:"))
async def admin_manual_unblock(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    account_id = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        result = await c.execute(
            "DELETE FROM manual_account_blocks WHERE account_id=$1",
            account_id
        )
    await q.answer("Akkaunt bo'shatildi." if result.endswith("1") else "Bu akkaunt qo'lda band qilinmagan.")
    await update_account_views()
    await admin_manual_blocks(q)


@dp.callback_query(F.data.startswith("admin:cancel_booking:"))
async def admin_cancel_booking(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return

    booking_id = int(q.data.rsplit(":", 1)[1])
    async with pool.acquire() as c:
        async with c.transaction():
            booking = await c.fetchrow("""
                SELECT id, user_id, account_id, status
                FROM bookings
                WHERE id=$1
                  AND status IN (
                      'awaiting_verification','awaiting_payment',
                      'receipt_received','paid','active'
                  )
                FOR UPDATE
            """, booking_id)

            if not booking:
                await q.answer("Buyurtma topilmadi yoki bekor qilib bo'lmaydi.", show_alert=True)
                return

            # To'liq bo'shatish: shu akkauntdagi eski/test band buyurtmalar ham bekor qilinadi.
            affected = await c.fetch("""
                SELECT user_id, bonus_reward_id
                FROM bookings
                WHERE account_id=$1
                  AND status IN (
                      'awaiting_verification','awaiting_payment',
                      'receipt_received','paid','active'
                  )
                FOR UPDATE
            """, booking["account_id"])

            await c.execute("""
                UPDATE bookings
                SET status='cancelled', expires_at=NULL
                WHERE account_id=$1
                  AND status IN (
                      'awaiting_verification','awaiting_payment',
                      'receipt_received','paid','active'
                  )
            """, booking["account_id"])

            await c.execute(
                "DELETE FROM manual_account_blocks WHERE account_id=$1",
                booking["account_id"]
            )

            for item in affected:
                if item["bonus_reward_id"] is not None:
                    await c.execute("""
                        UPDATE bonus_rewards
                        SET status='pending', reserved_at=NULL, used_at=NULL
                        WHERE id=$1 AND status IN ('reserved','redeemed')
                    """, item["bonus_reward_id"])

    for user_id in {item["user_id"] for item in affected}:
        try:
            await bot.send_message(
                user_id,
                f"⛔ {booking['account_id']}-akkaunt bo'yicha buyurtma admin tomonidan bekor qilindi. "
                "Akkaunt qayta bo'shatildi. To'lovni qaytarish admin bilan alohida kelishiladi."
            )
        except Exception:
            pass

    await q.message.edit_reply_markup(reply_markup=None)
    await q.answer("Akkauntdagi barcha band buyurtmalar va qo'lda blok olib tashlandi.")
    await update_account_views()

@dp.callback_query(F.data == "admin:bookings")
async def admin_bookings(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT b.id, b.account_id, b.user_id, b.hours, b.amount, b.status,
                   b.created_at, b.user_username, b.user_first_name, b.user_last_name,
                   b.original_amount, b.bonus_used, a.title
            FROM bookings b
            LEFT JOIN accounts a ON a.id=b.account_id
            ORDER BY b.id DESC LIMIT 30
        """)
    if not rows:
        await q.message.answer(
            "📦 Hozircha buyurtmalar yo'q.",
            reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]])
        )
    else:
        for r in rows:
            full_name = " ".join(x for x in [r["user_first_name"], r["user_last_name"]] if x)
            username = f"@{r['user_username']}" if r["user_username"] else "username yo'q"
            customer = full_name or "Ism mavjud emas"
            hours_label = "Tungi tarif" if r["hours"] == 0 else f"{r['hours']} soat"
            created = r["created_at"].astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M") if r["created_at"] else "—"
            status_names = {
                "awaiting_verification": "Xavfsizlik tekshiruvi kutilmoqda",
                "awaiting_payment": "To'lov kutilmoqda",
                "receipt_received": "Chek yuborilgan",
                "paid": "To'lov tasdiqlangan",
                "active": "Ijara faol",
                "expired": "Muddati tugagan",
                "rejected": "Rad etilgan",
                "cancelled": "Bekor qilingan",
            }
            profile_url = f"https://t.me/{r['user_username']}" if r["user_username"] else f"tg://user?id={r['user_id']}"
            text = (
                f"📦 BUYURTMA №{r['id']}\n\n"
                f"👤 Mijoz: {customer}\n"
                f"🔹 Username: {username}\n"
                f"🆔 Telegram ID: {r['user_id']}\n"
                f"🎮 Akkaunt: {r['title'] or ('ID ' + str(r['account_id']))} (#{r['account_id']})\n"
                f"⏱ Tarif: {hours_label}\n"
                f"💰 To'lov: {r['amount']:,} so'm\n"
                + (f"🎁 Bonus chegirmasi: {r['bonus_used']:,} so'm\n" if r["bonus_used"] else "")
                + (f"🧾 Asl narx: {r['original_amount']:,} so'm\n" if r["original_amount"] else "")
                + f"📌 Holat: {status_names.get(r['status'], r['status'])}\n"
                f"🗓 Buyurtma vaqti: {created}"
            )
            booking_buttons = [
                [InlineKeyboardButton(text="👤 Mijoz lichkasiga o'tish", url=profile_url)]
            ]
            if r["status"] in ("awaiting_verification", "awaiting_payment", "receipt_received", "paid", "active"):
                booking_buttons.append([
                    InlineKeyboardButton(
                        text="⛔ Buyurtmani bekor qilish / akkauntni bo'shatish",
                        callback_data=f"admin:cancel_booking:{r['id']}"
                    )
                ])
            booking_buttons.append([
                InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")
            ])
            await q.message.answer(text, reply_markup=kb(booking_buttons))
    await q.answer()


@dp.message(AdminState.waiting_price)
async def admin_price_save(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        await m.answer("Format: 1 3 150000", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
        return
    aid, hours, price = map(int, parts)
    if hours not in (0, 3, 6, 12, 24) or price <= 0:
        await m.answer("Soat 0, 3, 6, 12 yoki 24; narx 0 dan katta bo'lishi kerak.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
        return
    async with pool.acquire() as c:
        exists = await c.fetchval("SELECT EXISTS(SELECT 1 FROM accounts WHERE id=$1)", aid)
        if exists:
            await c.execute("""
                INSERT INTO account_prices(account_id, hours, price) VALUES($1,$2,$3)
                ON CONFLICT(account_id,hours) DO UPDATE SET price=EXCLUDED.price
            """, aid, hours, price)
    await m.answer("Narx saqlandi." if exists else "Akkaunt ID topilmadi.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))
    await state.clear()


@dp.message(AdminState.waiting_toggle)
async def admin_toggle_save(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    value = (m.text or "").strip()
    if not value.isdigit():
        await m.answer("Faqat akkaunt ID sini yuboring.")
        return
    async with pool.acquire() as c:
        row = await c.fetchrow("UPDATE accounts SET active=NOT active WHERE id=$1 RETURNING title, active", int(value))
    if row:
        await m.answer(f"{row['title']}: " + ("faol, mijozlarga ko'rinadi." if row["active"] else "yashirildi."))
    else:
        await m.answer("Akkaunt ID topilmadi.")
    await state.clear()
    await update_account_views()


@dp.message(F.text == "🎮 Akkauntlar")
async def reply_menu_accounts(m: Message):
    text, markup = await account_list_view()
    msg = await m.answer(text, reply_markup=markup)
    account_views.add((msg.chat.id, msg.message_id))


@dp.message(F.text == "🎁 BONUS")
async def reply_menu_bonus(m: Message):
    text, markup = await bonus_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)



@dp.message(F.text == "💬 Admin bilan aloqa")
async def reply_menu_contact(m: Message):
    await m.answer("Admin bilan bog'lanish: https://t.me/khoja_arenda", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]]))


@dp.message(F.text == "📜 Qoidalar")
async def reply_menu_rules(m: Message):
    await m.answer(
        "📜 KHOJA RENT foydalanish qoidalari bilan quyidagi Telegram kanalida tanishing:",
        reply_markup=kb([[
            InlineKeyboardButton(
                text="📜 Qoidalarni ochish",
                url="https://t.me/KHOJA_QOIDALAR"
            )
        ]])
    )


@dp.message(F.text == "⚙️ Admin paneli")
async def reply_menu_admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "⚙️ ADMIN PANELI — amalni tanlang:",
        reply_markup=kb([
            [InlineKeyboardButton(text="💰 Narxni o'zgartirish", callback_data="admin:price")],
            [InlineKeyboardButton(text="🔄 Akkauntni yashirish/ochish", callback_data="admin:toggle")],
            [InlineKeyboardButton(text="🔴 Qo'lda band qilish / bo'shatish", callback_data="admin:manual_blocks")],
            [InlineKeyboardButton(text="📦 Buyurtmalar", callback_data="admin:bookings")],
            [InlineKeyboardButton(text="🎁 Cashback so'rovlari", callback_data="admin:cashbacks")],
            [InlineKeyboardButton(text="⬅️ Asosiy menyu", callback_data="home")]
        ])
    )


async def expiry_watcher():
    while True:
        try:
            async with pool.acquire() as c:
                rows = await c.fetch("""
                    SELECT id, user_id
                    FROM bookings
                    WHERE status='active'
                      AND expires_at IS NOT NULL
                      AND expires_at <= NOW()
                """)

                for b in rows:
                    result = await c.execute("""
                        UPDATE bookings
                        SET status='expired'
                        WHERE id=$1 AND status='active'
                    """, b["id"])

                    if result.endswith("1"):
                        await bot.send_message(
                            b["user_id"],
                            f"Ijara №{b['id']} muddati tugadi. "
                            "Akkauntdan chiqishingizni so'raymiz."
                        )
                        await bot.send_message(
                            ADMIN_ID,
                            f"Ijara №{b['id']} tugadi. "
                            "PUBG sessiyasini rasmiy xavfsizlik sozlamalari orqali tekshiring."
                        )
        except Exception as e:
            print("Expiry watcher error:", repr(e))

        await update_account_views()
        await asyncio.sleep(10)



AI_SYSTEM_PROMPT = """
Siz KHOJA RENT xizmatining ko'p tilli AI yordamchisisiz.
Foydalanuvchi qaysi tilda yozsa, o'sha tilda (o'zbek, rus yoki ingliz) javob bering.
Qisqa, muloyim va aniq gapiring. Faqat KHOJA RENT xizmatiga oid savollarga yordam bering.
Akkaunt ijarasi, tarif, hujjat topshirish, to'lov, QR orqali kirish va qoidalarni tushuntiring.
Narxlar akkaunt va admin sozlamalariga qarab o'zgarishi mumkin; bazadan tekshirmasdan narx yoki bo'sh akkauntni va'da qilmang.
Hujjat, to'lov yoki QR tekshiruvini o'zingiz tasdiqlamang. Bunday ishlarni faqat admin bajaradi.
Ijara vaqti admin mijoz akkauntga kirganini tasdiqlagan paytda boshlanishini ayting.
PUBG paroli, SMS/pochta kodi, 2FA kodi yoki maxfiy autentifikatsiya tokenlarini so'ramang.
To'lov qaytarilishi yoki nizoli masalalarda admin bilan bog'lanishni tavsiya qiling.
Hech qachon admin vakolatiga ega ekaningizni yoki buyurtma holatini o'zgartirganingizni da'vo qilmang.
"""

AI_USER_ERROR = "AI bilan bog'lanishda xatolik yuz berdi. Birozdan keyin urinib ko'ring yoki admin bilan bog'laning."
_ai_alert_times = {}


def explain_ai_error(e: Exception):
    """(foydalanuvchiga matn, adminga aniq texnik sabab) qaytaradi."""
    code = getattr(e, "code", None)
    if isinstance(e, openai.AuthenticationError):
        return AI_USER_ERROR, "OPENAI_API_KEY noto'g'ri yoki bekor qilingan (401). Railway Variables'dagi kalitni tekshiring."
    if isinstance(e, openai.RateLimitError):
        if code == "insufficient_quota":
            return AI_USER_ERROR, "OpenAI hisobida mablag' tugagan (insufficient_quota). platform.openai.com -> Billing bo'limida balansni to'ldiring."
        return AI_USER_ERROR, "OpenAI so'rovlar limiti oshdi (429). Birozdan keyin o'zi tiklanadi."
    if isinstance(e, openai.PermissionDeniedError):
        return AI_USER_ERROR, f"Bu modelga yoki mamlakatingizdan ruxsat yo'q (403): {e}"
    if isinstance(e, openai.NotFoundError):
        return AI_USER_ERROR, f"Model topilmadi (404). OPENAI_MODEL='{AI_MODEL}' nomini tekshiring: {e}"
    if isinstance(e, openai.BadRequestError):
        return AI_USER_ERROR, f"OpenAI so'rovni qabul qilmadi (400): {e}"
    if isinstance(e, openai.APITimeoutError):
        return "AI javob berishga ulgurmadi. Qayta urinib ko'ring.", "OpenAI javobi vaqtida kelmadi (timeout)."
    if isinstance(e, openai.APIConnectionError):
        return AI_USER_ERROR, f"OpenAI serveriga ulanib bo'lmadi: {e}"
    return AI_USER_ERROR, f"{type(e).__name__}: {e}"


async def notify_admin_ai_error(detail: str):
    """Adminga xatoning haqiqiy sababini yuboradi (bir xil xabar 10 daqiqada 1 marta)."""
    now = datetime.now(timezone.utc)
    last = _ai_alert_times.get(detail)
    if last and now - last < timedelta(minutes=10):
        return
    _ai_alert_times[detail] = now
    try:
        await bot.send_message(ADMIN_ID, f"⚠️ AI xatosi:\n{detail[:900]}")
    except Exception as e:
        print("Admin AI alert error:", repr(e))


async def ai_reply(user_id: int, user_text: str) -> str:
    if AI_CLIENT is None:
        return "AI yordamchi hozir sozlanmagan. Admin OPENAI_API_KEY kalitini Railway Variables bo'limiga qo'shishi kerak."
    history = AI_HISTORY[user_id]
    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]
    messages.extend(list(history))
    messages.append({"role": "user", "content": user_text})
    response = await AI_CLIENT.chat.completions.create(
        model=AI_MODEL, messages=messages, temperature=0.4, max_tokens=500
    )
    answer = (response.choices[0].message.content or "").strip()
    if not answer:
        answer = "Kechirasiz, hozir javob tayyorlay olmadim. Admin bilan bog'laning."
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer


@dp.message(Command("aitest"))
async def ai_self_test(m: Message):
    """Admin uchun: AI ulanishini tekshiradi va aniq xato sababini ko'rsatadi."""
    if m.from_user.id != ADMIN_ID:
        return
    if AI_CLIENT is None:
        await m.answer("❌ OPENAI_API_KEY topilmadi. Railway -> Variables bo'limiga qo'shing va botni qayta ishga tushiring.")
        return
    try:
        r = await AI_CLIENT.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": "Javob ber: OK"}],
            max_tokens=10,
        )
        text = (r.choices[0].message.content or "").strip()
        await m.answer(f"✅ AI ishlayapti.\nModel: {AI_MODEL}\nJavob: {text}")
    except Exception as e:
        _, detail = explain_ai_error(e)
        print("AI test error:", repr(e))
        await m.answer(f"❌ AI ishlamayapti.\nModel: {AI_MODEL}\nSabab: {detail}")


@dp.message(StateFilter(None), F.voice)
async def ai_voice_chat(m: Message):
    if AI_CLIENT is None:
        await m.answer("Ovozli AI ishlashi uchun admin OPENAI_API_KEY sozlashi kerak.")
        return
    status = await m.answer("🎧 Ovozli xabaringizni tinglayapman...")
    try:
        voice_file = await bot.get_file(m.voice.file_id)
        audio_stream = await bot.download_file(voice_file.file_path)
        audio_stream.name = "voice.ogg"
        transcription = await AI_CLIENT.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe", file=audio_stream
        )
        spoken_text = (transcription.text or "").strip()
        if not spoken_text:
            await status.edit_text("Ovozni tushuna olmadim. Iltimos, qayta yuboring.")
            return
        answer = await ai_reply(m.from_user.id, spoken_text)
    except Exception as e:
        user_msg, detail = explain_ai_error(e)
        print("AI voice error:", repr(e))
        await notify_admin_ai_error(detail)
        await status.edit_text(user_msg)
        return

    # Matnli javob doim yuboriladi; ovoz (TTS) xato bersa ham foydalanuvchi javobsiz qolmaydi.
    try:
        await status.delete()
    except Exception:
        pass
    await m.answer(f"🗣 Siz: {spoken_text}\n\n🤖 KHOJA RENT AI: {answer}")
    try:
        speech = await AI_CLIENT.audio.speech.create(
            model="gpt-4o-mini-tts", voice="alloy", input=answer, response_format="mp3"
        )
        await m.answer_voice(BufferedInputFile(speech.content, filename="khoja-rent-ai.mp3"))
    except Exception as e:
        print("AI TTS error:", repr(e))


@dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def ai_text_chat(m: Message):
    menu_texts = {"🎮 Akkauntlar", "🎁 BONUS", "💬 Admin bilan aloqa", "📜 Qoidalar", "🧾 Qoidalar", "⚙️ Admin paneli"}
    if m.text in menu_texts:
        return
    try:
        await bot.send_chat_action(m.chat.id, "typing")
        await m.answer(await ai_reply(m.from_user.id, m.text[:2000]))
    except Exception as e:
        user_msg, detail = explain_ai_error(e)
        print("AI text error:", repr(e))
        await notify_admin_ai_error(detail)
        await m.answer(user_msg)


async def main():
    await init_db()
    print(
        "BOT ISHGA TUSHDI | AI:", "yoqilgan" if AI_CLIENT else "OPENAI_API_KEY YO'Q",
        "| model:", AI_MODEL, "| openai:", openai.__version__
    )

    # Telegram chat input yonidagi menyu tugmasini yoqadi.
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_my_commands([
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="menu", description="Asosiy menyuni ochish"),
        BotCommand(command="accounts", description="Akkauntlar ro'yxati"),
    ])

    asyncio.create_task(expiry_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

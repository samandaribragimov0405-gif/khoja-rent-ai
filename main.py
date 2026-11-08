import os
import re
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, MenuButtonCommands, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
CARD_DETAILS = os.getenv("CARD_DETAILS", "Admin bilan bog'laning")

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


class AdminState(StatesGroup):
    waiting_rename = State()
    waiting_price = State()
    waiting_toggle = State()


account_views = set()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_reply_keyboard(user_id: int):
    rows = [
        [KeyboardButton(text="🎮 Akkauntlar"), KeyboardButton(text="🎁 BONUS")],
        [KeyboardButton(text="💬 Admin bilan aloqa")],
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
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

            ALTER TABLE bookings
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

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


@dp.message(Command("menu"))
async def show_menu(m: Message):
    await m.answer(
        "Asosiy menyu:",
        reply_markup=main_reply_keyboard(m.from_user.id)
    )


@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Assalomu alaykum! KHOJA RENT AI botiga xush kelibsiz.\\n"
        "Pastdagi menyudan kerakli bo'limni tanlang:",
        reply_markup=main_reply_keyboard(m.from_user.id)
    )


async def get_bonus_stats(user_id: int):
    async with pool.acquire() as c:
        total = await c.fetchval("""
            SELECT COUNT(*) FROM bookings
            WHERE user_id=$1 AND status IN ('paid','active','expired')
        """, user_id)
        pending = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status IN ('pending','requested')
        """, user_id)
        paid = await c.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM bonus_rewards
            WHERE user_id=$1 AND status='paid'
        """, user_id)
        claimable = await c.fetchval("""
            SELECT COUNT(*) FROM bonus_rewards
            WHERE user_id=$1 AND status='pending'
        """, user_id)
    return int(total or 0), int(pending or 0), int(paid or 0), int(claimable or 0)


@dp.callback_query(F.data == "bonus")
async def customer_bonus(q: CallbackQuery):
    total, pending, paid, claimable = await get_bonus_stats(q.from_user.id)
    progress = total % 10
    remaining = 10 - progress if progress else 10
    text = (
        "🎁 KHOJA RENT BONUS\n\n"
        f"Jami tasdiqlangan ijaralar: {total} ta\n"
        f"Keyingi yutuq uchun: {progress}/10 ta\n"
        f"Yana kerak: {remaining} ta\n"
        "Har 10 ta tasdiqlangan ijara uchun 100 000 so'm cashback!\n\n"
        f"To'lanishi kutilayotgan cashback: {pending:,} so'm\n"
        f"Oldin to'langan cashback: {paid:,} so'm"
    )
    buttons = []
    if claimable:
        buttons.append([InlineKeyboardButton(
            text=f"🎉 Yutuqni olish ({claimable} ta)",
            callback_data="bonus:claim"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="home")])
    await q.message.answer(text, reply_markup=kb(buttons))
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
        "Pulni o'tkazishimiz uchun ism va familiyangizni yuboring."
    )
    await q.answer()


@dp.message(CashbackState.waiting_full_name)
async def cashback_receive_name(m: Message, state: FSMContext):
    full_name = " ".join((m.text or "").strip().split())
    if not full_name or len(full_name) < 5 or len(full_name) > 100 or len(full_name.split()) < 2:
        await m.answer("Iltimos, ism va familiyangizni to'liq yozing. Masalan: Ali Valiyev")
        return
    await state.update_data(cashback_full_name=full_name)
    await state.set_state(CashbackState.waiting_card_number)
    await m.answer("Endi cashback tushiriladigan 16 xonali karta raqamini yuboring.\nCVV, amal qilish muddati yoki SMS-kodni yubormang.")


@dp.message(CashbackState.waiting_card_number)
async def cashback_receive_card(m: Message, state: FSMContext):
    card_number = re.sub(r"[\s-]", "", (m.text or ""))
    if not re.fullmatch(r"\d{16}", card_number):
        await m.answer("Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qayta yuboring.")
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
        await m.answer("Bu yutuq allaqachon so'ralgan yoki mavjud emas. BONUS bo'limini yangilang.")
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
            )
        ]])
    )
    await m.answer(
        "✅ Ma'lumotlaringiz adminga yuborildi. "
        "Cashback kartangizga o'tkazilgach sizga xabar beramiz."
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


async def account_list_view():
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.id, a.title,
                   (SELECT b.status FROM bookings b
                    WHERE b.account_id=a.id AND b.status IN ('paid','active')
                    ORDER BY b.created_at DESC LIMIT 1) AS booking_status,
                   (SELECT b.expires_at FROM bookings b
                    WHERE b.account_id=a.id AND b.status='active'
                    ORDER BY b.created_at DESC LIMIT 1) AS expires_at,
                   EXISTS(
                       SELECT 1 FROM manual_account_blocks mb
                       WHERE mb.account_id=a.id
                   ) AS manually_blocked
            FROM accounts a
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
                label = f"{a['id']} AKKAUNT — 🔴 BAND ({hh:02d}:{mm:02d}:{ss:02d})"
                buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"account_busy:{a['id']}")])
            else:
                buttons.append([InlineKeyboardButton(text=f"{a['id']} AKKAUNT — 🟢 BO'SH", callback_data=f"account:{a['id']}")])
        elif status == "paid":
            buttons.append([InlineKeyboardButton(text=f"{a['id']} AKKAUNT — 🔴 BAND (ishga tushishi kutilmoqda)", callback_data=f"account_busy:{a['id']}")])
        else:
            buttons.append([InlineKeyboardButton(text=f"{a['id']} AKKAUNT — 🟢 BO'SH", callback_data=f"account:{a['id']}")])
    buttons.append([InlineKeyboardButton(text="🔄 Yangilash", callback_data="accounts")])
    buttons.append([InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="home")])
    text = "🎮 KHOJA RENT — AKKAUNTLAR\n\n🟢 BO'SH — tanlab buyurtma bering\n🔴 BAND — qolgan vaqt ko'rsatiladi"
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


@dp.callback_query(F.data == "home")
async def home(q: CallbackQuery):
    rows = [
        [InlineKeyboardButton(text="🎮 Akkauntlar", callback_data="accounts")],
        [InlineKeyboardButton(text="🎁 BONUS", callback_data="bonus")],
        [InlineKeyboardButton(text="💬 Admin bilan aloqa", url="https://t.me/khoja_arenda")]
    ]
    if q.from_user.id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="⚙️ Admin paneli", callback_data="admin:panel")])
    await q.message.answer("Bosh menyu:", reply_markup=kb(rows))
    await q.answer()


@dp.callback_query(F.data == "accounts")
async def accounts(q: CallbackQuery):
    text, markup = await account_list_view()
    msg = await q.message.answer(text, reply_markup=markup)
    account_views.add((msg.chat.id, msg.message_id))
    await q.answer()


@dp.callback_query(F.data.startswith("account_busy:"))
async def account_busy(q: CallbackQuery):
    await q.answer("Bu akkaunt hozir band. Bo'shashigacha qolgan vaqtni kuzating.", show_alert=True)


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

    await q.message.answer(
        f"Tanlangan: {a['title']}\nTarifni tanlang:",
        reply_markup=kb(buttons)
    )
    await q.answer()


@dp.callback_query(F.data.startswith("duration:"))
async def choose_duration(q: CallbackQuery):
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
            INSERT INTO bookings(user_id, account_id, hours, amount)
            VALUES($1, $2, $3, $4)
            RETURNING id
        """, q.from_user.id, aid, hours, amount)

    label = tariff_title(hours, amount)
    await q.message.answer(
        f"Buyurtma №{booking_id}\n"
        f"{a['title']}\n"
        f"Tarif: {label}\n"
        f"Jami: {amount:,} so'm\n\n"
        f"To'lov kartasi: {CARD_DETAILS}\n\n"
        "To'lovni amalga oshirgach, chek rasmini yuboring. "
        "To'lov admin tomonidan bank ilovasida tekshiriladi."
    )
    await q.message.answer(
        "Chekni yuborish uchun quyidagi tugmani bosing:",
        reply_markup=kb([[
            InlineKeyboardButton(
                text="Chek yuborish",
                callback_data=f"receipt:{booking_id}"
            )
        ]])
    )
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
    await q.message.answer("To'lov chekini rasm ko'rinishida yuboring.")
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
            f"Mijoz ID: {m.from_user.id}\n"
            f"Akkaunt: {a['title']}\n"
            f"Tarif: {label}\n"
            f"Summa: {b['amount']:,} so'm"
        ),
        reply_markup=kb([[
            InlineKeyboardButton(
                text="To'lov tasdiqlandi",
                callback_data=f"approve:{booking_id}"
            ),
            InlineKeyboardButton(
                text="Rad etish",
                callback_data=f"reject:{booking_id}"
            )
        ]])
    )

    await m.answer(
        "Chek adminga yuborildi. To'lov bank ilovasida tekshirilgach javob beramiz."
    )
    await state.clear()


@dp.message(ReceiptState.waiting_receipt)
async def receipt_not_photo(m: Message):
    await m.answer("Iltimos, chekni rasm ko'rinishida yuboring.")


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
        "Ijara boshlanishini kuting. PUBG paroli, SMS/pochta kodi yoki "
        "QR autentifikatsiya tokenini botga yubormang."
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

    await bot.send_message(
        b["user_id"],
        f"Buyurtma №{bid}: to'lov tasdiqlanmadi. Admin bilan bog'laning."
    )
    await q.message.edit_caption(
        (q.message.caption or "") + "\n\nRAD ETILDI"
    )
    await q.answer("Rad etildi.")


@dp.message(Command("start_rental"))
async def start_rental(m: Message):
    if m.from_user.id != ADMIN_ID:
        return

    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Foydalanish: /start_rental BUYURTMA_ID")
        return

    bid = int(parts[1])
    async with pool.acquire() as c:
        b = await c.fetchrow(
            "SELECT * FROM bookings WHERE id=$1 AND status='paid'",
            bid
        )
        if not b:
            await m.answer("To'langan buyurtma topilmadi.")
            return

        now = datetime.now(TASHKENT)

        if b["hours"] == 0:
            # Tungi tarif: ijara qaysi paytda boshlanishidan qat'i nazar,
            # eng yaqin keladigan 09:00 da tugaydi.
            expiry = now.replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            if now >= expiry:
                expiry += timedelta(days=1)
        else:
            expiry = now + timedelta(hours=b["hours"])

        await c.execute(
            "UPDATE bookings SET status='active', expires_at=$1 WHERE id=$2",
            expiry.astimezone(timezone.utc), bid
        )

    await bot.send_message(
        b["user_id"],
        f"Ijara №{bid} boshlandi.\n"
        f"Tugash vaqti: {expiry.strftime('%d.%m.%Y %H:%M')}"
    )
    await m.answer(
        f"Ijara boshlandi. Tugash vaqti: {expiry.strftime('%d.%m.%Y %H:%M')}"
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
            [InlineKeyboardButton(text="📋 Akkauntlar va narxlar", callback_data="admin:list")],
            [InlineKeyboardButton(text="✏️ Akkaunt nomini/almashtirish", callback_data="admin:rename")],
            [InlineKeyboardButton(text="💰 Narxni o'zgartirish", callback_data="admin:price")],
            [InlineKeyboardButton(text="🔄 Akkauntni yashirish/ochish", callback_data="admin:toggle")],
            [InlineKeyboardButton(text="🔴 Qo'lda band qilish / bo'shatish", callback_data="admin:manual_blocks")],
            [InlineKeyboardButton(text="📦 Buyurtmalar", callback_data="admin:bookings")],
            [InlineKeyboardButton(text="🎁 Cashback so'rovlari", callback_data="admin:cashbacks")],
            [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="home")]
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
        await q.message.answer("Hozircha cashback to'lash so'rovlari yo'q.")
    else:
        for r in rows:
            await q.message.answer(
                "🎁 CASHBACK SO'ROVI\n"
                f"Yutuq ID: {r['id']}\nMijoz ID: {r['user_id']}\n"
                f"Ism-familiya: {r['full_name']}\nKarta: {r['card_number']}\n"
                f"Summa: {r['amount']:,} so'm\nBosqich: {r['milestone_number']}",
                reply_markup=kb([[
                    InlineKeyboardButton(
                        text="✅ Cashback to'landi",
                        callback_data=f"bonus_paid:{r['id']}"
                    )
                ]])
            )
    await q.answer()


@dp.callback_query(F.data == "admin:list")
async def admin_list(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.id, a.title, a.active, p.hours, p.price
            FROM accounts a LEFT JOIN account_prices p ON p.account_id=a.id
            ORDER BY a.id, CASE WHEN p.hours=0 THEN 99 ELSE p.hours END
        """)
    lines = []
    for r in rows:
        state = "🟢 faol" if r["active"] else "⚪ yashirilgan"
        price = "narx belgilanmagan" if r["hours"] is None else tariff_title(r["hours"], r["price"])
        lines.append(f"{r['id']}. {r['title']} ({state}) — {price}")
    await q.message.answer("\n".join(lines) or "Akkauntlar yo'q.")
    await q.answer()


@dp.callback_query(F.data == "admin:rename")
async def admin_rename_start(q: CallbackQuery, state: FSMContext):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminState.waiting_rename)
    await q.message.answer("Akkaunt ID va yangi nomni yuboring.\nMasalan: 1 PUBG VIP")
    await q.answer()


@dp.callback_query(F.data == "admin:price")
async def admin_price_start(q: CallbackQuery, state: FSMContext):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminState.waiting_price)
    await q.message.answer("Format: AKKAUNT_ID SOAT NARX\nMasalan: 1 3 150000\nTungi tarifda soat o'rniga 0 yozing.")
    await q.answer()


@dp.callback_query(F.data == "admin:toggle")
async def admin_toggle_start(q: CallbackQuery, state: FSMContext):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminState.waiting_toggle)
    await q.message.answer("Akkaunt ID sini yuboring. Faol akkaunt yashiriladi, yashirilgani qayta ochiladi.")
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


@dp.callback_query(F.data == "admin:bookings")
async def admin_bookings(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT id, account_id, user_id, amount, status
            FROM bookings ORDER BY id DESC LIMIT 20
        """)
    lines = [f"№{r['id']} | akkaunt {r['account_id']} | mijoz {r['user_id']} | {r['amount']:,} so'm | {r['status']}" for r in rows]
    await q.message.answer("\n".join(lines) or "Buyurtmalar yo'q.")
    await q.answer()


@dp.message(AdminState.waiting_rename)
async def admin_rename_save(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await m.answer("Format: 1 PUBG VIP")
        return
    async with pool.acquire() as c:
        result = await c.execute("UPDATE accounts SET title=$1 WHERE id=$2", parts[1].strip(), int(parts[0]))
    await m.answer("Akkaunt nomi yangilandi." if result.endswith("1") else "Akkaunt ID topilmadi.")
    await state.clear()
    await update_account_views()


@dp.message(AdminState.waiting_price)
async def admin_price_save(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        await m.answer("Format: 1 3 150000")
        return
    aid, hours, price = map(int, parts)
    if hours not in (0, 3, 6, 12, 24) or price <= 0:
        await m.answer("Soat 0, 3, 6, 12 yoki 24; narx 0 dan katta bo'lishi kerak.")
        return
    async with pool.acquire() as c:
        exists = await c.fetchval("SELECT EXISTS(SELECT 1 FROM accounts WHERE id=$1)", aid)
        if exists:
            await c.execute("""
                INSERT INTO account_prices(account_id, hours, price) VALUES($1,$2,$3)
                ON CONFLICT(account_id,hours) DO UPDATE SET price=EXCLUDED.price
            """, aid, hours, price)
    await m.answer("Narx saqlandi." if exists else "Akkaunt ID topilmadi.")
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
    total, pending, paid, claimable = await get_bonus_stats(m.from_user.id)
    progress = total % 10
    remaining = 10 - progress if progress else 10
    text = (
        "🎁 KHOJA RENT BONUS\\n\\n"
        f"Jami tasdiqlangan ijaralar: {total} ta\\n"
        f"Keyingi yutuq uchun: {progress}/10 ta\\n"
        f"Yana kerak: {remaining} ta\\n"
        "Har 10 ta tasdiqlangan ijara uchun 100 000 so'm cashback!\\n\\n"
        f"To'lanishi kutilayotgan cashback: {pending:,} so'm\\n"
        f"Oldin to'langan cashback: {paid:,} so'm"
    )
    buttons = []
    if claimable:
        buttons.append([InlineKeyboardButton(
            text=f"🎉 Yutuqni olish ({claimable} ta)",
            callback_data="bonus:claim"
        )])
    await m.answer(text, reply_markup=kb(buttons) if buttons else None)


@dp.message(F.text == "💬 Admin bilan aloqa")
async def reply_menu_contact(m: Message):
    await m.answer("Admin bilan bog'lanish: https://t.me/khoja_arenda")


@dp.message(F.text == "⚙️ Admin paneli")
async def reply_menu_admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "⚙️ ADMIN PANELI — amalni tanlang:",
        reply_markup=kb([
            [InlineKeyboardButton(text="📋 Akkauntlar va narxlar", callback_data="admin:list")],
            [InlineKeyboardButton(text="✏️ Akkaunt nomini/almashtirish", callback_data="admin:rename")],
            [InlineKeyboardButton(text="💰 Narxni o'zgartirish", callback_data="admin:price")],
            [InlineKeyboardButton(text="🔄 Akkauntni yashirish/ochish", callback_data="admin:toggle")],
            [InlineKeyboardButton(text="🔴 Qo'lda band qilish / bo'shatish", callback_data="admin:manual_blocks")],
            [InlineKeyboardButton(text="📦 Buyurtmalar", callback_data="admin:bookings")],
            [InlineKeyboardButton(text="🎁 Cashback so'rovlari", callback_data="admin:cashbacks")],
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
        await asyncio.sleep(30)


async def main():
    await init_db()

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

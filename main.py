import os
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Assalomu alaykum! KHOJA RENT AI botiga xush kelibsiz.\n"
        "PUBG akkaunt ijarasi uchun quyidagidan tanlang:",
        reply_markup=kb([[
            InlineKeyboardButton(
                text="Akkauntlarni ko'rish",
                callback_data="accounts"
            )
        ]])
    )


@dp.callback_query(F.data == "accounts")
async def accounts(q: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT a.id, a.title,
                   COALESCE(
                       string_agg(
                           CASE WHEN p.hours=0
                                THEN 'TUNGI TARIF - ' || p.price::text
                                ELSE p.hours::text || ' SOAT - ' || p.price::text
                           END,
                           ' | ' ORDER BY CASE WHEN p.hours=0 THEN 99 ELSE p.hours END
                       ), 'Narx admin tomonidan belgilanadi'
                   ) AS prices
            FROM accounts a
            LEFT JOIN account_prices p ON p.account_id=a.id AND p.price > 0
            WHERE a.active=TRUE
            GROUP BY a.id, a.title
            ORDER BY a.id
        """)

    if not rows:
        await q.message.answer("Hozircha bo'sh akkaunt yo'q.")
    else:
        buttons = []
        for a in rows:
            buttons.append([InlineKeyboardButton(
                text=f"{a['title']} — {a['prices'][:45]}",
                callback_data=f"account:{a['id']}"
            )])
        await q.message.answer(
            "Akkauntni tanlang:",
            reply_markup=kb(buttons)
        )
    await q.answer()


@dp.callback_query(F.data.startswith("account:"))
async def choose_account(q: CallbackQuery):
    account_id = int(q.data.split(":")[1])

    async with pool.acquire() as c:
        a = await c.fetchrow(
            "SELECT * FROM accounts WHERE id=$1 AND active=TRUE",
            account_id
        )
        prices = await c.fetch("""
            SELECT hours, price
            FROM account_prices
            WHERE account_id=$1 AND price > 0
            ORDER BY CASE WHEN hours=0 THEN 99 ELSE hours END
        """, account_id)

    if not a:
        await q.answer("Akkaunt topilmadi.", show_alert=True)
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

    await bot.send_message(
        b["user_id"],
        f"Buyurtma №{bid}: to'lov tasdiqlandi.\n"
        "Ijara boshlanishini kuting. PUBG paroli, SMS/pochta kodi yoki "
        "QR autentifikatsiya tokenini botga yubormang."
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

        await asyncio.sleep(30)


async def main():
    await init_db()
    asyncio.create_task(expiry_watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

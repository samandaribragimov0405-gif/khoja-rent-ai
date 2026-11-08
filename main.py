import os
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
CARD_DETAILS = os.getenv("CARD_DETAILS", "Admin bilan bog'laning")
DURATIONS = [1, 3, 6, 12, 24]

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool = None

class ReceiptState(StatesGroup):
    waiting_receipt = State()

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS accounts(
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            hourly_price BIGINT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE
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
        """)
        count = await c.fetchval("SELECT COUNT(*) FROM accounts")
        if count == 0:
            # 30 editable placeholders. Admin can rename/update them in PostgreSQL.
            for i in range(1, 31):
                await c.execute(
                    "INSERT INTO accounts(title, hourly_price) VALUES($1, $2)",
                    f"PUBG akkaunt {i:02d}", 0
                )

@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Assalomu alaykum! KHOJA RENT AI botiga xush kelibsiz.\n"
        "PUBG akkaunt ijarasi uchun quyidagidan tanlang:",
        reply_markup=kb([[InlineKeyboardButton(text="Akkauntlarni ko'rish", callback_data="accounts")]])
    )

@dp.callback_query(F.data == "accounts")
async def accounts(q: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT id,title,hourly_price FROM accounts WHERE active=TRUE ORDER BY id")
    if not rows:
        await q.message.answer("Hozircha bo'sh akkaunt yo'q.")
    else:
        buttons = []
        for a in rows:
            price = f"{a['hourly_price']:,} so'm/soat" if a["hourly_price"] else "Narx admin tomonidan belgilanadi"
            buttons.append([InlineKeyboardButton(
                text=f"{a['title']} — {price}", callback_data=f"account:{a['id']}"
            )])
        await q.message.answer("Akkauntni tanlang:", reply_markup=kb(buttons))
    await q.answer()

@dp.callback_query(F.data.startswith("account:"))
async def choose_account(q: CallbackQuery):
    account_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        a = await c.fetchrow("SELECT * FROM accounts WHERE id=$1 AND active=TRUE", account_id)
    if not a:
        await q.answer("Akkaunt topilmadi.", show_alert=True)
        return
    if a["hourly_price"] <= 0:
        await q.answer("Bu akkaunt narxi hali belgilanmagan. Admin bilan bog'laning.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"{h} soat", callback_data=f"duration:{account_id}:{h}")]
               for h in DURATIONS]
    await q.message.answer(
        f"Tanlangan: {a['title']}\nSoatlik narx: {a['hourly_price']:,} so'm\n"
        "Necha soatga olmoqchisiz?",
        reply_markup=kb(buttons)
    )
    await q.answer()

@dp.callback_query(F.data.startswith("duration:"))
async def choose_duration(q: CallbackQuery):
    _, aid, hs = q.data.split(":")
    aid, hours = int(aid), int(hs)
    async with pool.acquire() as c:
        a = await c.fetchrow("SELECT * FROM accounts WHERE id=$1 AND active=TRUE", aid)
        if not a or a["hourly_price"] <= 0:
            await q.answer("Narx mavjud emas.", show_alert=True)
            return
        amount = a["hourly_price"] * hours
        booking_id = await c.fetchval(
            """INSERT INTO bookings(user_id,account_id,hours,amount)
               VALUES($1,$2,$3,$4) RETURNING id""",
            q.from_user.id, aid, hours, amount
        )
    await q.message.answer(
        f"Buyurtma №{booking_id}\n{a['title']} — {hours} soat\n"
        f"Jami: {amount:,} so'm\n\n"
        f"To'lov kartasi: {CARD_DETAILS}\n\n"
        "To'lovni amalga oshirgach, chek rasmini shu chatga yuboring. "
        "To'lov admin tomonidan bank ilovasida tekshiriladi."
    )
    await q.message.answer("Chekni yuborish uchun quyidagi tugmani bosing:",
        reply_markup=kb([[InlineKeyboardButton(text="Chek yuborish", callback_data=f"receipt:{booking_id}")]]))
    await q.answer()

@dp.callback_query(F.data.startswith("receipt:"))
async def receipt_start(q: CallbackQuery, state: FSMContext):
    booking_id = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        b = await c.fetchrow(
            "SELECT id FROM bookings WHERE id=$1 AND user_id=$2 AND status='awaiting_payment'",
            booking_id, q.from_user.id
        )
    if not b:
        await q.answer("Buyurtma topilmadi yoki allaqachon ko'rib chiqilmoqda.", show_alert=True)
        return
    await state.update_data(booking_id=booking_id)
    await state.set_state(ReceiptState.waiting_receipt)
    await q.message.answer("To'lov chekini rasm yoki fayl ko'rinishida yuboring.")
    await q.answer()

@dp.message(ReceiptState.waiting_receipt, F.photo)
async def receive_receipt(m: Message, state: FSMContext):
    data = await state.get_data()
    booking_id = data["booking_id"]
    file_id = m.photo[-1].file_id
    async with pool.acquire() as c:
        await c.execute("UPDATE bookings SET receipt_file_id=$1,status='receipt_received' WHERE id=$2 AND user_id=$3",
                        file_id, booking_id, m.from_user.id)
        b = await c.fetchrow("SELECT * FROM bookings WHERE id=$1", booking_id)
        a = await c.fetchrow("SELECT title FROM accounts WHERE id=$1", b["account_id"])
    await bot.send_photo(
        ADMIN_ID, file_id,
        caption=f"Chek — buyurtma №{booking_id}\nMijoz: {m.from_user.id}\n"
                f"Akkaunt: {a['title']}\nMuddat: {b['hours']} soat\nSumma: {b['amount']:,} so'm",
        reply_markup=kb([[
            InlineKeyboardButton(text="To'lov tasdiqlandi", callback_data=f"approve:{booking_id}"),
            InlineKeyboardButton(text="Rad etish", callback_data=f"reject:{booking_id}")
        ]])
    )
    await m.answer("Chek adminga yuborildi. To'lov bank ilovasida tekshirilgach javob beramiz.")
    await state.clear()

@dp.message(ReceiptState.waiting_receipt)
async def receipt_not_photo(m: Message):
    await m.answer("Iltimos, chekni rasm ko'rinishida yuboring.")

@dp.callback_query(F.data.startswith("approve:"))
async def approve(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True); return
    bid = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        b = await c.fetchrow("SELECT * FROM bookings WHERE id=$1", bid)
        if not b or b["status"] != "receipt_received":
            await q.answer("Buyurtma holati o'zgargan.", show_alert=True); return
        await c.execute("UPDATE bookings SET status='paid' WHERE id=$1", bid)
    await bot.send_message(
        b["user_id"],
        f"Buyurtma №{bid}: to'lov tasdiqlandi.\n"
        "Endi PUBG ilovasida rasmiy kirish usulidan o'zingiz foydalaning. "
        "Parol, SMS/pochta kodi yoki QR autentifikatsiya tokenini botga yubormang. "
        "Kirish tayyor bo'lgach, admin bilan bog'laning."
    )
    await q.message.edit_caption((q.message.caption or "") + "\n\nTO'LOV TASDIQLANDI")
    await q.answer("Tasdiqlandi.")

@dp.callback_query(F.data.startswith("reject:"))
async def reject(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        await q.answer("Ruxsat yo'q.", show_alert=True); return
    bid = int(q.data.split(":")[1])
    async with pool.acquire() as c:
        b = await c.fetchrow("SELECT * FROM bookings WHERE id=$1", bid)
        if not b:
            await q.answer("Buyurtma topilmadi.", show_alert=True); return
        await c.execute("UPDATE bookings SET status='rejected' WHERE id=$1", bid)
    await bot.send_message(b["user_id"], f"Buyurtma №{bid}: to'lov tasdiqlanmadi. Admin bilan bog'laning.")
    await q.message.edit_caption((q.message.caption or "") + "\n\nRAD ETILDI")
    await q.answer("Rad etildi.")

@dp.message(Command("start_rental"))
async def start_rental(m: Message):
    if m.from_user.id != ADMIN_ID: return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Foydalanish: /start_rental BUYURTMA_ID"); return
    bid = int(parts[1])
    async with pool.acquire() as c:
        b = await c.fetchrow("SELECT * FROM bookings WHERE id=$1 AND status='paid'", bid)
        if not b:
            await m.answer("To'langan buyurtma topilmadi."); return
        expiry = datetime.now(timezone.utc) + timedelta(hours=b["hours"])
        await c.execute("UPDATE bookings SET status='active',expires_at=$1 WHERE id=$2", expiry, bid)
    await bot.send_message(b["user_id"], f"Ijara №{bid} boshlandi. Tugash vaqti: {expiry.astimezone().strftime('%Y-%m-%d %H:%M')}")
    await m.answer("Ijara boshlandi va vaqti qayd etildi.")

@dp.message(Command("setprice"))
async def set_price(m: Message):
    if m.from_user.id != ADMIN_ID: return
    parts = m.text.split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await m.answer("Foydalanish: /setprice AKKAUNT_ID SOATLIK_NARX"); return
    aid, price = int(parts[1]), int(parts[2])
    async with pool.acquire() as c:
        result = await c.execute("UPDATE accounts SET hourly_price=$1 WHERE id=$2", price, aid)
    await m.answer("Narx saqlandi." if result.endswith("1") else "Akkaunt ID topilmadi.")

@dp.message(Command("accounts"))
async def admin_accounts(m: Message):
    if m.from_user.id != ADMIN_ID: return
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT id,title,hourly_price FROM accounts ORDER BY id")
    lines = [f"{r['id']}. {r['title']} — {r['hourly_price']:,} so'm/soat" for r in rows]
    await m.answer("\n".join(lines) or "Akkauntlar yo'q.")

async def expiry_watcher():
    while True:
        try:
            async with pool.acquire() as c:
                rows = await c.fetch(
                    "SELECT id,user_id,expires_at FROM bookings WHERE status='active' AND expires_at <= NOW()"
                )
                for b in rows:
                    await c.execute("UPDATE bookings SET status='expired' WHERE id=$1", b["id"])
                    await bot.send_message(
                        b["user_id"],
                        f"Ijara №{b['id']} muddati tugadi. Akkauntdan chiqishingizni so'raymiz. "
                        "Admin PUBG'ning rasmiy xavfsizlik sozlamalari orqali sessiya holatini tekshiradi."
                    )
                    await bot.send_message(ADMIN_ID, f"Ijara №{b['id']} tugadi. Rasmiy usulda sessiyani bekor qilishni tekshiring.")
        except Exception as e:
            print("Expiry watcher error:", repr(e))
        await asyncio.sleep(30)

async def main():
    await init_db()
    asyncio.create_task(expiry_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

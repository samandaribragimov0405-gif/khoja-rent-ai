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

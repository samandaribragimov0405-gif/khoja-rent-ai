# KHOJA RENT AI — starter

## Muhim xavfsizlik cheklovlari
- Bot mijozdan PUBG paroli, email/SMS kodi yoki QR autentifikatsiya tokenini so'ramaydi va saqlamaydi.
- Mijoz PUBG ilovasida rasmiy kirish jarayonini o'zi tasdiqlaydi.
- Ijara tugaganda bot mijoz va adminni ogohlantiradi. Faol sessiyani avtomatik bekor qilish faqat PUBG rasmiy ravishda qo'llab-quvvatlaydigan xavfsiz API/usul bo'lsa qo'shiladi.
- Bank kartasiga oddiy o'tkazma avtomatik tasdiqlanmaydi. Ushbu starterda admin bank ilovasidagi haqiqiy tushumni tekshirib, botda tasdiqlaydi. Chek rasmi yolg'iz o'zi tasdiq emas.

## Railway sozlash
1. Railway'da PostgreSQL qo'shing.
2. GitHub repository'ga ushbu fayllarni yuklang.
3. Railway'da GitHub repository'ni tanlab deploy qiling.
4. Variables bo'limida `.env.example` dagi qiymatlarni kiriting.
5. Deploy loglarini tekshiring.

## Bot buyruqlari
- `/start` — mijoz menyusi
- `/accounts` — admin akkauntlar va narxlar ro'yxati
- `/setprice AKKAUNT_ID SOATLIK_NARX` — soatlik narx
- `/start_rental BUYURTMA_ID` — to'lov tasdiqlangach ijara vaqtini boshlash

Boshlang'ich bazada 30 ta nomi vaqtinchalik akkaunt yozuvi yaratiladi. Narxlar admin tomonidan `/setprice` bilan belgilanadi.

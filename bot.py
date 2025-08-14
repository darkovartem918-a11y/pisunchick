# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================
# НАСТРОЙКИ (ВПИШИ СВОЁ)
# =========================
BOT_TOKEN = "8260873367:AAFukyyO1G1jv3s2DhR8JPGIn5RIt_Y8iRQ"
CRYPTOBOT_TOKEN = "445933:AAXG2qakL3LbL0A7NXU6NN8zwsr633StfIo"   # из @CryptoBot -> @CryptoPayAPI
ADMIN_CHAT_ID = -1002882485091                        # id админ-чата (или твой user id)

# Покупка звёзд
PRICE_PER_STAR_RUB = 1.8
MIN_STARS = 100
USDT_TO_RUB = 80  # фикс RUB→USDT (1 USDT = 80₽) для пополнений и покупки

SUPPORT_CONTACT = "@homkaqwerty2"
REVIEWS_URL = "https://t.me/noviuyz"

DB_FILE = "db.json"

# =========================
# ЛОГИ
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stars-exchange-bot")

# =========================
# МОДЕЛИ ДАННЫХ
# =========================
class BuyStates(StatesGroup):
    waiting_stars = State()
    waiting_username = State()

class ExchangeStates(StatesGroup):
    waiting_amount = State()
    waiting_requisites = State()

class BalanceStates(StatesGroup):
    waiting_deposit_rub = State()
    paying_with_balance_stars = State()
    paying_with_balance_username = State()

@dataclass
class BaseOrder:
    order_id: int
    user_id: int
    invoice_id: Optional[int] = None
    usdt_amount: float = 0.0
    status: str = "new"              # new|pending|paid|done
    admin_msg_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)

@dataclass
class StarOrder(BaseOrder):
    stars: int = 0
    username_for_stars: str = ""

@dataclass
class ExchangeOrder(BaseOrder):
    rate: int = 0
    payout_rub: int = 0
    requisites: str = ""

@dataclass
class TopUp:
    topup_id: int
    user_id: int
    invoice_id: Optional[int] = None
    rub_amount: int = 0
    usdt_amount: float = 0.0
    status: str = "new"              # new|paid|done
    created_at: float = field(default_factory=time.time)

# =========================
# ПАМЯТЬ (IN-MEMORY)
# =========================
star_orders: Dict[int, StarOrder] = {}           # user_id -> текущий заказ на звёзды (оплата USDT)
exchange_orders: Dict[int, ExchangeOrder] = {}   # user_id -> текущий обмен
orders_by_id: Dict[int, BaseOrder] = {}          # order_id -> любой заказ (для админ-кнопок)
topups_by_user: Dict[int, TopUp] = {}            # последний активный топап пользователя
topups_by_id: Dict[int, TopUp] = {}              # по id

# =========================
# ХРАНИЛИЩЕ (ПЕРСИСТЕНТНОЕ)
# =========================
db = {
    "users": {},   # user_id: {"balance_rub": int, "paid_orders": int, "spent_rub": int, "visited": bool}
    "stats": {
        "visitors": 0,
        "total_paid_orders": 0,    # количество завершённых заказов (звёзды + обмен)
        "active_orders": 0,        # текущее число активных (paid|pending)
        "total_topups_rub": 0,
        "total_spent_rub": 0
    }
}

def load_db():
    global db
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
            # страховка типов
            db.setdefault("users", {})
            db.setdefault("stats", {})
            db["stats"].setdefault("visitors", 0)
            db["stats"].setdefault("total_paid_orders", 0)
            db["stats"].setdefault("active_orders", 0)
            db["stats"].setdefault("total_topups_rub", 0)
            db["stats"].setdefault("total_spent_rub", 0)
    except FileNotFoundError:
        save_db()

def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def ensure_user(user_id: int):
    if str(user_id) not in db["users"]:
        db["users"][str(user_id)] = {
            "balance_rub": 0,
            "paid_orders": 0,
            "spent_rub": 0,
            "visited": False
        }
        save_db()

def mark_visited(user_id: int):
    ensure_user(user_id)
    if not db["users"][str(user_id)]["visited"]:
        db["users"][str(user_id)]["visited"] = True
        db["stats"]["visitors"] += 1
        save_db()

# =========================
# ИНИЦИАЛИЗАЦИЯ БОТА
# =========================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# =========================
# УТИЛИТЫ
# =========================
def exchange_rate_for(usdt: float) -> int:
    if usdt < 1:
        return 70
    if 1 <= usdt <= 6:
        return 70
    if 7 <= usdt <= 15:
        return 72
    if 16 <= usdt <= 30:
        return 73
    if 31 <= usdt <= 50:
        return 74
    if 51 <= usdt <= 70:
        return 75
    if 71 <= usdt <= 100:
        return 79
    return 79

def gen_order_id() -> int:
    # случайный 6-значный
    return random.randint(100000, 999999)

def gen_topup_id() -> int:
    return random.randint(100000, 999999)

async def cb_create_invoice_usdt(amount_usdt: float, description: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json={
                "asset": "USDT",
                "amount": round(float(amount_usdt), 2),
                "description": description,
                "allow_comments": False
            }
        ) as r:
            data = await r.json()
            log.info(f"[CryptoBot] createInvoice -> {data}")
            return data

async def cb_get_invoice(invoice_id: int) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        ) as r:
            data = await r.json()
            log.info(f"[CryptoBot] getInvoices -> {data}")
            return data

def user_check_kb(order_id: int, invoice_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check:{order_id}:{invoice_id}")]
        ]
    )

def user_check_topup_kb(topup_id: int, invoice_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить оплату пополнения", callback_data=f"check_topup:{topup_id}:{invoice_id}")]
        ]
    )

def admin_pending_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Заказ в ожидании", callback_data=f"admin_pending:{order_id}")]
        ]
    )

def admin_done_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Заказ выполнен", callback_data=f"admin_done:{order_id}")]
        ]
    )

# =========================
# КЛАВИАТУРЫ МЕНЮ
# =========================
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💫 Купить звёзды"), KeyboardButton(text="💱 Обмен USDT на ₽")],
        [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="💳 Оплатить картой")],
        [KeyboardButton(text="🛠 Техподдержка"), KeyboardButton(text="⭐️ Отзывы / Репутация")],
    ],
    resize_keyboard=True
)
back_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔙 Вернуться в меню")]],
    resize_keyboard=True
)

# =========================
# ГЛОБАЛЬНАЯ «Назад»
# =========================
@dp.message(F.text == "🔙 Вернуться в меню")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Вы в главном меню.", reply_markup=main_kb)

# =========================
# /start
# =========================
@dp.message(Command("start", "menu"))
async def start_cmd(message: Message, state: FSMContext):
    load_db()
    mark_visited(message.from_user.id)
    await state.clear()
    await message.answer(
        "Привет! 👋\n\n"
        "— <b>Купить звёзды</b> по 1.8₽/шт (оплата USDT или с баланса)\n"
        "— <b>Обмен USDT→₽</b> по фиксированным курсам\n"
        "— <b>Личный кабинет</b> для пополнения и оплаты с баланса\n\n"
        "Выбирай действие 👇",
        reply_markup=main_kb
    )

# =========================
# ЛИЧНЫЙ КАБИНЕТ
# =========================
def cabinet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="cab:topup")],
            [InlineKeyboardButton(text="💫 Оплатить звёзды с баланса", callback_data="cab:pay_stars")]
        ]
    )

@dp.message(F.text == "👤 Личный кабинет")
async def my_cabinet(message: Message):
    load_db()
    ensure_user(message.from_user.id)
    u = db["users"][str(message.from_user.id)]
    # посчитаем активные (по памяти)
    active_orders = sum(1 for o in orders_by_id.values()
                        if o.user_id == message.from_user.id and o.status in ("paid", "pending"))
    txt = (
        "<b>Личный кабинет</b>\n\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"Баланс: <b>{u['balance_rub']} ₽</b>\n"
        f"Заказов завершено: <b>{u['paid_orders']}</b>\n"
        f"Активных заказов: <b>{active_orders}</b>"
    )
    await message.answer(txt, reply_markup=cabinet_kb())

# ---- Пополнение баланса
@dp.callback_query(F.data == "cab:topup")
async def cab_topup(call: CallbackQuery, state: FSMContext):
    await state.set_state(BalanceStates.waiting_deposit_rub)
    await call.message.answer(
        "Введите сумму пополнения в ₽ (целое число). Оплата будет в USDT по фиксированному курсу.",
        reply_markup=back_kb
    )
    await call.answer()

@dp.message(BalanceStates.waiting_deposit_rub, F.text)
async def on_deposit_amount(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    try:
        rub = int(message.text.strip())
        if rub <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно целое число ₽ больше 0. Пример: 500", reply_markup=back_kb)
        return

    usdt = round(rub / USDT_TO_RUB, 2)
    topup_id = gen_topup_id()
    resp = await cb_create_invoice_usdt(usdt, f"Пополнение баланса на {rub}₽ (#{topup_id})")
    if not resp.get("ok"):
        err = resp.get("error", {}).get("message", "Ошибка создания счёта")
        await message.answer(f"❌ {err}", reply_markup=main_kb)
        await state.clear()
        return

    invoice_url = resp["result"]["pay_url"]
    invoice_id = int(resp["result"]["invoice_id"])

    topup = TopUp(
        topup_id=topup_id,
        user_id=message.from_user.id,
        invoice_id=invoice_id,
        rub_amount=rub,
        usdt_amount=usdt,
        status="new"
    )
    topups_by_user[message.from_user.id] = topup
    topups_by_id[topup_id] = topup

    await state.clear()
    await message.answer(
        "🧾 <b>Счёт на пополнение создан</b>\n"
        f"ID пополнения: <b>#{topup_id}</b>\n"
        f"К зачислению: <b>{rub} ₽</b>\n"
        f"К оплате: <b>{usdt} USDT</b>\n\n"
        f"Оплатите по ссылке:\n{invoice_url}\n\n"
        "После оплаты нажмите кнопку ниже.",
        reply_markup=user_check_topup_kb(topup_id, invoice_id)
    )

@dp.callback_query(F.data.startswith("check_topup:"))
async def on_check_topup(call: CallbackQuery):
    try:
        _, topup_id_str, invoice_id_str = call.data.split(":")
        topup_id = int(topup_id_str)
        invoice_id = int(invoice_id_str)
    except Exception:
        await call.answer("Неверные данные.", show_alert=True)
        return

    top = topups_by_id.get(topup_id)
    if not top or top.user_id != call.from_user.id:
        await call.answer("Пополнение не найдено.", show_alert=True)
        return

    data = await cb_get_invoice(invoice_id)
    if not data.get("ok") or not data["result"]["items"]:
        err = data.get("error", {}).get("message", "Ошибка проверки.")
        await call.answer(f"Ошибка: {err}", show_alert=True)
        return

    status = data["result"]["items"][0]["status"]
    if status != "paid":
        await call.answer("Оплата не найдена. Проверьте позже.", show_alert=True)
        return

    if top.status == "paid":
        await call.answer("Это пополнение уже зачислено.", show_alert=False)
        return

    # Зачисляем баланс
    load_db()
    ensure_user(top.user_id)
    db["users"][str(top.user_id)]["balance_rub"] += int(top.rub_amount)
    db["stats"]["total_topups_rub"] += int(top.rub_amount)
    save_db()

    top.status = "paid"

    await call.message.answer(
        f"✅ Пополнение было получено.\n"
        f"На ваш баланс зачислено <b>{top.rub_amount} ₽</b>.",
        reply_markup=main_kb
    )
    await call.answer()

# ---- Оплата звёзд с баланса
@dp.callback_query(F.data == "cab:pay_stars")
async def cab_pay_stars(call: CallbackQuery, state: FSMContext):
    await state.set_state(BalanceStates.paying_with_balance_stars)
    await call.message.answer(
        f"Сколько звёзд хотите купить с баланса? Минимум {MIN_STARS}. Отправьте целое число.",
        reply_markup=back_kb
    )
    await call.answer()

@dp.message(BalanceStates.paying_with_balance_stars, F.text)
async def on_pay_stars_from_balance_amount(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    if not message.text.isdigit():
        await message.answer("Нужно целое число. Пример: 250", reply_markup=back_kb)
        return

    stars = int(message.text)
    if stars < MIN_STARS:
        await message.answer(f"Минимум для покупки — {MIN_STARS} звёзд.", reply_markup=back_kb)
        return

    await state.update_data(stars=stars)
    await state.set_state(BalanceStates.paying_with_balance_username)
    await message.answer("Укажите юзернейм для выдачи звёзд (формат: @username).", reply_markup=back_kb)

@dp.message(BalanceStates.paying_with_balance_username, F.text)
async def on_pay_stars_from_balance_username(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    username = message.text.strip()
    if not username.startswith("@"):
        await message.answer("Пожалуйста, укажите юзернейм в формате @username.", reply_markup=back_kb)
        return

    data = await state.get_data()
    stars = int(data["stars"])
    rub_price = int(stars * PRICE_PER_STAR_RUB)

    load_db()
    ensure_user(message.from_user.id)
    bal = db["users"][str(message.from_user.id)]["balance_rub"]

    if bal < rub_price:
        need = rub_price - bal
        await message.answer(
            f"Недостаточно средств на балансе.\n"
            f"Цена заказа: {rub_price} ₽\n"
            f"Ваш баланс: {bal} ₽\n"
            f"Не хватает: {need} ₽\n\n"
            "Пополните баланс через Личный кабинет.",
            reply_markup=main_kb
        )
        await state.clear()
        return

    # списываем
    db["users"][str(message.from_user.id)]["balance_rub"] = bal - rub_price
    db["users"][str(message.from_user.id)]["paid_orders"] += 1
    db["users"][str(message.from_user.id)]["spent_rub"] += rub_price
    db["stats"]["total_spent_rub"] += rub_price
    db["stats"]["total_paid_orders"] += 1
    save_db()

    # создаём «виртуальный» заказ (без CryptoBot) и моментально отправляем админам
    order_id = gen_order_id()
    order = StarOrder(
        order_id=order_id,
        user_id=message.from_user.id,
        invoice_id=None,
        usdt_amount=0.0,  # оплата с баланса
        stars=stars,
        username_for_stars=username,
        status="paid"
    )
    orders_by_id[order_id] = order

    admin_text = (
        "✨ Заказ на звёзды (оплата с баланса)\n"
        f"ID заказа: #{order_id}\n"
        f"Пользователь: @{message.from_user.username or 'id:'+str(order.user_id)}\n"
        f"Адрес выдачи: {username}\n"
        f"Количество: {stars}\n"
        f"Оплата: с баланса, списано {rub_price} ₽"
    )
    try:
        msg = await bot.send_message(ADMIN_CHAT_ID, admin_text, reply_markup=admin_pending_kb(order_id))
        order.admin_msg_id = msg.message_id
    except Exception as e:
        log.error(f"Не удалось отправить заказ в админ-чат: {e}")

    await message.answer(
        f"✅ Заказ #{order_id} оплачен с баланса и передан администрации. Ожидайте выполнения.",
        reply_markup=main_kb
    )
    await state.clear()

# =========================
# ПОКУПКА ЗВЁЗД (через USDT)
# =========================
@dp.message(F.text == "💫 Купить звёзды")
async def buy_stars_start(message: Message, state: FSMContext):
    await state.set_state(BuyStates.waiting_stars)
    await message.answer(
        f"Сколько звёзд хотите? Минимум <b>{MIN_STARS}</b>.\n"
        "Отправьте целое число.",
        reply_markup=back_kb
    )

@dp.message(BuyStates.waiting_stars, F.text)
async def buy_stars_amount(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    if not message.text.isdigit():
        await message.answer("Нужно целое число. Пример: 200", reply_markup=back_kb)
        return

    stars = int(message.text)
    if stars < MIN_STARS:
        await message.answer(f"Минимум для покупки — {MIN_STARS} звёзд.", reply_markup=back_kb)
        return

    await state.update_data(stars=stars)
    await state.set_state(BuyStates.waiting_username)
    await message.answer(
        "Укажите <b>юзернейм</b>, куда выдать звёзды (пример: <code>@username</code>).",
        reply_markup=back_kb
    )

@dp.message(BuyStates.waiting_username, F.text)
async def buy_stars_username(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    username = message.text.strip()
    if not username.startswith("@"):
        await message.answer("Пожалуйста, укажите юзернейм в формате <code>@username</code>.", reply_markup=back_kb)
        return

    data = await state.get_data()
    stars = int(data["stars"])

    rub_sum = stars * PRICE_PER_STAR_RUB
    usdt_sum = round(rub_sum / USDT_TO_RUB, 2)
    order_id = gen_order_id()

    resp = await cb_create_invoice_usdt(usdt_sum, f"Покупка {stars} звёзд (#{order_id})")
    if not resp.get("ok"):
        err = resp.get("error", {}).get("message", "Ошибка создания счёта")
        await message.answer(f"❌ {err}", reply_markup=main_kb)
        await state.clear()
        return

    invoice_url = resp["result"]["pay_url"]
    invoice_id = int(resp["result"]["invoice_id"])

    order = StarOrder(
        order_id=order_id,
        user_id=message.from_user.id,
        invoice_id=invoice_id,
        usdt_amount=usdt_sum,
        stars=stars,
        username_for_stars=username,
        status="new"
    )
    star_orders[message.from_user.id] = order
    orders_by_id[order_id] = order

    await message.answer(
        "🧾 <b>Счёт на покупку звёзд создан</b>\n"
        f"ID заказа: <b>#{order_id}</b>\n"
        f"Адрес выдачи: <b>{username}</b>\n"
        f"Кол-во: <b>{stars}</b>\n"
        f"К оплате: <b>{usdt_sum} USDT</b>\n\n"
        f"Оплатите по ссылке:\n{invoice_url}\n\n"
        "После оплаты нажмите «✅ Проверить оплату».",
        reply_markup=user_check_kb(order_id, invoice_id)
    )
    await state.clear()

# =========================
# ОБМЕН USDT → ₽
# =========================
@dp.message(F.text == "💱 Обмен USDT на ₽")
async def exch_start(message: Message, state: FSMContext):
    await state.set_state(ExchangeStates.waiting_amount)
    await message.answer(
        "Сколько USDT хотите обменять? (число, можно с точкой)\n\n"
        "<i>Курсы:</i>\n"
        "1–6$ → 70₽\n7–15$ → 72₽\n16–30$ → 73₽\n31–50$ → 74₽\n51–70$ → 75₽\n71–100$ → 79₽",
        reply_markup=back_kb
    )

@dp.message(ExchangeStates.waiting_amount, F.text)
async def exch_amount(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    txt = message.text.replace(",", ".").strip()
    try:
        usdt = float(txt)
    except ValueError:
        await message.answer("Не понял сумму. Пример: 12.5", reply_markup=back_kb)
        return
    if usdt <= 0:
        await message.answer("Сумма должна быть больше 0.", reply_markup=back_kb)
        return

    rate = exchange_rate_for(usdt)
    payout = int(round(usdt * rate))

    await state.update_data(usdt=round(usdt, 2), rate=rate, payout=payout)
    await state.set_state(ExchangeStates.waiting_requisites)
    await message.answer(
        f"Курс: <b>{rate} ₽</b> за 1 USDT\n"
        f"К выплате: <b>{payout} ₽</b>\n\n"
        f"Отправьте одним сообщением <b>реквизиты</b> (номер карты/телефон + банк).",
        reply_markup=back_kb
    )

@dp.message(ExchangeStates.waiting_requisites, F.text & F.text.len() >= 4)
async def exch_requisites(message: Message, state: FSMContext):
    if message.text == "🔙 Вернуться в меню":
        await back_to_menu(message, state)
        return

    data = await state.get_data()
    usdt = float(data["usdt"])
    rate = int(data["rate"])
    payout = int(data["payout"])
    requisites = message.text.strip()

    order_id = gen_order_id()
    desc = f"Обмен USDT→RUB: {usdt} USDT, курс {rate}₽ (#{order_id})"
    inv = await cb_create_invoice_usdt(usdt, desc)
    if not inv.get("ok"):
        err = inv.get("error", {}).get("message", "Ошибка создания счёта")
        await message.answer(f"❌ {err}", reply_markup=main_kb)
        await state.clear()
        return

    invoice_url = inv["result"]["pay_url"]
    invoice_id = int(inv["result"]["invoice_id"])

    order = ExchangeOrder(
        order_id=order_id,
        user_id=message.from_user.id,
        invoice_id=invoice_id,
        usdt_amount=usdt,
        rate=rate,
        payout_rub=payout,
        requisites=requisites,
        status="new"
    )
    exchange_orders[message.from_user.id] = order
    orders_by_id[order_id] = order

    await state.clear()
    await message.answer(
        f"🧾 <b>Заявка #{order_id} создана</b>\n"
        f"К оплате: <b>{usdt} USDT</b>\n\n"
        f"Оплата по ссылке:\n{invoice_url}\n\n"
        f"После оплаты нажмите «✅ Проверить оплату».",
        reply_markup=user_check_kb(order_id, invoice_id)
    )

# =========================
# КНОПКА ПОЛЬЗОВАТЕЛЯ: ПРОВЕРИТЬ ОПЛАТУ (заказы)
# =========================
@dp.callback_query(F.data.startswith("check:"))
async def on_check_payment(call: CallbackQuery):
    try:
        _, order_id_str, invoice_id_str = call.data.split(":")
        order_id = int(order_id_str)
        invoice_id = int(invoice_id_str)
    except Exception:
        await call.answer("Неверные данные.", show_alert=True)
        return

    order: Optional[BaseOrder] = orders_by_id.get(order_id)
    if not order or order.user_id != call.from_user.id:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    data = await cb_get_invoice(invoice_id)
    if not data.get("ok") or not data["result"]["items"]:
        err = data.get("error", {}).get("message", "Ошибка проверки.")
        await call.answer(f"Ошибка: {err}", show_alert=True)
        return

    status = data["result"]["items"][0]["status"]
    if status != "paid":
        await call.answer("Оплата не найдена. Проверьте позже.", show_alert=True)
        return

    if order.status in ("paid", "pending", "done"):
        await call.answer("Оплата уже зафиксирована.", show_alert=False)
        return

    # фиксируем оплату и шлём админам
    order.status = "paid"
    db["stats"]["active_orders"] += 1
    save_db()

    # текст для админов
    if isinstance(order, StarOrder):
        text_admin = (
            "✨ Оплачен заказ на звёзды\n"
            f"ID заказа: #{order.order_id}\n"
            f"Пользователь: @{call.from_user.username or 'id:'+str(order.user_id)}\n"
            f"Адрес выдачи: {order.username_for_stars}\n"
            f"Количество: {order.stars}\n"
            f"Сумма: {order.usdt_amount} USDT"
        )
    else:
        text_admin = (
            "💱 Оплаченная сделка по обмену\n"
            f"ID сделки: #{order.order_id}\n"
            f"Пользователь: @{call.from_user.username or 'id:'+str(order.user_id)}\n"
            f"Сумма: {order.usdt_amount} USDT\n"
            f"К выплате клиенту: {order.payout_rub} ₽\n"
            f"Реквизиты клиента: {order.requisites}"
        )

    try:
        msg = await bot.send_message(ADMIN_CHAT_ID, text_admin, reply_markup=admin_pending_kb(order.order_id))
        order.admin_msg_id = msg.message_id
    except Exception as e:
        log.error(f"Не удалось отправить заказ в админ-чат: {e}")

    await call.message.answer("✅ Оплата найдена. Заказ передан администрации. Ожидайте выполнения.")
    await call.answer()

# =========================
# АДМИН-КНОПКИ
# =========================
@dp.callback_query(F.data.startswith("admin_pending:"))
async def on_admin_pending(call: CallbackQuery):
    if call.message.chat.id != ADMIN_CHAT_ID:
        await call.answer("Недоступно.", show_alert=True)
        return
    try:
        _, order_id_str = call.data.split(":")
        order_id = int(order_id_str)
    except Exception:
        await call.answer("Неверные данные.", show_alert=True)
        return

    order = orders_by_id.get(order_id)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    order.status = "pending"
    await call.message.edit_reply_markup(reply_markup=admin_done_kb(order.order_id))
    await call.answer("Статус: в ожидании.")

@dp.callback_query(F.data.startswith("admin_done:"))
async def on_admin_done(call: CallbackQuery):
    if call.message.chat.id != ADMIN_CHAT_ID:
        await call.answer("Недоступно.", show_alert=True)
        return
    try:
        _, order_id_str = call.data.split(":")
        order_id = int(order_id_str)
    except Exception:
        await call.answer("Неверные данные.", show_alert=True)
        return

    order = orders_by_id.get(order_id)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return

    order.status = "done"
    # метрики
    load_db()
    ensure_user(order.user_id)
    db["users"][str(order.user_id)]["paid_orders"] += 1
    db["stats"]["total_paid_orders"] += 1
    if db["stats"]["active_orders"] > 0:
        db["stats"]["active_orders"] -= 1
    save_db()

    # Обновим сообщение в админ-чате
    new_text = call.message.html_text + "\n\n<b>Статус:</b> Заказ выполнен ✅"
    await call.message.edit_text(new_text)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Отмечено как выполнено.")

    # Клиенту — длинное сообщение
    try:
        if isinstance(order, StarOrder):
            text_user = (
                f"✅ Ваш заказ #{order.order_id} на покупку звёзд выполнен!\n\n"
                f"• Количество звёзд: {order.stars}\n"
                f"• Адрес выдачи: {order.username_for_stars}\n\n"
                "Звёзды отправлены на указанный юзернейм. "
                f"Если что-то не так — напишите в техподдержку {SUPPORT_CONTACT}.\n\n"
                "Спасибо за покупку! 🌟"
            )
        else:
            text_user = (
                f"✅ Ваша заявка #{order.order_id} на обмен USDT→₽ завершена!\n\n"
                f"• Сумма обмена: {order.usdt_amount} USDT\n"
                f"• Курс: {order.rate} ₽\n"
                f"• Выплата: {order.payout_rub} ₽\n"
                f"• Реквизиты: {order.requisites}\n\n"
                f"Перевод отправлен. Если в течение разумного времени оплата не дошла, напишите в техподдержку {SUPPORT_CONTACT}.\n\n"
                "Спасибо за сделку!"
            )
        await bot.send_message(order.user_id, text_user)
    except Exception as e:
        log.error(f"Не удалось отправить пользователю сообщение о завершении: {e}")

# =========================
# ПРОЧЕЕ МЕНЮ
# =========================
@dp.message(F.text == "💳 Оплатить картой")
async def pay_card(message: Message):
    await message.answer(f"Для оплаты картой напишите администрации: {SUPPORT_CONTACT}")

@dp.message(F.text == "🛠 Техподдержка")
async def support(message: Message):
    await message.answer(f"Техподдержка: {SUPPORT_CONTACT}")

@dp.message(F.text == "⭐️ Отзывы / Репутация")
async def reviews(message: Message):
    await message.answer(f"Отзывы и репутация: {REVIEWS_URL}")

# =========================
# АДМИН-СТАТИСТИКА
# =========================
@dp.message(Command("melluser"))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        # разрешим только админу/в админ-чате
        return
    load_db()
    total_orders_paid = db["stats"]["total_paid_orders"]
    visitors = db["stats"]["visitors"]
    active = db["stats"]["active_orders"]
    total_topups = db["stats"]["total_topups_rub"]
    total_spent = db["stats"]["total_spent_rub"]

    txt = (
        "<b>Статистика бота</b>\n\n"
        f"Посетителей (уникальных): <b>{visitors}</b>\n"
        f"Завершённых заказов: <b>{total_orders_paid}</b>\n"
        f"Активных заказов: <b>{active}</b>\n"
        f"Сумма пополнений: <b>{total_topups} ₽</b>\n"
        f"Списано с балансов: <b>{total_spent} ₽</b>"
    )
    await message.answer(txt)

# =========================
# ЗАПУСК
# =========================
async def main():
    load_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

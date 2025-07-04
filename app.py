import os
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage 
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, BufferedInputFile
from aiogram.filters import Command
from aiogram import F
from PIL import Image
import io
import asyncio

# Для Aiohttp Webhook
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Отримання змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")

CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "0")
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.strip().lstrip('-').isdigit() else 0

MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER", "")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") 

# Перевірка на наявність критичних змінних
if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN не встановлено! Бот не зможе працювати без токена.")
    exit(1) 
if not CHANNEL_ID:
    logging.warning("⚠️ CHANNEL_ID не встановлено або не є дійсним числом. Публікація в канал може бути недоступна.")
if not MONOBANK_CARD_NUMBER:
    logging.warning("⚠️ MONOBANK_CARD_NUMBER не встановлено. Інформація про комісію може бути неповною.")
if not WEBHOOK_URL:
    logging.warning("⚠️ WEBHOOK_URL не встановлено. Webhook може не працювати належним чином.")


# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Створення станів для FSM для спрощеного процесу
class NewSimpleProduct(StatesGroup):
    name = State()
    price = State()
    photo = State() # Тільки одне фото
    description = State()
    contact = State() # Нове поле для контакту
    confirm = State()

# --- Допоміжні функції ---
def get_main_menu_keyboard():
    """Повертає клавіатуру головного меню."""
    keyboard_buttons = [
        [types.KeyboardButton(text="📦 Додати оголошення")],
        [types.KeyboardButton(text="📖 Правила")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)

# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати оголошення")
async def add_simple_product_start(message: types.Message, state: FSMContext):
    """Початок спрощеного процесу додавання нового оголошення."""
    logging.info(f"Користувач {message.from_user.id} почав додавати оголошення.")
    await state.set_state(NewSimpleProduct.name)
    await message.answer("✏️ Введіть назву оголошення:")

@dp.message(NewSimpleProduct.name)
async def process_simple_name(message: types.Message, state: FSMContext):
    """Обробка назви оголошення."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewSimpleProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewSimpleProduct.price)
async def process_simple_price(message: types.Message, state: FSMContext):
    """Обробка ціни оголошення."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text)
    await state.set_state(NewSimpleProduct.photo)
    await message.answer("📷 Завантажте одне фото для оголошення (або натисніть /skip_photo, якщо фото не потрібне).")

@dp.message(NewSimpleProduct.photo, F.content_type == types.ContentType.PHOTO)
async def process_simple_photo(message: types.Message, state: FSMContext):
    """Обробка фото оголошення (одне фото)."""
    logging.info(f"Користувач {message.from_user.id} додав фото.")
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await state.set_state(NewSimpleProduct.description)
    await message.answer("📝 Введіть опис оголошення:")

@dp.message(NewSimpleProduct.photo, Command("skip_photo"))
async def skip_simple_photo(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографії."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photo_file_id=None)
    await state.set_state(NewSimpleProduct.description)
    await message.answer("📝 Введіть опис оголошення:")

@dp.message(NewSimpleProduct.description)
async def process_simple_description(message: types.Message, state: FSMContext):
    """Обробка опису оголошення."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewSimpleProduct.contact)
    await message.answer("📞 Вкажіть контакт для зв'язку (наприклад, @ваш_нік, номер телефону, посилання):")

@dp.message(NewSimpleProduct.contact)
async def process_simple_contact(message: types.Message, state: FSMContext):
    """Обробка контакту для зв'язку."""
    logging.info(f"Користувач {message.from_user.id} ввів контакт: {message.text}")
    await state.update_data(contact=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"📞 Контакт: {user_data['contact']}\n"
    )
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Опублікувати")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewSimpleProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewSimpleProduct.confirm)
async def process_simple_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Опублікувати":
        user_data = await state.get_data()
        username = message.from_user.username if message.from_user.username else f"id{message.from_user.id}"
        
        if CHANNEL_ID == 0:
            await message.answer("ID каналу не налаштовано. Неможливо опублікувати оголошення.", reply_markup=get_main_menu_keyboard())
            logging.error("CHANNEL_ID не встановлено, неможливо опублікувати оголошення.")
            await state.clear()
            return

        caption = (
            f"**Нове оголошення:**\n\n"
            f"📦 Назва: {user_data['name']}\n"
            f"💰 Ціна: {user_data['price']}\n"
            f"📝 Опис: {user_data['description']}\n"
            f"📞 Контакт: {user_data['contact']}\n"
            f"👤 Опублікував: @{username}"
        )
        
        try:
            if user_data.get('photo_file_id'):
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=user_data['photo_file_id'],
                    caption=caption,
                    parse_mode='Markdown'
                )
            else:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    parse_mode='Markdown'
                )
            await message.answer("✅ Ваше оголошення опубліковано!", reply_markup=get_main_menu_keyboard())
            logging.info(f"✅ Оголошення від {message.from_user.id} опубліковано в каналі {CHANNEL_ID}.")
        except Exception as e:
            logging.error(f"❌ Помилка публікації оголошення в канал: {e}")
            await message.answer("Виникла помилка при публікації оголошення. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📖 Правила")
async def show_rules(message: types.Message, state: FSMContext):
    """Показує правила користування ботом."""
    logging.info(f"Користувач {message.from_user.id} переглядає правила.")
    await state.clear()
    rules_text = (
        "📌 **Умови користування:**\n\n"
        " * 🧾 Покупець оплачує доставку.\n"
        " * 💰 Продавець сплачує комісію платформи: **10%**\n"
        f" * 💳 Оплата комісії на Monobank: `{MONOBANK_CARD_NUMBER}`"
    )
    await message.answer(rules_text, parse_mode='Markdown')

# --- Налаштування Webhook для Aiohttp ---

async def on_startup_webhook(aiohttp_app: web.Application):
    """
    Функція, яка виконується при запуску aiohttp веб-сервера.
    Встановлює вебхук для Telegram.
    """
    if not WEBHOOK_URL:
        logging.error("❌ WEBHOOK_URL не встановлено. Webhook не буде налаштовано.")
        return
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN не встановлено. Webhook не буде налаштовано.")
        return

    base_url = WEBHOOK_URL.rstrip('/')
    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{base_url}{webhook_path}"
    
    logging.info(f"ℹ️ Спроба встановити Webhook на: {full_webhook_url}")
    try:
        current_webhook_info = await bot.get_webhook_info()
        if current_webhook_info.url != full_webhook_url:
            await bot.set_webhook(full_webhook_url)
            logging.info(f"✅ Webhook успішно встановлено на: {full_webhook_url}")
        else:
            logging.info(f"✅ Webhook вже встановлено на: {full_webhook_url}. Пропуск налаштування.")
    except Exception as e:
        logging.error(f"❌ Помилка встановлення Webhook: {e}")

async def on_shutdown_webhook(aiohttp_app: web.Application):
    """
    Функція, яка виконується при зупинці aiohttp веб-сервера.
    Видаляє вебхук з Telegram.
    """
    logging.info("ℹ️ Видалення Webhook...")
    try:
        await bot.delete_webhook()
        logging.info("✅ Webhook успішно видалено.")
    except Exception as e:
        logging.error(f"❌ Помилка видалення Webhook: {e}")

async def health_check_handler(request):
    """Обробник для health check."""
    return web.json_response({"status": "ok", "message": "Bot service is running."})

async def main():
    """Основна функція для запуску бота та веб-сервера."""
    aiohttp_app = web.Application()
    
    # Додаємо обробник для вебхука Telegram
    webhook_path = f"/webhook/{BOT_TOKEN}"
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(aiohttp_app, path=webhook_path)

    # Реєструємо health check endpoint
    aiohttp_app.router.add_get('/', health_check_handler)

    # Реєструємо функції запуску/зупинки для aiohttp
    aiohttp_app.on_startup.append(on_startup_webhook)
    aiohttp_app.on_shutdown.append(on_shutdown_webhook)

    # Запускаємо aiohttp веб-сервер
    # Для Render.com порт зазвичай 10000 і хост 0.0.0.0
    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    
    # Отримуємо порт з змінних оточення, за замовчуванням 10000
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logging.info("🎉 Бот запущено та готовий до роботи!")
    
    # Тримаємо основний цикл подій активним
    while True:
        await asyncio.sleep(3600) # Спимо годину, щоб додаток не завершився

if __name__ == '__main__':
    # Запускаємо основну асинхронну функцію
    asyncio.run(main())


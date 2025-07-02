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
import psycopg2
from psycopg2 import sql
from datetime import datetime

# Для Aiohttp Webhook
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Отримання змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]

CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "0")
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.strip().lstrip('-').isdigit() else 0

MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER", "")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") 

# Перевірка на наявність критичних змінних
if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN не встановлено! Бот не зможе працювати без токена.")
    # Якщо токен відсутній, виходимо, оскільки бот не може працювати
    exit(1) 
if not ADMIN_IDS:
    logging.warning("⚠️ ADMIN_IDS не встановлено або порожнє. Функції модерації можуть бути недоступні.")
if not CHANNEL_ID:
    logging.warning("⚠️ CHANNEL_ID не встановлено або не є дійсним числом. Публікація в канал може бути недоступна.")
if not MONOBANK_CARD_NUMBER:
    logging.warning("⚠️ MONOBANK_CARD_NUMBER не встановлено. Інформація про комісію може бути неповною.")
if not WEBHOOK_URL:
    logging.warning("⚠️ WEBHOOK_URL не встановлено. Webhook може не працювати належним чином.")


# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Створення станів для FSM
class NewProduct(StatesGroup):
    name = State()
    price = State()
    photos = State()
    location = State()
    description = State()
    delivery = State()
    confirm = State()

class ModeratorActions(StatesGroup):
    waiting_for_moderation_action = State()
    rotating_photos = State()

# --- База даних ---
def get_db_connection():
    """Встановлює з'єднання з базою даних PostgreSQL."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logging.error("DATABASE_URL не встановлено. Неможливо підключитися до бази даних.")
        raise ValueError("DATABASE_URL environment variable is not set.")
    conn = psycopg2.connect(database_url)
    return conn

async def init_db():
    """Ініціалізує таблиці в базі даних, якщо вони не існують."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                photos TEXT[],
                location TEXT,
                description TEXT NOT NULL,
                delivery TEXT NOT NULL,
                status TEXT DEFAULT 'moderation',
                moderator_message_id BIGINT,
                channel_message_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMP,
                views INT DEFAULT 0,
                republish_count INT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS product_photos (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL,
                photo_index INTEGER NOT NULL
            );
        """)
        conn.commit()
        logging.info("✅ База даних ініціалізована успішно.")
    except Exception as e:
        logging.error(f"❌ Помилка ініціалізації бази даних: {e}")
    finally:
        if conn:
            conn.close()

async def add_product_to_db(user_id: int, username: str, name: str, price: str, location: str, description: str, delivery: str):
    """Додає новий товар до бази даних."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO products (user_id, username, name, price, location, description, delivery)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;""",
            (user_id, username, name, price, location, description, delivery)
        )
        product_id = cur.fetchone()[0]
        conn.commit()
        return product_id
    except Exception as e:
        logging.error(f"❌ Помилка додавання товару до БД: {e}")
        return None
    finally:
        if conn:
            conn.close()

async def add_product_photo_to_db(product_id: int, file_id: str, photo_index: int):
    """Додає фотографію до товару в базі даних."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO product_photos (product_id, file_id, photo_index)
               VALUES (%s, %s, %s);""",
            (product_id, file_id, photo_index)
        )
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Помилка додавання фото до БД: {e}")
    finally:
        if conn:
            conn.close()

async def get_product_photos_from_db(product_id: int):
    """Отримує список file_id фотографій для товару."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT file_id FROM product_photos WHERE product_id = %s ORDER BY photo_index;""",
            (product_id,)
        )
        photos = [row[0] for row in cur.fetchall()]
        return photos
    except Exception as e:
        logging.error(f"❌ Помилка отримання фото з БД: {e}")
        return []
    finally:
        if conn:
            conn.close()

async def get_product_by_id(product_id: int):
    """Отримує інформацію про товар за його ID."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE id = %s;", (product_id,))
        product = cur.fetchone()
        if product:
            column_names = [desc[0] for desc in cur.description]
            return dict(zip(column_names, product))
        return None
    except Exception as e:
        logging.error(f"❌ Помилка отримання товару за ID: {e}")
        return None
    finally:
        if conn:
            conn.close()

async def get_user_products(user_id: int):
    """Отримує список товарів користувача."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name, price, status, created_at, views, republish_count FROM products WHERE user_id = %s ORDER BY created_at DESC;", (user_id,))
        products = []
        for row in cur.fetchall():
            products.append({
                'id': row[0],
                'name': row[1],
                'price': row[2],
                'status': row[3],
                'created_at': row[4],
                'views': row[5],
                'republish_count': row[6]
            })
        return products
    except Exception as e:
        logging.error(f"❌ Помилка отримання товарів користувача: {e}")
        return []
    finally:
        if conn:
            conn.close()

async def update_product_status(product_id: int, status: str, channel_message_id: int = None):
    """Оновлює статус товару."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if status == 'published' and channel_message_id:
            cur.execute(
                """UPDATE products SET status = %s, published_at = CURRENT_TIMESTAMP, channel_message_id = %s WHERE id = %s;""",
                (status, channel_message_id, product_id)
            )
        else:
            cur.execute(
                """UPDATE products SET status = %s WHERE id = %s;""",
                (status, product_id)
            )
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Помилка оновлення статусу товару: {e}")
    finally:
        if conn:
            conn.close()

async def update_product_moderator_message_id(product_id: int, message_id: int):
    """Оновлює ID повідомлення модератору для товару."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """UPDATE products SET moderator_message_id = %s WHERE id = %s;""",
            (message_id, product_id)
        )
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Помилка оновлення ID повідомлення модератору: {e}")
    finally:
        if conn:
            conn.close()

async def delete_product_from_db(product_id: int):
    """Видаляє товар з бази даних."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM products WHERE id = %s;", (product_id,))
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Помилка видалення товару з БД: {e}")
    finally:
        if conn:
            conn.close()

async def update_product_price(product_id: int, new_price: str):
    """Оновлює ціну товару."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """UPDATE products SET price = %s WHERE id = %s;""",
            (new_price, product_id)
        )
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Помилка оновлення ціни товару: {e}")
    finally:
        if conn:
            conn.close()

async def increment_product_republish_count(product_id: int):
    """Збільшує лічильник переопублікацій товару."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """UPDATE products SET republish_count = republish_count + 1 WHERE id = %s RETURNING republish_count;""",
            (product_id,)
        )
        new_count = cur.fetchone()[0]
        conn.commit()
        return new_count
    except Exception as e:
        logging.error(f"❌ Помилка збільшення лічильника переопублікацій: {e}")
        return None
    finally:
        if conn:
            conn.close()

async def update_product_photos_in_db(product_id: int, new_file_ids: list):
    """Оновлює фотографії товару в базі даних."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM product_photos WHERE product_id = %s;", (product_id,))
        for i, file_id in enumerate(new_file_ids):
            cur.execute(
                """INSERT INTO product_photos (product_id, file_id, photo_index)
                   VALUES (%s, %s, %s);""",
                (product_id, file_id, i)
            )
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Помилка оновлення фотографій товару в БД: {e}")
    finally:
        if conn:
            conn.close()

# --- Допоміжні функції ---
def get_main_menu_keyboard():
    """Повертає клавіатуру головного меню."""
    keyboard_buttons = [
        [types.KeyboardButton(text="📦 Додати товар")],
        [types.KeyboardButton(text="📋 Мої товари")],
        [types.KeyboardButton(text="📖 Правила")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)

def get_product_moderation_keyboard(product_id: int):
    """Повертає клавіатуру для модерації товару."""
    keyboard_buttons = [
        [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"publish_product_{product_id}")],
        [InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject_product_{product_id}")],
        [InlineKeyboardButton(text="🔄 Повернути фото", callback_data=f"rotate_photos_{product_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

def get_product_actions_keyboard(product_id: int, channel_message_id: int, republish_count: int):
    """Повертає клавіатуру дій для користувача в розділі "Мої товари"."""
    buttons = []
    if channel_message_id and CHANNEL_ID != 0:
        channel_short_id = str(CHANNEL_ID).replace('-100', '')
        buttons.append([InlineKeyboardButton(text="👁 Переглянути в каналі", url=f"https://t.me/c/{channel_short_id}/{channel_message_id}")]) 
    if republish_count < 3:
        buttons.append([InlineKeyboardButton(text="🔁 Переопублікувати", callback_data=f"republish_product_{product_id}")])
    buttons.append([InlineKeyboardButton(text="✅ Продано", callback_data=f"sold_product_{product_id}")])
    buttons.append([InlineKeyboardButton(text="✏ Змінити ціну", callback_data=f"change_price_{product_id}")])
    buttons.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"delete_product_{product_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_photo_rotation_keyboard(product_id: int, photo_index: int):
    """Повертає клавіатуру для повороту фото."""
    keyboard_buttons = [
        [InlineKeyboardButton(text="🔃 Повернути фото на 90°", callback_data=f"rotate_single_photo_{product_id}_{photo_index}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

def get_photo_rotation_done_keyboard(product_id: int):
    """Повертає клавіатуру "Готово" після редагування фото."""
    keyboard_buttons = [
        [InlineKeyboardButton(text="✅ Готово", callback_data=f"done_rotating_photos_{product_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

async def send_product_to_moderation(product_id: int, user_id: int, username: str):
    """Надсилає товар модераторам для перевірки."""
    product = await get_product_by_id(product_id)
    if not product:
        logging.error(f"Товар з ID {product_id} не знайдено для модерації.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{username}" if username else f"👤 Продавець: <a href='tg://user?id={user_id}'>{user_id}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(user_id, "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    if len(photos) < 10:
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
        await message.answer(f"Фото {len(photos)} додано. Залишилось {10 - len(photos)}.")
    else:
        logging.info(f"Користувач {message.from_user.id} спробував додати більше 10 фото.")
        await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message(NewProduct.photos, Command("done_photos"))
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    user_data = await state.get_data()
    if not user_data.get('photos'):
        logging.warning(f"Користувач {message.from_user.id} намагався завершити фото без фото.")
        await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
        return
    logging.info(f"Користувач {message.from_user.id} завершив завантаження фото.")
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message(NewProduct.photos, Command("skip_photos"))
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    logging.info(f"Користувач {message.from_user.id} пропустив завантаження фото.")
    await state.update_data(photos=[])
    await state.set_state(NewProduct.location)
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message(NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    logging.info(f"Користувач {message.from_user.id} ввів геолокацію: {message.text}")
    await state.update_data(location=message.text)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.location, Command("skip_location"))
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    logging.info(f"Користувач {message.from_user.id} пропустив введення геолокації.")
    await state.update_data(location=None)
    await state.set_state(NewProduct.description)
    await message.answer("📝 Введіть опис товару:")

@dp.message(NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    logging.info(f"Користувач {message.from_user.id} ввів опис.")
    await state.update_data(description=message.text)
    await state.set_state(NewProduct.delivery)
    keyboard_buttons = [
        [types.KeyboardButton(text="Наложка Укрпошта")],
        [types.KeyboardButton(text="Наложка Нова пошта")]
    ]
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    logging.info(f"Користувач {message.from_user.id} обрав доставку: {message.text}")
    await state.update_data(delivery=message.text)
    user_data = await state.get_data()
    
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {user_data['name']}\n"
        f"💰 Ціна: {user_data['price']}\n"
        f"📝 Опис: {user_data['description']}\n"
        f"🚚 Доставка: {user_data['delivery']}\n"
    )
    if user_data['location']:
        confirmation_text += f"📍 Геолокація: {user_data['location']}\n"
    
    keyboard_buttons = [
        [types.KeyboardButton(text="✅ Підтвердити")],
        [types.KeyboardButton(text="❌ Скасувати")]
    ]
    
    await state.set_state(NewProduct.confirm)
    await message.answer(confirmation_text, reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True))

@dp.message(NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    logging.info(f"Користувач {message.from_user.id} підтверджує/скасовує оголошення: {message.text}")
    if message.text == "✅ Підтвердити":
        user_data = await state.get_data()
        user_id = message.from_user.id
        username = message.from_user.username if message.from_user.username else f"id{user_id}"
        
        product_id = await add_product_to_db(
            user_id,
            username,
            user_data['name'],
            user_data['price'],
            user_data['location'],
            user_data['description'],
            user_data['delivery']
        )

        if product_id:
            for i, file_id in enumerate(user_data['photos']):
                await add_product_photo_to_db(product_id, file_id, i)
            
            await send_product_to_moderation(product_id, user_id, username)
            await message.answer(f"✅ Товар «{user_data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
        else:
            await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()

@dp.message(F.text == "📋 Мої товари")
async def my_products(message: types.Message, state: FSMContext):
    """Показує список товарів користувача."""
    logging.info(f"Користувач {message.from_user.id} переглядає свої товари.")
    await state.clear()
    user_products = await get_user_products(message.from_user.id)
    if not user_products:
        await message.answer("У вас ще немає доданих товарів.")
        return
    
    for product in user_products:
        status_emoji = "✅" if product['status'] == 'published' else "⏳"
        status_text = "Опубліковано" if product['status'] == 'published' else "На модерації"
        
        text = (
            f"📦 Назва: {product['name']}\n"
            f"💰 Ціна: {product['price']}\n"
            f"Статус: {status_emoji} {status_text}\n"
            f"Дата: {product['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
            f"Перегляди: {product['views']}\n"
        )
        
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

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

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query(F.data.startswith('publish_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_publish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Опублікувати' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if CHANNEL_ID == 0:
        await callback_query.answer("ID каналу не налаштовано. Неможливо опублікувати.")
        logging.error("CHANNEL_ID не встановлено, неможливо опублікувати товар.")
        return

    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"**Новий товар на модерацію:**\n\n"
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if not ADMIN_IDS:
            logging.error("Немає ADMIN_IDS для надсилання на модерацію. Повідомлення не буде надіслано модераторам.")
            await bot.send_message(product['user_id'], "Наразі модератори недоступні. Спробуйте пізніше.")
            return

        moderator_messages = []
        
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"✅ Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"❌ Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обробник команди /start."""
    logging.info(f"Отримано команду /start від {message.from_user.id}")
    await state.clear()
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message(F.text == "📦 Додати товар")
async def add_product_start(message: types.Message, state: FSMContext):
    """Початок процесу додавання нового товару."""
    logging.info(f"Користувач {message.from_user.id} почав додавати товар.")
    await state.set_state(NewProduct.name)
    await message.answer("✏️ Введіть назву товару:")

@dp.message(NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    logging.info(f"Користувач {message.from_user.id} ввів назву: {message.text}")
    await state.update_data(name=message.text)
    await state.set_state(NewProduct.price)
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message(NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    logging.info(f"Користувач {message.from_user.id} ввів ціну: {message.text}")
    await state.update_data(price=message.text, photos=[])
    await state.set_state(NewProduct.photos)
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")


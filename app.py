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
if not os.getenv("DATABASE_URL"):
    logging.error("❌ DATABASE_URL не встановлено. Функціонал бази даних буде недоступний.")


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

class ChangingPrice(StatesGroup):
    new_price = State()

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

        # Відправляємо медіа-групу модератору
        # Розбиваємо на групи по 10 фото, якщо їх більше
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                await bot.send_media_group(
                    chat_id=ADMIN_IDS[0],
                    media=chunk
                )
        else:
            await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown'
            )

        # Відправляємо окреме повідомлення з кнопками модерації
        moderator_keyboard_message = await bot.send_message(
            chat_id=ADMIN_IDS[0],
            text="Оберіть дію для товару:",
            reply_markup=get_product_moderation_keyboard(product_id)
        )
        await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

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
    await message.answer("📷 Завантажте фотографії (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message(NewProduct.photos, F.content_type == types.ContentType.PHOTO)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару. Приймає будь-яку кількість фото."""
    user_data = await state.get_data()
    photos = user_data.get('photos', [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    logging.info(f"Користувач {message.from_user.id} додав фото. Всього: {len(photos)}")
    await message.answer(f"Фото {len(photos)} додано. Ви можете додати більше або натисніть /done_photos, щоб продовжити.")

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
        f"**Новий товар:**\n\n" # Змінено на "Новий товар" для публікації
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        channel_message_id = None
        if media_group:
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            # Відправляємо медіа-групу в канал
            # Telegram API дозволяє до 10 фото в media_group
            sent_messages_in_channel = await bot.send_media_group(
                chat_id=CHANNEL_ID,
                media=media_group
            )
            channel_message_id = sent_messages_in_channel[0].message_id
        else:
            sent_message_in_channel = await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode='Markdown'
            )
            channel_message_id = sent_message_in_channel.message_id
        
        await update_product_status(product_id, 'published', channel_message_id)
        await callback_query.answer("Товар опубліковано!")
        
        await bot.send_message(product['user_id'], f"✅ Ваш товар «{product['name']}» опубліковано в каналі!")
        
        # Видаляємо повідомлення модератору з кнопками та фото
        if product['moderator_message_id']:
            try:
                # Видаляємо повідомлення з кнопками модерації
                await bot.delete_message(callback_query.message.chat.id, product['moderator_message_id'])
                # Telegram API не дозволяє видаляти медіа-групу одним запитом,
                # тому для видалення фото, які були надіслані модератору,
                # потрібно було б зберегти їх ID, що ускладнить логіку.
                # Наразі, залишаємо їх, фокусуючись на видаленні кнопок.
            except Exception as e:
                logging.warning(f"Не вдалося видалити повідомлення модератора: {e}")

    except Exception as e:
        logging.error(f"❌ Помилка публікації товару: {e}")
        await callback_query.answer("Помилка при публікації товару.")

@dp.callback_query(F.data.startswith('reject_product_'), F.from_user.id.in_(ADMIN_IDS))
async def process_reject_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Відхилити' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Відхилити' для товару {product_id}")
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    await update_product_status(product_id, 'rejected')
    await delete_product_from_db(product_id) # Видаляємо товар повністю
    await callback_query.answer("Товар відхилено.")
    
    await bot.send_message(product['user_id'], f"❌ Ваш товар «{product['name']}» відхилено модератором.")
    
    if product['moderator_message_id']:
        try:
            await bot.delete_message(callback_query.message.chat.id, product['moderator_message_id'])
        except Exception as e:
            logging.warning(f"Не вдалося видалити повідомлення модератора: {e}")

@dp.callback_query(F.data.startswith('rotate_photos_'), F.from_user.id.in_(ADMIN_IDS))
async def process_rotate_photos(callback_query: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Обробник кнопки 'Повернути фото' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} натиснув 'Повернути фото' для товару {product_id}")
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    photos_file_ids = await get_product_photos_from_db(product_id)
    if not photos_file_ids:
        await callback_query.answer("У цього товару немає фотографій для редагування.")
        return

    await state.update_data(
        product_id_to_rotate=product_id,
        current_photo_index=0,
        original_photos_file_ids=photos_file_ids, # Зберігаємо оригінальні file_id
        rotated_photos_file_ids=list(photos_file_ids) # Копія, яку будемо змінювати
    )

    await state.set_state(ModeratorActions.rotating_photos)
    await callback_query.answer("Переходимо в режим редагування фото.")
    
    # Відправляємо перше фото для повороту
    await send_photo_for_rotation(callback_query.message.chat.id, product_id, 0, photos_file_ids[0], bot)

async def send_photo_for_rotation(chat_id: int, product_id: int, photo_index: int, file_id: str, bot: Bot):
    """Надсилає одне фото модератору для повороту."""
    logging.info(f"Надсилання фото {photo_index} товару {product_id} для повороту.")
    all_photos = await get_product_photos_from_db(product_id) # Для коректного відображення кількості
    await bot.send_photo(
        chat_id=chat_id,
        photo=file_id,
        caption=f"Фото {photo_index + 1}/{len(all_photos)}",
        reply_markup=get_photo_rotation_keyboard(product_id, photo_index)
    )
    product = await get_product_by_id(product_id)
    if product and product['moderator_message_id']:
        try:
            # Оновлюємо клавіатуру під повідомленням модерації на "Готово"
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=product['moderator_message_id'],
                reply_markup=get_photo_rotation_done_keyboard(product_id)
            )
        except Exception as e:
            logging.warning(f"Не вдалося оновити повідомлення модератора для кнопки 'Готово': {e}")


@dp.callback_query(F.data.startswith('rotate_single_photo_'), ModeratorActions.rotating_photos, F.from_user.id.in_(ADMIN_IDS))
async def process_rotate_single_photo(callback_query: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Обробник кнопки 'Повернути фото на 90°' для модератора."""
    parts = callback_query.data.split('_')
    product_id = int(parts[-2])
    photo_index = int(parts[-1])
    logging.info(f"Модератор {callback_query.from_user.id} повертає фото {photo_index} товару {product_id}.")

    user_data = await state.get_data()
    if user_data['product_id_to_rotate'] != product_id:
        await callback_query.answer("Помилка: невідповідність товару.")
        return

    original_file_id = user_data['rotated_photos_file_ids'][photo_index]

    try:
        file_info = await bot.get_file(original_file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        image = Image.open(io.BytesIO(downloaded_file.read()))
        
        rotated_image = image.rotate(-90, expand=True) # Поворот на 90 градусів проти годинникової стрілки

        byte_arr = io.BytesIO()
        rotated_image.save(byte_arr, format='JPEG') # Зберігаємо як JPEG
        byte_arr.seek(0)

        # Надсилаємо повернуте фото назад модератору, щоб отримати новий file_id
        uploaded_photo = await bot.send_photo(
            chat_id=callback_query.message.chat.id,
            photo=BufferedInputFile(byte_arr.getvalue(), filename=f"rotated_photo_{product_id}_{photo_index}.jpg"),
            caption=f"Повернуте фото {photo_index + 1}"
        )
        new_file_id = uploaded_photo.photo[-1].file_id

        # Оновлюємо file_id у стані FSM
        user_data['rotated_photos_file_ids'][photo_index] = new_file_id
        await state.update_data(rotated_photos_file_ids=user_data['rotated_photos_file_ids'])
        await callback_query.answer("Фото повернуто.")
        
        # Оновлюємо клавіатуру під поточним фото, щоб можна було повернути його ще раз
        await bot.edit_message_reply_markup(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            reply_markup=get_photo_rotation_keyboard(product_id, photo_index)
        )

    except Exception as e:
        logging.error(f"❌ Помилка повороту фото: {e}")
        await callback_query.answer("Помилка при повороті фотографії.")

@dp.callback_query(F.data.startswith('done_rotating_photos_'), ModeratorActions.rotating_photos, F.from_user.id.in_(ADMIN_IDS))
async def process_done_rotating_photos(callback_query: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Обробник кнопки 'Готово' після редагування фото."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Модератор {callback_query.from_user.id} завершив редагування фото для товару {product_id}.")
    user_data = await state.get_data()
    if user_data['product_id_to_rotate'] != product_id:
        await callback_query.answer("Помилка: невідповідність товару.")
        return
    
    new_photos_file_ids = user_data['rotated_photos_file_ids']
    await update_product_photos_in_db(product_id, new_photos_file_ids)

    product = await get_product_by_id(product_id)
    if product:
        # Повідомляємо користувача про оновлення та надсилаємо на повторну модерацію
        await bot.send_message(
            product['user_id'],
            "🔄 Ваш товар оновлено.\n"
            "📸 Фото були повернуті для правильного відображення.\n"
            "Тепер товар знову надіслано на модерацію.",
            reply_markup=get_main_menu_keyboard()
        )
        await update_product_status(product_id, 'moderation') # Змінюємо статус на модерацію
        await send_product_to_moderation(product_id, product['user_id'], product['username']) # Повторно надсилаємо на модерацію
    
    await callback_query.answer("Редагування фото завершено. Товар знову надіслано на модерацію.")
    await state.clear() # Очищаємо стан FSM


# --- Обробники Callback-кнопок (Користувач) ---
@dp.callback_query(F.data.startswith('republish_product_'))
async def process_republish_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Переопублікувати' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Користувач {callback_query.from_user.id} натиснув 'Переопублікувати' для товару {product_id}.")
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if product['republish_count'] >= 3:
        await callback_query.answer("Ви досягли ліміту переопублікацій (3 рази).")
        return

    new_republish_count = await increment_product_republish_count(product_id)
    await update_product_status(product_id, 'moderation') # Змінюємо статус на модерацію
    await send_product_to_moderation(product_id, product['user_id'], product['username'])
    
    await callback_query.answer(f"Товар надіслано на переопублікацію. Залишилось {3 - new_republish_count} спроб.")
    await bot.send_message(product['user_id'], f"🔁 Ваш товар «{product['name']}» надіслано на повторну модерацію.")

@dp.callback_query(F.data.startswith('sold_product_'))
async def process_sold_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Продано' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Користувач {callback_query.from_user.id} натиснув 'Продано' для товару {product_id}.")
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    try:
        price_value = 0.0
        # Очищаємо ціну від тексту, залишаємо тільки числа та крапки/коми
        cleaned_price = product['price'].lower().replace('грн', '').replace('usd', '').replace('договірна', '').strip().replace(',', '.')
        
        if cleaned_price:
            try:
                price_value = float(cleaned_price)
            except ValueError:
                await callback_query.answer("Не вдалося розрахувати комісію. Перевірте формат ціни.")
                return
        else:
            await callback_query.answer("Не вдалося розрахувати комісію. Будь ласка, вкажіть ціну в грн або USD.")
            return

        # Якщо ціна вказана в USD, конвертуємо в гривні (приблизний курс)
        if "usd" in product['price'].lower():
            price_value *= 40 # Приблизний курс USD до UAH
            
        commission = price_value * 0.10 # 10% комісія
        
        await update_product_status(product_id, 'sold')
        
        # Видаляємо оголошення з каналу, якщо воно було опубліковано
        if product['channel_message_id'] and CHANNEL_ID != 0:
            try:
                await bot.delete_message(CHANNEL_ID, product['channel_message_id'])
            except Exception as e:
                logging.warning(f"Не вдалося видалити повідомлення з каналу: {e}")

        await callback_query.answer("Статус товару оновлено на 'Продано'.")
        await bot.send_message(
            callback_query.from_user.id,
            f"💸 Комісія 10% = {commission:.2f} грн\n"
            f"💳 Оплатіть на картку Monobank: `{MONOBANK_CARD_NUMBER}`",
            parse_mode='Markdown'
        )
    except ValueError:
        await callback_query.answer("Не вдалося розрахувати комісію. Перевірте формат ціни.")
    except Exception as e:
        logging.error(f"❌ Помилка при обробці 'Продано': {e}")
        await callback_query.answer("Виникла помилка.")

@dp.callback_query(F.data.startswith('change_price_'))
async def process_change_price(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки 'Змінити ціну' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Користувач {callback_query.from_user.id} натиснув 'Змінити ціну' для товару {product_id}.")
    
    await state.set_state(ChangingPrice.new_price)
    await state.update_data(product_id_to_change_price=product_id)
    
    await callback_query.answer("Введіть нову ціну:")
    await bot.send_message(callback_query.from_user.id, "Введіть нову ціну (наприклад, 600 грн або 25 USD):")

@dp.message(ChangingPrice.new_price)
async def process_new_price(message: types.Message, state: FSMContext):
    """Обробка нової ціни."""
    logging.info(f"Користувач {message.from_user.id} ввів нову ціну: {message.text}")
    user_data = await state.get_data()
    product_id = user_data['product_id_to_change_price']
    new_price = message.text
    
    await update_product_price(product_id, new_price)
    
    await update_product_status(product_id, 'moderation') # Відправляємо на модерацію після зміни ціни
    product = await get_product_by_id(product_id)
    if product:
        await send_product_to_moderation(product_id, product['user_id'], product['username'])

    await message.answer(f"Ціну товару оновлено на '{new_price}' і відправлено на повторну модерацію.", reply_markup=get_main_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data.startswith('delete_product_'))
async def process_delete_product(callback_query: types.CallbackQuery, bot: Bot):
    """Обробник кнопки 'Видалити' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    logging.info(f"Користувач {callback_query.from_user.id} натиснув 'Видалити' для товару {product_id}.")
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    await delete_product_from_db(product_id)
    
    # Видаляємо оголошення з каналу, якщо воно було опубліковано
    if product['channel_message_id'] and CHANNEL_ID != 0:
        try:
            await bot.delete_message(CHANNEL_ID, product['channel_message_id'])
        except Exception as e:
            logging.warning(f"Не вдалося видалити повідомлення з каналу: {e}")

    await callback_query.answer("Товар видалено.")
    await bot.send_message(callback_query.from_user.id, f"🗑 Ваш товар «{product['name']}» видалено.")


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
    # Ініціалізуємо базу даних тільки якщо DATABASE_URL встановлено
    if os.getenv("DATABASE_URL"):
        await init_db() 
    else:
        logging.warning("⚠️ DATABASE_URL не встановлено. Функціонал бази даних буде недоступний.")
    
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


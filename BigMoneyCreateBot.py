import os
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from PIL import Image
import io
import asyncio
import psycopg2
from psycopg2 import sql
from datetime import datetime

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Отримання змінних оточення
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS").split(',')]
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
MONOBANK_CARD_NUMBER = os.getenv("MONOBANK_CARD_NUMBER")
DATABASE_URL = os.getenv("DATABASE_URL") # URL для підключення до PostgreSQL

# Ініціалізація бота та диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

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
    conn = psycopg2.connect(DATABASE_URL)
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
        logging.info("База даних ініціалізована успішно.")
    except Exception as e:
        logging.error(f"Помилка ініціалізації бази даних: {e}")
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
        logging.error(f"Помилка додавання товару до БД: {e}")
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
        logging.error(f"Помилка додавання фото до БД: {e}")
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
        logging.error(f"Помилка отримання фото з БД: {e}")
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
            # Повертаємо словник для зручності
            column_names = [desc[0] for desc in cur.description]
            return dict(zip(column_names, product))
        return None
    except Exception as e:
        logging.error(f"Помилка отримання товару за ID: {e}")
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
        logging.error(f"Помилка отримання товарів користувача: {e}")
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
        logging.error(f"Помилка оновлення статусу товару: {e}")
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
        logging.error(f"Помилка оновлення ID повідомлення модератору: {e}")
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
        logging.error(f"Помилка видалення товару з БД: {e}")
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
        logging.error(f"Помилка оновлення ціни товару: {e}")
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
        logging.error(f"Помилка збільшення лічильника переопублікацій: {e}")
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
        # Видаляємо старі фото
        cur.execute("DELETE FROM product_photos WHERE product_id = %s;", (product_id,))
        # Додаємо нові фото
        for i, file_id in enumerate(new_file_ids):
            cur.execute(
                """INSERT INTO product_photos (product_id, file_id, photo_index)
                   VALUES (%s, %s, %s);""",
                (product_id, file_id, i)
            )
        conn.commit()
    except Exception as e:
        logging.error(f"Помилка оновлення фотографій товару в БД: {e}")
    finally:
        if conn:
            conn.close()

# --- Допоміжні функції ---
def get_main_menu_keyboard():
    """Повертає клавіатуру головного меню."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add(
        types.KeyboardButton("📦 Додати товар"),
        types.KeyboardButton("📋 Мої товари"),
        types.KeyboardButton("📖 Правила")
    )
    return keyboard

def get_product_moderation_keyboard(product_id: int):
    """Повертає клавіатуру для модерації товару."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✅ Опублікувати", callback_data=f"publish_product_{product_id}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_product_{product_id}"),
        InlineKeyboardButton("🔄 Повернути фото", callback_data=f"rotate_photos_{product_id}")
    )
    return keyboard

def get_product_actions_keyboard(product_id: int, channel_message_id: int, republish_count: int):
    """Повертає клавіатуру дій для користувача в розділі "Мої товари"."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    if channel_message_id:
        keyboard.add(InlineKeyboardButton("👁 Переглянути в каналі", url=f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{channel_message_id}")) # Для публічних каналів
    if republish_count < 3:
        keyboard.add(InlineKeyboardButton("🔁 Переопублікувати", callback_data=f"republish_product_{product_id}"))
    keyboard.add(
        InlineKeyboardButton("✅ Продано", callback_data=f"sold_product_{product_id}"),
        InlineKeyboardButton("✏ Змінити ціну", callback_data=f"change_price_{product_id}"),
        InlineKeyboardButton("🗑 Видалити", callback_data=f"delete_product_{product_id}")
    )
    return keyboard

def get_photo_rotation_keyboard(product_id: int, photo_index: int):
    """Повертає клавіатуру для повороту фото."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔃 Повернути фото на 90°", callback_data=f"rotate_single_photo_{product_id}_{photo_index}")
    )
    return keyboard

def get_photo_rotation_done_keyboard(product_id: int):
    """Повертає клавіатуру "Готово" після редагування фото."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✅ Готово", callback_data=f"done_rotating_photos_{product_id}")
    )
    return keyboard

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
        if media_group:
            # Надсилаємо фото та опис
            moderator_messages = []
            # Перше фото з описом, решта без
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'
            
            # Розбиваємо media_group на частини по 10 фото, якщо їх більше
            for i in range(0, len(media_group), 10):
                chunk = media_group[i:i+10]
                sent_messages = await bot.send_media_group(
                    chat_id=ADMIN_IDS[0], # Відправляємо першому адміну
                    media=chunk
                )
                moderator_messages.extend(sent_messages)

            # Надсилаємо клавіатуру модератору окремим повідомленням
            moderator_keyboard_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text="Оберіть дію:",
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            # Зберігаємо ID повідомлення з клавіатурою для модератора
            await update_product_moderator_message_id(product_id, moderator_keyboard_message.message_id)

        else:
            # Якщо фото немає, надсилаємо тільки текст
            moderator_message = await bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=caption,
                parse_mode='Markdown',
                reply_markup=get_product_moderation_keyboard(product_id)
            )
            await update_product_moderator_message_id(product_id, moderator_message.message_id)

        logging.info(f"Товар {product_id} надіслано на модерацію.")
    except Exception as e:
        logging.error(f"Помилка надсилання товару на модерацію: {e}")


# --- Обробники команд та повідомлень ---

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Обробник команди /start."""
    await message.answer("Привіт! Я BigMoneyCreateBot, допоможу тобі опублікувати оголошення.", reply_markup=get_main_menu_keyboard())

@dp.message_handler(text="📦 Додати товар")
async def add_product_start(message: types.Message):
    """Початок процесу додавання нового товару."""
    await NewProduct.name.set()
    await message.answer("✏️ Введіть назву товару:")

@dp.message_handler(state=NewProduct.name)
async def process_name(message: types.Message, state: FSMContext):
    """Обробка назви товару."""
    async with state.proxy() as data:
        data['name'] = message.text
    await NewProduct.next()
    await message.answer("💰 Введіть ціну (наприклад, 500 грн, 20 USD або договірна):")

@dp.message_handler(state=NewProduct.price)
async def process_price(message: types.Message, state: FSMContext):
    """Обробка ціни товару."""
    async with state.proxy() as data:
        data['price'] = message.text
        data['photos'] = [] # Ініціалізуємо список для file_id фото
    await NewProduct.next()
    await message.answer("📷 Завантажте до 10 фотографій (кожне окремим повідомленням або альбомом). Коли закінчите, натисніть /done_photos")

@dp.message_handler(content_types=types.ContentType.PHOTO, state=NewProduct.photos)
async def process_photos(message: types.Message, state: FSMContext):
    """Обробка фотографій товару."""
    async with state.proxy() as data:
        if len(data['photos']) < 10:
            data['photos'].append(message.photo[-1].file_id) # Зберігаємо file_id найбільшої фотографії
            await message.answer(f"Фото {len(data['photos'])} додано. Залишилось {10 - len(data['photos'])}.")
        else:
            await message.answer("Ви вже додали максимальну кількість фотографій (10). Натисніть /done_photos")

@dp.message_handler(commands=['done_photos'], state=NewProduct.photos)
async def done_photos(message: types.Message, state: FSMContext):
    """Завершення завантаження фотографій."""
    async with state.proxy() as data:
        if not data['photos']:
            await message.answer("Будь ласка, завантажте хоча б одне фото або натисніть /skip_photos, якщо фото не потрібні.")
            return
    await NewProduct.next()
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")

@dp.message_handler(commands=['skip_photos'], state=NewProduct.photos)
async def skip_photos(message: types.Message, state: FSMContext):
    """Пропуск завантаження фотографій."""
    async with state.proxy() as data:
        data['photos'] = []
    await NewProduct.next()
    await message.answer("📍 Тепер введіть геолокацію (необов'язково). Якщо не потрібно, натисніть /skip_location")


@dp.message_handler(state=NewProduct.location)
async def process_location(message: types.Message, state: FSMContext):
    """Обробка геолокації товару."""
    async with state.proxy() as data:
        data['location'] = message.text
    await NewProduct.next()
    await message.answer("📝 Введіть опис товару:")

@dp.message_handler(commands=['skip_location'], state=NewProduct.location)
async def skip_location(message: types.Message, state: FSMContext):
    """Пропуск введення геолокації."""
    async with state.proxy() as data:
        data['location'] = None
    await NewProduct.next()
    await message.answer("📝 Введіть опис товару:")

@dp.message_handler(state=NewProduct.description)
async def process_description(message: types.Message, state: FSMContext):
    """Обробка опису товару."""
    async with state.proxy() as data:
        data['description'] = message.text
    await NewProduct.next()
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add("Наложка Укрпошта", "Наложка Нова пошта")
    await message.answer("🚚 Оберіть спосіб доставки:", reply_markup=keyboard)

@dp.message_handler(state=NewProduct.delivery)
async def process_delivery(message: types.Message, state: FSMContext):
    """Обробка способу доставки."""
    async with state.proxy() as data:
        data['delivery'] = message.text
    
    # Формуємо підтвердження
    confirmation_text = (
        f"Будь ласка, перевірте введені дані:\n\n"
        f"📦 Назва: {data['name']}\n"
        f"💰 Ціна: {data['price']}\n"
        f"📝 Опис: {data['description']}\n"
        f"🚚 Доставка: {data['delivery']}\n"
    )
    if data['location']:
        confirmation_text += f"📍 Геолокація: {data['location']}\n"
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add("✅ Підтвердити", "❌ Скасувати")
    
    await NewProduct.next()
    await message.answer(confirmation_text, reply_markup=keyboard)

@dp.message_handler(state=NewProduct.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    """Підтвердження або скасування створення оголошення."""
    if message.text == "✅ Підтвердити":
        async with state.proxy() as data:
            user_id = message.from_user.id
            username = message.from_user.username if message.from_user.username else f"id{user_id}"
            
            product_id = await add_product_to_db(
                user_id,
                username,
                data['name'],
                data['price'],
                data['location'],
                data['description'],
                data['delivery']
            )

            if product_id:
                for i, file_id in enumerate(data['photos']):
                    await add_product_photo_to_db(product_id, file_id, i)
                
                await send_product_to_moderation(product_id, user_id, username)
                await message.answer(f"✅ Товар «{data['name']}» надіслано на модерацію. Очікуйте!", reply_markup=get_main_menu_keyboard())
            else:
                await message.answer("Виникла помилка при збереженні товару. Спробуйте ще раз.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Створення оголошення скасовано.", reply_markup=get_main_menu_keyboard())
    
    await state.finish()

@dp.message_handler(text="📋 Мої товари")
async def my_products(message: types.Message):
    """Показує список товарів користувача."""
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
        
        # Отримуємо ID повідомлення в каналі для кнопки "Переглянути в каналі"
        full_product_data = await get_product_by_id(product['id'])
        channel_message_id = full_product_data['channel_message_id'] if full_product_data else None

        await message.answer(text, reply_markup=get_product_actions_keyboard(product['id'], channel_message_id, product['republish_count']))

@dp.message_handler(text="📖 Правила")
async def show_rules(message: types.Message):
    """Показує правила користування ботом."""
    rules_text = (
        "📌 **Умови користування:**\n\n"
        " * 🧾 Покупець оплачує доставку.\n"
        " * 💰 Продавець сплачує комісію платформи: **10%**\n"
        f" * 💳 Оплата комісії на Monobank: `{MONOBANK_CARD_NUMBER}`"
    )
    await message.answer(rules_text, parse_mode='Markdown')

# --- Обробники Callback-кнопок (Модератор) ---
@dp.callback_query_handler(lambda c: c.data.startswith('publish_product_'), user_id=ADMIN_IDS)
async def process_publish_product(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Опублікувати' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    photos_file_ids = await get_product_photos_from_db(product_id)
    media_group = []
    for file_id in photos_file_ids:
        media_group.append(InputMediaPhoto(media=file_id))

    caption = (
        f"📦 Назва: {product['name']}\n"
        f"💰 Ціна: {product['price']}\n"
        f"📝 Опис: {product['description']}\n"
        f"🚚 Доставка: {product['delivery']}\n"
    )
    if product['location']:
        caption += f"📍 Геолокація: {product['location']}\n"
    caption += f"👤 Продавець: @{product['username']}" if product['username'] else f"👤 Продавець: <a href='tg://user?id={product['user_id']}'>{product['user_id']}</a>"

    try:
        if media_group:
            # Перше фото з описом, решта без
            media_group[0].caption = caption
            media_group[0].parse_mode = 'Markdown'

            sent_messages = await bot.send_media_group(
                chat_id=CHANNEL_ID,
                media=media_group
            )
            # Зберігаємо ID першого повідомлення в каналі (для посилання)
            channel_message_id = sent_messages[0].message_id
        else:
            # Якщо фото немає, надсилаємо тільки текст
            sent_message = await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode='Markdown'
            )
            channel_message_id = sent_message.message_id
        
        await update_product_status(product_id, 'published', channel_message_id)
        await callback_query.answer("Товар опубліковано!")
        
        # Повідомляємо користувача
        await bot.send_message(product['user_id'], f"✅ Ваш товар «{product['name']}» опубліковано в каналі!")
        
        # Видаляємо повідомлення модератора з кнопками
        if product['moderator_message_id']:
            try:
                await bot.delete_message(callback_query.message.chat.id, product['moderator_message_id'])
            except Exception as e:
                logging.warning(f"Не вдалося видалити повідомлення модератора: {e}")

    except Exception as e:
        logging.error(f"Помилка публікації товару: {e}")
        await callback_query.answer("Помилка при публікації товару.")

@dp.callback_query_handler(lambda c: c.data.startswith('reject_product_'), user_id=ADMIN_IDS)
async def process_reject_product(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Відхилити' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    product = await get_product_by_id(product_id)
    
    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    await update_product_status(product_id, 'rejected')
    await delete_product_from_db(product_id) # Видаляємо товар з БД при відхиленні
    await callback_query.answer("Товар відхилено.")
    
    # Повідомляємо користувача
    await bot.send_message(product['user_id'], f"❌ Ваш товар «{product['name']}» відхилено модератором.")
    
    # Видаляємо повідомлення модератора з кнопками
    if product['moderator_message_id']:
        try:
            await bot.delete_message(callback_query.message.chat.id, product['moderator_message_id'])
        except Exception as e:
            logging.warning(f"Не вдалося видалити повідомлення модератора: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('rotate_photos_'), user_id=ADMIN_IDS)
async def process_rotate_photos(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки 'Повернути фото' для модератора."""
    product_id = int(callback_query.data.split('_')[-1])
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    photos_file_ids = await get_product_photos_from_db(product_id)
    if not photos_file_ids:
        await callback_query.answer("У цього товару немає фотографій для редагування.")
        return

    async with state.proxy() as data:
        data['product_id_to_rotate'] = product_id
        data['current_photo_index'] = 0
        data['original_photos_file_ids'] = photos_file_ids # Зберігаємо оригінальні file_id
        data['rotated_photos_file_ids'] = list(photos_file_ids) # Копія для збереження змінених file_id

    await ModeratorActions.rotating_photos.set()
    await callback_query.answer("Переходимо в режим редагування фото.")
    
    # Надсилаємо перше фото для редагування
    await send_photo_for_rotation(callback_query.message.chat.id, product_id, 0, photos_file_ids[0])

async def send_photo_for_rotation(chat_id: int, product_id: int, photo_index: int, file_id: str):
    """Надсилає одне фото модератору для повороту."""
    await bot.send_photo(
        chat_id=chat_id,
        photo=file_id,
        caption=f"Фото {photo_index + 1}/{len(await get_product_photos_from_db(product_id))}",
        reply_markup=get_photo_rotation_keyboard(product_id, photo_index)
    )
    # Додаємо кнопку "Готово" після останнього фото
    if photo_index == len(await get_product_photos_from_db(product_id)) - 1:
        await bot.send_message(
            chat_id=chat_id,
            text="Коли закінчите редагування фото, натисніть 'Готово'.",
            reply_markup=get_photo_rotation_done_keyboard(product_id)
        )

@dp.callback_query_handler(lambda c: c.data.startswith('rotate_single_photo_'), state=ModeratorActions.rotating_photos, user_id=ADMIN_IDS)
async def process_rotate_single_photo(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки 'Повернути фото на 90°' для модератора."""
    parts = callback_query.data.split('_')
    product_id = int(parts[-2])
    photo_index = int(parts[-1])

    async with state.proxy() as data:
        if data['product_id_to_rotate'] != product_id:
            await callback_query.answer("Помилка: невідповідність товару.")
            return

        original_file_id = data['rotated_photos_file_ids'][photo_index] # Беремо поточний file_id для повороту

        try:
            # Завантажуємо фото
            file_info = await bot.get_file(original_file_id)
            downloaded_file = await bot.download_file(file_info.file_path)
            
            # Відкриваємо зображення за допомогою PIL
            image = Image.open(io.BytesIO(downloaded_file.read()))
            
            # Повертаємо на 90 градусів
            rotated_image = image.rotate(-90, expand=True) # Повертаємо проти годинникової стрілки

            # Зберігаємо повернуте зображення в буфер
            byte_arr = io.BytesIO()
            rotated_image.save(byte_arr, format='JPEG') # Зберігаємо як JPEG
            byte_arr.seek(0)

            # Надсилаємо повернуте фото назад в Telegram і отримуємо новий file_id
            uploaded_photo = await bot.send_photo(
                chat_id=callback_query.message.chat.id,
                photo=types.InputFile(byte_arr, filename=f"rotated_photo_{product_id}_{photo_index}.jpg"),
                caption=f"Повернуте фото {photo_index + 1}"
            )
            new_file_id = uploaded_photo.photo[-1].file_id # Отримуємо file_id нового фото

            # Оновлюємо file_id в стані FSM
            data['rotated_photos_file_ids'][photo_index] = new_file_id
            await callback_query.answer("Фото повернуто.")
            
            # Оновлюємо повідомлення з кнопкою для поточного фото
            await bot.edit_message_reply_markup(
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                reply_markup=get_photo_rotation_keyboard(product_id, photo_index)
            )

        except Exception as e:
            logging.error(f"Помилка повороту фото: {e}")
            await callback_query.answer("Помилка при повороті фотографії.")

@dp.callback_query_handler(lambda c: c.data.startswith('done_rotating_photos_'), state=ModeratorActions.rotating_photos, user_id=ADMIN_IDS)
async def process_done_rotating_photos(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки 'Готово' після редагування фото."""
    product_id = int(callback_query.data.split('_')[-1])
    async with state.proxy() as data:
        if data['product_id_to_rotate'] != product_id:
            await callback_query.answer("Помилка: невідповідність товару.")
            return
        
        new_photos_file_ids = data['rotated_photos_file_ids']
        await update_product_photos_in_db(product_id, new_photos_file_ids)

    product = await get_product_by_id(product_id)
    if product:
        # Повідомляємо користувача про оновлення фото
        await bot.send_message(
            product['user_id'],
            "🔄 Ваш товар оновлено.\n"
            "📸 Фото були повернуті для правильного відображення.\n"
            "Перевірте та подайте повторно, якщо потрібно.",
            reply_markup=get_main_menu_keyboard()
        )
        # Надсилаємо товар на повторну модерацію
        await update_product_status(product_id, 'moderation')
        await send_product_to_moderation(product_id, product['user_id'], product['username'])
    
    await callback_query.answer("Редагування фото завершено. Товар знову надіслано на модерацію.")
    await state.finish()

# --- Обробники Callback-кнопок (Користувач) ---
@dp.callback_query_handler(lambda c: c.data.startswith('republish_product_'))
async def process_republish_product(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Переопублікувати' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    if product['republish_count'] >= 3:
        await callback_query.answer("Ви досягли ліміту переопублікацій (3 рази).")
        return

    new_republish_count = await increment_product_republish_count(product_id)
    await update_product_status(product_id, 'moderation')
    await send_product_to_moderation(product_id, product['user_id'], product['username'])
    
    await callback_query.answer(f"Товар надіслано на переопублікацію. Залишилось {3 - new_republish_count} спроб.")
    await bot.send_message(product['user_id'], f"🔁 Ваш товар «{product['name']}» надіслано на повторну модерацію.")

@dp.callback_query_handler(lambda c: c.data.startswith('sold_product_'))
async def process_sold_product(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Продано' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    try:
        # Припускаємо, що ціна може бути у форматі "ЧИСЛО грн" або "ЧИСЛО USD"
        price_value = 0
        if "грн" in product['price'].lower():
            price_value = float(product['price'].lower().replace('грн', '').strip())
        elif "usd" in product['price'].lower():
            price_value = float(product['price'].lower().replace('usd', '').strip()) * 40 # Приклад курсу
        else:
            await callback_query.answer("Не вдалося розрахувати комісію. Будь ласка, вкажіть ціну в грн або USD.")
            return

        commission = price_value * 0.10 # 10% комісія
        
        await update_product_status(product_id, 'sold')
        
        # Видаляємо товар з каналу, якщо він був опублікований
        if product['channel_message_id']:
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
        logging.error(f"Помилка при обробці 'Продано': {e}")
        await callback_query.answer("Виникла помилка.")

@dp.callback_query_handler(lambda c: c.data.startswith('change_price_'))
async def process_change_price(callback_query: types.CallbackQuery, state: FSMContext):
    """Обробник кнопки 'Змінити ціну' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    
    await state.set_state('ChangingPrice.new_price')
    async with state.proxy() as data:
        data['product_id_to_change_price'] = product_id
    
    await callback_query.answer("Введіть нову ціну:")
    await bot.send_message(callback_query.from_user.id, "Введіть нову ціну (наприклад, 600 грн або 25 USD):")

class ChangingPrice(StatesGroup):
    new_price = State()

@dp.message_handler(state=ChangingPrice.new_price)
async def process_new_price(message: types.Message, state: FSMContext):
    """Обробка нової ціни."""
    async with state.proxy() as data:
        product_id = data['product_id_to_change_price']
        new_price = message.text
        
        await update_product_price(product_id, new_price)
        
        # Оновлюємо статус на модерацію, щоб модератор міг перевірити нову ціну
        await update_product_status(product_id, 'moderation')
        product = await get_product_by_id(product_id)
        if product:
            await send_product_to_moderation(product_id, product['user_id'], product['username'])

        await message.answer(f"Ціну товару оновлено на '{new_price}' і відправлено на повторну модерацію.", reply_markup=get_main_menu_keyboard())
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('delete_product_'))
async def process_delete_product(callback_query: types.CallbackQuery):
    """Обробник кнопки 'Видалити' для користувача."""
    product_id = int(callback_query.data.split('_')[-1])
    product = await get_product_by_id(product_id)

    if not product:
        await callback_query.answer("Товар не знайдено.")
        return
    
    await delete_product_from_db(product_id)
    
    # Видаляємо товар з каналу, якщо він був опублікований
    if product['channel_message_id']:
        try:
            await bot.delete_message(CHANNEL_ID, product['channel_message_id'])
        except Exception as e:
            logging.warning(f"Не вдалося видалити повідомлення з каналу: {e}")

    await callback_query.answer("Товар видалено.")
    await bot.send_message(callback_query.from_user.id, f"🗑 Ваш товар «{product['name']}» видалено.")

# --- Запуск бота ---
async def on_startup(dp):
    """Виконується при запуску бота."""
    await init_db()
    logging.info("Бот запущено!")

if __name__ == '__main__':
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)

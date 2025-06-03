import os
import re
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import asyncio

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
import gspread
from google.oauth2.service_account import Credentials

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class FuelTrackingBot:
    def __init__(self, telegram_token: str, google_sheets_credentials_path: str, spreadsheet_id: str):
        self.telegram_token = telegram_token
        self.spreadsheet_id = spreadsheet_id
        self.supported_cars = []
        self.supported_generators = []

        # Настройка Google Sheets
        self.scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]

        try:
            self.credentials = Credentials.from_service_account_file(
                google_sheets_credentials_path, scopes=self.scope
            )
            self.gc = gspread.authorize(self.credentials)
            self.spreadsheet = self.gc.open_by_key(spreadsheet_id)
            logger.info("✅ Успішно підключились до Google Sheets")
            
            # Завантажуємо списки автомобілів та генераторів
            self.load_vehicles_and_generators()
            
        except FileNotFoundError:
            logger.error("❌ Файл credentials.json не знайдено!")
            raise Exception("Файл з обліковими даними Google не знайдено. Перевірте шлях до credentials.json")
        except gspread.exceptions.APIError as e:
            if "SERVICE_DISABLED" in str(e):
                logger.error("❌ Google Sheets API не увімкнено!")
                raise Exception(
                    "Google Sheets API не увімкнено у вашому проекті!\n"
                    "Увімкніть API за посиланням: https://console.developers.google.com/apis/api/sheets.googleapis.com/overview\n"
                    "Також увімкніть Google Drive API: https://console.developers.google.com/apis/api/drive.googleapis.com/overview"
                )
            else:
                logger.error(f"❌ Помилка Google Sheets API: {e}")
                raise Exception(f"Помилка доступу до Google Sheets: {e}")
        except PermissionError:
            logger.error("❌ Немає доступу до Google Sheets!")
            raise Exception(
                "Немає доступу до Google Таблиці!\n"
                "1. Переконайтеся, що увімкнено Google Sheets API та Google Drive API\n"
                "2. Перевірте, що Service Account має доступ до таблиці\n"
                "3. Зачекайте кілька хвилин після увімкнення API"
            )

        # Регулярные выражения для парсинга сообщений (улучшенные)
        self.purchase_pattern = re.compile(
            r'(?P<car_number>\d+)\s*(?:\n|\s)+[Кк]упил\s+(?P<volume>\d+)\s*литр[а-я]*\s*по\s+(?P<price>\d+(?:[.,]\d+)?)\s*грн',
            re.IGNORECASE | re.MULTILINE
        )

        self.refuel_pattern = re.compile(
            r'(?P<car_number>\d+)\s*(?:\n|\s)+[Зз]аправка\s+(?P<volume>\d+)\s*литр[а-я]*.*?[Пп]робег[:\s]*(?P<mileage>\d+)\s*км',
            re.IGNORECASE | re.MULTILINE | re.DOTALL
        )

        self.generator_pattern = re.compile(
            r'(?P<car_number>\d+)\s*(?:\n|\s)+[Зз]аправка\s+генератора.*?(?P<volume>\d+)\s*литр[а-я]*.*?цена\s+(?P<price>\d+(?:[.,]\d+)?)\s*грн.*?моточасы[:\s]*(?P<hours>\d+)',
            re.IGNORECASE | re.MULTILINE | re.DOTALL
        )

        # Состояние пользователя для многошагового ввода
        self.user_states = {}

    def load_vehicles_and_generators(self):
        """Завантаження списку автомобілів та генераторів з таблиці"""
        try:
            # Створюємо або отримуємо лист з автомобілями
            vehicles_sheet = self.get_or_create_worksheet("Автомобілі")
            if len(vehicles_sheet.get_all_values()) <= 1:  # Якщо лист порожній (тільки заголовки)
                vehicles_sheet.append_row(["Номер", "Назва", "Тип"])

            # Завантажуємо дані
            vehicles_data = vehicles_sheet.get_all_records()
            self.supported_cars = [str(v['Номер']) for v in vehicles_data if v['Тип'] == 'Автомобіль']
            self.supported_generators = [str(v['Номер']) for v in vehicles_data if v['Тип'] == 'Генератор']
            
            logger.info(f"✅ Завантажено {len(self.supported_cars)} автомобілів та {len(self.supported_generators)} генераторів")
            
        except Exception as e:
            logger.error(f"❌ Помилка при завантаженні списку автомобілів та генераторів: {e}")
            raise Exception("Не вдалося завантажити список автомобілів та генераторів")

    def test_connection(self):
        """Тестирование подключения к Google Sheets"""
        try:
            # Пробуем получить информацию о таблице
            sheet_info = self.spreadsheet.title
            logger.info(f"✅ Подключение успешно! Таблица: '{sheet_info}'")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка подключения: {e}")
            return False

    def get_or_create_worksheet(self, sheet_name: str):
        """Отримати або створити лист у таблиці"""
        try:
            worksheet = self.spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = self.spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)
            # Додаємо заголовки в залежності від типу листа
            if sheet_name == "Автомобілі":
                headers = ["Номер", "Назва", "Тип"]
            elif "Авто" in sheet_name:
                headers = ["Дата", "Тип операции", "Объём (л)", "Цена за литр", "Общая стоимость", "Пробег",
                           "Пользователь", "Фото"]
            elif "Генератор" in sheet_name:
                headers = ["Дата", "Объём (л)", "Цена за литр", "Общая стоимость", "Моточасы", "Пользователь", "Фото"]
            else:
                headers = ["Дані"]

            worksheet.append_row(headers)

        return worksheet

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        # Создаём клавиатуру с кнопками
        keyboard = [
            [KeyboardButton("🟢 Закупка топлива"), KeyboardButton("🔵 Заправка авто")],
            [KeyboardButton("🟡 Заправка генератора"), KeyboardButton("⚡ Генератор")],
            [KeyboardButton("📊 Остатки"), KeyboardButton("📈 История")],
            [KeyboardButton("📋 Шаблоны")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        cars_list = "\n".join([f"🚗 Авто {car}" for car in self.supported_cars])
        generators_list = "\n".join([f"⚡ Генератор {gen}" for gen in self.supported_generators])

        welcome_message = f"""
🛠 Добро пожаловать в бот учёта топлива!

Выберите действие с помощью кнопок ниже или используйте команды:

📋 **Команды:**
/остаток [номер] - остаток топлива
/генератор [номер] - информация по генератору  
/история [номер] - последние операции
/шаблоны - примеры сообщений

🚗 **Доступные авто:**
{cars_list}

⚡ **Доступные генераторы:**
{generators_list}

Нажмите кнопку для быстрого ввода данных!
        """
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)

    async def templates(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /шаблони"""
        templates_message = """
📑 Шаблони введення даних:

1️⃣ Закупка палива:
1. Натисніть кнопку "🟢 Закупка топлива"
2. Введіть номер авто (наприклад: 5513)
3. Введіть об'єм та ціну у форматі:
   200 літрів по 58 грн
4. Додайте фото чека до повідомлення

2️⃣ Заправка автомобіля:
1. Натисніть кнопку "🔵 Заправка авто"
2. Введіть номер авто (наприклад: 5513)
3. Введіть об'єм та пробіг у форматі:
   30 літрів. Пробіг: 125000 км
4. Додайте фото чека до повідомлення

3️⃣ Заправка генератора:
1. Натисніть кнопку "🟡 Заправка генератора"
2. Введіть номер генератора (наприклад: 5513)
3. Введіть об'єм, ціну та моточаси у форматі:
   10 літрів, ціна 60 грн, моточаси: 255
4. Додайте фото чека до повідомлення

❗️ Важливо: Фото чека обов'язкове для всіх операцій!
💡 Для скасування операції напишіть "отмена"
"""
        await update.message.reply_text(templates_message)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /история"""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Укажіть номер автомобіля. Приклад: /история 5513"
            )
            return

        car_number = context.args[0]
        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Помилка: Номер автомобіля {car_number} не підтримується.\n"
                f"Доступні номери: {', '.join(self.supported_cars)}"
            )
            return

        try:
            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            records = worksheet.get_all_records()
            
            if not records:
                await update.message.reply_text(f"📊 Немає даних по автомобілю {car_number}")
                return

            # Беремо останні 5 записів
            last_records = records[-5:]
            message = f"📈 Останні 5 операцій по автомобілю {car_number}:\n\n"
            
            for record in reversed(last_records):
                operation_type = record.get('Тип операции', 'Невідомо')
                volume = record.get('Объём (л)', 'Н/Д')
                date = record.get('Дата', 'Н/Д')
                
                if operation_type == 'Заправка':
                    mileage = record.get('Пробег', 'Н/Д')
                    message += (
                        f"⛽ Заправка: {volume} л\n"
                        f"📏 Пробіг: {mileage} км\n"
                        f"📅 {date}\n"
                    )
                else:
                    price = record.get('Цена за литр', 'Н/Д')
                    message += (
                        f"🛒 Закупівля: {volume} л\n"
                        f"💰 Ціна: {price} грн/л\n"
                        f"📅 {date}\n"
                    )
                message += "---\n"
            
            await update.message.reply_text(message)
            
        except Exception as e:
            logger.error(f"Помилка при отриманні історії: {e}")
            await update.message.reply_text(
                "⚠️ Помилка при отриманні історії. Спробуйте ще раз або зверніться до адміністратора."
            )

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /остаток"""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Укажіть номер автомобіля. Приклад: /остаток 5513"
            )
            return

        car_number = context.args[0]
        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Помилка: Номер автомобіля {car_number} не підтримується.\n"
                f"Доступні номери: {', '.join(self.supported_cars)}"
            )
            return

        try:
            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            records = worksheet.get_all_records()

            total_purchased = 0
            total_consumed = 0
            total_cost = 0
            last_mileage = 0

            for record in records:
                if record.get('Тип операции') == 'Закупка':
                    volume = float(record.get('Объём (л)', 0) or 0)
                    price = float(record.get('Цена за литр', 0) or 0)
                    total_purchased += volume
                    total_cost += volume * price
                elif record.get('Тип операции') == 'Заправка':
                    total_consumed += float(record.get('Объём (л)', 0) or 0)
                    last_mileage = int(record.get('Пробег', 0) or 0)

            balance = total_purchased - total_consumed
            avg_price = total_cost / total_purchased if total_purchased > 0 else 0

            message = f"""
📊 Статистика по автомобілю {car_number}:

💰 Закуплено: {total_purchased:.1f} л
⛽ Витрачено: {total_consumed:.1f} л
📈 Залишок: {balance:.1f} л
💵 Середня ціна: {avg_price:.2f} грн/л
📏 Останній пробіг: {last_mileage} км
            """
            await update.message.reply_text(message)

        except Exception as e:
            logger.error(f"Помилка при отриманні залишку: {e}")
            await update.message.reply_text(
                "⚠️ Помилка при отриманні даних про залишок. Спробуйте ще раз або зверніться до адміністратора."
            )

    async def generator_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /генератор"""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Укажіть номер генератора. Приклад: /генератор 5513"
            )
            return

        generator_number = context.args[0]
        if not self.validate_generator_number(generator_number):
            await update.message.reply_text(
                f"⚠️ Помилка: Номер генератора {generator_number} не підтримується.\n"
                f"Доступні номери: {', '.join(self.supported_generators)}"
            )
            return

        try:
            worksheet_name = f"Генератор {generator_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            records = worksheet.get_all_records()
            
            if not records:
                await update.message.reply_text(f"📊 Немає даних по генератору {generator_number}")
                return

            # Отримуємо останні 5 записів
            last_records = records[-5:]
            total_volume = sum(float(r.get('Объём (л)', 0) or 0) for r in records)
            total_cost = sum(float(r.get('Общая стоимость', 0) or 0) for r in records)
            last_hours = int(last_records[-1].get('Моточасы', 0) or 0)
            
            message = f"""
⚡ Статистика по генератору {generator_number}:

📊 Загальна статистика:
⛽ Загальний об'єм: {total_volume:.1f} л
💰 Загальна вартість: {total_cost:.2f} грн
🕐 Останні моточаси: {last_hours}

📈 Останні 5 заправок:
"""
            
            for record in reversed(last_records):
                volume = record.get('Объём (л)', 'Н/Д')
                price = record.get('Цена за литр', 'Н/Д')
                hours = record.get('Моточасы', 'Н/Д')
                date = record.get('Дата', 'Н/Д')
                
                message += f"""
⛽ Об'єм: {volume} л
💰 Ціна: {price} грн/л
🕐 Моточаси: {hours}
📅 {date}
---
"""
            
            await update.message.reply_text(message)
            
        except Exception as e:
            logger.error(f"Помилка при отриманні інформації про генератор: {e}")
            await update.message.reply_text(
                "⚠️ Помилка при отриманні даних про генератор. Спробуйте ще раз або зверніться до адміністратора."
            )

    async def handle_button_press(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий на кнопки"""
        text = update.message.text if update.message.text else ""
        user_id = update.message.from_user.id

        # Команда отмены
        if text.lower() in ["отмена", "cancel", "/cancel"]:
            if user_id in self.user_states:
                del self.user_states[user_id]
                await update.message.reply_text("❌ Операция отменена.")
            return

        if text == "🟢 Закупка топлива":
            self.user_states[user_id] = {"action": "purchase", "step": "car_number"}
            await update.message.reply_text(
                "🚗 Введите номер автомобиля (например: 5513):\n\n"
                "💡 Для отмены напишите 'отмена'"
            )

        elif text == "🔵 Заправка авто":
            self.user_states[user_id] = {"action": "refuel", "step": "car_number"}
            await update.message.reply_text(
                "🚗 Введите номер автомобиля (например: 5513):\n\n"
                "💡 Для отмены напишите 'отмена'"
            )

        elif text == "🟡 Заправка генератора":
            self.user_states[user_id] = {"action": "generator", "step": "car_number"}
            await update.message.reply_text(
                "⚡ Введите номер генератора (например: 5513):\n\n"
                "💡 Для отмены напишите 'отмена'"
            )

        elif text == "📊 Остатки":
            self.user_states[user_id] = {"action": "balance", "step": "car_number"}
            await update.message.reply_text(
                "🚗 Введите номер автомобиля для проверки остатка (например: 5513):\n\n"
                "💡 Для отмены напишите 'отмена'"
            )

        elif text == "⚡ Генератор":
            self.user_states[user_id] = {"action": "generator_info", "step": "car_number"}
            await update.message.reply_text(
                "⚡ Введите номер генератора для просмотра информации (например: 5513):\n\n"
                "💡 Для отмены напишите 'отмена'"
            )

        elif text == "📈 История":
            self.user_states[user_id] = {"action": "history", "step": "car_number"}
            await update.message.reply_text(
                "🚗 Введите номер автомобиля для просмотра истории (например: 5513):\n\n"
                "💡 Для отмены напишите 'отмена'"
            )

        elif text == "📋 Шаблоны":
            await self.templates(update, context)

        else:
            # Если не кнопка, пробуем обработать как обычное сообщение
            await self.handle_text_input(update, context)

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстового ввода в зависимости от состояния пользователя"""
        user_id = update.message.from_user.id
        text = update.message.text

        # Если пользователь в процессе многошагового ввода
        if user_id in self.user_states:
            await self.handle_step_input(update, context)
            return

        # Иначе пробуем автоматически распознать формат
        await self.handle_message(update, context)
        """Обработка текстовых сообщений"""
        text = update.message.text
        user = update.message.from_user
        username = user.username or f"{user.first_name} {user.last_name or ''}".strip()

        # Проверяем наличие фото
        photo_info = "Есть" if update.message.photo else "Нет"

        # Попытка обработать как закупку топлива
        purchase_match = self.purchase_pattern.search(text)
        if purchase_match:
            await self.handle_purchase(update, purchase_match, username, photo_info)
            return

        # Попытка обработать как заправку автомобиля
        refuel_match = self.refuel_pattern.search(text)
        if refuel_match:
            await self.handle_refuel(update, refuel_match, username, photo_info)
            return

        # Попытка обработать как заправку генератора
        generator_match = self.generator_pattern.search(text)
        if generator_match:
            await self.handle_generator_refuel(update, generator_match, username, photo_info)
            return

        # Если ничего не подошло
        await update.message.reply_text(
            "⚠️ Не удалось распознать сообщение. Используйте /шаблоны для просмотра правильных форматов."
        )

    def validate_car_number(self, car_number: str) -> bool:
        """Перевірка чи підтримується номер автомобіля"""
        return car_number in self.supported_cars

    def validate_generator_number(self, generator_number: str) -> bool:
        """Перевірка чи підтримується номер генератора"""
        return generator_number in self.supported_generators

    async def handle_purchase(self, update: Update, match, username: str, photo_url: str = None):
        """Обробка закупівлі палива"""
        if not photo_url:
            await update.message.reply_text(
                "⚠️ Помилка: Фото чека обов'язкове для закупівлі палива.\n"
                "Спробуйте знову."
            )
            return

        car_number = match.group('car_number')

        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Помилка: Номер автомобіля {car_number} не підтримується.\n"
                f"Доступні номери: {', '.join(self.supported_cars)}"
            )
            return

        volume = float(match.group('volume'))
        price = float(match.group('price').replace(',', '.'))
        total_cost = volume * price

        try:
            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)

            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            row_data = [
                current_date,
                "Закупка",
                volume,
                price,
                total_cost,
                "",  # Пробег не применим для закупки
                username,
                photo_url
            ]

            worksheet.append_row(row_data)

            await update.message.reply_text(
                f"✅ Прийнято! {volume} літрів по {price} грн додано на склад авто {car_number} з фото чека.\n"
                f"💰 Загальна вартість: {total_cost} грн"
            )

            # Очищаємо стан користувача
            if update.message.from_user.id in self.user_states:
                del self.user_states[update.message.from_user.id]

        except Exception as e:
            logger.error(f"Помилка при записі закупівлі: {e}")
            await update.message.reply_text(
                "⚠️ Помилка при збереженні даних. Спробуйте ще раз або зверніться до адміністратора."
            )

    # Додатковий метод для обробки лише фото (якщо потрібно)
    async def handle_photo_only(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка окремих фото від користувачів"""
        user_id = update.message.from_user.id

        # Перевіряємо, чи користувач в процесі введення даних
        if user_id in self.user_states:
            # Якщо користувач надіслав лише фото без тексту під час введення об'єму
            state = self.user_states[user_id]
            if state["action"] == "purchase" and state["step"] == "volume":
                await update.message.reply_text(
                    "📸 Фото отримано! Тепер введіть об'єм та ціну:\n"
                    "Приклад: 200 літрів по 58 грн"
                )
                return

        # Якщо фото надіслано поза контекстом
        await update.message.reply_text(
            "📸 Фото отримано, але для реєстрації закупівлі використовуйте команду /fuel"
        )

    # Метод для налаштування заголовків таблиці
    def setup_worksheet_headers(self, worksheet):
        """Налаштування заголовків для робочого листа"""
        headers = [
            "Дата/Час",
            "Тип операції",
            "Об'єм (л)",
            "Ціна за літр (грн)",
            "Загальна вартість (грн)",
            "Пробег (км)",
            "Користувач",
            "Фото чека"
        ]

        # Перевіряємо, чи є заголовки
        if not worksheet.get_all_values():
            worksheet.append_row(headers)

            # Форматування заголовків (опціонально, якщо підтримується)
            try:
                worksheet.format('A1:H1', {
                    "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8},
                    "textFormat": {"bold": True}
                })
            except:
                pass

    async def handle_refuel(self, update: Update, match, username: str, photo_url: str = None):
        """Обробка заправки автомобіля"""
        if not photo_url:
            await update.message.reply_text(
                "⚠️ Помилка: Фото чека обов'язкове для закупівлі палива.\n"
                "Спробуйте знову."
            )
            return

        car_number = match.group('car_number')
        
        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Помилка: Номер автомобіля {car_number} не підтримується.\n"
                f"Доступні номери: {', '.join(self.supported_cars)}"
            )
            return

        try:
            volume = float(match.group('volume'))
            mileage = int(match.group('mileage'))
            
            if volume <= 0:
                await update.message.reply_text("⚠️ Помилка: Об'єм заправки повинен бути більше 0")
                return
                
            if mileage < 0:
                await update.message.reply_text("⚠️ Помилка: Пробіг не може бути від'ємним")
                return

            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            row_data = [
                current_date,
                "Заправка",
                volume,
                "",  # Цена не применима для заправки
                "",  # Общая стоимость не применима для заправки
                mileage,
                username,
                photo_url
            ]
            
            worksheet.append_row(row_data)
            
            # Отримуємо остаток після заправки
            records = worksheet.get_all_records()
            total_purchased = sum(float(r.get('Объём (л)', 0) or 0) for r in records if r.get('Тип операции') == 'Закупка')
            total_consumed = sum(float(r.get('Объём (л)', 0) or 0) for r in records if r.get('Тип операции') == 'Заправка')
            balance = total_purchased - total_consumed
            
            await update.message.reply_text(
                f"✅ Заправка {volume} л записана з фото чека.\n"
                f"📏 Пробіг: {mileage} км\n"
                f"📊 Залишок на складі: {balance:.1f} л"
            )
            
        except ValueError as e:
            logger.error(f"Помилка при обробці даних заправки: {e}")
            await update.message.reply_text(
                "⚠️ Помилка: Неправильний формат даних.\n"
                "Приклад правильного введення:\n"
                "Заправка 30 літрів. Пробіг: 125000 км"
            )
        except Exception as e:
            logger.error(f"Помилка при записі заправки: {e}")
            await update.message.reply_text(
                "⚠️ Помилка при збереженні даних. Спробуйте ще раз або зверніться до адміністратора."
            )

    async def handle_generator_refuel(self, update: Update, match, username: str, photo_url: str = None):
        """Обробка заправки генератора"""
        if not photo_url:
            await update.message.reply_text(
                "⚠️ Помилка: Фото чека обов'язкове для закупівлі палива.\n"
                "Спробуйте знову."
            )
            return

        car_number = match.group('car_number')
        
        if not self.validate_generator_number(car_number):
            await update.message.reply_text(
                f"⚠️ Помилка: Номер генератора {car_number} не підтримується.\n"
                f"Доступні номери: {', '.join(self.supported_generators)}"
            )
            return

        try:
            volume = float(match.group('volume'))
            price = float(match.group('price').replace(',', '.'))
            hours = int(match.group('hours'))
            
            if volume <= 0:
                await update.message.reply_text("⚠️ Помилка: Об'єм заправки повинен бути більше 0")
                return
                
            if price <= 0:
                await update.message.reply_text("⚠️ Помилка: Ціна повинна бути більше 0")
                return
                
            if hours < 0:
                await update.message.reply_text("⚠️ Помилка: Моточаси не можуть бути від'ємними")
                return

            total_cost = volume * price
            
            worksheet_name = f"Генератор {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            row_data = [
                current_date,
                volume,
                price,
                total_cost,
                hours,
                username,
                photo_url
            ]
            
            worksheet.append_row(row_data)
            
            await update.message.reply_text(
                f"✅ Записано: Генератор {car_number} з фото чека\n"
                f"⛽ Об'єм: {volume} л\n"
                f"💰 Ціна: {price} грн/л\n"
                f"💵 Загальна вартість: {total_cost} грн\n"
                f"🕐 Моточаси: {hours}"
            )
            
        except ValueError as e:
            logger.error(f"Помилка при обробці даних заправки генератора: {e}")
            await update.message.reply_text(
                "⚠️ Помилка: Неправильний формат даних.\n"
                "Приклад правильного введення:\n"
                "Заправка генератора\n"
                "10 літрів, ціна 60 грн, моточаси: 255"
            )
        except Exception as e:
            logger.error(f"Помилка при записі заправки генератора: {e}")
            await update.message.reply_text(
                "⚠️ Помилка при збереженні даних. Спробуйте ще раз або зверніться до адміністратора."
            )

    async def handle_step_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обробка покрокового введення даних"""
        user_id = update.message.from_user.id
        text = update.message.text or update.message.caption
        state = self.user_states[user_id]

        if text and text.lower() in ["отмена", "cancel", "/cancel"]:
            del self.user_states[user_id]
            await update.message.reply_text("❌ Операцію скасовано.")
            return

        if state["action"] == "purchase":
            if state["step"] == "car_number":
                if not self.validate_car_number(text):
                    await update.message.reply_text(
                        f"⚠️ Помилка: Номер автомобіля {text} не підтримується.\n"
                        f"Доступні номери: {', '.join(self.supported_cars)}"
                    )
                    return
                state["car_number"] = text
                state["step"] = "volume"
                await update.message.reply_text(
                    "⛽ Введіть об'єм та ціну у форматі:\n"
                    "200 літрів по 58 грн\n\n"
                    "📸 Також додайте фото чека разом з повідомленням"
                )
            elif state["step"] == "volume":
                # Перевірка формату тексту
                match = re.search(r'(\d+)\s*літр(?:[а-яіїєґ]*)?\s*по\s*(\d+(?:[.,]\d+)?)\s*грн', text or "",
                                  re.IGNORECASE)
                if not match:
                    await update.message.reply_text(
                        "⚠️ Помилка: Неправильний формат.\n"
                        "Приклад: 200 літрів по 58 грн"
                    )
                    return

                volume = float(match.group(1))
                price = float(match.group(2).replace(',', '.'))
                username = update.message.from_user.username or f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()

                # Обробка фото
                photo_url = None
                if update.message.photo:
                    try:
                        # Берем найбільше за розміром фото
                        photo = update.message.photo[-1]
                        photo_file = await context.bot.get_file(photo.file_id)
                        photo_url = photo_file.file_path
                    except Exception as e:
                        logger.error(f"Помилка при отриманні фото: {e}")

                # Створюємо об'єкт match для handle_purchase
                match_obj = type('Match', (), {
                    'group': lambda x: {
                        'car_number': state["car_number"],
                        'volume': str(volume),
                        'price': str(price)
                    }[x]
                })

                await self.handle_purchase(update, match_obj, username, photo_url)
                del self.user_states[user_id]

        elif state["action"] == "refuel":
            if state["step"] == "car_number":
                if not self.validate_car_number(text):
                    await update.message.reply_text(
                        f"⚠️ Помилка: Номер автомобіля {text} не підтримується.\n"
                        f"Доступні номери: {', '.join(self.supported_cars)}"
                    )
                    return
                state["car_number"] = text
                state["step"] = "volume"
                await update.message.reply_text(
                    "⛽ Введіть об'єм та пробіг у форматі:\n"
                    "30 літрів. Пробіг: 125000 км\n\n"
                    "📸 Також додайте фото чека разом з повідомленням"
                )
            elif state["step"] == "volume":
                match = re.search(r'(\d+)\s*літр[а-яіїєґ]*.*?[Пп]роб[іе]г[:\s]*(\d+)\s*км', text, re.IGNORECASE | re.DOTALL)
                if not match:
                    await update.message.reply_text(
                        "⚠️ Помилка: Неправильний формат.\n"
                        "Приклад: 30 літрів. Пробіг: 125000 км"
                    )
                    return

                volume = float(match.group(1))
                mileage = int(match.group(2))
                username = update.message.from_user.username or f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()

                # Обробка фото
                photo_url = None
                if update.message.photo:
                    try:
                        photo = update.message.photo[-1]
                        photo_file = await context.bot.get_file(photo.file_id)
                        photo_url = photo_file.file_path
                    except Exception as e:
                        logger.error(f"Помилка при отриманні фото: {e}")

                # Створюємо об'єкт match для handle_refuel
                match_obj = type('Match', (), {
                    'group': lambda x: {
                        'car_number': state["car_number"],
                        'volume': str(volume),
                        'mileage': str(mileage)
                    }[x]
                })

                await self.handle_refuel(update, match_obj, username, photo_url)
                del self.user_states[user_id]

        elif state["action"] == "generator":
            if state["step"] == "car_number":
                if not self.validate_generator_number(text):
                    await update.message.reply_text(
                        f"⚠️ Помилка: Номер генератора {text} не підтримується.\n"
                        f"Доступні номери: {', '.join(self.supported_generators)}"
                    )
                    return
                state["car_number"] = text
                state["step"] = "volume"
                await update.message.reply_text(
                    "⛽ Введіть об'єм, ціну та моточаси у форматі:\n"
                    "10 літрів, ціна 60 грн, моточаси: 255\n\n"
                    "📸 Також додайте фото чека разом з повідомленням"
                )
            elif state["step"] == "volume":
                match = re.search(r'(\d+)\s*літр[а-яіїєґ]*.*?ціна\s*(\d+(?:[.,]\d+)?)\s*грн.*?моточаси[:\s]*(\d+)', text, re.IGNORECASE | re.DOTALL)
                if not match:
                    await update.message.reply_text(
                        "⚠️ Помилка: Неправильний формат.\n"
                        "Приклад: 10 літрів, ціна 60 грн, моточаси: 255"
                    )
                    return

                volume = float(match.group(1))
                price = float(match.group(2).replace(',', '.'))
                hours = int(match.group(3))
                username = update.message.from_user.username or f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()

                # Обробка фото
                photo_url = None
                if update.message.photo:
                    try:
                        photo = update.message.photo[-1]
                        photo_file = await context.bot.get_file(photo.file_id)
                        photo_url = photo_file.file_path
                    except Exception as e:
                        logger.error(f"Помилка при отриманні фото: {e}")

                # Створюємо об'єкт match для handle_generator_refuel
                match_obj = type('Match', (), {
                    'group': lambda x: {
                        'car_number': state["car_number"],
                        'volume': str(volume),
                        'price': str(price),
                        'hours': str(hours)
                    }[x]
                })

                await self.handle_generator_refuel(update, match_obj, username, photo_url)
                del self.user_states[user_id]

        elif state["action"] == "history":
            if state["step"] == "car_number":
                if not self.validate_car_number(text):
                    await update.message.reply_text(
                        f"⚠️ Помилка: Номер автомобіля {text} не підтримується.\n"
                        f"Доступні номери: {', '.join(self.supported_cars)}"
                    )
                    return
                
                # Створюємо контекст з аргументами для history
                context.args = [text]
                await self.history(update, context)
                del self.user_states[user_id]

        elif state["action"] == "balance":
            if state["step"] == "car_number":
                if not self.validate_car_number(text):
                    await update.message.reply_text(
                        f"⚠️ Помилка: Номер автомобіля {text} не підтримується.\n"
                        f"Доступні номери: {', '.join(self.supported_cars)}"
                    )
                    return
                
                # Створюємо контекст з аргументами для balance
                context.args = [text]
                await self.balance(update, context)
                del self.user_states[user_id]

        elif state["action"] == "generator_info":
            if state["step"] == "car_number":
                if not self.validate_generator_number(text):
                    await update.message.reply_text(
                        f"⚠️ Помилка: Номер генератора {text} не підтримується.\n"
                        f"Доступні номери: {', '.join(self.supported_generators)}"
                    )
                    return
                
                # Створюємо контекст з аргументами для generator_info
                context.args = [text]
                await self.generator_info(update, context)
                del self.user_states[user_id]

    def run(self):
        """Запуск бота"""
        application = Application.builder().token(self.telegram_token).build()

        # Регистрация обработчиков команд
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("templates", self.templates))
        application.add_handler(CommandHandler("balance", self.balance))
        application.add_handler(CommandHandler("generator", self.generator_info))
        application.add_handler(CommandHandler("history", self.history))

        # Обработчик кнопок и текстовых сообщений
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_button_press
        ))

        # Обработчик фотографий с подписями
        application.add_handler(MessageHandler(
            filters.PHOTO,
            self.handle_button_press
        ))

        logger.info("🤖 Бот запущен и готов к работе!")

        # Запуск бота
        application.run_polling()


if __name__ == "__main__":
    # Настройте эти параметры
    TELEGRAM_TOKEN = "8188884027:AAE4UprngplID-8bddLp63LwS13HOkRACp8"
    GOOGLE_SHEETS_CREDENTIALS_PATH = "credentials.json"
    SPREADSHEET_ID = "1IwuHWYLZaiUPfNFc2YpZg9k5OOVRYX99Nt0V2i5T1lA"

    try:
        bot = FuelTrackingBot(
            telegram_token=TELEGRAM_TOKEN,
            google_sheets_credentials_path=GOOGLE_SHEETS_CREDENTIALS_PATH,
            spreadsheet_id=SPREADSHEET_ID
        )

        # Тестируем подключение
        if bot.test_connection():
            print("🚀 Запускаем бота...")
            bot.run()
        else:
            print("❌ Не удалось запустить бота из-за проблем с подключением")

    except Exception as e:
        print(f"❌ Ошибка при запуске бота: {e}")

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
        self.last_sheets_check = None
        self.sheets_check_interval = 60

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
            logger.info("✅ Успешно подключились к Google Sheets")
            
            # Загружаем списки автомобилей и генераторов
            self.load_vehicles_and_generators()
            
        except FileNotFoundError:
            logger.error("❌ Файл credentials.json не найден!")
            raise Exception("Файл с учетными данными Google не найден. Проверьте путь к credentials.json")
        except gspread.exceptions.APIError as e:
            if "SERVICE_DISABLED" in str(e):
                logger.error("❌ Google Sheets API не включен!")
                raise Exception(
                    "Google Sheets API не включен в вашем проекте!\n"
                    "Включите API по ссылке: https://console.developers.google.com/apis/api/sheets.googleapis.com/overview\n"
                    "Также включите Google Drive API: https://console.developers.google.com/apis/api/drive.googleapis.com/overview"
                )
            else:
                logger.error(f"❌ Ошибка Google Sheets API: {e}")
                raise Exception(f"Ошибка доступа к Google Sheets: {e}")
        except PermissionError:
            logger.error("❌ Нет доступа к Google Sheets!")
            raise Exception(
                "Нет доступа к Google Таблице!\n"
                "1. Убедитесь, что включены Google Sheets API и Google Drive API\n"
                "2. Проверьте, что Service Account имеет доступ к таблице\n"
                "3. Подождите несколько минут после включения API"
            )

        # Регулярные выражения для парсинга сообщений
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
        """Загружаем списки автомобилей и генераторов из существующих листов"""
        try:
            # Получаем все листы таблицы
            all_sheets = self.spreadsheet.worksheets()
            
            # Ищем листы с автомобилями и генераторами
            self.supported_cars = []
            self.supported_generators = []
            
            for sheet in all_sheets:
                sheet_name = sheet.title
                # Ищем номер в названии листа
                number_match = re.search(r'\d+', sheet_name)
                if number_match:
                    number = number_match.group(0)
                    if "Авто" in sheet_name:
                        self.supported_cars.append(number)
                    elif "Генератор" in sheet_name:
                        self.supported_generators.append(number)
            
            logger.info(f"✅ Найдено {len(self.supported_cars)} автомобилей и {len(self.supported_generators)} генераторов")
            
        except Exception as e:
            logger.error(f"❌ Ошибка при загрузке списка автомобилей и генераторов: {e}")
            raise Exception("Не удалось загрузить список автомобилей и генераторов")

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
        """Получить или создать лист в таблице"""
        try:
            worksheet = self.spreadsheet.worksheet(sheet_name)
            # Проверяем, есть ли заголовки
            if len(worksheet.get_all_values()) == 0:
                self.setup_worksheet_headers(worksheet)
        except gspread.WorksheetNotFound:
            worksheet = self.spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)
            self.setup_worksheet_headers(worksheet)

        return worksheet

    def setup_worksheet_headers(self, worksheet):
        """Настройка заголовков для рабочего листа"""
        sheet_name = worksheet.title
        
        if "Генератор" in sheet_name:
            headers = ["Дата", "Объём (л)", "Цена за литр", "Общая стоимость", "Моточасы", "Пользователь", "Фото"]
        else:  # для листов с автомобилями
            headers = ["Дата", "Тип операции", "Объём (л)", "Цена за литр", "Общая стоимость", "Пробег", "Пользователь", "Фото"]
            
        worksheet.append_row(headers)
        
        # Форматирование заголовков
        try:
            range_to_format = 'A1:H1' if "Генератор" not in sheet_name else 'A1:G1'
            worksheet.format(range_to_format, {
                "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8},
                "textFormat": {"bold": True}
            })
        except Exception as e:
            logger.error(f"Ошибка при форматировании заголовков: {e}")

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
/balance [номер] - остаток топлива
/generator [номер] - информация по генератору  
/history [номер] - последние операции
/templates - примеры сообщений

🚗 **Доступные автомобили:**
{cars_list}

⚡ **Доступные генераторы:**
{generators_list}

Нажмите кнопку для быстрого ввода данных!
        """
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)

    async def templates(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /templates"""
        templates_message = """
📑 Шаблоны ввода данных:

1️⃣ Закупка топлива:
1. Нажмите кнопку "🟢 Закупка топлива"
2. Введите номер автомобиля (например: 5513)
3. Введите объем и цену в формате:
   200 литров по 58 грн
4. Добавьте фото чека к сообщению

2️⃣ Заправка автомобиля:
1. Нажмите кнопку "🔵 Заправка авто"
2. Введите номер автомобиля (например: 5513)
3. Введите объем и пробег в формате:
   30 литров. Пробег: 125000 км
4. Добавьте фото чека к сообщению

3️⃣ Заправка генератора:
1. Нажмите кнопку "🟡 Заправка генератора"
2. Введите номер генератора (например: 5513)
3. Введите объем, цену и моточасы в формате:
   10 литров, цена 60 грн, моточасы: 255
4. Добавьте фото чека к сообщению

4️⃣ Просмотр информации:
• История операций:
  - Нажмите кнопку "📈 История" или используйте команду /history [номер]
  - Пример: /history 5513

• Остатки топлива:
  - Нажмите кнопку "📊 Остатки" или используйте команду /balance [номер]
  - Пример: /balance 5513

• Информация по генератору:
  - Нажмите кнопку "⚡ Генератор" или используйте команду /generator [номер]
  - Пример: /generator 5513

❗️ Важно: Фото чека обязательно для всех операций!
💡 Для отмены операции напишите "отмена"
"""
        await update.message.reply_text(templates_message)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /история"""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Укажите номер автомобиля. Пример: /история 5513"
            )
            return

        car_number = context.args[0]
        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Ошибка: Номер автомобиля {car_number} не поддерживается.\n"
                f"Доступные номера: {', '.join(self.supported_cars)}"
            )
            return

        try:
            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            records = worksheet.get_all_records()
            
            if not records:
                await update.message.reply_text(f"📊 Нет данных по автомобилю {car_number}")
                return

            # Берем последние 5 записей
            last_records = records[-5:]
            message = f"📈 Последние 5 операций по автомобилю {car_number}:\n\n"
            
            for record in reversed(last_records):
                operation_type = record.get('Тип операции', 'Неизвестно')
                volume = record.get('Объём (л)', 'Н/Д')
                date = record.get('Дата', 'Н/Д')
                
                if operation_type == 'Заправка':
                    mileage = record.get('Пробег', 'Н/Д')
                    message += (
                        f"⛽ Заправка: {volume} л\n"
                        f"📏 Пробег: {mileage} км\n"
                        f"📅 {date}\n"
                    )
                else:
                    price = record.get('Цена за литр', 'Н/Д')
                    message += (
                        f"🛒 Закупка: {volume} л\n"
                        f"💰 Цена: {price} грн/л\n"
                        f"📅 {date}\n"
                    )
                message += "---\n"
            
            await update.message.reply_text(message)
            
        except Exception as e:
            logger.error(f"Ошибка при получении истории: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка при получении истории. Попробуйте еще раз или обратитесь к администратору."
            )

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /остаток"""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Укажите номер автомобиля. Пример: /остаток 5513"
            )
            return

        car_number = context.args[0]
        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Ошибка: Номер автомобиля {car_number} не поддерживается.\n"
                f"Доступные номера: {', '.join(self.supported_cars)}"
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
📊 Статистика по автомобилю {car_number}:

💰 Закуплено: {total_purchased:.1f} л
⛽ Израсходовано: {total_consumed:.1f} л
📈 Остаток: {balance:.1f} л
💵 Средняя цена: {avg_price:.2f} грн/л
📏 Последний пробег: {last_mileage} км
            """
            await update.message.reply_text(message)

        except Exception as e:
            logger.error(f"Ошибка при получении остатка: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка при получении данных об остатке. Попробуйте еще раз или обратитесь к администратору."
            )

    async def generator_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /генератор"""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Укажите номер генератора. Пример: /генератор 5513"
            )
            return

        generator_number = context.args[0]
        if not self.validate_generator_number(generator_number):
            await update.message.reply_text(
                f"⚠️ Ошибка: Номер генератора {generator_number} не поддерживается.\n"
                f"Доступные номера: {', '.join(self.supported_generators)}"
            )
            return

        try:
            worksheet_name = f"Генератор {generator_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            records = worksheet.get_all_records()
            
            if not records:
                await update.message.reply_text(f"📊 Нет данных по генератору {generator_number}")
                return

            # Получаем последние 5 записей
            last_records = records[-5:]
            total_volume = sum(float(r.get('Объём (л)', 0) or 0) for r in records)
            total_cost = sum(float(r.get('Общая стоимость', 0) or 0) for r in records)
            last_hours = int(last_records[-1].get('Моточасы', 0) or 0)
            
            message = f"""
⚡ Статистика по генератору {generator_number}:

📊 Общая статистика:
⛽ Общий объем: {total_volume:.1f} л
💰 Общая стоимость: {total_cost:.2f} грн
🕐 Последние моточасы: {last_hours}

📈 Последние 5 заправок:
"""
            
            for record in reversed(last_records):
                volume = record.get('Объём (л)', 'Н/Д')
                price = record.get('Цена за литр', 'Н/Д')
                hours = record.get('Моточасы', 'Н/Д')
                date = record.get('Дата', 'Н/Д')
                
                message += f"""
⛽ Объем: {volume} л
💰 Цена: {price} грн/л
🕐 Моточасы: {hours}
📅 {date}
---
"""
            
            await update.message.reply_text(message)
            
        except Exception as e:
            logger.error(f"Ошибка при получении информации о генераторе: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка при получении данных о генераторе. Попробуйте еще раз или обратитесь к администратору."
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
            try:
                await self.handle_step_input(update, context)
            except KeyError:
                # Если состояние было удалено в другом месте
                logger.info(f"Состояние пользователя {user_id} уже было удалено")
            return

        await update.message.reply_text(
            "⚠️ Не удалось распознать сообщение. Попробуйте снова."
        )

    def validate_car_number(self, car_number: str) -> bool:
        """Перевірка чи підтримується номер автомобіля"""
        return car_number in self.supported_cars

    def validate_generator_number(self, generator_number: str) -> bool:
        """Перевірка чи підтримується номер генератора"""
        return generator_number in self.supported_generators

    async def handle_purchase(self, update: Update, match, username: str, photo_url: str = None):
        """Обработка закупки топлива"""
        if not photo_url:
            await update.message.reply_text(
                "⚠️ Ошибка: Фото чека обязательно для закупки топлива.\n"
                "Попробуйте снова."
            )
            return

        car_number = match.group('car_number')

        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Ошибка: Номер автомобиля {car_number} не поддерживается.\n"
                f"Доступные номера: {', '.join(self.supported_cars)}"
            )
            return

        volume = float(match.group('volume'))
        price = float(match.group('price').replace(',', '.'))
        total_cost = volume * price

        try:
            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)

            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Форматування photo_url для Google Sheets (клікабельна мініатюра)
            formula = None
            if photo_url:
                formula = f'=HYPERLINK("{photo_url}"; IMAGE("{photo_url}"))'

            # Додаємо рядок з простим лінком у полі фото
            row_data = [
                current_date,
                "Закупка",
                volume,
                price,
                total_cost,
                "",  # Пробег не применим для закупки
                username,
                photo_url if photo_url else ""
            ]

            worksheet.append_row(row_data)

            # Якщо є фото, оновлюємо клітинку на формулу
            if formula:
                last_row = len(worksheet.get_all_values())
                photo_col = len(row_data)  # фото завжди останнє поле
                worksheet.update_cell(last_row, photo_col, formula)

            await update.message.reply_text(
                f"✅ Принято! {volume} литров по {price} грн добавлено на склад автомобиль {car_number} с фото чека.\n"
                f"💰 Общая стоимость: {total_cost} грн"
            )

            # Очищаем состояние пользователя
            if update.message.from_user.id in self.user_states:
                del self.user_states[update.message.from_user.id]

        except Exception as e:
            logger.error(f"Ошибка при записи закупки: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка при сохранении данных. Попробуйте еще раз или обратитесь к администратору."
            )

    # Додатковий метод для обробки лише фото (якщо потрібно)
    async def handle_photo_only(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка окремих фото від користувачів"""
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

    async def handle_refuel(self, update: Update, match, username: str, photo_url: str = None):
        """Обработка заправки автомобиля"""
        if not photo_url:
            await update.message.reply_text(
                "⚠️ Ошибка: Фото чека обязательно для заправки автомобиля.\n"
                "Попробуйте снова."
            )
            return

        car_number = match.group('car_number')
        
        if not self.validate_car_number(car_number):
            await update.message.reply_text(
                f"⚠️ Ошибка: Номер автомобиля {car_number} не поддерживается.\n"
                f"Доступные номера: {', '.join(self.supported_cars)}"
            )
            return

        try:
            volume = float(match.group('volume'))
            mileage = int(match.group('mileage'))
            
            if volume <= 0:
                await update.message.reply_text("⚠️ Ошибка: Объем заправки должен быть больше 0")
                return
                
            if mileage < 0:
                await update.message.reply_text("⚠️ Ошибка: Пробег не может быть отрицательным")
                return

            worksheet_name = f"Авто {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Форматування photo_url для Google Sheets (клікабельна мініатюра)
            formula = None
            if photo_url:
                formula = f'=HYPERLINK("{photo_url}"; IMAGE("{photo_url}"))'

            row_data = [
                current_date,
                "Заправка",
                volume,
                "",  # Цена не применима для заправки
                "",  # Общая стоимость не применима для заправки
                mileage,
                username,
                photo_url if photo_url else ""
            ]
            
            worksheet.append_row(row_data)
            
            # Получаем остаток после заправки
            records = worksheet.get_all_records()
            total_purchased = sum(float(r.get('Объём (л)', 0) or 0) for r in records if r.get('Тип операции') == 'Закупка')
            total_consumed = sum(float(r.get('Объём (л)', 0) or 0) for r in records if r.get('Тип операции') == 'Заправка')
            balance = total_purchased - total_consumed
            
            # Якщо є фото, оновлюємо клітинку на формулу
            if formula:
                last_row = len(worksheet.get_all_values())
                photo_col = len(row_data)  # фото завжди останнє поле
                worksheet.update_cell(last_row, photo_col, formula)

            await update.message.reply_text(
                f"✅ Заправка {volume} л записана с фото чека.\n"
                f"📏 Пробег: {mileage} км\n"
                f"📊 Остаток на складе: {balance:.1f} л"
            )
            
        except ValueError as e:
            logger.error(f"Ошибка при обработке данных заправки: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка: Неправильный формат данных.\n"
                "Пример правильного ввода:\n"
                "Заправка 30 литров. Пробег: 125000 км"
            )
        except Exception as e:
            logger.error(f"Ошибка при записи заправки: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка при сохранении данных. Попробуйте еще раз или обратитесь к администратору."
            )

    async def handle_generator_refuel(self, update: Update, match, username: str, photo_url: str = None):
        """Обработка заправки генератора"""
        if not photo_url:
            await update.message.reply_text(
                "⚠️ Ошибка: Фото чека обязательно для закупки топлива.\n"
                "Попробуйте снова."
            )
            return

        car_number = match.group('car_number')
        
        if not self.validate_generator_number(car_number):
            await update.message.reply_text(
                f"⚠️ Ошибка: Номер генератора {car_number} не поддерживается.\n"
                f"Доступные номера: {', '.join(self.supported_generators)}"
            )
            return

        try:
            volume = float(match.group('volume'))
            price = float(match.group('price').replace(',', '.'))
            hours = int(match.group('hours'))
            
            if volume <= 0:
                await update.message.reply_text("⚠️ Ошибка: Объем заправки должен быть больше 0")
                return
                
            if price <= 0:
                await update.message.reply_text("⚠️ Ошибка: Цена должна быть больше 0")
                return
                
            if hours < 0:
                await update.message.reply_text("⚠️ Ошибка: Моточасы не могут быть отрицательными")
                return

            total_cost = volume * price
            
            worksheet_name = f"Генератор {car_number}"
            worksheet = self.get_or_create_worksheet(worksheet_name)
            
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Форматування photo_url для Google Sheets (клікабельна мініатюра)
            formula = None
            if photo_url:
                formula = f'=HYPERLINK("{photo_url}"; IMAGE("{photo_url}"))'

            row_data = [
                current_date,
                volume,
                price,
                total_cost,
                hours,
                username,
                photo_url if photo_url else ""
            ]
            
            worksheet.append_row(row_data)
            
            # Якщо є фото, оновлюємо клітинку на формулу
            if formula:
                last_row = len(worksheet.get_all_values())
                photo_col = len(row_data)  # фото завжди останнє поле
                worksheet.update_cell(last_row, photo_col, formula)

            await update.message.reply_text(
                f"✅ Записано: Генератор {car_number} с фото чека\n"
                f"⛽ Объем: {volume} л\n"
                f"💰 Цена: {price} грн/л\n"
                f"💵 Общая стоимость: {total_cost} грн\n"
                f"🕐 Моточасы: {hours}"
            )
            
        except ValueError as e:
            logger.error(f"Ошибка при обработке данных заправки генератора: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка: Неправильный формат данных.\n"
                "Пример правильного ввода:\n"
                "Заправка генератора\n"
                "10 литров, цена 60 грн, моточасы: 255"
            )
        except Exception as e:
            logger.error(f"Ошибка при записи заправки генератора: {e}")
            await update.message.reply_text(
                "⚠️ Ошибка при сохранении данных. Попробуйте еще раз или обратитесь к администратору."
            )

    async def handle_step_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка пошагового ввода данных"""
        user_id = update.message.from_user.id
        text = update.message.text or update.message.caption

        if user_id not in self.user_states:
            return

        state = self.user_states[user_id]

        if text and text.lower() in ["отмена", "cancel", "/cancel"]:
            if user_id in self.user_states:
                del self.user_states[user_id]
                await update.message.reply_text("❌ Операция отменена.")
            return

        try:
            if state["action"] == "purchase":
                if state["step"] == "car_number":
                    if not self.validate_car_number(text):
                        await update.message.reply_text(
                            f"⚠️ Ошибка: Номер автомобиля {text} не поддерживается.\n"
                            f"Доступные номера: {', '.join(self.supported_cars)}"
                        )
                        return
                    state["car_number"] = text
                    state["step"] = "volume"
                    await update.message.reply_text(
                        "⛽ Введите объем и цену в формате:\n"
                        "200 литров по 58 грн\n\n"
                        "📸 Также добавьте фото чека к сообщению"
                    )
                elif state["step"] == "volume":
                    # Проверка формата текста
                    match = re.search(r'(\d+).*?(\d+(?:[.,]\d+)?)\s*', text or "", re.IGNORECASE | re.DOTALL)
                    if not match:
                        await update.message.reply_text(
                            "⚠️ Ошибка: Неправильный формат.\n"
                            "Пример: 200 литров по 58 грн"
                        )
                        return

                    volume = float(match.group(1))
                    price = float(match.group(2).replace(',', '.'))
                    username = update.message.from_user.username or f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()

                    # Обработка фото
                    photo_url = None
                    if update.message.photo:
                        try:
                            # Берем самое большое по размеру фото
                            photo = update.message.photo[-1]
                            photo_file = await context.bot.get_file(photo.file_id)
                            photo_url = photo_file.file_path
                        except Exception as e:
                            logger.error(f"Ошибка при получении фото: {e}")
                    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith('image/'):
                        try:
                            doc_file = await context.bot.get_file(update.message.document.file_id)
                            photo_url = doc_file.file_path
                        except Exception as e:
                            logger.error(f"Ошибка при получении документа-скриншота: {e}")

                    # Создаем объект match для handle_purchase
                    match_obj = type('Match', (), {
                        'group': lambda x: {
                            'car_number': state["car_number"],
                            'volume': str(volume),
                            'price': str(price)
                        }[x]
                    })

                    await self.handle_purchase(update, match_obj, username, photo_url)
                    if user_id in self.user_states:
                        del self.user_states[user_id]

            elif state["action"] == "refuel":
                if state["step"] == "car_number":
                    if not self.validate_car_number(text):
                        await update.message.reply_text(
                            f"⚠️ Ошибка: Номер автомобиля {text} не поддерживается.\n"
                            f"Доступные номера: {', '.join(self.supported_cars)}"
                        )
                        return
                    state["car_number"] = text
                    state["step"] = "volume"
                    await update.message.reply_text(
                        "⛽ Введите объем и пробег в формате:\n"
                        "30 литров. Пробег: 125000 км\n\n"
                        "📸 Также добавьте фото чека к сообщению"
                    )
                elif state["step"] == "volume":
                    match = re.search(r'(\d+).*?(\d+)\s*', text, re.IGNORECASE | re.DOTALL)
                    if not match:
                        await update.message.reply_text(
                            "⚠️ Ошибка: Неправильный формат.\n"
                            "Пример: 30 литров. Пробег: 125000 км"
                        )
                        return

                    volume = float(match.group(1))
                    mileage = int(match.group(2))
                    username = update.message.from_user.username or f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()

                    # Обработка фото
                    photo_url = None
                    if update.message.photo:
                        try:
                            photo = update.message.photo[-1]
                            photo_file = await context.bot.get_file(photo.file_id)
                            photo_url = photo_file.file_path
                        except Exception as e:
                            logger.error(f"Ошибка при получении фото: {e}")

                    # Создаем объект match для handle_refuel
                    match_obj = type('Match', (), {
                        'group': lambda x: {
                            'car_number': state["car_number"],
                            'volume': str(volume),
                            'mileage': str(mileage)
                        }[x]
                    })

                    await self.handle_refuel(update, match_obj, username, photo_url)
                    if user_id in self.user_states:
                        del self.user_states[user_id]

            elif state["action"] == "generator":
                if state["step"] == "car_number":
                    if not self.validate_generator_number(text):
                        await update.message.reply_text(
                            f"⚠️ Ошибка: Номер генератора {text} не поддерживается.\n"
                            f"Доступные номера: {', '.join(self.supported_generators)}"
                        )
                        return
                    state["car_number"] = text
                    state["step"] = "volume"
                    await update.message.reply_text(
                        "⛽ Введите объем, цену и моточасы в формате:\n"
                        "10 литров, цена 60 грн, моточасы: 255\n\n"
                        "📸 Также добавьте фото чека к сообщению"
                    )
                elif state["step"] == "volume":
                    numbers = re.findall(r'\d+(?:[.,]\d+)?', text)
                    if len(numbers) >= 3:
                        try:
                            volume = float(numbers[0])
                            price = float(numbers[1].replace(',', '.'))
                            hours = int(numbers[2])
                            
                            username = update.message.from_user.username or f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()

                            # Обработка фото
                            photo_url = None
                            if update.message.photo:
                                try:
                                    photo = update.message.photo[-1]
                                    photo_file = await context.bot.get_file(photo.file_id)
                                    photo_url = photo_file.file_path
                                except Exception as e:
                                    logger.error(f"Ошибка при получении фото: {e}")

                            # Создаем объект match для handle_generator_refuel
                            match_obj = type('Match', (), {
                                'group': lambda x: {
                                    'car_number': state["car_number"],
                                    'volume': str(volume),
                                    'price': str(price),
                                    'hours': str(hours)
                                }[x]
                            })

                            await self.handle_generator_refuel(update, match_obj, username, photo_url)
                            if user_id in self.user_states:
                                del self.user_states[user_id]
                        except ValueError as e:
                            logger.error(f"Ошибка при конвертации чисел: {e}")
                            await update.message.reply_text(
                                "⚠️ Ошибка: Неправильный формат чисел.\n"
                                "Пример: 10 литров, цена 60 грн, моточасы: 255"
                            )
                            return
                    else:
                        await update.message.reply_text(
                            "⚠️ Ошибка: Неправильный формат.\n"
                            "Пример: 10 литров, цена 60 грн, моточасы: 255"
                        )
                        return

            elif state["action"] == "history":
                if state["step"] == "car_number":
                    if not self.validate_car_number(text):
                        await update.message.reply_text(
                            f"⚠️ Ошибка: Номер автомобиля {text} не поддерживается.\n"
                            f"Доступные номера: {', '.join(self.supported_cars)}"
                        )
                        return
                    
                    # Создаем контекст с аргументами для history
                    context.args = [text]
                    await self.history(update, context)
                    if user_id in self.user_states:
                        del self.user_states[user_id]

            elif state["action"] == "balance":
                if state["step"] == "car_number":
                    if not self.validate_car_number(text):
                        await update.message.reply_text(
                            f"⚠️ Ошибка: Номер автомобиля {text} не поддерживается.\n"
                            f"Доступные номера: {', '.join(self.supported_cars)}"
                        )
                        return
                    
                    # Создаем контекст с аргументами для balance
                    context.args = [text]
                    await self.balance(update, context)
                    if user_id in self.user_states:
                        del self.user_states[user_id]

            elif state["action"] == "generator_info":
                if state["step"] == "car_number":
                    if not self.validate_generator_number(text):
                        await update.message.reply_text(
                            f"⚠️ Ошибка: Номер генератора {text} не поддерживается.\n"
                            f"Доступные номера: {', '.join(self.supported_generators)}"
                        )
                        return
                    
                    # Создаем контекст с аргументами для generator_info
                    context.args = [text]
                    await self.generator_info(update, context)
                    if user_id in self.user_states:
                        del self.user_states[user_id]
        except Exception as e:
            logger.error(f"Ошибка при обработке шага: {e}")
            if user_id in self.user_states:
                del self.user_states[user_id]
            await update.message.reply_text("⚠️ Произошла ошибка при обработке данных. Попробуйте еще раз.")

    async def check_sheets_updates(self):
        """Проверка обновлений в таблице"""
        try:
            current_time = datetime.now()
            if (self.last_sheets_check is None or 
                (current_time - self.last_sheets_check).total_seconds() >= self.sheets_check_interval):
                
                # Получаем текущие листы
                all_sheets = self.spreadsheet.worksheets()
                current_cars = []
                current_generators = []
                
                for sheet in all_sheets:
                    sheet_name = sheet.title
                    number_match = re.search(r'\d+', sheet_name)
                    if number_match:
                        number = number_match.group(0)
                        if "Авто" in sheet_name:
                            current_cars.append(number)
                            # Проверяем и добавляем заголовки если нужно
                            if len(sheet.get_all_values()) == 0:
                                self.setup_worksheet_headers(sheet)
                                logger.info(f"Добавлены заголовки для листа {sheet_name}")
                        elif "Генератор" in sheet_name:
                            current_generators.append(number)
                            # Проверяем и добавляем заголовки если нужно
                            if len(sheet.get_all_values()) == 0:
                                self.setup_worksheet_headers(sheet)
                                logger.info(f"Добавлены заголовки для листа {sheet_name}")
                
                # Проверяем изменения
                if set(current_cars) != set(self.supported_cars):
                    logger.info(f"Обнаружены изменения в списке автомобилей: {current_cars}")
                    self.supported_cars = current_cars
                
                if set(current_generators) != set(self.supported_generators):
                    logger.info(f"Обнаружены изменения в списке генераторов: {current_generators}")
                    self.supported_generators = current_generators
                
                self.last_sheets_check = current_time
                
        except Exception as e:
            logger.error(f"Ошибка при проверке обновлений таблицы: {e}")

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

        # Обработчик документов-изображений (скриншотов)
        application.add_handler(MessageHandler(
            filters.Document.IMAGE,
            self.handle_button_press
        ))

        # Добавляем периодическую проверку обновлений
        application.job_queue.run_repeating(
            lambda context: asyncio.create_task(self.check_sheets_updates()),
            interval=self.sheets_check_interval,
            first=10
        )

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

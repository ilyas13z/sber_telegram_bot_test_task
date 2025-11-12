import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import requests
from dotenv import load_dotenv
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from telegram.constants import ParseMode

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_AUTH_TOKEN = os.environ.get("CLOUDFLARE_AUTH_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "300"))  # 5 минут по умолчанию
STORAGE_FILE = "bot_storage.json"

# Глобальное хранилище
class BotStorage:
    def __init__(self):
        self.data = self.load()
        self.start_time = datetime.now()
        
    def load(self) -> Dict:
        """Загрузка данных из файла"""
        try:
            if os.path.exists(STORAGE_FILE):
                with open(STORAGE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки хранилища: {e}")
        return {"chats": {}}
    
    def save(self):
        """Сохранение данных в файл"""
        try:
            with open(STORAGE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения хранилища: {e}")
    
    def get_chat(self, chat_id: str) -> Dict:
        """Получить данные чата"""
        if chat_id not in self.data["chats"]:
            self.data["chats"][chat_id] = {
                "polls": [],
                "active_poll": None
            }
            self.save()
        return self.data["chats"][chat_id]
    
    def clear_chat(self, chat_id: str):
        """Очистить историю чата"""
        self.data["chats"][chat_id] = {
            "polls": [],
            "active_poll": None
        }
        self.save()
    
    def add_poll(self, chat_id: str, poll_data: Dict):
        """Добавить опрос в историю"""
        chat = self.get_chat(chat_id)
        chat["polls"].append(poll_data)
        self.save()
    
    def set_active_poll(self, chat_id: str, poll_data: Optional[Dict]):
        """Установить активный опрос"""
        chat = self.get_chat(chat_id)
        chat["active_poll"] = poll_data
        self.save()
    
    def get_code_history(self, chat_id: str) -> List[str]:
        """Получить историю выбранных строк кода"""
        chat = self.get_chat(chat_id)
        return [poll["winner"] for poll in chat["polls"] if poll.get("winner")]

storage = BotStorage()

# LLM функции
def call_llm(messages: List[Dict[str, str]], max_tokens: int = 500) -> str:
    """Вызов Cloudflare AI API"""
    try:
        response = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            headers={"Authorization": f"Bearer {CLOUDFLARE_AUTH_TOKEN}"},
            json={"messages": messages},
            timeout=30
        )
        result = response.json()
        
        if not result.get("success"):
            logger.error(f"LLM API error: {result}")
            return ""
        
        return result.get("result", {}).get("response", "")
    except Exception as e:
        logger.error(f"Ошибка вызова LLM: {e}")
        return ""

def generate_code_options(code_history: List[str]) -> List[str]:
    """Генерация 4 вариантов следующей строки кода"""
    if not code_history:
        prompt = """Generate 4 different first lines of Python code to start a simple program.
Each line must be:
- Syntactically correct Python
- Maximum 95 characters
- Different from each other
- A logical start to a program

Format: Return ONLY 4 lines separated by newlines, nothing else."""
    else:
        code_text = "\n".join(code_history)
        prompt = f"""Given this Python code:
```python
{code_text}
```

Generate 4 different next lines of code. Each line must be:
- Syntactically correct Python
- Maximum 95 characters
- Different from each other
- Logical continuation of the code above
- Properly indented

Format: Return ONLY 4 lines separated by newlines, nothing else."""
    
    messages = [
        {"role": "system", "content": "You are a Python code generator. Return only code lines, no explanations."},
        {"role": "user", "content": prompt}
    ]
    
    response = call_llm(messages)
    if not response:
        # Запасные варианты
        if not code_history:
            return [
                "# Simple Python program",
                "import sys",
                "def main():",
                "if __name__ == '__main__':"
            ]
        else:
            return [
                "    pass",
                "    # TODO: implement",
                "    return None",
                "    print('Done')"
            ]
    
    lines = [line.strip() for line in response.split('\n') if line.strip()]
    # Фильтруем markdown блоки и комментарии
    lines = [line for line in lines if not line.startswith('```') and len(line) <= 95]
    
    # Убираем дубликаты и берём первые 4
    unique_lines = []
    for line in lines:
        if line not in unique_lines:
            unique_lines.append(line)
        if len(unique_lines) == 4:
            break
    
    # Если недостаточно уникальных строк, дополняем
    while len(unique_lines) < 4:
        unique_lines.append(f"    # Option {len(unique_lines) + 1}")
    
    return unique_lines[:4]

def complete_code(code_history: List[str]) -> str:
    """Доделать код до компилируемого состояния"""
    if not code_history:
        return "# Empty code"
    
    code_text = "\n".join(code_history)
    prompt = f"""Given this incomplete Python code:
```python
{code_text}
```

Complete this code to make it syntactically correct and runnable.
Rules:
- Do NOT add new logic or features
- Only add necessary closing brackets, indentation fixes, and minimal completion
- Add 'pass' statements where needed
- Ensure all blocks are properly closed
- Keep it minimal

Return ONLY the complete Python code, nothing else."""
    
    messages = [
        {"role": "system", "content": "You are a Python code completion assistant. Return only code, no explanations."},
        {"role": "user", "content": prompt}
    ]
    
    response = call_llm(messages, max_tokens=1000)
    if not response:
        return code_text + "\n    pass"
    
    # Убираем markdown блоки
    lines = response.split('\n')
    code_lines = []
    in_code_block = False
    
    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            continue
        if not in_code_block or not line.strip().startswith('```'):
            code_lines.append(line)
    
    return '\n'.join(code_lines).strip()

# Проверка прав администратора
def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    return user_id in ADMIN_IDS

async def check_admin(update: Update) -> bool:
    """Проверка прав администратора с сообщением"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Эта команда доступна только администраторам.")
        return False
    return True

# Команды бота
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - очистка истории и отправка первого опроса (только для админов) или приветствие для обычных пользователей"""
    
    try:
        # Если админ - очищаем историю и создаем новый опрос
        if is_admin(update.effective_user.id):
            # Проверяем аргументы команды
            if context.args:
                try:
                    target_chat_id = int(context.args[0])
                except ValueError:
                    await update.message.reply_text("❌ Неверный формат chat_id. Используйте: /start [chat_id]")
                    return
            else:
                # Если chat_id не указан, используем текущий чат
                target_chat_id = update.effective_chat.id
            
            chat_id_str = str(target_chat_id)
            
            # Получаем текущую историю кода для завершения
            code_history = storage.get_code_history(chat_id_str)
            
            # Если есть код, завершаем его и отправляем
            if code_history:
                await update.message.reply_text(f"⏳ Завершаю текущий код чата {target_chat_id}...")
                completed_code = complete_code(code_history)
                
                # Отправляем завершённый код
                message = f"✅ **Завершённый код чата {target_chat_id}:**\n\n```python\n{completed_code}\n```"
                await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
                
                # Отправляем файл
                filename = f"generated_code_{target_chat_id}.py"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(completed_code)
                
                with open(filename, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=filename,
                        caption=f"📎 Завершённый код чата {target_chat_id}"
                    )
                
                # Удаляем временный файл
                try:
                    os.remove(filename)
                except:
                    pass
            
            # Очищаем историю
            storage.clear_chat(chat_id_str)
            logger.info(f"История чата {target_chat_id} очищена")
            
            await update.message.reply_text(f"✅ История чата {target_chat_id} очищена. Генерирую первый опрос...")
            
            # Отправляем первый опрос
            await send_poll(target_chat_id, context)
            
        else:
            # Для обычных пользователей - просто приветствие
            chat_id = str(update.effective_chat.id)
            chat_data = storage.get_chat(chat_id)
            active_poll = chat_data.get("active_poll")
            polls_count = len(chat_data["polls"])
            
            welcome_text = f"""👋 Добро пожаловать в бот коллективной генерации кода\\!

📊 Статус чата:
• Ваш id чата \\(попросите админа начать вам сессию\\): `{chat_id}`
• Завершено опросов: {polls_count}
• Активный опрос: {"Да" if active_poll else "Нет"}

💡 Вы можете:
• Участвовать в голосованиях
• Просматривать текущий код: /code

⚠️ Управление \\(только для админов\\): /start, /stop, /sendnow, /code\\_completed"""
            
            await update.message.reply_text(welcome_text, parse_mode="MarkdownV2")
        
    except Exception as e:
        logger.error(f"Ошибка в /start: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /code - показать текущий код"""
    chat_id = str(update.effective_chat.id)
    
    try:
        code_history = storage.get_code_history(chat_id)
        
        if not code_history:
            await update.message.reply_text("📝 Код пока пуст. Используйте /start для начала генерации.")
            return
        
        code_text = "\n".join(code_history)
        message = f"📝 **Текущий код:**\n\n```python\n{code_text}\n```"
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Ошибка в /code: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def code_completed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /code_completed - доделать код до компилируемого"""
    if not await check_admin(update):
        return
    
    chat_id = str(update.effective_chat.id)
    
    try:
        code_history = storage.get_code_history(chat_id)
        
        if not code_history:
            await update.message.reply_text("📝 Код пуст. Нечего доделывать.")
            return
        
        await update.message.reply_text("⏳ Доделываю код...")
        
        completed_code = complete_code(code_history)
        
        # Отправляем текст
        message = f"✅ **Завершённый код:**\n\n```python\n{completed_code}\n```"
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        
        # Отправляем файл
        with open("generated_code.py", "w", encoding="utf-8") as f:
            f.write(completed_code)
        
        with open("generated_code.py", "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="generated_code.py",
                caption="📎 Завершённый код"
            )
        
    except Exception as e:
        logger.error(f"Ошибка в /code_completed: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def sendnow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /sendnow - отправить следующий опрос немедленно"""
    if not await check_admin(update):
        return
    
    chat_id = update.effective_chat.id
    
    try:
        # Проверяем, есть ли активный опрос
        chat_data = storage.get_chat(str(chat_id))
        if chat_data.get("active_poll"):
            await update.message.reply_text("⚠️ Уже есть активный опрос. Дождитесь его завершения.")
            return
        
        await update.message.reply_text("📤 Отправляю новый опрос...")
        await send_poll(chat_id, context)
        
    except Exception as e:
        logger.error(f"Ошибка в /sendnow: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /health - показать статус бота"""
    if not await check_admin(update):
        return
    
    try:
        uptime = datetime.now() - storage.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        
        chat_id = str(update.effective_chat.id)
        chat_data = storage.get_chat(chat_id)
        active_poll = chat_data.get("active_poll")
        
        status = f"""🏥 **Статус бота**

⏱️ Uptime: {hours}ч {minutes}м {seconds}с
📊 Активный опрос: {"Да" if active_poll else "Нет"}
📝 Опросов завершено: {len(chat_data["polls"])}
🗄️ Чатов в хранилище: {len(storage.data["chats"])}
"""
        
        if active_poll:
            time_left = active_poll.get("close_time", 0) - datetime.now().timestamp()
            if time_left > 0:
                status += f"⏰ До закрытия опроса: {int(time_left)}с\n"
        
        await update.message.reply_text(status, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Ошибка в /health: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /logs - показать последние 100 строк лога"""
    if not await check_admin(update):
        return
    
    try:
        if not os.path.exists('bot.log'):
            await update.message.reply_text("📋 Лог файл не найден.")
            return
        
        with open('bot.log', 'r', encoding='utf-8') as f:
            lines = f.readlines()
            last_lines = lines[-100:]
            log_text = ''.join(last_lines)
        
        if len(log_text) > 4000:
            log_text = log_text[-4000:]
        
        await update.message.reply_text(f"```\n{log_text}\n```", parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Ошибка в /logs: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def alllogs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /alllogs - отправить весь лог файлом"""
    if not await check_admin(update):
        return
    
    try:
        if not os.path.exists('bot.log'):
            await update.message.reply_text("📋 Лог файл не найден.")
            return
        
        with open('bot.log', 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename="bot.log",
                caption="📋 Полный лог бота"
            )
        
    except Exception as e:
        logger.error(f"Ошибка в /alllogs: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# Работа с опросами
async def send_poll(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправка нового опроса"""
    chat_id_str = str(chat_id)
    
    try:
        # Генерируем варианты кода
        code_history = storage.get_code_history(chat_id_str)
        options = generate_code_options(code_history)
        
        logger.info(f"Сгенерированы опции для чата {chat_id}: {options}")
        
        # Отправляем опрос
        poll_message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"🗳️ Выберите следующую строку кода (строка #{len(code_history) + 1}):",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False
        )
        
        # Сохраняем активный опрос
        close_time = datetime.now().timestamp() + POLL_TIMEOUT
        poll_data = {
            "poll_id": poll_message.poll.id,
            "message_id": poll_message.message_id,
            "options": options,
            "votes": {i: 0 for i in range(4)},
            "close_time": close_time,
            "created_at": datetime.now().isoformat()
        }
        storage.set_active_poll(chat_id_str, poll_data)
        
        logger.info(f"Опрос {poll_message.poll.id} отправлен в чат {chat_id}")
        
        # Планируем закрытие опроса
        context.job_queue.run_once(
            close_poll_callback,
            POLL_TIMEOUT,
            data={"chat_id": chat_id, "poll_id": poll_message.poll.id},
            name=f"close_poll_{chat_id}_{poll_message.poll.id}"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отправки опроса: {e}")

async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ответов на опрос"""
    try:
        poll_answer = update.poll_answer
        poll_id = poll_answer.poll_id
        
        # Находим чат с этим опросом
        for chat_id_str, chat_data in storage.data["chats"].items():
            active_poll = chat_data.get("active_poll")
            if active_poll and active_poll["poll_id"] == poll_id:
                # Обновляем голоса
                for option_id in poll_answer.option_ids:
                    if option_id in active_poll["votes"]:
                        active_poll["votes"][option_id] += 1
                
                storage.save()
                logger.info(f"Обновлены голоса для опроса {poll_id}: {active_poll['votes']}")
                break
        
    except Exception as e:
        logger.error(f"Ошибка обработки ответа на опрос: {e}")

async def close_poll_callback(context: ContextTypes.DEFAULT_TYPE):
    """Callback для закрытия опроса по таймеру"""
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    poll_id = job_data["poll_id"]
    
    await close_poll(chat_id, poll_id, context)

async def close_poll(chat_id: int, poll_id: str, context: ContextTypes.DEFAULT_TYPE, flag_stop: bool = False):
    """Закрытие опроса и выбор победителя"""
    chat_id_str = str(chat_id)
    
    try:
        chat_data = storage.get_chat(chat_id_str)
        active_poll = chat_data.get("active_poll")
        
        if not active_poll or active_poll["poll_id"] != poll_id:
            logger.warning(f"Опрос {poll_id} не найден или уже закрыт")
            return
        
        # Останавливаем опрос
        try:
            await context.bot.stop_poll(chat_id, active_poll["message_id"])
        except Exception as e:
            logger.error(f"Ошибка остановки опроса: {e}")
        
        # Определяем победителя
        votes = active_poll["votes"]
        winner_index = max(votes, key=votes.get)
        winner_line = active_poll["options"][winner_index]
        
        # Сохраняем результат
        poll_result = {
            "poll_id": poll_id,
            "options": active_poll["options"],
            "votes": votes,
            "winner": winner_line,
            "winner_index": winner_index,
            "closed_at": datetime.now().isoformat()
        }
        storage.add_poll(chat_id_str, poll_result)
        storage.set_active_poll(chat_id_str, None)
        
        # Отправляем результат
        result_text = f"""✅ **Опрос завершён!**

Победившая строка:
```python
{winner_line}
```

Голоса: {votes[winner_index]}
"""
        await context.bot.send_message(chat_id, result_text, parse_mode=ParseMode.MARKDOWN)
        
        logger.info(f"Опрос {poll_id} закрыт, победитель: {winner_line}")
        
        if flag_stop:
            return
        # Отправляем следующий опрос через 5 секунд (только если не было вызвано из /stop)
        await asyncio.sleep(5)
        
        # Проверяем, что нет активного опроса (на случай если /stop был вызван)
        chat_data = storage.get_chat(chat_id_str)
        if not chat_data.get("active_poll"):
            await send_poll(chat_id, context)
        
    except Exception as e:
        logger.error(f"Ошибка закрытия опроса: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ошибок"""
    logger.error(f"Update {update} caused error {context.error}")

async def post_init(application: Application):
    """Функция инициализации после запуска"""
    logger.info("Бот успешно запущен и готов к работе!")

def main():
    """Основная функция запуска бота"""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не установлен!")
        return
    
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_AUTH_TOKEN:
        logger.error("Cloudflare credentials не установлены!")
        return
    
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не установлен, команды администратора будут недоступны!")
    
    # ИСПРАВЛЕНИЕ: Создаём event loop для Python 3.10+
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    # Создаём приложение
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start_command))
    # application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("code", code_command))
    application.add_handler(CommandHandler("code_completed", code_completed_command))
    application.add_handler(CommandHandler("sendnow", sendnow_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("alllogs", alllogs_command))
    
    # Регистрируем обработчики опросов
    application.add_handler(PollAnswerHandler(poll_answer_handler))
    
    # Обработчик ошибок
    application.add_error_handler(error_handler)
    
    logger.info("Запуск бота...")
    
    # Запускаем бота
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
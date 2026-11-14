import os
import telebot
from telebot.types import BotCommand
from utils.llm import call_llm
from dotenv import load_dotenv

def main():
    load_dotenv()

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    # threaded=False: отключение многопоточности, 
    # обрабатываем сообщения последовательно.
    bot = telebot.TeleBot(TOKEN, threaded=False)

    bot.set_my_commands([
        BotCommand("new", "Начать новый диалог"),
    ])

    # Словарь для хранения истории разных чатов.
    # Ключ — ID чата, значение — список сообщений.
    chat_histories = {}

    # Общая инструкция для модели.
    SYSTEM_PROMPT = """
Ты — дружелюбный AI-переводчик русского и казахского языков.
Тебя зовут Салават. Тебя создал Ринат.

Твои задачи:
1. Первеодил с русского на казахский и обратно
2. Предлагай синонимы и антонимы слов
3. Будь любетезен

Если пользователь просит объяснить перевод, объясняй как лучший переводчик. 
"""

    # Создаём новую историю диалога.
    def create_history():
        return [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    @bot.message_handler(commands=["new"])
    def new_chat(message):
        chat_id = message.chat.id

        # Сбрасываем историю только текущего чата.
        chat_histories[chat_id] = create_history()

        bot.send_message(chat_id, "Начат новый диалог!")

    @bot.message_handler(content_types=["photo"])
    def handle_photo(message):
        bot.send_message(message.chat.id, "Это картинка 🖼️")

    @bot.message_handler(content_types=["text"])
    def handle_text(message):
        chat_id = message.chat.id
        user_text = message.text

        # Если пользователь пишет впервые,
        # создаём для него новую историю.
        if chat_id not in chat_histories:
            chat_histories[chat_id] = create_history()

        # Получаем историю текущего чата.
        messages = chat_histories[chat_id]

        # Добавляем новое сообщение пользователя.
        messages.append({
            "role": "user",
            "content": user_text
        })

        try:
            # Отправляем историю модели.
            reply = call_llm(messages)
        except Exception as error:
            print("Ошибка LLM:", error)

            # Удаляем сообщение, которое не удалось обработать.
            messages.pop()

            bot.send_message(
                chat_id,
                "Не удалось получить ответ. Попробуйте ещё раз."
            )
            return

        # Сохраняем ответ модели.
        messages.append({
            "role": "assistant",
            "content": reply
        })

        # Показываем ответ в Telegram.
        bot.send_message(chat_id, reply)


    print("AI-бот запущен!")
    bot.infinity_polling()

if __name__ == "__main__":
    main()

import os
import base64
import telebot

from telebot.types import BotCommand
from utils.llm import call_llm
from dotenv import load_dotenv


SYSTEM_PROMPT = "Ты — дружелюбный AI-переводчик русского и казахского языков."


def main():
    load_dotenv()

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    bot = telebot.TeleBot(TOKEN, threaded=False)

    bot.set_my_commands([
        BotCommand("new", "Начать новый диалог"),
    ])

    # Истории отдельных чатов.
    chat_histories = {}

    def create_history():
        return [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def get_history(chat_id):
        if chat_id not in chat_histories:
            chat_histories[chat_id] = create_history()

        return chat_histories[chat_id]

    def process_message(chat_id, content):
        bot.send_chat_action(chat_id, 'typing')
        messages = get_history(chat_id)

        messages.append({
            "role": "user",
            "content": content,
        })

        try:
            reply = call_llm(messages)
        except Exception as error:
            print("Ошибка LLM:", error)
            messages.pop()

            bot.send_message(
                chat_id,
                "Не удалось получить ответ. Попробуйте ещё раз."
            )
            return

        messages.append({
            "role": "assistant",
            "content": reply,
        })

        bot.send_message(chat_id, reply)

    @bot.message_handler(commands=["new"])
    def new_chat(message):
        chat_histories[message.chat.id] = create_history()
        bot.send_message(message.chat.id, "Начат новый диалог!")

    @bot.message_handler(content_types=["photo"])
    def handle_photo(message):
        chat_id = message.chat.id

        try:
            # Берём фотографию максимального разрешения.
            photo = message.photo[-1]

            # Скачиваем фотографию из Telegram.
            file_info = bot.get_file(photo.file_id)
            image_bytes = bot.download_file(file_info.file_path)

            # Кодируем изображение в Base64.
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")

            # Формируем Data URL.
            image_url = f"data:image/jpeg;base64,{image_base64}"

            # Мультимодальное сообщение.
            content = [
                {
                    "type": "text",
                    "text": 'Переведи текст на изображении',
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_url,
                        "detail": "auto",
                    },
                },
            ]

            process_message(chat_id, content)

        except Exception as error:
            print("Ошибка обработки фотографии:", error)
            bot.send_message(
                chat_id,
                "Не удалось обработать фотографию. Попробуйте ещё раз."
            )

    @bot.message_handler(content_types=["text"])
    def handle_text(message):
        process_message(message.chat.id, message.text)

    print("AI-бот запущен!")
    bot.infinity_polling()


if __name__ == "__main__":
    main()

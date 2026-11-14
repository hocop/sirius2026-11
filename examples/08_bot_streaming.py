"""Версия 1: несколько диалогов и сохранение в JSON."""
import json
import time
import os
from pathlib import Path
import telebot
from telebot.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from utils.llm import call_llm, stream_llm

SYSTEM_PROMPT = ("Ты переводчик русского и казахского языков. Определи язык текста "
                 "и переведи на другой язык. Если пользователь задаёт вопрос о "
                 "предыдущем переводе, ответь на него кратко и понятно.")
DATA_FILE = Path(__file__).with_name('chat_histories.json')


def new_dialog():
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def load_data():
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open(encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        print('Не удалось прочитать файл истории. Начинаем с пустой истории.')
        return {}


def save_data(data):
    # Сначала записываем во временный файл, затем заменяем основной.
    temp = DATA_FILE.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    temp.replace(DATA_FILE)


def main():
    load_dotenv()
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise RuntimeError('Не задан TELEGRAM_BOT_TOKEN')
    bot = telebot.TeleBot(token, threaded=False)
    bot.set_my_commands([BotCommand('new', 'Новый диалог'),
                         BotCommand('history', 'История диалогов')])
    data = load_data()

    def get_user(chat_id):
        key = str(chat_id)  # В JSON ключи словаря — строки.
        if key not in data:
            data[key] = {'active': 1, 'next_id': 2,
                         'dialogs': [{'id': 1, 'messages': new_dialog()}]}
            save_data(data)
        return data[key]

    def active_dialog(user):
        return next(d for d in user['dialogs'] if d['id'] == user['active'])

    @bot.message_handler(commands=['new'])
    def new_chat(message):
        user = get_user(message.chat.id)
        dialog_id = user['next_id']
        user['next_id'] += 1
        user['active'] = dialog_id
        user['dialogs'].append({'id': dialog_id, 'messages': new_dialog()})
        save_data(data)
        bot.send_message(message.chat.id, f'Начат диалог №{dialog_id}!')

    @bot.message_handler(commands=['history'])
    def history(message):
        user = get_user(message.chat.id)
        keyboard = InlineKeyboardMarkup()
        for dialog in reversed(user['dialogs'][-5:]):
            first = next((m['content'] for m in dialog['messages']
                          if m['role'] == 'user'), 'Пустой диалог')
            title = first[:35] + ('…' if len(first) > 35 else '')
            mark = '✓ ' if dialog['id'] == user['active'] else ''
            keyboard.add(InlineKeyboardButton(
                f'{mark}№{dialog["id"]}: {title}', callback_data=f'open:{dialog["id"]}'))
        bot.send_message(message.chat.id, 'Последние диалоги:', reply_markup=keyboard)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('open:'))
    def open_dialog(call):
        user = get_user(call.message.chat.id)
        dialog_id = int(call.data.split(':', 1)[1])
        if not any(d['id'] == dialog_id for d in user['dialogs']):
            bot.answer_callback_query(call.id, 'Диалог не найден')
            return
        user['active'] = dialog_id
        save_data(data)
        bot.answer_callback_query(call.id, 'Диалог выбран')
        bot.send_message(call.message.chat.id, f'Открыт диалог №{dialog_id}. Можете продолжать!')

    @bot.message_handler(content_types=['photo'])
    def photo(message):
        bot.send_message(message.chat.id, 'Это картинка 🖼️')

    @bot.message_handler(content_types=['text'])
    def text(message):
        user = get_user(message.chat.id)
        messages = active_dialog(user)['messages']
        messages.append({'role': 'user', 'content': message.text})
        # Отправляем одно сообщение-заглушку и постепенно редактируем его.
        # Это работает в обычных чатах и не требует специального Bot API.
        placeholder = None
        reply = ''
        last_update = 0.0
        last_text = ''
        try:
            placeholder = bot.send_message(message.chat.id, 'Перевожу…')
            for chunk in stream_llm(messages):
                reply += chunk
                now = time.monotonic()
                # Не редактируем сообщение на каждом токене.
                if now - last_update >= 1.2 and reply != last_text:
                    preview = reply[:4000]
                    bot.edit_message_text(preview, message.chat.id, placeholder.message_id)
                    last_update = now
                    last_text = preview
            if not reply:
                raise ValueError('Модель вернула пустой ответ')
        except Exception as error:
            print('Ошибка стриминга:', error)
            messages.pop()
            if placeholder:
                try:
                    bot.edit_message_text('Ошибка генерации. Попробуйте ещё раз.',
                                          message.chat.id, placeholder.message_id)
                except Exception:
                    pass
            else:
                bot.send_message(message.chat.id, 'Ошибка генерации. Попробуйте ещё раз.')
            return
        messages.append({'role': 'assistant', 'content': reply})
        save_data(data)
        # Первые 4000 символов оставляем в уже созданном сообщении.
        try:
            if reply[:4000] != last_text:
                bot.edit_message_text(reply[:4000], message.chat.id, placeholder.message_id)
        except Exception as error:
            print('Не удалось обновить финальное сообщение:', error)
        for i in range(4000, len(reply), 4000):
            bot.send_message(message.chat.id, reply[i:i + 4000])

    print('Бот с историей и стримингом запущен!')
    bot.infinity_polling()


if __name__ == '__main__':
    main()

"""Telegram translator: JSON history, LLM streaming, photos, interactive HTML flashcards.
Run from project root with utils/llm.py and .env present.
"""
import base64
import json
import os
import re
import time
import uuid
from pathlib import Path

import requests
import telebot
from dotenv import load_dotenv
from telebot.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from utils.llm import call_llm, stream_llm

load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('Не задан TELEGRAM_BOT_TOKEN')

# Experiment with these settings. 0.15 = update at most every 150 ms.
STREAM_UPDATE_INTERVAL = float(os.getenv('STREAM_UPDATE_INTERVAL', '0.15'))
STREAM_MAX_CHARS = int(os.getenv('STREAM_MAX_CHARS', '4000'))
STREAM_MODE = os.getenv('STREAM_MODE', 'draft')  # draft | edit
DATA_FILE = Path(os.getenv('BOT_DATA_FILE', 'translator_data.json'))

SYSTEM_PROMPT = '''Ты — доброжелательный преподаватель и переводчик русского и казахского языков.
Если текст русский — переведи на казахский; если казахский — на русский.
При вопросе о предыдущем переводе объясни слова и грамматику с опорой на историю.
При фотографии переводи видимый текст; если текст неразборчив, честно сообщи об этом.
Для обычного перевода сначала дай перевод, затем при необходимости короткое пояснение.
Не выдумывай содержание изображений и не путай казахский с татарским.
Все ответы отображаются в Telegram, поэтому запрещено:
Использовать Markdown, HTML-теги для форматирования.
Использовать тройные обратные кавычки для блоков кода.
Отвечай только содержимым сообщения, без Markdown-обёртки и без пояснений о форматировании.'''

bot = telebot.TeleBot(TOKEN, threaded=False)
api_url = f'https://api.telegram.org/bot{TOKEN}/'


def telegram_api(method, **payload):
    response = requests.post(api_url + method, json=payload, timeout=12)
    response.raise_for_status()

    if not response.ok:
        print("Telegram error:", response.status_code)
        print("Telegram response:", response.text)
        raise RuntimeError(
            f"Ошибка Telegram: {response.status_code}: {response.text}"
        )
    
    data = response.json()
    if not data.get('ok'):
        raise RuntimeError(f'{method}: {data.get("description", data)}')
    return data['result']


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding='utf-8'))
    return {}


data = load_data()


def save():
    tmp = DATA_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(DATA_FILE)


def state(chat_id):
    key = str(chat_id)
    if key not in data:
        data[key] = {'active': None, 'dialogs': [], 'words': {}}
    return data[key]


def current_dialog(chat_id):
    s = state(chat_id)
    for dialog in s['dialogs']:
        if dialog['id'] == s['active']:
            return dialog
    dialog = {'id': uuid.uuid4().hex[:10], 'title': 'Новый диалог',
              'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}]}
    s['dialogs'].append(dialog)
    s['active'] = dialog['id']
    save()
    return dialog


def compact_history(messages):
    """Don't persist base64 images; retain a textual placeholder for future prompts."""
    result = []
    for m in messages:
        content = m['content']
        if isinstance(content, list):
            content = '[Пользователь прислал фотографию для перевода]'
        result.append({'role': m['role'], 'content': content})
    return result


def send_text(chat_id, text):
    text = text or '(пустой ответ)'
    # Split long final messages at safe lengths.
    for pos in range(0, len(text), 4000):
        bot.send_message(chat_id, text[pos:pos+4000])


def stream_reply(chat_id, messages):
    text = ''
    last = 0.0
    last_sent = ''
    draft_id = int(time.time() * 1000) % 2147483647
    # Send immediately, before waiting for the first LLM token.
    placeholder = bot.send_message(chat_id, '💭 Думаю… Ойланып жатырмын…')
    mode = STREAM_MODE
    try:
        for chunk in stream_llm(messages):
            text += chunk
            now = time.monotonic()
            if now - last < STREAM_UPDATE_INTERVAL:
                continue
            # Telegram accepts only a bounded message length.
            partial = text[:max(1, min(STREAM_MAX_CHARS, 4000))]
            if partial == last_sent:
                continue
            try:
                if mode == 'draft':
                    telegram_api('sendMessageDraft', chat_id=chat_id,
                                 draft_id=draft_id, text=partial)
                else:
                    bot.edit_message_text(partial, chat_id, placeholder.message_id)
                last_sent = partial
                last = now
            except Exception as exc:
                print('Промежуточное обновление не удалось:', exc)
                # Keep consuming LLM chunks even if Telegram rejects an update.
        if not text.strip():
            raise RuntimeError('LLM вернула пустой ответ')
        if placeholder:
            try:
                bot.delete_message(chat_id, placeholder.message_id)
            except Exception as exc:
                print('Не удалось удалить черновик:', exc)
        send_text(chat_id, text)
        return text
    except Exception:
        if placeholder:
            try:
                bot.delete_message(chat_id, placeholder.message_id)
            except Exception:
                pass
        raise


def process_message(chat_id, content):
    dialog = current_dialog(chat_id)
    history = compact_history(dialog['messages'])
    history.append({'role': 'user', 'content': content})
    try:
        reply = stream_reply(chat_id, history)
    except Exception as exc:
        print('Ошибка LLM:', repr(exc))
        bot.send_message(chat_id, 'Не удалось получить ответ. Попробуйте ещё раз.')
        return
    # Persist text only, avoiding megabytes of base64 in JSON.
    if isinstance(content, list):
        content = '[Фотография: переведи текст на изображении]'
    dialog['messages'].append({'role': 'user', 'content': content})
    dialog['messages'].append({'role': 'assistant', 'content': reply})
    if dialog['title'] == 'Новый диалог':
        dialog['title'] = content[:42] if isinstance(content, str) else 'Перевод фотографии'
    save()


def history_keyboard(s):
    kb = InlineKeyboardMarkup()
    for dialog in s['dialogs'][-5:][::-1]:
        title = dialog['title'][:35]
        marker = '✓ ' if dialog['id'] == s['active'] else ''
        kb.add(InlineKeyboardButton(marker + title, callback_data='open:' + dialog['id']))
    return kb


@bot.message_handler(commands=['start', 'help'])
def help_command(message):
    bot.send_message(message.chat.id, '🇰🇿 Переводчик RU ↔ KZ\n/new — новый диалог\n/history — открыть историю\n/practice — повторять слова\nПришли текст или фотографию.')


@bot.message_handler(commands=['new'])
def new_command(message):
    s = state(message.chat.id)
    s['active'] = None
    current_dialog(message.chat.id)
    bot.send_message(message.chat.id, 'Начат новый диалог. Старые сохранены!')


@bot.message_handler(commands=['history'])
def history_command(message):
    s = state(message.chat.id)
    current_dialog(message.chat.id)
    bot.send_message(message.chat.id, 'Последние диалоги:', reply_markup=history_keyboard(s))


@bot.callback_query_handler(func=lambda c: c.data.startswith('open:'))
def open_dialog(c):
    s = state(c.message.chat.id)
    dialog_id = c.data.split(':', 1)[1]
    if any(d['id'] == dialog_id for d in s['dialogs']):
        s['active'] = dialog_id
        save()
        bot.answer_callback_query(c.id, 'Диалог открыт')
        bot.send_message(c.message.chat.id, 'Диалог восстановлен. Можешь продолжать!')
    else:
        bot.answer_callback_query(c.id, 'Диалог не найден')


@bot.message_handler(content_types=['photo'])
def photo_handler(message):
    try:
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        raw = bot.download_file(file_info.file_path)
        image_url = 'data:image/jpeg;base64,' + base64.b64encode(raw).decode('ascii')
        content = [
            {'type': 'text', 'text': message.caption or 'Переведи текст на изображении'},
            {'type': 'image_url', 'image_url': {'url': image_url, 'detail': 'auto'}}
        ]
        process_message(message.chat.id, content)
    except Exception as exc:
        print('Ошибка фото:', repr(exc))
        bot.send_message(message.chat.id, 'Не удалось обработать фотографию.')


def parse_cards_response(raw):
    """Accept a JSON array or common object wrappers; reject malformed cards."""
    text = raw.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Some models add a short explanation around the JSON document.
        decoder = json.JSONDecoder()
        parsed = None
        for index, char in enumerate(text):
            if char not in '[{':
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
                parsed = candidate
                break
            except json.JSONDecodeError:
                continue
        if parsed is None:
            raise ValueError('Модель не вернула корректный JSON')

    if isinstance(parsed, dict):
        for key in ('cards', 'flashcards', 'words', 'items', 'карточки'):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            # A single card is also useful if the model returned one object.
            if 'word' in parsed and 'translation' in parsed:
                parsed = [parsed]

    if not isinstance(parsed, list):
        raise ValueError('Модель вернула JSON, но не список карточек')

    cards = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        word = item.get('word')
        translation = item.get('translation')
        if not isinstance(word, str) or not word.strip():
            continue
        if not isinstance(translation, str) or not translation.strip():
            continue
        card = {}
        for key in ('word', 'translation', 'hint', 'example', 'example_translation'):
            value = item.get(key, '')
            card[key] = value[:180].strip() if isinstance(value, str) else ''
        cards.append(card)
    return cards


def generate_cards(chat_id, count):
    s = state(chat_id)
    excerpts = []
    for dialog in s['dialogs'][-5:]:
        for m in dialog['messages'][-16:]:
            if m['role'] in ('user', 'assistant') and isinstance(m['content'], str):
                excerpts.append(m['content'][:350])
    if not excerpts:
        return []

    prompt = (
        f'Составь {count} карточек для изучения казахского языка. '
        'Используй слова из диалога, если их мало — добавь полезные слова по его темам. '
        'Ответь ТОЛЬКО JSON-объектом вида {"cards": [{"word": "Сәлем", '
        '"translation": "Привет", "hint": "Амандасу сөзі", '
        '"example": "Сәлем, досым!", "example_translation": "Привет, друг!"}]}. '
        'Каждая карточка должна содержать все пять строковых полей. '
        'word, hint и example должны быть на казахском; '
        'translation и example_translation — на русском. '
        'Без markdown, без пояснений. Диалог:\n' + '\n'.join(excerpts[-24:])
    )
    request_messages = [
        {'role': 'system', 'content': 'Ты создаёшь карточки казахского языка. Возвращай только валидный JSON.'},
        {'role': 'user', 'content': prompt},
    ]

    # Retry once with explicit format feedback. Do not expose private chats in logs.
    for attempt in range(2):
        raw = call_llm(request_messages)
        try:
            cards = parse_cards_response(raw)
            if cards:
                return cards[:count]
            raise ValueError('В ответе нет карточек с полями word и translation')
        except (ValueError, json.JSONDecodeError) as exc:
            print(f'Карточки: попытка {attempt + 1} не удалась: {exc}; '
                  f'тип ответа: {type(raw).__name__}; длина: {len(raw)}')
            if attempt == 1:
                raise ValueError('Не удалось получить карточки в формате JSON после двух попыток') from exc
            request_messages.extend([
                {'role': 'assistant', 'content': raw},
                {'role': 'user', 'content': 'Формат предыдущего ответа неверный. '
                 'Верни только JSON с ключом cards, внутри массив объектов '
                 'с полями word, translation, hint, example, example_translation.'},
            ])
    return []


@bot.message_handler(commands=['practice'])
def practice_command(message):
    kb = InlineKeyboardMarkup()
    kb.row(*[InlineKeyboardButton(str(n) + ' слов', callback_data='practice:' + str(n)) for n in (5, 10, 15)])
    bot.send_message(message.chat.id, 'Сколько слов будем повторять?', reply_markup=kb)


def card_keyboard(session, session_id):
    kb = InlineKeyboardMarkup()
    index = session['index']
    if not session['revealed']:
        kb.add(InlineKeyboardButton('👀 Показать перевод',
                                    callback_data=f'card:{session_id}:show:{index}'))
    else:
        kb.row(InlineKeyboardButton('🔁 Не знаю', callback_data=f'card:{session_id}:no:{index}'),
               InlineKeyboardButton('✅ Знаю', callback_data=f'card:{session_id}:yes:{index}'))
    return kb


def card_text(session):
    index = session['index']
    cards = session['cards']
    if index >= len(cards):
        return (f"🎉 Повторение завершено!\n"
                f"✅ Знаю: {session['known']}\n"
                f"🔁 Повторить: {session['unknown']}")
    card = cards[index]
    text = f"🇰🇿 Карточка {index + 1}/{len(cards)}\n\n{card['word']}"
    if session['revealed']:
        text += f"\n\n🇷🇺 {card['translation']}"
        if card.get('example'):
            text += f"\n\nПример: {card['example']}"
        if card.get('example_translation'):
            text += f"\nПеревод: {card['example_translation']}"
    else:
        text += f"\n\n💡 {card.get('hint') or 'Вспомни перевод'}"
    return text


@bot.callback_query_handler(func=lambda c: c.data.startswith('practice:'))
def start_practice(c):
    if c.message.chat.type != 'private':
        bot.answer_callback_query(c.id, 'Открой бота в личном чате', show_alert=True)
        return
    if c.from_user.id != c.message.chat.id:
        bot.answer_callback_query(c.id, 'Эта кнопка не для тебя')
        return
    bot.answer_callback_query(c.id)
    chat_id = c.message.chat.id
    try:
        count = int(c.data.split(':')[1])
        if count not in (5, 10, 15):
            return
        bot.send_message(chat_id, 'Подбираю карточки из твоих диалогов…')
        cards = generate_cards(chat_id, count)
        if not cards:
            bot.send_message(chat_id, 'Пока нет материала. Сначала переведи несколько предложений.')
            return
        session_id = uuid.uuid4().hex[:10]
        session = {'cards': cards, 'index': 0, 'revealed': False,
                   'known': 0, 'unknown': 0}
        state(chat_id).setdefault('sessions', {})[session_id] = session
        save()
        bot.send_message(chat_id, card_text(session),
                         reply_markup=card_keyboard(session, session_id))
    except Exception as exc:
        print('Ошибка карточек:', repr(exc))
        bot.send_message(chat_id, 'Не удалось создать карточки. Попробуй ещё раз.')


@bot.callback_query_handler(func=lambda c: c.data.startswith('card:'))
def handle_card(c):
    chat_id = c.message.chat.id
    if c.message.chat.type != 'private' or c.from_user.id != chat_id:
        bot.answer_callback_query(c.id, 'Эта карточка не для тебя')
        return
    try:
        _, session_id, action, index_str = c.data.split(':')
        session = state(chat_id).get('sessions', {}).get(session_id)
        if not session or int(index_str) != session['index']:
            bot.answer_callback_query(c.id, 'Карточка уже неактуальна')
            return
        if action == 'show' and not session['revealed']:
            session['revealed'] = True
        elif action in ('yes', 'no') and session['revealed']:
            card = session['cards'][session['index']]
            known = action == 'yes'
            state(chat_id)['words'][card['word'].strip().lower()] = {
                'card': card, 'known': known
            }
            session['known' if known else 'unknown'] += 1
            session['index'] += 1
            session['revealed'] = False
        else:
            bot.answer_callback_query(c.id, 'Действие недоступно')
            return
        save()
        bot.answer_callback_query(c.id)
        finished = session['index'] >= len(session['cards'])
        bot.edit_message_text(card_text(session), chat_id, c.message.message_id,
                              reply_markup=None if finished else card_keyboard(session, session_id))
        if finished:
            state(chat_id).get('sessions', {}).pop(session_id, None)
            save()
    except Exception as exc:
        print('Ошибка обработки карточки:', repr(exc))
        bot.answer_callback_query(c.id, 'Не удалось обновить карточку')


@bot.message_handler(content_types=['text'])
def text_handler(message):
    if message.text.startswith('/'):
        bot.send_message(message.chat.id, 'Неизвестная команда. /help')
        return
    process_message(message.chat.id, message.text)


def main():
    bot.set_my_commands([
        BotCommand('new', 'Новый диалог'), BotCommand('history', 'История диалогов'),
        BotCommand('practice', 'Карточки для повторения'), BotCommand('help', 'Помощь')])
    print('Бот запущен. STREAM_UPDATE_INTERVAL=', STREAM_UPDATE_INTERVAL,
          'STREAM_MAX_CHARS=', STREAM_MAX_CHARS, 'STREAM_MODE=', STREAM_MODE)
    # Keep the existing synchronous polling for messages and callbacks.
    offset = None
    while True:
        try:
            updates = telegram_api('getUpdates', offset=offset, timeout=8,
                                   allowed_updates=['message', 'callback_query'])
            for update in updates:
                offset = update['update_id'] + 1
                bot.process_new_updates([telebot.types.Update.de_json(json.dumps(update))])
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print('Polling error:', repr(exc))
            time.sleep(2)


if __name__ == '__main__':
    main()

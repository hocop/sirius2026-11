from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("API_URL")
API_KEY = os.getenv('API_KEY')
MODEL = os.getenv('MODEL')

client = OpenAI(
    base_url=API_URL,
    api_key=API_KEY,
    max_retries=1,
)


def call_llm(messages: list) -> str:
    # Streaming keeps the connection alive during long generations (avoids API timeouts);
    # the chunks are joined and returned as a single string.
    stream = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        stream=True,
        extra_body={'chat_template_kwargs': {'enable_thinking': False}}
    )

    chunks = []
    for chunk in stream:
        if not chunk.choices:
            continue
        content = chunk.choices[0].delta.content
        if content:
            chunks.append(content)

    return ''.join(chunks)

def stream_llm(messages: list):
    stream = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        stream=True,
        extra_body={'chat_template_kwargs': {'enable_thinking': False}}
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

# Panda Multi-Agent Starter

Минимальное веб-приложение на FastAPI для параллельных запросов к OpenAI и Anthropic.

## Переменные окружения

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL`

## Запуск локально

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Откройте `http://127.0.0.1:8000`.

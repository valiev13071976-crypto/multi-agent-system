import asyncio
import logging
from datetime import datetime

from config.constants import (
    AGENT_TIMEOUT,
    MAX_RETRIES,
    RETRY_BACKOFF_SECONDS,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


class BaseAgent:
    """
    Базовый класс для всех агентов Panda Multi-Agent.

    Общие функции:
    - timeout
    - retry
    - обработка ошибок
    - логирование
    """

    def __init__(self, name: str, model=None):
        self.name = name
        self.model = model


    async def run(self, prompt: str):

        attempt = 0

        while attempt <= MAX_RETRIES:

            try:
                start_time = datetime.now()

                result = await asyncio.wait_for(
                    self.execute(prompt),
                    timeout=AGENT_TIMEOUT
                )

                elapsed = (
                    datetime.now() - start_time
                ).total_seconds()

                logging.info(
                    f"{self.name} completed in {elapsed:.2f}s"
                )

                return {
                    "agent": self.name,
                    "status": "ok",
                    "response": result,
                    "confidence": None
                }


            except asyncio.TimeoutError:

                logging.warning(
                    f"{self.name}: timeout attempt {attempt + 1}"
                )


            except Exception as e:

                logging.error(
                    f"{self.name}: error {str(e)}"
                )


            attempt += 1

            if attempt <= MAX_RETRIES:
                await asyncio.sleep(
                    RETRY_BACKOFF_SECONDS * attempt
                )


        return {
            "agent": self.name,
            "status": "error",
            "response": None,
            "confidence": 0
        }


    async def execute(self, prompt: str):
        """
        Метод должен быть реализован
        каждым конкретным агентом.
        """

        raise NotImplementedError(
            "Agent must implement execute()"
        )


    async def health_check(self):

        return True

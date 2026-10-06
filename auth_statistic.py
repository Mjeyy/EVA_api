import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

import requests
from dotenv import load_dotenv

PAGE_SIZE = 100
DEFAULT_DAYS = 30
DEFAULT_OUTPUT = "statistics.csv"
REQUEST_TIMEOUT = 60
ENV_PATH = Path(__file__).resolve().parent / ".env"


class EvaApiError(Exception):
    """Ошибка HTTP или JSON-RPC при вызове EVA."""


class EvaClient:
    def __init__(self, base_url: str, token: str, timeout: float = REQUEST_TIMEOUT):
        self.base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "EvaClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        payload = {
            "callid": str(uuid4()),
            "method": method,
            "kwargs": params,
            "fields": ["**"],
            "flags": {"admin_mode": True},
            "no_meta": True,
            "jsonrpc": "2.2",
        }
        if not params:
            payload.pop("kwargs")

        url = urljoin(self.base_url, "api/")
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise EvaApiError(f"Не удалось вызвать {method}: {exc}") from exc

        if response.status_code != 200:
            raise EvaApiError(
                f"{method} вернул HTTP {response.status_code}: {response.text}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise EvaApiError(f"{method} вернул не JSON") from exc

        error = body.get("error")
        if error:
            message = error.get("message", error) if isinstance(error, dict) else error
            raise EvaApiError(f"{method}: {message}")
        return body

    def list_users(self) -> list[dict]:
        users: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while True:
            body = self.call(
                "CmfPerson.list",
                params={
                    "filter": ["login", "!=", None],
                    "fields": ["name", "login"],
                    "slice": [offset, offset + PAGE_SIZE],
                },
            )
            page = body.get("result") or []
            if not isinstance(page, list):
                raise EvaApiError("CmfPerson.list вернул неожиданный result")

            fresh = []
            for user in page:
                login = user.get("login")
                if not login or login in seen:
                    continue
                seen.add(login)
                fresh.append(user)

            if not fresh:
                break
            users.extend(fresh)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return users

    def last_login(self, user_login: str) -> dict | None:
        body = self.call(
            "CmfAudit.get",
            params={
                "filter": [
                    ["cmf_author.login", "==", user_login],
                    ["operate", "==", "login_successed"],
                ],
                "fields": ["cmf_created_at"],
                "order_by": ["-cmf_created_at"],
            },
        )
        result = body.get("result")
        if not result or not isinstance(result, dict):
            return None
        return result

    def login_count(self, user_login: str, since: str):
        body = self.call(
            "CmfAudit.count",
            params={
                "filter": [
                    ["cmf_author.login", "==", user_login],
                    ["operate", "==", "login_successed"],
                    ["cmf_created_at", ">=", since],
                ],
                "fields": ["id"],
            },
        )
        return body.get("result")


def period_column(days: int) -> str:
    remainder = abs(days) % 100
    last_digit = remainder % 10
    if 11 <= remainder <= 14:
        word = "дней"
    elif last_digit == 1:
        word = "день"
    elif 2 <= last_digit <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"Количество авторизаций за {days} {word}"


def load_settings() -> tuple[str, str]:
    load_dotenv(ENV_PATH)
    base_url = os.environ.get("EVA_BASE_URL", "").strip()
    token = os.environ.get("EVA_TOKEN", "").strip()
    if not base_url:
        raise SystemExit("Укажите EVA_BASE_URL в файле .env")
    if not token:
        raise SystemExit("Укажите EVA_TOKEN в файле .env")
    return base_url, token


def collect_statistics(client: EvaClient, days: int) -> list[dict]:
    since = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")
    users = client.list_users()
    logging.info("Пользователей с логином: %s", len(users))
    logging.info("Период входов: с %s", since)

    rows = []
    for index, user in enumerate(users, start=1):
        login = user.get("login")
        logging.info("(%s/%s) %s", index, len(users), login)
        audit = client.last_login(login)
        if not audit:
            continue
        rows.append(
            {
                "name": user.get("name") or "",
                "login": login,
                "last_login": audit.get("cmf_created_at") or "",
                "count": client.login_count(login, since),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict], days: int) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            [
                "ФИО",
                "Логин",
                "Дата последнего входа в систему",
                period_column(days),
            ]
        )
        for row in rows:
            writer.writerow(
                [row["name"], row["login"], row["last_login"], row["count"]]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сбор статистики входа в систему EVA"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="Сколько дней учитывать для числа входов (по умолчанию 30)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Путь к CSV (по умолчанию statistics.csv)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days должен быть больше нуля")
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    base_url, token = load_settings()
    with EvaClient(base_url, token) as client:
        rows = collect_statistics(client, args.days)
    output = Path(args.output)
    write_csv(output, rows, args.days)
    logging.info("Записано строк: %s → %s", len(rows), output)


if __name__ == "__main__":
    try:
        main()
    except EvaApiError as exc:
        logging.error("%s", exc)
        sys.exit(1)

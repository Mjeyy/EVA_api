import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from auth_statistic import PAGE_SIZE, EvaApiError, EvaClient, load_settings

DEFAULT_DAYS = 30
DEFAULT_OUTPUT = "inactive_participants.csv"
DEFAULT_GROUP = "Участники"
PROFILE_CHUNK = 50


@dataclass
class Participant:
    name: str
    login: str
    does_not_work: bool | None = None
    groups: set[str] = field(default_factory=set)


def fetch_page(
    client: EvaClient,
    method: str,
    *,
    filter_=None,
    fields: list[str],
    slice_: list[int],
    order_by: list[str] | None = None,
) -> list[dict]:
    params: dict = {"fields": fields, "slice": slice_}
    if filter_ is not None:
        params["filter"] = filter_
    if order_by is not None:
        params["order_by"] = order_by
    body = client.call(method, params)
    page = body.get("result")
    if page is None:
        return []
    if not isinstance(page, list):
        raise EvaApiError(f"{method} вернул неожиданный result")
    return [item for item in page if isinstance(item, dict)]


def iter_pages(
    client: EvaClient,
    method: str,
    *,
    filter_=None,
    fields: list[str],
    order_by: list[str] | None = None,
):
    offset = 0
    seen: set[str] = set()
    while True:
        page = fetch_page(
            client,
            method,
            filter_=filter_,
            fields=fields,
            slice_=[offset, offset + PAGE_SIZE],
            order_by=order_by,
        )
        fresh = []
        for item in page:
            ident = item.get("id")
            if ident:
                if ident in seen:
                    continue
                seen.add(ident)
            fresh.append(item)
        if not fresh:
            break
        yield fresh
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE


def as_optional_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "да", "yes"}:
            return True
        if lowered in {"false", "0", "нет", "no", ""}:
            return False
    return None


def work_status(value: bool | None) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    return ""


def author_login(record: dict) -> str:
    dotted = record.get("cmf_author.login")
    if isinstance(dotted, str) and dotted.strip():
        return dotted.strip()
    author = record.get("cmf_author")
    if isinstance(author, dict):
        return str(author.get("login") or "").strip()
    if isinstance(author, str) and "@" in author:
        return author.strip()
    return ""


def remember_person(people: dict[str, Participant], record: dict, group_name: str) -> None:
    login = str(record.get("login") or "").strip()
    if not login:
        return
    person = people.get(login)
    if person is None:
        person = Participant(name=str(record.get("name") or ""), login=login)
        people[login] = person
    name = record.get("name")
    if name and not person.name:
        person.name = str(name)
    if "does_not_work" in record:
        fired = as_optional_bool(record.get("does_not_work"))
        if fired is True or person.does_not_work is None:
            person.does_not_work = fired
    if group_name:
        person.groups.add(group_name)


def find_groups(client: EvaClient, needle: str) -> list[dict]:
    groups: list[dict] = []
    for page in iter_pages(
        client,
        "CmfPersonGroup.list",
        fields=["id", "name", "code"],
    ):
        groups.extend(page)
    matched = [group for group in groups if needle in str(group.get("name") or "")]
    if matched:
        logging.info("Групп со словом «%s»: %s", needle, len(matched))
        return matched
    raise SystemExit(
        f"Групп со словом «{needle}» в имени нет. Файл отчёта не записан."
    )


def group_members(client: EvaClient, group_id: str) -> list[dict]:
    members: list[dict] = []
    for page in iter_pages(
        client,
        "CmfPerson.list",
        filter_=[
            ["rg_member_of.id", "==", group_id],
            ["login", "!=", None],
        ],
        fields=["id", "name", "login", "does_not_work"],
    ):
        members.extend(page)
    return members


def collect_participants(client: EvaClient, groups: list[dict]) -> dict[str, Participant]:
    people: dict[str, Participant] = {}
    for group in groups:
        group_id = str(group.get("id") or "")
        if not group_id:
            continue
        label = str(group.get("name") or group.get("code") or group_id)
        members = group_members(client, group_id)
        logging.info("Группа %s: участников %s", label, len(members))
        for person in members:
            remember_person(people, person, label)
    logging.info("Участников с логином: %s", len(people))
    return people


def fill_profiles(client: EvaClient, people: dict[str, Participant]) -> None:
    logins = list(people)
    for start in range(0, len(logins), PROFILE_CHUNK):
        chunk = logins[start : start + PROFILE_CHUNK]
        page = fetch_page(
            client,
            "CmfPerson.list",
            filter_=[["login", "IN", chunk]],
            fields=["name", "login", "does_not_work"],
            slice_=[0, len(chunk)],
        )
        for record in page:
            login = str(record.get("login") or "").strip()
            person = people.get(login)
            if person is None:
                continue
            if record.get("name"):
                person.name = str(record["name"])
            if "does_not_work" in record:
                person.does_not_work = as_optional_bool(record.get("does_not_work"))


def audit_has_records(client: EvaClient) -> bool:
    page = fetch_page(
        client,
        "CmfAudit.list",
        fields=["id", "cmf_created_at"],
        slice_=[0, 1],
        order_by=["-cmf_created_at"],
    )
    return bool(page)


def collect_activity(
    client: EvaClient, since: str, logins: set[str]
) -> dict[str, tuple[int, str]]:
    stats = {login: (0, "") for login in logins}
    offset = 0
    seen: set[str] = set()
    resolved = 0
    total = 0
    while True:
        page = fetch_page(
            client,
            "CmfAudit.list",
            filter_=[["cmf_created_at", ">=", since]],
            fields=["id", "cmf_author", "cmf_author.login", "cmf_created_at"],
            slice_=[offset, offset + PAGE_SIZE],
        )
        if offset == 0 and not page:
            if not audit_has_records(client):
                raise SystemExit(
                    "Аудит этому токену недоступен: CmfAudit.list не вернул записей. "
                    "Файл отчёта не записан."
                )
            logging.info("За период с %s действий в аудите нет", since)
            return stats

        fresh = []
        for record in page:
            ident = record.get("id")
            if ident:
                if ident in seen:
                    continue
                seen.add(ident)
            fresh.append(record)
        if not fresh:
            break

        for record in fresh:
            total += 1
            login = author_login(record)
            if login:
                resolved += 1
            if login not in stats:
                continue
            count, last = stats[login]
            created = str(record.get("cmf_created_at") or "")
            if created > last:
                last = created
            stats[login] = (count + 1, last)

        logging.info("Аудит: просмотрено %s", total)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if total and resolved == 0:
        raise EvaApiError(
            "CmfAudit.list не вернул логин автора. Файл отчёта не записан."
        )
    logging.info("Событий за период: %s, с логином автора: %s", total, resolved)
    return stats


def last_activity(client: EvaClient, login: str) -> str:
    page = fetch_page(
        client,
        "CmfAudit.list",
        filter_=[["cmf_author.login", "==", login]],
        fields=["cmf_created_at"],
        slice_=[0, 1],
        order_by=["-cmf_created_at"],
    )
    if not page:
        return "никогда"
    return str(page[0].get("cmf_created_at") or "никогда")


def build_rows(
    client: EvaClient,
    people: dict[str, Participant],
    activity: dict[str, tuple[int, str]],
) -> list[dict]:
    rows = []
    inactive = [
        person
        for person in people.values()
        if activity.get(person.login, (0, ""))[0] == 0
    ]
    logging.info("Без действий за период: %s", len(inactive))
    history = {}
    for index, person in enumerate(inactive, start=1):
        logging.info("(%s/%s) последнее действие %s", index, len(inactive), person.login)
        history[person.login] = last_activity(client, person.login)

    for person in people.values():
        count, last_in_period = activity.get(person.login, (0, ""))
        active = count > 0
        rows.append(
            {
                "name": person.name,
                "login": person.login,
                "does_not_work": work_status(person.does_not_work),
                "groups": ", ".join(sorted(person.groups)),
                "count": count,
                "last_activity": last_in_period if active else history.get(person.login, "никогда"),
                "active": "да" if active else "нет",
                "sort_active": 1 if active else 0,
            }
        )
    rows.sort(key=lambda row: (row["sort_active"], row["name"].casefold(), row["login"]))
    return rows


def build_report(client: EvaClient, since: str, group_name: str) -> list[dict]:
    groups = find_groups(client, group_name)
    people = collect_participants(client, groups)
    if people:
        fill_profiles(client, people)
    else:
        logging.info("В группах нет участников с логином")
        return []
    activity = collect_activity(client, since, set(people))
    return build_rows(client, people, activity)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            [
                "ФИО",
                "Логин",
                "Не работает/уволен",
                "Группы",
                "Число действий за период",
                "Дата последней активности",
                "Активен",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["name"],
                    row["login"],
                    row["does_not_work"],
                    row["groups"],
                    row["count"],
                    row["last_activity"],
                    row["active"],
                ]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Участники групп проекта EVA и их действия за период"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="Сколько дней считать периодом активности (по умолчанию 30)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Путь к CSV (по умолчанию inactive_participants.csv)",
    )
    parser.add_argument(
        "--group",
        default=DEFAULT_GROUP,
        help="Подстрока в имени группы (по умолчанию «Участники»)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days должен быть больше нуля")
    if not str(args.group).strip():
        parser.error("--group не должен быть пустым")
    args.group = str(args.group).strip()
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    since = (datetime.now().date() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    logging.info("Период действий: с %s", since)
    base_url, token = load_settings()
    with EvaClient(base_url, token) as client:
        rows = build_report(client, since, args.group)
    output = Path(args.output)
    write_csv(output, rows)
    inactive = sum(1 for row in rows if row["active"] == "нет")
    logging.info(
        "Записано строк: %s, из них неактивных: %s → %s",
        len(rows),
        inactive,
        output,
    )


if __name__ == "__main__":
    try:
        main()
    except EvaApiError as exc:
        logging.error("%s", exc)
        sys.exit(1)

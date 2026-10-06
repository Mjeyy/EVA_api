# Статистика входа в EVA

Скрипт собирает по пользователям [EVA](https://projects.cifra.works/):

- ФИО
- логин
- дату последнего успешного входа
- число успешных входов за последние 30 дней

Пользователи, которые ни разу успешно не входили, в отчёт не попадают.

## Подготовка

На машине нужен Python 3.14. Виртуальное окружение `.venv` уже создано в каталоге проекта. Если его нет, создайте заново:

```bash
cd "/Users/mjey/Projects/API EVA"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Откройте `.env` и вставьте токен в `EVA_TOKEN`. Адрес системы уже указан:

```
EVA_BASE_URL=https://projects.cifra.works/
EVA_TOKEN=ваш_токен
```

Файл `.env` не коммитится. Шаблон без секрета лежит в `.env.example`.

## Запуск

```bash
cd "/Users/mjey/Projects/API EVA"
source .venv/bin/activate
python auth_statistic.py --days 10
```

Другой период и имя файла:

```bash
python auth_statistic.py --days 30 --output statistics.csv
```

Результат — `statistics.csv` в текущей папке. Разделитель колонок — `;`, кодировка UTF-8 с BOM, чтобы Excel открыл кириллицу. Повторный запуск перезаписывает файл.

В консоли печатается прогресс по пользователям. Токен в вывод не попадает.

## Если скрипт остановился сразу

Пустой `EVA_TOKEN` или пустой `EVA_BASE_URL` — скрипт завершается до запроса к API и пишет, какое поле заполнить.

Ошибка HTTP или поле `error` в ответе JSON-RPC печатается в консоль, файл статистики при этом не обновляется.

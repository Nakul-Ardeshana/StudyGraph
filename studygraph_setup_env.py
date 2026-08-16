import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote_plus

ENV_PATH = Path('.env')
EXAMPLE_PATH = Path('.env.example')


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        if EXAMPLE_PATH.exists():
            path.write_text(EXAMPLE_PATH.read_text(encoding='utf-8'), encoding='utf-8')
            print('[SETUP] Created .env from .env.example')
        else:
            path.write_text('FLASK_SECRET_KEY=\nOPENAI_API_KEY=\nDATABASE_URL=\n', encoding='utf-8')
            print('[SETUP] Created default .env')

    lines = path.read_text(encoding='utf-8').splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
    return lines, values


def write_env(path: Path, lines: list[str], updates: dict[str, str]) -> None:
    remaining = dict(updates)
    output: list[str] = []

    for line in lines:
        if '=' not in line or line.lstrip().startswith('#'):
            output.append(line)
            continue

        key = line.split('=', 1)[0].strip()
        if key in remaining:
            output.append(f'{key}={remaining.pop(key)}')
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f'{key}={value}')

    path.write_text('\n'.join(output).rstrip() + '\n', encoding='utf-8')


def build_database_url() -> str:
    print('\n[DB SETUP] This app uses a local PostgreSQL database.')
    print('[DB SETUP] Recommended defaults: localhost, 5432, postgres, studygraph')
    print('[DB SETUP] Make sure PostgreSQL is running and the database exists in pgAdmin.\n')

    full_mode = input('Use full DATABASE_URL instead of separate fields? [y/N]: ').strip().lower()
    if full_mode == 'y':
        url = input('Paste full DATABASE_URL: ').strip()
        if not url:
            raise SystemExit('[ERROR] DATABASE_URL cannot be blank.')
        return url

    host = input('Host [default: localhost]: ').strip() or 'localhost'
    port = input('Port [default: 5432]: ').strip() or '5432'
    user = input('PostgreSQL username [default: postgres]: ').strip() or 'postgres'
    name = input('Database name [default: studygraph]: ').strip() or 'studygraph'
    password = input('Password for PostgreSQL user: ')

    return (
        'postgresql+psycopg://'
        f'{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}'
    )


def test_database(url: str) -> None:
    print('\n[DB SETUP] Testing PostgreSQL connection...')
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import ArgumentError, OperationalError
    except Exception as exc:
        raise SystemExit(f'[ERROR] SQLAlchemy is not installed correctly: {exc}')

    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        print('[OK] PostgreSQL connection successful.')
    except ArgumentError:
        raise SystemExit(
            '[ERROR] DATABASE_URL is not formatted correctly.\n'
            'Example: postgresql+psycopg://postgres:password@localhost:5432/studygraph'
        )
    except OperationalError as exc:
        msg = str(exc).lower()
        print('[ERROR] Could not connect to PostgreSQL.')
        if 'password authentication failed' in msg:
            print('Reason: The PostgreSQL username or password is incorrect.')
            print('Fix: Rerun setup and replace DATABASE_URL with the correct password.')
        elif 'does not exist' in msg and 'database' in msg:
            print('Reason: The database name does not exist.')
            print('Fix: Open pgAdmin and create the database, or use the correct database name.')
        elif 'connection refused' in msg or 'could not connect' in msg:
            print('Reason: PostgreSQL is not running, or the host/port is wrong.')
            print('Fix: Start the PostgreSQL service and check the port, usually 5432.')
        elif 'timeout' in msg:
            print('Reason: The connection timed out.')
            print('Fix: Check the host, port, firewall, and PostgreSQL service.')
        else:
            print(f'Reason: {exc}')
        raise SystemExit(1)


def main() -> int:
    lines, values = read_env(ENV_PATH)
    updates: dict[str, str] = {}

    if not values.get('FLASK_SECRET_KEY') or values.get('FLASK_SECRET_KEY') == 'change_me':
        updates['FLASK_SECRET_KEY'] = secrets.token_urlsafe(32)
        print('[SETUP] Generated FLASK_SECRET_KEY automatically.')

    if not values.get('OPENAI_API_KEY') or values.get('OPENAI_API_KEY') == 'your_openai_api_key_here':
        key = input('Enter OpenAI API key (or press Enter to add it later): ').strip()
        if key:
            updates['OPENAI_API_KEY'] = key
            print('[SETUP] OPENAI_API_KEY will be saved to .env')
        else:
            print('[SETUP] OPENAI_API_KEY left blank. Add it to .env before using AI features.')

    current_url = values.get('DATABASE_URL', '')
    needs_db = not current_url or current_url == 'postgresql+psycopg://postgres:your_password@localhost:5432/studygraph'

    if current_url and not needs_db:
        print('\n[SETUP] DATABASE_URL already exists in .env')
        replace = input('Replace it? [y/N]: ').strip().lower()
        if replace == 'y':
            current_url = build_database_url()
            updates['DATABASE_URL'] = current_url
    else:
        current_url = build_database_url()
        updates['DATABASE_URL'] = current_url

    test_database(current_url)

    if updates:
        write_env(ENV_PATH, lines, updates)
        print('[SETUP] Saved updated values to .env')
    else:
        print('[SETUP] No .env changes needed.')

    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n[ERROR] Setup cancelled.')
        raise SystemExit(1)

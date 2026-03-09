# Claude Remote Control

Telegram bot to control Claude Agent via chat.

## Setup

### 1. Clone and build ACP

```shell
git submodule update --init --recursive
cd claude-agent-acp
npm install
npm run build
cd ..
```

### 2. Create a Telegram Bot

1. Open Telegram, find [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, set a name and username for the bot
3. BotFather will return a token — save it

### 3. Get your User ID

1. Find [@userinfobot](https://t.me/userinfobot) on Telegram
2. Send any message, the bot will return your user ID

### 4. Installation

```shell
python -m venv venv
source ./venv/bin/activate
pip install -r requirements.txt

```

Create a `.env` file:

```
TELEGRAM_BOT_TOKEN=
ACP_PATH=claude-agent-acp/dist/index.js
ALLOWED_USER_IDS=
```

Multiple users separated by commas: `ALLOWED_USER_IDS=123,456,789`

## Run

```shell
python bot.py
```

Open Telegram, find the bot by the username you created, and send a message to get started.

## Development

- **Lint**: `ruff check .`
- **Format**: `ruff format .`
- **Check format**: `ruff format --check .`

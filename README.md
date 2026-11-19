# 🔌 Tapo EV & Device Charging Monitor Telegram Bot

An asynchronous Telegram bot that connects to a **TP-Link Tapo energy-monitoring smart plug** (e.g. Tapo P110, P115, P110M) to monitor and control charging for an Electric Vehicle (EV), Plug-in Hybrid (PHEV), or other high-power appliances.

---

## ✨ Features

- **Interactive 5% Step Battery Selector**:
  - Step 1: Select your vehicle's current battery percentage (`0%`, `5%`, `10%`, ..., `95%`).
  - Step 2: Select your target charge percentage (`...`, `80%`, `90%`, `100%`, or relative shortcuts `+10%`, `+20%`, etc.).
  - Calculates the required energy drawn from the wall based on your usable battery capacity (e.g. 11.8 kWh) and charging efficiency (e.g. 85%).
- **Automatic Limit Enforcement**:
  - Turns on the smart plug to start charging.
  - Automatically switches **OFF** the plug when the programmed target is reached.
- **Natural Full Charge Handling (Stays ON)**:
  - When the vehicle finishes charging on its own (battery reaches 100% or is unplugged), the plug **stays ON** (default behavior, customizable).
- **End-of-Charge Summary Reports**:
  - Sends a detailed Telegram card when charging ends:
    - ⏱ Session duration.
    - 🔌 Energy delivered in the session (kWh) and estimated battery % added.
    - 🕒 Total energy delivered in the rolling lookback window (default: 18 hours).
    - 💰 Estimated session cost based on your electricity tariff (€/kWh).
- **Interactive Configuration Panel (`/configure`)**:
  - Change usable battery capacity, charging efficiency, rolling window hours, idle threshold, auto-off behavior, and electricity cost directly from Telegram with inline buttons.
- **High Precision Rolling Window**:
  - Local SQLite database records time-series samples to calculate rolling consumption across any window (e.g. last 18 hours), even across multiple charge interruptions.
- **Built-in Mock Simulator**:
  - Set `TAPO_MOCK=true` to test and interact with the bot without needing a physical plug online.

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.14).
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather).
- Your Telegram User ID (obtain from [@userinfobot](https://t.me/userinfobot)) for security whitelist.
- A TP-Link Tapo Smart Plug with energy monitoring (P110, P115) connected to your local Wi-Fi.

### 2. Installation
```bash
git clone <repo_url> tapo-charging-bot
cd tapo-charging-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env` with your credentials:
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_ALLOWED_USER_IDS=123456789

# Tapo Smart Plug
TAPO_IP=192.168.1.100
TAPO_USERNAME=your_tapo_email@domain.com
TAPO_PASSWORD=your_tapo_password
TAPO_MOCK=false

# Vehicle & Charging defaults
DEFAULT_BATTERY_CAPACITY_KWH=11.8
CHARGING_EFFICIENCY=0.85
DEFAULT_ROLLING_WINDOW_HOURS=18.0
CHARGING_IDLE_THRESHOLD_WATTS=15.0
AUTO_OFF_ON_FULL=false
ELECTRICITY_COST_PER_KWH=0.15
```

### 4. Running the Bot
```bash
source venv/bin/activate
python -m src.main
```

---

## 📱 Bot Commands & Usage

| Command | Description |
|---|---|
| `/start` or `/menu` | Opens the main interactive control dashboard with live power, session status, and buttons. |
| `/status` | Refreshes and displays the current charging state and progress bar. |
| `/charge` | Launches the interactive charge setup wizard with 5% step buttons. |
| `/charge 20 80` | Quick CLI command: sets start to 20%, target to 80%, calculates kWh, and starts. |
| `/charge 5kwh` | Quick CLI command: charges 5.0 kWh and stops automatically. |
| `/stop` or `/off` | Manually turns off the plug and concludes the active session. |
| `/on` | Manually turns on the plug. |
| `/configure` | Opens the interactive settings menu (battery, efficiency, lookback window, costs). |
| `/history` | Displays the last 5 charging sessions with kWh delivered and costs. |
| `/help` | Shows command descriptions and guidance. |

---

## 🧪 Running Automated Tests

Run the full pytest suite:
```bash
source venv/bin/activate
pytest -v
```

---

## 🐳 Deployment with Docker & Portainer

A ready-to-use [`Dockerfile`](file:///home/jorge/_STORAGE/tapo-charging-bot/Dockerfile) and [`docker-compose.yml`](file:///home/jorge/_STORAGE/tapo-charging-bot/docker-compose.yml) are provided.

### Why `network_mode: host`?
TP-Link Tapo plugs (P110/P115) use local Wi-Fi LAN IP addresses (e.g. `192.168.1.x`) and the local KLAP protocol. `network_mode: host` allows the Docker container to communicate directly with your local Wi-Fi devices without Docker bridge/NAT routing issues.

### Deploying via Portainer Web UI:
1. Open your Portainer instance (e.g. `https://your-server:9443` or `http://localhost:9000`).
2. Go to **Stacks** ➔ **Add stack**.
3. Name: `tapo-charging-bot`.
4. **Build method**:
   - **Repository** (if hosted on GitHub/GitLab): point to your repo URL and select `docker-compose.yml`.
   - OR **Web editor**: Paste the contents of [`docker-compose.yml`](file:///home/jorge/_STORAGE/tapo-charging-bot/docker-compose.yml).
5. In the **Environment variables** section in Portainer, add:
   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_ALLOWED_USER_IDS=123456789
   TAPO_IP=192.168.1.100
   TAPO_USERNAME=your_tapo_email@example.com
   TAPO_PASSWORD=your_tapo_password
   TAPO_MOCK=false
   DEFAULT_BATTERY_CAPACITY_KWH=11.8
   CHARGING_EFFICIENCY=0.85
   DEFAULT_ROLLING_WINDOW_HOURS=18.0
   CHARGING_IDLE_THRESHOLD_WATTS=15.0
   AUTO_OFF_ON_FULL=false
   ELECTRICITY_COST_PER_KWH=0.15
   TZ=Europe/Madrid
   ```
6. Click **Deploy the stack**.

### Deploying via Docker Compose CLI:
```bash
# 1. Ensure .env is populated with your credentials
cp .env.example .env

# 2. Build and start in detached mode
docker compose up -d --build

# 3. View live logs
docker compose logs -f
```

---

## 🛠 Running as a 24/7 Linux Service (systemd)

To ensure the bot runs in the background and restarts automatically on reboot:

```bash
# 1. Edit the service file if necessary
nano systemd/tapo-bot.service

# 2. Copy service file to systemd directory
sudo cp systemd/tapo-bot.service /etc/systemd/system/

# 3. Reload systemd daemon and enable service
sudo systemctl daemon-reload
sudo systemctl enable tapo-bot
sudo systemctl start tapo-bot

# 4. Check service status & logs
sudo systemctl status tapo-bot
journalctl -u tapo-bot -f
```

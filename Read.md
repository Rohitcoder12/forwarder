# Advanced Telegram Auto-Forwarder & Content Engine

This is a powerful, multi-featured Telegram bot built with Python, Telethon, and `python-telegram-bot`. It combines a sophisticated auto-forwarder with an advanced content manipulation engine, all managed directly from a user-friendly bot interface.

The system is designed to run 24/7 on a VPS and is composed of two parts:
1.  **The User Client (Engine):** A Telethon script that logs into your personal Telegram account to read messages from any source you are a member of and perform actions.
2.  **The Control Bot (Interface):** A standard Telegram bot for receiving your commands to manage tasks.

---

## 🚀 Key Features

### Auto-Forwarder (`/newtask`)
- **Forward from Anywhere:** Forwards from any channel, group, or user you are a part of, even if it's private or restricted.
- **Multiple Destinations:** A single forwarding task can send messages to multiple destination chats at once.
- **Advanced Content Filtering:**
    - **Whitelist Words:** Only forward messages that contain specific keywords.
    - **Blacklist Words:** Block messages that contain specific keywords.
- **Advanced Media Filtering:**
    - Individually block or allow **Photos, Videos, Documents/Files,** and **Text-Only** messages for each task.
- **Advanced User Filtering:**
    - **Block My Own Messages:** Prevent messages you send in the source chat from being forwarded.
    - **Block Replies to Me:** Ignore messages that are direct replies to your own messages (useful for downloader bots).
- **Text Manipulation Engine:**
    - **Remove Text:** Automatically remove unwanted text blocks (like ads or spam) from captions before forwarding. Supports multi-line removal rules.
    - **Replace Text:** Automatically find and replace specific words or phrases in captions (e.g., `Stepmom => Mother`).
- **Content Enhancement:**
    - **"Beautiful Captioning" Mode:** Automatically detects Tera-links (`terasharefile`, `terabox`, etc.) and reformats the post into a clean, professional layout with links enumerated as `V1`, `V2`, etc.
    - **Custom Footer:** Add a unique, standard footer text to all messages forwarded by a specific task.
    - **Automatic Video Thumbnails:** Generates a thumbnail for videos that are sent without one, ensuring a professional look.

### Bot Management
- **Full Control via Telegram:** All tasks and settings are managed through a simple, command-based interface.
- **Interactive Setup:** A guided, button-based menu for creating and configuring new tasks.
- **Task Management:** View all active tasks with `/tasks` and delete them with `/delete`.
- **Help Command:** A comprehensive `/help` command that explains all features.

---

## ⚙️ Deployment Guide

Follow these steps to deploy the bot on an Ubuntu 22.04 VPS.

### Phase 1: Prerequisites
1.  **VPS:** A small VPS from any provider (DigitalOcean, Vultr, Linode, etc.) running Ubuntu 22.04.
2.  **Telegram API Credentials:** Get your `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org).
3.  **Bot Token:** Create a new bot with [@BotFather](https://t.me/BotFather) on Telegram to get your bot token.

### Phase 2: Prepare Your Code for GitHub
**Never commit your secret keys to GitHub!** We will prepare the project locally before uploading.

1.  **Create the Project Files:** In a folder on your local computer, create the following four files.

    *   `forwarder.py`: Copy the full Python code into this file.

    *   `requirements.txt`: This file lists the necessary Python libraries.
        ```
        telethon
        python-telegram-bot==13.7
        python-dotenv
        opencv-python
        Pillow
        ```

    *   `.gitignore`: This file prevents sensitive files from being uploaded.
        ```
        # Python virtual environment
        venv/
        __pycache__/

        # Session and database files
        *.session
        *.session-journal
        *.db
        *.db-journal

        # Environment file with secrets
        .env
        ```

    *   `.env`: This file will hold your secret keys. **This file will NOT be uploaded to GitHub.**
        ```
        API_ID=1234567
        API_HASH=yourapicredentialshash
        BOT_TOKEN=yourbottokengoeshere
        ```
        > **⚠️ IMPORTANT:** Replace the placeholder values with your actual credentials.

2.  **Create a Private GitHub Repository:**
    - Go to GitHub and create a **new private repository**.
    - Follow the instructions to push your local folder to this new repository. The commands will look like this:
      ```bash
      git init -b main
      git add .
      git commit -m "Initial commit of the forwarder bot"
      git remote add origin https://github.com/YourUsername/YourRepoName.git
      git push -u origin main
      ```

### Phase 3: Server Setup & Deployment

1.  **Connect to Your VPS:**
    ```bash
    ssh root@YOUR_VPS_IP
    ```

2.  **Update and Install Software:**
    ```bash
    sudo apt update && sudo apt upgrade -y
    sudo apt install python3-pip python3-venv git -y
    ```

3.  **Clone Your Repository:**
    ```bash
    git clone https://github.com/YourUsername/YourRepoName.git
    cd YourRepoName
    ```

4.  **Set Up Python Environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

5.  **Create the `.env` File on the Server:**
    - Create the file using nano: `nano .env`
    - Paste your credentials in the same format as before:
      ```
      API_ID=1234567
      API_HASH=yourapicredentialshash
      BOT_TOKEN=yourbottokengoeshere
      ```
    - Save and exit by pressing `Ctrl+X`, then `Y`, then `Enter`.

6.  **First-Time Login:**
    - Run the bot manually once to log in to your Telegram account.
      ```bash
      python forwarder.py
      ```
    - The script will ask for your phone number, the code Telegram sends you, and your 2FA password (if you have one).
    - Once you see "Telethon client started...", the `.session` file has been created. You can stop the script with `Ctrl+C`.

### Phase 4: Run as a 24/7 Service

We will use `systemd` to keep the bot running forever.

1.  **Create a Service File:**
    ```bash
    sudo nano /etc/systemd/system/forwarder.service
    ```

2.  **Paste the following configuration.** **Remember to replace `yourusername` and `YourRepoName`** with your actual server username and project folder name.
    ```ini
    [Unit]
    Description=Telegram Forwarder Bot
    After=network.target

    [Service]
    User=yourusername
    Group=yourusername
    WorkingDirectory=/home/yourusername/YourRepoName
    ExecStart=/home/yourusername/YourRepoName/venv/bin/python /home/yourusername/YourRepoName/forwarder.py
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    ```

3.  **Enable and Start the Service:**
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable forwarder
    sudo systemctl start forwarder
    ```

4.  **Check the Status:**
    - To check if it's running correctly (it should say `active (running)`):
      ```bash
      sudo systemctl status forwarder
      ```
    - To watch the live logs for debugging:
      ```bash
      sudo journalctl -u forwarder -f
      ```

---

## 🤖 How to Use the Bot

Once deployed, interact with your Control Bot on Telegram:

- `/start`: Shows a welcome message.
- `/help`: Displays a detailed list of all commands and features.
- `/newtask`: Starts the interactive, button-based setup for a new forwarding rule.
- `/tasks`: Lists all currently active forwarding rules.
- `/delete`: Starts the process to delete a rule by its ID.
- `/cancel`: Aborts any ongoing setup process.

---

## 🔄 Updating the Bot

1.  Make your code changes on your local computer.
2.  Push the changes to your GitHub repository.
    ```bash
    git add .
    git commit -m "A description of your new feature or fix"
    git push
    ```
3.  SSH into your VPS, navigate to the project folder, and pull the updates.
    ```bash
    cd /path/to/YourRepoName
    git pull
    ```
4.  Restart the service to apply the changes.
    ```bash
    sudo systemctl restart forwarder
    ```

---

> **⚠️ Security Warning:** Your `api_id`, `api_hash`, `BOT_TOKEN`, and the `.session` file grant full access to your Telegram account and bot. Never share them with anyone. Always use a private GitHub repository.
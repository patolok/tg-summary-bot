<p align="center">
  <img src="userpic.jpg" alt="Bot Logo" width="160"/>
</p>

# Telegram Group Message Summarizer Bot

This project is a Telegram bot designed to archive group messages, export them daily to text files, generate daily discussion summaries using a language model, and automatically post these summaries to a designated thread in the same group.

---

## Features

- **Automatic Message Archiving:**  
  The bot saves all messages from a target Telegram group to an SQLite database, excluding messages from ignored threads or channels.

- **Scheduled Daily Export:**  
  At a configurable time, the bot exports all messages from the past day to one or more `.txt` files, splitting them by size if necessary.

- **Summary Generation:**  
  The exported messages can be processed by a language model (external to this bot) to generate a summary of the day's discussions.

- **Automated Summary Posting:**  
  At another configurable time, the bot posts the generated summary to a specific thread (topic) in the group.

- **Customizable via Config File:**  
  All key parameters (group ID, thread IDs, export/post times, file size limits, etc.) are set in a simple `config.txt` file.

- **Logging:**  
  The bot logs its actions to the console for easy monitoring and debugging.

---

### How It Works

1. **Bot Setup:**  
   Add the bot to your Telegram group and ensure it has permission to read messages.

2. **Message Handling:**  
   The bot listens to all messages in the group, filtering out unwanted messages (e.g., from ignored threads or channels), and saves relevant ones to the database.

3. **Daily Export:**  
   At the configured time (`TIME_EXPORT`), the bot exports the previous day's messages to one or more text files in the `messages/` directory.

4. **Summary Creation:**  
   (External step) Use your preferred language model to generate a summary from the exported messages and save it as `summary.txt` in the appropriate daily folder.

5. **Summary Posting:**  
   At the configured time (`TIME_POST`), the bot posts the contents of `summary.txt` to the specified summary thread in the group.

---
### Directory Structure
```
├── verter.py                 # Main bot script
├── config.txt                # Configuration file
├── messages.db               # SQLite database (auto-created)
├── messages/                 # Exported messages and summaries
│   └── DD.MM.YYYY/          
│       ├── messages_part1.txt
│       ├── messages_part2.txt
│       └── summary.txt 
├── userpic.jpg               # Bot logo for README
└── README.md                 # This file
```

### Installation

1. **Clone the Repository:**
   ```bash
	git clone https://github.com/yourusername/yourrepo.git
	cd yourrepo
	```
2. **Install Dependencies:** Make sure you have Python 3.8+ installed.
	```bash
	pip install python-telegram-bot pytz
	```
3. **Configure the Bot:** Edit `config.txt`
    ```txt
    TOKEN=your_telegram_bot_token
    TARGET_CHAT_ID=-1234567890
    SUMMARY_TOPIC_ID=12345
    TIME_EXPORT=23:59
    TIME_POST=09:00
    MAX_FILE_SIZE=50000
    MAX_SUMMARY_SIZE=4000
    IGNORED_TOPIC_IDS=111,222,333
    ```
4. **Run the Bot:**
    ```bash
    python verter.py
    ```

### How to Generate Summaries

After the bot exports messages, you need to process the exported `.txt` files with your preferred language model (e.g., GPT, Llama, etc.) to create a summary. Save the summary as `summary.txt` in the corresponding daily directory under `messages/`.

The bot will automatically post this summary at the scheduled time.

---
### Example Workflow

1. **Bot runs and archives messages.**
2. **At `TIME_EXPORT`, messages are exported to `messages/DD.MM.YYYY/messages_part*.txt`.**
3. **You process the files with a language model and save the result as `summary.txt` in the same folder.**
4. **At `TIME_POST`, the bot posts the summary to the specified thread in the group.**
#### Acknowledgements

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [pytz](https://pypi.org/project/pytz/)
---
#### Contact
For questions, suggestions, or contributions, please open an issue or contact the maintainer.
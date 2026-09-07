import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple

logger = logging.getLogger("AudioFilter")

BASE_DIR = Path(__file__).resolve().parent
FILTER_CONFIG_FILE = BASE_DIR / "filter_rules.json"

DEFAULT_FILTER_RULES = {
    "blocked_users": [],
    "block_keywords": ["spam", "giveaway", "free followers", "click here", "discord.gg/scam"],
    "allow_list": [],
    "min_length": 1,
    "max_length": 500,
    "block_bots": False,
    "block_keywords_active": True,
}

class AudioFilter:
    def __init__(self):
        self.rules = self.load_rules()
        self.blocked_users: List[str] = self.rules.get("blocked_users", [])
        self.block_keywords: List[str] = self.rules.get("block_keywords", [])
        self.allow_list: List[str] = self.rules.get("allow_list", [])
        self.min_length: int = self.rules.get("min_length", 1)
        self.max_length: int = self.rules.get("max_length", 500)
        self.block_bots: bool = self.rules.get("block_bots", False)
        self.block_keywords_active: bool = self.rules.get("block_keywords_active", True)
        self.filtered_log: List[Dict[str, Any]] = []

    def load_rules(self) -> dict:
        if FILTER_CONFIG_FILE.exists():
            try:
                return {**DEFAULT_FILTER_RULES, **json.loads(FILTER_CONFIG_FILE.read_text(encoding="utf-8"))}
            except Exception:
                return DEFAULT_FILTER_RULES
        return DEFAULT_FILTER_RULES

    def save(self):
        data = {
            "blocked_users": self.blocked_users,
            "block_keywords": self.block_keywords,
            "allow_list": self.allow_list,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "block_bots": self.block_bots,
            "block_keywords_active": self.block_keywords_active,
        }
        try:
            FILTER_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not save filter rules: {e}")

    def clean_for_tts(self, text: str) -> str:
        if not text:
            return ""
        clean = re.sub(r"https?://\S+|www\.\S+", "", text)
        clean = re.sub(r"[:_~*#]+", " ", clean)
        return clean.strip()

    def should_block(self, item: dict) -> Tuple[bool, str]:
        sender = item.get("sender", "").strip()
        message = item.get("message", "").strip()
        sender_lower = sender.lower()

        if sender_lower in [u.lower() for u in self.allow_list]:
            return False, ""

        if sender_lower in [u.lower() for u in self.blocked_users]:
            return True, f"User {sender} is manually banned"

        if self.block_keywords_active and self.block_keywords:
            msg_lower = message.lower()
            for kw in self.block_keywords:
                k = kw.strip().lower()
                if k and k in msg_lower:
                    return True, f"Message contains blocked keyword: {kw}"

        if self.block_bots:
            if re.search(r"https?://|discord\.gg/", message, re.IGNORECASE):
                return True, "Spam link detected in message"

        if len(message) < self.min_length:
            return True, "Message too short"
        if len(message) > self.max_length:
            return True, "Message too long"

        return False, ""

    def log_filtered(self, chat_item: dict, reason: str):
        entry = {
            "id": f"fl_{int(time.time()*1000)}_{len(self.filtered_log)}",
            "time": chat_item.get("time", ""),
            "source": chat_item.get("platform", ""),
            "sender": chat_item.get("sender", ""),
            "message": chat_item.get("raw_message", ""),
            "reason": reason
        }
        self.filtered_log.insert(0, entry)
        if len(self.filtered_log) > 100:
            self.filtered_log.pop()

    def block_user(self, username: str, source: str = "all", reason: str = "Manual block", duration: int = 0):
        u = username.strip()
        if u and u.lower() not in [b.lower() for b in self.blocked_users]:
            self.blocked_users.append(u)
            self.save()

    def unblock_user(self, username: str):
        u = username.strip().lower()
        self.blocked_users = [b for b in self.blocked_users if b.lower() != u]
        self.save()

    def toggle_blocked_user(self, username: str) -> bool:
        u = username.strip().lower()
        matched = next((b for b in self.blocked_users if b.lower() == u), None)
        if matched:
            self.blocked_users.remove(matched)
            self.save()
            return False
        else:
            self.blocked_users.append(username.strip())
            self.save()
            return True

audio_filter = AudioFilter()

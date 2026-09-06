import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional
from playwright.async_api import async_playwright

logger = logging.getLogger("ChatSources")

BASE_DIR = Path(__file__).resolve().parent
BROWSER_PROFILE_DIR = BASE_DIR / "browser_profile"
BROWSER_PROFILE_DIR.mkdir(exist_ok=True)

# Ensure Playwright always finds the isolated portable Chromium
pw_browsers = BASE_DIR / "runtime" / "playwright-browsers"
if pw_browsers.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_browsers.resolve())

def get_system_browser() -> Optional[str]:
    candidates = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Applicationrave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Applicationrave.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/brave-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("brave-browser") or shutil.which("brave") or shutil.which("google-chrome")

BRAVE_PATH = get_system_browser()

class ChatSourceManager:
    def __init__(self, message_callback: Callable, status_callback: Optional[Callable] = None):
        self.message_callback = message_callback
        self.status_callback = status_callback
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.seen_signatures: Dict[str, float] = {}
        self.playwright = None
        self.context = None
        self.pages: Dict[str, Any] = {}
        self.running = False
        self.headless = True
        self.scraper_task = None

    def get_sources(self) -> List[Dict[str, Any]]:
        return list(self.sources.values())

    def detect_platform_name(self, url: str) -> str:
        u = url.lower().strip()
        if "rumble.com" in u: return "Rumble"
        elif "kick.com" in u: return "Kick"
        elif "twitch.tv" in u: return "Twitch"
        elif "x.com" in u or "twitter.com" in u: return "X.com"
        elif any(d in u for d in ["blaze.stream", "theblaze.com"]): return "Blaze"
        elif "youtube.com" in u or "youtu.be" in u: return "YouTube"
        elif "discord" in u: return "Discord"
        return "Stream"

    def normalize_chat_url(self, target: str, platform: str) -> str:
        target = target.strip()
        p = platform.lower()

        if "kick.com" in target.lower() or p == "kick":
            slug = target.rstrip("/").split("/")[-1].replace("@", "").strip()
            if "popout" not in target.lower():
                return f"https://kick.com/popout/{slug}/chat"
            return target

        elif "x.com" in target.lower() or "twitter.com" in target.lower() or p in ["x.com", "x"]:
            clean = target.strip()
            if not clean.startswith("http"):
                clean = f"https://x.com/{clean.lstrip('@')}"
            return clean

        elif "rumble.com" in target.lower() or p == "rumble":
            id_m = re.search(r"(\d{7,14})", target)
            if id_m and "popup" not in target.lower() and "/v" not in target.lower():
                return f"https://rumble.com/chat/popup/{id_m.group(1)}"
            return target

        elif "twitch.tv" in target.lower() or p == "twitch":
            slug = target.rstrip("/").split("/")[-1].lstrip("#").strip()
            if "popout" not in target.lower():
                return f"https://www.twitch.tv/popout/{slug}/chat?popout="
            return target

        elif "youtube.com" in target.lower() or "youtu.be" in target.lower() or p == "youtube":
            vid_m = re.search(r"(?:v=|\/live\/|\/)([a-zA-Z0-9_-]{11})", target)
            if vid_m:
                return f"https://www.youtube.com/live_chat?v={vid_m.group(1)}&is_popout=1"
            return target

        return target

    async def notify_status(self, source_id: str, platform: str, target: str, status: str, log_msg: str):
        if source_id in self.sources:
            self.sources[source_id]["status"] = status
        logger.info(f"[Source Status] [{platform.upper()}] {status.upper()}: {log_msg}")
        if self.status_callback:
            try:
                await self.status_callback(source_id, platform, target, status, log_msg)
            except Exception:
                pass

    async def add_source(self, source_id: str, platform: str, target: str, token: str = "") -> Dict[str, Any]:
        target_clean = target.strip()
        detected_plat = self.detect_platform_name(target_clean)
        if detected_plat != "Stream":
            platform = detected_plat
        chat_url = self.normalize_chat_url(target_clean, platform)

        source_data = {
            "id": source_id,
            "platform": platform,
            "target": target_clean,
            "chat_url": chat_url,
            "token": token.strip(),
            "status": "connecting",
            "message_count": 0,
            "connected_at": time.time()
        }
        self.sources[source_id] = source_data
        await self.notify_status(source_id, platform, target_clean, "connecting", f"Connecting ({chat_url})...")
        await self.ensure_scraper_running()

        # Wait up to 12 seconds for browser context to be ready
        for _ in range(24):
            if self.context:
                break
            await asyncio.sleep(0.5)

        if self.context and source_id not in self.pages:
            asyncio.create_task(self.open_source_page(source_id, chat_url, platform))
        elif not self.context:
            logger.error(f"[Sources] Scraper context timed out while adding {platform}.")
            await self.notify_status(source_id, platform, target_clean, "error", "Browser failed to launch.")

        return source_data

    async def remove_source(self, source_id: str):
        if source_id in self.pages:
            try: await self.pages[source_id].close()
            except Exception: pass
            del self.pages[source_id]
        if source_id in self.sources:
            src = self.sources[source_id]
            await self.notify_status(source_id, src["platform"], src["target"], "disconnected", "Disconnected")
            del self.sources[source_id]

    async def toggle_browser_visibility(self, visible: bool):
        self.headless = not visible
        for sid, p in list(self.pages.items()):
            try: await p.close()
            except Exception: pass
        self.pages.clear()
        if self.context:
            try: await self.context.close()
            except Exception: pass
            self.context = None
        if self.playwright:
            try: await self.playwright.stop()
            except Exception: pass
            self.playwright = None
        self.running = False
        await self.ensure_scraper_running()

    async def ensure_scraper_running(self):
        if not self.running:
            self.running = True
            self.scraper_task = asyncio.create_task(self._main_scraper_loop())

    def _attach_x_websocket_listener(self, page, source_id: str, platform: str):
        def on_websocket(ws):
            url_l = ws.url.lower()
            if any(k in url_l for k in ["chatapi", "chatnow", "pscp.tv", "periscope"]):
                logger.info(f"[X.com Live Stream Hook] Attached to live chat socket: {ws.url}")

                def on_frame(payload):
                    try:
                        text_data = payload if isinstance(payload, str) else payload.decode('utf-8', errors='ignore')
                        data = json.loads(text_data)

                        if data.get("kind") == 1 and "payload" in data:
                            inner = json.loads(data["payload"])
                            body = inner.get("body", "").strip()
                            sender_info = inner.get("sender", {})
                            user = sender_info.get("display_name") or sender_info.get("username") or ""
                            user = re.sub(r"[@:]", "", user).strip()

                            if user and body and body != ":":
                                sig = f"x.com:::{user.lower()}:::{body.lower()}"
                                if sig not in self.seen_signatures:
                                    self.seen_signatures.append(sig)
                                    if source_id in self.sources:
                                        self.sources[source_id]["message_count"] += 1
                                    logger.info(f"[Chat Ingest (Socket)] [X.COM] {user}: {body}")
                                    asyncio.create_task(
                                        self.message_callback("X.com", self.sources[source_id]["target"], user, body, "#38bdf8", source_id)
                                    )
                    except Exception:
                        pass

                ws.on("framereceived", on_frame)

        page.on("websocket", on_websocket)

    async def open_source_page(self, source_id: str, url: str, platform: str):
        if not self.context:
            return
        try:
            logger.info(f"[Persistent Profile] Opening live stream for {platform}: {url}")
            page = await self.context.new_page()
            page.set_default_timeout(20000)

            if platform == "X.com" or "x.com" in url.lower() or "twitter.com" in url.lower():
                self._attach_x_websocket_listener(page, source_id, platform)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                logger.debug(f"Page commit notice for {platform}: {e}")

            await asyncio.sleep(2.5)

            # Auto-click age gate, cookies, and mute video elements to save GPU/CPU
            try:
                await page.evaluate("""
                    () => {
                        document.querySelectorAll('video').forEach(v => {
                            v.muted = true;
                            v.pause();
                        });
                        const clickBtn = (textArray) => {
                            let btns = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
                            let target = btns.find(b => textArray.some(t => (b.innerText || '').toLowerCase().includes(t)));
                            if (target) target.click();
                        };
                        clickBtn(['18+', 'accept', 'agree', 'start watching', 'i understand', 'not now', 'dismiss']);
                    }
                """)
            except Exception:
                pass

            try:
                p_title = await page.title()
                logger.info(f"[{platform} Ready] Title: '{p_title}' | URL: {page.url}")
            except Exception:
                pass

            # Tag existing initial chat history so backlog is NOT spoken on startup
            try:
                initial_msgs = await asyncio.wait_for(self.extract_messages_from_page(page, platform), timeout=4.0)
                for m in initial_msgs:
                    u = m.get("user", "").strip().lower()
                    t = m.get("text", "").strip().lower()
                    if u and t:
                        self.seen_signatures.append(f"{platform.lower()}:::{u}:::{t}")
            except Exception:
                pass

            if source_id in self.sources:
                self.sources[source_id]["message_count"] = 0

            self.pages[source_id] = page
            await self.notify_status(source_id, platform, url, "connected", f"✓ Connected: {platform} ({url})")
        except Exception as e:
            logger.warning(f"Notice on page load for {platform}: {e}")
            if 'page' in locals() and not page.is_closed():
                self.pages[source_id] = page
            await self.notify_status(source_id, platform, url, "connected", f"✓ Active: {platform}")

    async def _main_scraper_loop(self):
        try:
                        # Purge stale Chromium lockfiles from previous runs
            for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
                lock_f = BROWSER_PROFILE_DIR / lock_name
                if lock_f.exists():
                    try:
                        if lock_f.is_dir(): shutil.rmtree(lock_f)
                        else: lock_f.unlink()
                    except Exception: pass

            self.playwright = await async_playwright().start()

            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                executable_path=BRAVE_PATH if (BRAVE_PATH and os.path.exists(BRAVE_PATH)) else None,
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1280,900"
                ],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900}
            )

            await self.context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """)

            for sid, sdata in list(self.sources.items()):
                asyncio.create_task(self.open_source_page(sid, sdata.get("chat_url", sdata["target"]), sdata["platform"]))

            while self.running:
                for sid, page in list(self.pages.items()):
                    if sid not in self.sources or page.is_closed():
                        continue
                    try:
                        sdata = self.sources[sid]
                        platform_name = sdata["platform"]
                        color_map = {
                            "Rumble": "#85c742",
                            "Kick": "#53fc18",
                            "Twitch": "#9146ff",
                            "X.com": "#38bdf8",
                            "Blaze": "#f97316",
                            "YouTube": "#ff0000"
                        }
                        color = color_map.get(platform_name, "#38bdf8")

                        new_messages = await asyncio.wait_for(
                            self.extract_messages_from_page(page, platform_name),
                            timeout=3.5
                        )

                        for msg in new_messages:
                            user = msg.get("user", "").strip()
                            text = msg.get("text", "").strip()

                            if not user or not text or text == ":":
                                continue

                            now_ts = time.time()
                            # Clean signatures older than 60s
                            self.seen_signatures = {k: ts for k, ts in self.seen_signatures.items() if (now_ts - ts) < 60.0}

                            sig = f"{platform_name.lower()}:::{user.lower()}:::{text.lower()}"
                            last_ts = self.seen_signatures.get(sig)

                            if last_ts and (now_ts - last_ts) < 8.0:
                                # Duplicate within 8 seconds -> Forward to moderation log table
                                await self.message_callback(platform_name, sdata["target"], user, text, color, sid, is_duplicate=True)
                                continue

                            self.seen_signatures[sig] = now_ts
                            sdata["message_count"] += 1
                            logger.info(f"[Chat Ingest] [{platform_name.upper()}] {user}: {text}")
                            await self.message_callback(platform_name, sdata["target"], user, text, color, sid, is_duplicate=False)
                    except (asyncio.TimeoutError, Exception) as err:
                        logger.debug(f"[Scraper Poll Notice] {sid}: {err}")

                await asyncio.sleep(0.7)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Playwright Fatal] {e}")
        finally:
            if self.context:
                await self.context.close()
            if self.playwright:
                await self.playwright.stop()
            self.running = False

    async def extract_messages_from_page(self, page, platform: str = "Stream"):
        results = []
        target_frames = [page]
        for f in page.frames:
            if f != page and any(w in f.url.lower() for w in ["chat", "stream", "live", "popout"]):
                target_frames.append(f)

        for frame in target_frames:
            try:
                frame_msgs = await frame.evaluate("""
                    () => {
                        let results = [];

                        let scrollBoxes = document.querySelectorAll('#chat-history-list, .chat-history, [data-testid="cellInnerDiv"], .chat-scroll, #chatroom-messages, div[class*="chat"]');
                        scrollBoxes.forEach(sb => {
                            sb.scrollTop = sb.scrollHeight;
                        });

                        let host = window.location.hostname;

                        // 1. DEDICATED X.COM / TWITTER PARSER
                        if (host.includes("x.com") || host.includes("twitter.com")) {
                            let seenSigs = new Set();
                            let allDivs = Array.from(document.querySelectorAll('div, li, article, section')).filter(el => {
                                let t = (el.innerText || '').trim();
                                return t.split('@').length === 2 && 
                                       !t.includes('Send a message') && 
                                       !t.includes('views') && 
                                       !t.includes('Ended ') && 
                                       !t.includes('Broadcast ended') && 
                                       t.length > 4 && 
                                       t.length < 350;
                            });

                            allDivs.forEach(card => {
                                let raw = (card.innerText || '').trim();
                                let m = raw.match(/^(.*?)\\s*@([a-zA-Z0-9_]+)[\\s:]+(.+)$/s);
                                if (m) {
                                    let u = m[1].replace(/[@:\\n\\r]/g, '').trim() || m[2].trim();
                                    let t = m[3].replace(/[\\n\\r]+/g, ' ').trim();
                                    if (u && t && t !== ':' && !seenSigs.has(`${u}:::${t}`)) {
                                        seenSigs.add(`${u}:::${t}`);
                                        let sig = `${u}:::${t}`;
                                        if (card.getAttribute('data-tts-sig') === sig) return;
                                        card.setAttribute('data-tts-sig', sig);
                                        results.push({ user: u, text: t });
                                    }
                                }
                            });
                        }

                        // 2. DEDICATED BLAZE.STREAM PARSER
                        if (host.includes("blaze.stream") || host.includes("theblaze.com")) {
                            let seenSigs = new Set();
                            let chatBox = document.querySelector('div[class*="chat-container"], div[class*="chat-messages"], div[class*="chat-list"], div[class*="stream-chat"], div[class*="chat"]');
                            let bCards = chatBox ? Array.from(chatBox.querySelectorAll('div, p, li')) : Array.from(document.querySelectorAll('div[class*="message"], div[class*="chat-item"]'));
                            
                            bCards.forEach(card => {
                                let raw = (card.innerText || '').trim();
                                if (!raw || !raw.includes(':') || raw.startsWith('Stream Chat') || raw.includes('gifted a Sub') || raw.length > 350) return;

                                let parts = raw.split(':', 2);
                                let u = parts[0].trim();
                                let t = parts[1].trim();

                                if (t.includes('\\n')) t = t.split('\\n')[0].trim();
                                t = t.replace(/\\b(Subscriber|VIP|Moderator|Mod|Admin)\\b/gi, '').trim();

                                if (u.includes('\\n')) {
                                    let uLines = u.split('\\n');
                                    u = uLines[uLines.length - 1].trim();
                                }
                                u = u.replace(/[@:]/g, '').trim();

                                if (u && t && t.length > 0 && t !== ':' && !seenSigs.has(`${u}:::${t}`)) {
                                    seenSigs.add(`${u}:::${t}`);
                                    let sig = `${u}:::${t}`;
                                    if (card.getAttribute('data-tts-sig') === sig) return;
                                    card.setAttribute('data-tts-sig', sig);
                                    results.push({ user: u, text: t });
                                }
                            });
                        }

                        // 3. DEDICATED KICK.COM PARSER
                        if (host.includes("kick.com")) {
                            let kickRows = document.querySelectorAll('div.chat-entry, div[data-chat-entry], div[class*="chat-entry"], div.chatroom-chat-message, #chatroom-messages > div > div');
                            kickRows.forEach(row => {
                                let u = "";
                                let t = "";

                                let kickUser = row.querySelector('.chat-entry-username, button[data-chat-entry-username], [data-chat-entry-user], span[class*="username"], span.font-bold');
                                let kickMsg = row.querySelector('.chat-entry-message, span[class*="break-words"], span.chat-entry-content');

                                if (kickUser) u = kickUser.innerText.trim();
                                if (kickMsg) {
                                    let emote = kickMsg.querySelector('img[data-emote-name], img[alt]');
                                    t = kickMsg.innerText.trim() || emote?.getAttribute('data-emote-name') || emote?.getAttribute('alt') || "";
                                }

                                if (!u || !t) {
                                    let lines = row.innerText.trim().split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                                    if (lines.length >= 2) {
                                        u = lines[0];
                                        t = lines.slice(1).join(' ');
                                    } else if (lines.length === 1 && lines[0].includes(':')) {
                                        let parts = lines[0].split(':', 2);
                                        u = parts[0].trim();
                                        t = parts[1].trim();
                                    }
                                }

                                if (u.startsWith("Replying to") && u.includes(':')) u = u.split(':')[0].trim();
                                u = u.replace(/[@:]/g, '').trim();
                                t = t.replace(/^[:\\s\\-]+/, '').trim();

                                if (u && t && t.length > 0 && t !== ':') {
                                    let sig = `${u}:::${t}`;
                                    if (row.getAttribute('data-tts-sig') === sig) return;
                                    row.setAttribute('data-tts-sig', sig);
                                    results.push({ user: u, text: t });
                                }
                            });
                        }

                        // 4. RUMBLE / TWITCH / YOUTUBE PARSER
                        let generalRows = document.querySelectorAll('#chat-history-list > li, li.chat-history--row, .chat-history--row, li.chat-history--rant, div.chat-line__message, div[data-a-target="chat-line-message"], yt-live-chat-text-message-renderer');
                        generalRows.forEach(row => {
                            let user = "";
                            let text = "";

                            let rUser = row.querySelector('.chat-history--username a, .chat-history--username, [class*="username"], [class*="author"], a[href*="/user/"], a[href*="/c/"], .chat-author__display-name, span[data-a-target="chat-message-username"], #author-name');
                            let rMsg = row.querySelector('.chat-history--message, [class*="message-text"], [class*="message-body"], [class*="chat-text"], .text-fragment, span[data-a-target="chat-line-message-body"], #message');

                            if (rUser) user = rUser.innerText.trim();
                            if (rMsg) text = rMsg.innerText.trim();

                            // Fallback colon separator if DOM selectors are obscured
                            if (!user || !text) {
                                let fullRow = (row.innerText || '').trim();
                                if (fullRow.includes(':')) {
                                    let parts = fullRow.split(':', 2);
                                    if (!user) user = parts[0].replace(/\b(Subscriber|VIP|Moderator|Mod|Admin)\b/gi, '').trim();
                                    if (!text) text = parts[1].trim();
                                }
                            }

                            if (!user || !text) {
                                let lines = row.innerText.trim().split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                                if (lines.length >= 2) {
                                    user = lines[0];
                                    text = lines.slice(1).join(' ');
                                } else if (lines.length === 1 && lines[0].includes(':')) {
                                    let parts = lines[0].split(':', 2);
                                    user = parts[0].trim();
                                    text = parts[1].trim();
                                }
                            }

                            user = user.replace(/[:\\n\\r]+$/g, '').replace(/^@/, '').trim();
                            text = text.replace(/^[:\\s\\-]+/, '').trim();

                            if (user && text.toLowerCase().startsWith(user.toLowerCase())) {
                                text = text.substring(user.length).replace(/^[:\\s\\-]+/, '').trim();
                            }

                            if (!user || !text || text.length === 0 || text === ':') return;

                            let sig = `${user}:::${text}`;
                            if (row.getAttribute('data-tts-sig') === sig) return;
                            row.setAttribute('data-tts-sig', sig);

                            if (!user.includes("http") && !user.includes("Sign in") && !text.includes("Terms of Service")) {
                                results.push({ user: user, text: text });
                            }
                        });

                        return results;
                    }
                """)
                if frame_msgs:
                    results.extend(frame_msgs)
            except Exception:
                pass
        return results

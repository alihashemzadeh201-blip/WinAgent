"""Program launching for the Windows backend.

``ShellExecuteEx`` alone proved fragile in the field: it can fail with "access denied" when COM
is not initialised on the calling thread, some security products block it, and a failure looks
exactly like "no such program" unless the Win32 error code is captured.  This module therefore

* turns whatever the model asked for ("Microsoft Paint", "برنامه نقاشی", "pbrush", "mspaint.exe",
  a path, ``ms-settings:``) into concrete *candidates* – executables, URIs, files, Start-menu
  shortcuts and Store apps;
* tries several independent ways of starting each candidate (CreateProcess, ShellExecuteEx,
  ``cmd /c start``, ``os.startfile``, ``explorer.exe``, PowerShell ``Start-Process``), recording
  the reason for every failure;
* builds an error message that tells the model – and the user – what actually went wrong.

All OS access goes through :class:`LaunchEnv` and injectable strategy callables so that the
logic is unit-tested on any platform.
"""

from __future__ import annotations

import base64
import ctypes
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .base import BackendError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
BROWSER = "@browser"  # placeholder resolved to the default browser at runtime

# ---------------------------------------------------------------------------
# Friendly names -> what to launch.
# Values are an executable, a URI, "explorer.exe shell:..." or several
# alternatives separated by " | " (tried in order).  Keys are normalised with
# normalize_app_name() when the table is built, so spelling variants such as
# "ماشین‌حساب" (with ZWNJ) match automatically.
# ---------------------------------------------------------------------------
_ALIASES_SRC: dict[str, str] = {
    # --- accessories ---------------------------------------------------------
    "notepad": "notepad.exe", "دفترچه یادداشت": "notepad.exe", "نوت پد": "notepad.exe", "not defteri": "notepad.exe",
    "paint": "mspaint.exe", "mspaint": "mspaint.exe", "ms paint": "mspaint.exe", "pbrush": "mspaint.exe",
    "paintbrush": "mspaint.exe", "نقاشی": "mspaint.exe", "پینت": "mspaint.exe",
    "paint 3d": "ms-paint:",
    "calculator": "calc.exe | calculator:", "calc": "calc.exe | calculator:", "ماشین حساب": "calc.exe | calculator:",
    "hesap makinesi": "calc.exe | calculator:",
    "wordpad": "write.exe", "write": "write.exe", "ورد پد": "write.exe",
    "notepad++": "notepad++.exe", "notepad plus plus": "notepad++.exe", "npp": "notepad++.exe", "نوت پد پلاس پلاس": "notepad++.exe",
    "snipping tool": "snippingtool.exe | ms-screensketch: | ms-screenclip:", "snip": "ms-screenclip:",
    "screenshot tool": "ms-screenclip:", "snip and sketch": "ms-screensketch:", "ابزار برش": "snippingtool.exe | ms-screensketch:",
    "اسکرین شات": "ms-screenclip:",
    "character map": "charmap.exe", "on-screen keyboard": "osk.exe", "osk": "osk.exe", "کیبورد مجازی": "osk.exe",
    "صفحه کلید مجازی": "osk.exe", "magnifier": "magnify.exe", "ذره بین": "magnify.exe", "narrator": "narrator.exe",
    "clock": "ms-clock:", "alarms": "ms-clock:", "alarms and clock": "ms-clock:", "ساعت": "ms-clock:", "زنگ هشدار": "ms-clock:",
    "saat": "ms-clock:", "weather": "bingweather:", "آب و هوا": "bingweather:", "هواشناسی": "bingweather:", "hava durumu": "bingweather:",
    "camera": "microsoft.windows.camera:", "دوربین": "microsoft.windows.camera:", "kamera": "microsoft.windows.camera:",
    "photos": "ms-photos:", "عکس ها": "ms-photos:", "تصاویر": "ms-photos:", "گالری": "ms-photos:",
    "mail": "outlookmail:", "ایمیل": "outlookmail:", "posta": "outlookmail:", "calendar": "outlookcal:", "تقویم": "outlookcal:",
    "takvim": "outlookcal:", "maps": "bingmaps:", "نقشه": "bingmaps:", "people": "ms-people:", "to do": "ms-todo:",
    "todo": "ms-todo:", "phone link": "ms-phone:", "your phone": "ms-phone:", "xbox": "xbox:",
    "media player": "mswindowsmusic: | wmplayer.exe", "windows media player": "wmplayer.exe | mswindowsmusic:",
    "music": "mswindowsmusic:", "groove": "mswindowsmusic:", "موزیک": "mswindowsmusic:", "موسیقی": "mswindowsmusic:",
    "movies and tv": "mswindowsvideo:", "films and tv": "mswindowsvideo:", "فیلم": "mswindowsvideo:", "پخش کننده": "mswindowsmusic: | wmplayer.exe",
    "مدیا پلیر": "wmplayer.exe | mswindowsmusic:", "voice recorder": "ms-callrecording:", "feedback hub": "feedback-hub:",
    "get help": "ms-contact-support:", "tips": "ms-get-started:",
    # --- shell / system ------------------------------------------------------
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "files": "explorer.exe", "windows explorer": "explorer.exe",
    "فایل اکسپلورر": "explorer.exe", "اکسپلورر": "explorer.exe", "فایل ها": "explorer.exe", "پوشه ها": "explorer.exe",
    "dosya gezgini": "explorer.exe",
    "this pc": "explorer.exe shell:MyComputerFolder", "my computer": "explorer.exe shell:MyComputerFolder", "computer": "explorer.exe shell:MyComputerFolder",
    "این رایانه": "explorer.exe shell:MyComputerFolder", "مای کامپیوتر": "explorer.exe shell:MyComputerFolder", "کامپیوتر من": "explorer.exe shell:MyComputerFolder",
    "downloads": "explorer.exe shell:Downloads", "دانلودها": "explorer.exe shell:Downloads", "دانلود ها": "explorer.exe shell:Downloads",
    "پوشه دانلود": "explorer.exe shell:Downloads", "documents": "explorer.exe shell:Personal", "اسناد": "explorer.exe shell:Personal",
    "داکیومنت": "explorer.exe shell:Personal", "pictures": "explorer.exe shell:My Pictures", "desktop": "explorer.exe shell:Desktop",
    "دسکتاپ": "explorer.exe shell:Desktop", "میز کار": "explorer.exe shell:Desktop", "recycle bin": "explorer.exe shell:RecycleBinFolder",
    "سطل زباله": "explorer.exe shell:RecycleBinFolder", "سطل بازیافت": "explorer.exe shell:RecycleBinFolder",
    "run": "explorer.exe shell:::{2559a1f3-21d7-11d4-bdaf-00c04f60b9f0}", "run dialog": "explorer.exe shell:::{2559a1f3-21d7-11d4-bdaf-00c04f60b9f0}",
    "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe", "تسک منیجر": "taskmgr.exe", "مدیر وظایف": "taskmgr.exe",
    "مدیریت وظایف": "taskmgr.exe", "görev yöneticisi": "taskmgr.exe",
    "control panel": "control.exe", "control": "control.exe", "کنترل پنل": "control.exe", "denetim masası": "control.exe",
    "settings": "ms-settings:", "windows settings": "ms-settings:", "تنظیمات": "ms-settings:", "تنظیمات ویندوز": "ms-settings:", "ayarlar": "ms-settings:",
    "display settings": "ms-settings:display", "screen settings": "ms-settings:display", "تنظیمات نمایش": "ms-settings:display",
    "تنظیمات صفحه نمایش": "ms-settings:display", "night light": "ms-settings:nightlight", "bluetooth": "ms-settings:bluetooth",
    "bluetooth settings": "ms-settings:bluetooth", "بلوتوث": "ms-settings:bluetooth", "wifi": "ms-settings:network-wifi",
    "wi-fi": "ms-settings:network-wifi", "wifi settings": "ms-settings:network-wifi", "وای فای": "ms-settings:network-wifi",
    "network settings": "ms-settings:network", "network": "ms-settings:network", "شبکه": "ms-settings:network", "vpn": "ms-settings:network-vpn",
    "proxy": "ms-settings:network-proxy", "airplane mode": "ms-settings:network-airplanemode", "mobile hotspot": "ms-settings:network-mobilehotspot",
    "sound settings": "ms-settings:sound", "sound": "ms-settings:sound", "volume": "ms-settings:sound", "صدا": "ms-settings:sound",
    "تنظیمات صدا": "ms-settings:sound", "notifications": "ms-settings:notifications", "focus assist": "ms-settings:quiethours",
    "power and sleep": "ms-settings:powersleep", "power settings": "ms-settings:powersleep", "battery": "ms-settings:batterysaver",
    "storage": "ms-settings:storagesense", "apps": "ms-settings:appsfeatures", "apps and features": "ms-settings:appsfeatures",
    "installed apps": "ms-settings:appsfeatures", "add or remove programs": "ms-settings:appsfeatures | appwiz.cpl",
    "uninstall programs": "ms-settings:appsfeatures | appwiz.cpl", "default apps": "ms-settings:defaultapps",
    "optional features": "ms-settings:optionalfeatures", "windows update": "ms-settings:windowsupdate", "update": "ms-settings:windowsupdate",
    "بروزرسانی ویندوز": "ms-settings:windowsupdate", "آپدیت ویندوز": "ms-settings:windowsupdate", "آپدیت": "ms-settings:windowsupdate",
    "personalization": "ms-settings:personalization", "personalisation": "ms-settings:personalization", "شخصی سازی": "ms-settings:personalization",
    "wallpaper": "ms-settings:personalization-background", "background settings": "ms-settings:personalization-background",
    "تصویر زمینه": "ms-settings:personalization-background", "lock screen": "ms-settings:lockscreen", "themes": "ms-settings:themes",
    "colors": "ms-settings:colors", "colours": "ms-settings:colors", "taskbar settings": "ms-settings:taskbar", "start settings": "ms-settings:personalization-start",
    "fonts": "ms-settings:fonts", "language settings": "ms-settings:regionlanguage", "language": "ms-settings:regionlanguage",
    "region": "ms-settings:regionformatting", "زبان": "ms-settings:regionlanguage", "keyboard settings": "ms-settings:keyboard",
    "typing settings": "ms-settings:typing", "mouse settings": "ms-settings:mousetouchpad", "touchpad settings": "ms-settings:devices-touchpad",
    "printers": "ms-settings:printers", "printers and scanners": "ms-settings:printers", "پرینتر": "ms-settings:printers",
    "date and time": "ms-settings:dateandtime", "time settings": "ms-settings:dateandtime", "تاریخ و ساعت": "ms-settings:dateandtime",
    "about": "ms-settings:about", "about this pc": "ms-settings:about", "privacy": "ms-settings:privacy", "accounts": "ms-settings:yourinfo",
    "sign-in options": "ms-settings:signinoptions", "developer settings": "ms-settings:developers", "recovery": "ms-settings:recovery",
    "clipboard settings": "ms-settings:clipboard", "accessibility": "ms-settings:easeofaccess", "ease of access": "ms-settings:easeofaccess",
    "windows security": "windowsdefender:", "security": "windowsdefender:", "defender": "windowsdefender:", "windows defender": "windowsdefender:",
    "antivirus": "windowsdefender:", "امنیت ویندوز": "windowsdefender:", "ویندوز دیفندر": "windowsdefender:", "آنتی ویروس": "windowsdefender:",
    "action center": "ms-actioncenter:", "quick settings": "ms-actioncenter:", "available networks": "ms-availablenetworks:",
    "store": "ms-windows-store:", "microsoft store": "ms-windows-store:", "windows store": "ms-windows-store:", "app store": "ms-windows-store:",
    "فروشگاه": "ms-windows-store:", "استور": "ms-windows-store:", "مایکروسافت استور": "ms-windows-store:", "mağaza": "ms-windows-store:",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "command line": "cmd.exe", "dos": "cmd.exe", "خط فرمان": "cmd.exe", "سی ام دی": "cmd.exe",
    "komut istemi": "cmd.exe", "powershell": "powershell.exe", "windows powershell": "powershell.exe", "پاورشل": "powershell.exe",
    "pwsh": "pwsh.exe", "powershell 7": "pwsh.exe", "terminal": "wt.exe | cmd.exe", "windows terminal": "wt.exe | cmd.exe",
    "ترمینال": "wt.exe | cmd.exe", "wsl": "wsl.exe", "ubuntu": "ubuntu.exe | wsl.exe", "linux": "wsl.exe",
    "registry editor": "regedit.exe", "regedit": "regedit.exe", "registry": "regedit.exe", "رجیستری": "regedit.exe", "ویرایشگر رجیستری": "regedit.exe",
    "device manager": "devmgmt.msc", "دیوایس منیجر": "devmgmt.msc", "مدیریت دستگاه": "devmgmt.msc", "services": "services.msc",
    "سرویس ها": "services.msc", "event viewer": "eventvwr.msc", "disk management": "diskmgmt.msc", "مدیریت دیسک": "diskmgmt.msc",
    "computer management": "compmgmt.msc", "task scheduler": "taskschd.msc", "group policy": "gpedit.msc", "local group policy editor": "gpedit.msc",
    "local security policy": "secpol.msc", "windows firewall": "wf.msc | firewall.cpl", "firewall": "wf.msc | firewall.cpl", "فایروال": "wf.msc | firewall.cpl",
    "system information": "msinfo32.exe", "system info": "msinfo32.exe", "msinfo": "msinfo32.exe", "dxdiag": "dxdiag.exe",
    "performance monitor": "perfmon.exe", "resource monitor": "resmon.exe", "disk cleanup": "cleanmgr.exe", "system configuration": "msconfig.exe",
    "msconfig": "msconfig.exe", "network connections": "ncpa.cpl", "programs and features": "appwiz.cpl", "system properties": "sysdm.cpl",
    "mouse properties": "main.cpl", "power options": "powercfg.cpl", "internet options": "inetcpl.cpl", "user accounts": "netplwiz.exe",
    "remote desktop": "mstsc.exe", "rdp": "mstsc.exe", "ریموت دسکتاپ": "mstsc.exe", "quick assist": "quickassist.exe",
    "windows tools": "explorer.exe shell:::{D20EA4E1-3957-11d2-A40B-0C5020524153}", "administrative tools": "explorer.exe shell:::{D20EA4E1-3957-11d2-A40B-0C5020524153}",
    "startup folder": "explorer.exe shell:Startup", "temp folder": "explorer.exe %TEMP%", "appdata": "explorer.exe %APPDATA%",
    # --- browsers ------------------------------------------------------------
    "browser": BROWSER, "web browser": BROWSER, "default browser": BROWSER, "internet": BROWSER, "مرورگر": BROWSER, "اینترنت": BROWSER, "tarayıcı": BROWSER,
    "edge": "msedge.exe", "microsoft edge": "msedge.exe", "msedge": "msedge.exe", "ادج": "msedge.exe", "اج": "msedge.exe", "مایکروسافت اج": "msedge.exe",
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "کروم": "chrome.exe", "گوگل کروم": "chrome.exe",
    "firefox": "firefox.exe", "mozilla firefox": "firefox.exe", "فایرفاکس": "firefox.exe", "brave": "brave.exe", "brave browser": "brave.exe",
    "opera": "opera.exe | launcher.exe", "opera gx": "opera.exe", "vivaldi": "vivaldi.exe", "tor": "firefox.exe", "internet explorer": "iexplore.exe",
    # --- office / productivity -----------------------------------------------
    "word": "winword.exe", "microsoft word": "winword.exe", "ms word": "winword.exe", "winword": "winword.exe", "ورد": "winword.exe",
    "excel": "excel.exe", "microsoft excel": "excel.exe", "اکسل": "excel.exe", "powerpoint": "powerpnt.exe", "microsoft powerpoint": "powerpnt.exe",
    "power point": "powerpnt.exe", "پاورپوینت": "powerpnt.exe", "outlook": "outlook.exe", "اوت لوک": "outlook.exe", "اوتلوک": "outlook.exe",
    "onenote": "onenote.exe", "وان نوت": "onenote.exe", "access": "msaccess.exe", "publisher": "mspub.exe", "teams": "ms-teams.exe | teams.exe",
    "microsoft teams": "ms-teams.exe | teams.exe", "تیمز": "ms-teams.exe | teams.exe", "onedrive": "onedrive.exe",
    "libreoffice": "soffice.exe", "libreoffice writer": "swriter.exe", "libreoffice calc": "scalc.exe", "acrobat": "acrobat.exe | acrord32.exe",
    "adobe acrobat": "acrobat.exe | acrord32.exe", "adobe reader": "acrord32.exe | acrobat.exe", "pdf reader": "acrord32.exe | acrobat.exe | msedge.exe",
    "notion": "notion.exe", "obsidian": "obsidian.exe", "evernote": "evernote.exe",
    # --- development ---------------------------------------------------------
    "vscode": "code.exe | code", "vs code": "code.exe | code", "visual studio code": "code.exe | code", "code": "code.exe | code",
    "وی اس کد": "code.exe | code", "ویژوال استودیو کد": "code.exe | code", "visual studio": "devenv.exe", "cursor": "cursor.exe",
    "pycharm": "pycharm64.exe", "intellij": "idea64.exe", "intellij idea": "idea64.exe", "android studio": "studio64.exe", "webstorm": "webstorm64.exe",
    "rider": "rider64.exe", "sublime": "sublime_text.exe", "sublime text": "sublime_text.exe", "atom": "atom.exe", "eclipse": "eclipse.exe",
    "git bash": "git-bash.exe", "github desktop": "githubdesktop.exe", "python": "python.exe", "پایتون": "python.exe", "idle": "idle.bat",
    "postman": "postman.exe", "docker": "docker desktop.exe", "docker desktop": "docker desktop.exe", "virtualbox": "virtualbox.exe",
    "vmware": "vmware.exe", "putty": "putty.exe", "filezilla": "filezilla.exe", "winscp": "winscp.exe", "wireshark": "wireshark.exe",
    "sql server management studio": "ssms.exe", "ssms": "ssms.exe", "dbeaver": "dbeaver.exe", "mysql workbench": "mysqlworkbench.exe",
    # --- communication / media / misc ----------------------------------------
    "telegram": "telegram.exe", "تلگرام": "telegram.exe", "whatsapp": "whatsapp: | whatsapp.exe", "واتساپ": "whatsapp: | whatsapp.exe",
    "واتس اپ": "whatsapp: | whatsapp.exe", "discord": "discord.exe", "دیسکورد": "discord.exe", "slack": "slack.exe", "zoom": "zoom.exe | zoom",
    "زوم": "zoom.exe | zoom", "skype": "skype.exe | skype", "اسکایپ": "skype.exe | skype", "signal": "signal.exe", "viber": "viber.exe",
    "spotify": "spotify.exe | spotify:", "اسپاتیفای": "spotify.exe | spotify:", "vlc": "vlc.exe", "وی ال سی": "vlc.exe", "itunes": "itunes.exe",
    "steam": "steam.exe | steam:", "استیم": "steam.exe | steam:", "epic games": "epicgameslauncher.exe", "obs": "obs64.exe", "obs studio": "obs64.exe",
    "photoshop": "photoshop.exe", "فتوشاپ": "photoshop.exe", "illustrator": "illustrator.exe", "premiere": "adobe premiere pro.exe", "gimp": "gimp-2.10.exe | gimp.exe",
    "7zip": "7zfm.exe", "7-zip": "7zfm.exe", "winrar": "winrar.exe", "anydesk": "anydesk.exe", "teamviewer": "teamviewer.exe",
    "figma": "figma.exe", "canva": "canva.exe", "blender": "blender.exe", "audacity": "audacity.exe", "handbrake": "handbrake.exe",
    "qbittorrent": "qbittorrent.exe", "utorrent": "utorrent.exe", "idm": "idman.exe", "internet download manager": "idman.exe",
    "google drive": "googledrivefs.exe", "dropbox": "dropbox.exe", "nvidia control panel": "nvcplui.exe",
}

# Well-known applications probed at start-up so the model knows what is installed.
KNOWN_APPS: list[tuple[str, str]] = [
    ("Google Chrome", "chrome.exe"), ("Microsoft Edge", "msedge.exe"), ("Mozilla Firefox", "firefox.exe"), ("Brave", "brave.exe"),
    ("Opera", "opera.exe"), ("Vivaldi", "vivaldi.exe"),
    ("Microsoft Word", "winword.exe"), ("Microsoft Excel", "excel.exe"), ("Microsoft PowerPoint", "powerpnt.exe"),
    ("Microsoft Outlook", "outlook.exe"), ("OneNote", "onenote.exe"), ("Microsoft Access", "msaccess.exe"), ("Microsoft Teams", "ms-teams.exe"),
    ("LibreOffice", "soffice.exe"), ("Adobe Acrobat", "acrobat.exe"), ("Adobe Reader", "acrord32.exe"),
    ("Visual Studio Code", "code.exe"), ("Cursor", "cursor.exe"), ("Visual Studio", "devenv.exe"), ("PyCharm", "pycharm64.exe"),
    ("IntelliJ IDEA", "idea64.exe"), ("Android Studio", "studio64.exe"), ("Notepad++", "notepad++.exe"), ("Sublime Text", "sublime_text.exe"),
    ("Git", "git.exe"), ("Python", "python.exe"), ("Node.js", "node.exe"), ("Windows Terminal", "wt.exe"), ("PowerShell 7", "pwsh.exe"),
    ("WSL", "wsl.exe"), ("Docker Desktop", "docker desktop.exe"), ("Postman", "postman.exe"), ("VirtualBox", "virtualbox.exe"),
    ("Telegram", "telegram.exe"), ("WhatsApp", "whatsapp.exe"), ("Discord", "discord.exe"), ("Slack", "slack.exe"), ("Zoom", "zoom.exe"),
    ("Skype", "skype.exe"), ("Spotify", "spotify.exe"), ("VLC media player", "vlc.exe"), ("Steam", "steam.exe"), ("OBS Studio", "obs64.exe"),
    ("7-Zip", "7zfm.exe"), ("WinRAR", "winrar.exe"), ("Adobe Photoshop", "photoshop.exe"), ("GIMP", "gimp-2.10.exe"), ("AnyDesk", "anydesk.exe"),
    ("TeamViewer", "teamviewer.exe"), ("Notion", "notion.exe"), ("Obsidian", "obsidian.exe"), ("Figma", "figma.exe"),
    ("Internet Download Manager", "idman.exe"), ("qBittorrent", "qbittorrent.exe"),
]

# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------
_TRANS = str.maketrans({
    "ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه",      # Arabic -> Persian letter forms
    "\u200c": " ", "\u200d": "", "\u200e": "", "\u200f": "",  # ZWNJ / ZWJ / direction marks
    "_": " ", "\u00a0": " ", "\u2019": "'", "\u2018": "'",
})
_LEAD_WORDS = {"open", "launch", "start", "run", "the", "a", "an", "my", "microsoft", "ms", "windows", "google", "app",
               "application", "program", "برنامه", "نرم", "افزار", "اپلیکیشن", "اپ", "ی", "باز", "کن", "لطفا", "لطفاً"}
_TRAIL_WORDS = {"app", "application", "program", "browser", "desktop", "برنامه", "را", "رو", "باز", "کن", "بازکن", "لطفا", "لطفاً"}
_ARGS_LIKE_RE = re.compile(r"""^(?:["'\-/]|[a-z]:[\\/]|\\\\|https?://|www\.|\.{1,2}(?:[\\/]|$)|%\w+%)""", re.I)
_BRAND_WORDS = {"microsoft", "google", "adobe", "windows", "mozilla", "apple", "jetbrains", "oracle", "autodesk", "ms"}
_PENALTY_WORDS = {"uninstall", "uninstaller", "remove", "setup", "installer", "update", "updater", "repair", "help", "readme",
                  "documentation", "docs", "manual", "website", "homepage", "license", "changelog", "troubleshoot", "safe", "mode"}
_URI_RE = re.compile(r"^[a-z][a-z0-9+.\-]+:", re.I)
_DRIVE_RE = re.compile(r"^[a-z]:[\\/]", re.I)
_EXE_EXT = (".exe", ".com", ".bat", ".cmd")
_DOC_EXT = (".msc", ".cpl", ".lnk", ".url", ".appref-ms")


def split_program_args(raw: str) -> Optional[tuple[str, str]]:
    """Split "notepad C:\\x.txt" / "code ." / "chrome --incognito" into (program, args); None when not applicable."""
    raw = (raw or "").strip()
    if " " not in raw or raw.startswith(("\"", "'")):
        return None
    head, _, rest = raw.partition(" ")
    rest = rest.strip()
    if not head or not rest:
        return None
    if (_ARGS_LIKE_RE.match(rest) or _URI_RE.match(rest) or "\\" in rest or "/" in rest
            or re.search(r"\.[a-z0-9]{1,5}$", rest, re.I)):
        return head, rest
    return None


def normalize_app_name(name: str) -> str:
    """Lower-case, unify Persian/Arabic letter forms, drop ZWNJ and stray quotes, collapse spaces."""
    s = str(name or "").translate(_TRANS).lower().strip()
    s = s.strip("\"'`").strip()
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(".").strip()


def _compact(s: str) -> str:
    return re.sub(r"[\s\-]+", "", s)


def _alias_forms(normalized: str) -> list[str]:
    forms = [normalized]
    if normalized.endswith(".exe"):
        forms.append(normalized[:-4])
    words = normalized.split(" ")
    while len(words) > 1 and words[0] in _LEAD_WORDS:
        words = words[1:]
        forms.append(" ".join(words))
    while len(words) > 1 and words[-1] in _TRAIL_WORDS:
        words = words[:-1]
        forms.append(" ".join(words))
    if len(words) == 1 and words[0].endswith(".exe"):
        forms.append(words[0][:-4])
    seen: dict[str, None] = {}
    for f in forms:
        if f:
            seen.setdefault(f, None)
    return list(seen)


APP_ALIASES: dict[str, str] = {normalize_app_name(k): v for k, v in _ALIASES_SRC.items()}
_COMPACT_ALIASES: dict[str, str] = {}
for _k, _v in APP_ALIASES.items():
    _COMPACT_ALIASES.setdefault(_compact(_k), _v)


def lookup_alias(name: str) -> Optional[str]:
    """Return the alias value for ``name`` (exact, then with filler words removed, then fuzzy)."""
    n = normalize_app_name(name)
    if not n or _DRIVE_RE.match(n) or "\\" in n or "/" in n:
        return None
    forms = _alias_forms(n)
    for f in forms:
        if f in APP_ALIASES:
            return APP_ALIASES[f]
    for f in forms:
        c = _compact(f)
        if c in _COMPACT_ALIASES:
            return _COMPACT_ALIASES[c]
    if len(n) >= 4:
        close = difflib.get_close_matches(_compact(forms[-1]), list(_COMPACT_ALIASES), n=1, cutoff=0.87)
        if close:
            return _COMPACT_ALIASES[close[0]]
    return None


def _norm_key(s: str) -> str:
    return re.sub(r"[^0-9a-z\u0600-\u06ff+#]+", "", normalize_app_name(s))


def _words(s: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-z\u0600-\u06ff+#]+", normalize_app_name(s)) if w]


def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(haystack[i:i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1))


def best_match(query: str, names: list[str]) -> Optional[tuple[int, int]]:
    """Pick the entry of ``names`` that best matches ``query``.

    Returns ``(tier, index)`` where tier 0 = exact, 1 = whole-word match ("chrome" in "Google Chrome"),
    2 = prefix, 3 = fuzzy – or None.  Lower tiers win; ties go to the shortest name.  Whole-word
    matching prevents "word" from selecting "WordPad".
    """
    qn = _norm_key(query)
    if not qn:
        return None
    qw = _words(query)
    for f in _alias_forms(normalize_app_name(query)):
        if _words(f):
            qw = _words(f)
    best: Optional[tuple[tuple[int, ...], int]] = None
    norms: list[str] = []
    for i, name in enumerate(names):
        nn = _norm_key(name)
        norms.append(nn)
        if not nn:
            continue
        nw = _words(name)
        extra: list[str] = []
        if nn == qn or nn == _compact(" ".join(qw)):
            tier = 0
        elif _contains_sequence(nw, qw):
            tier = 1
            extra = [w for w in nw if w not in qw]
        elif len(qn) >= 4 and nn.startswith(qn):
            tier = 2
        else:
            continue
        # "Uninstall Foo" / "Foo Help" never beat "Foo"; fewer extra words first; "Microsoft Edge" beats
        # "Edge Beta" because its extra word is a brand name
        penalty = sum(1 for w in nw if w in _PENALTY_WORDS and w not in qw)
        non_brand = sum(1 for w in extra if w not in _BRAND_WORDS)
        key = (tier, penalty, len(extra), non_brand, len(nn), i)
        if best is None or key < best[0]:
            best = (key, i)
    if best is not None:
        return best[0][0], best[1]
    close = difflib.get_close_matches(qn, [n for n in norms if n], n=1, cutoff=0.86)
    if close:
        return 3, norms.index(close[0])
    return None


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    kind: str                 # "exe" | "file" | "uri" | "storeapp"
    target: str               # what to launch (name, path, URI or AppUserModelId)
    args: str = ""
    label: str = ""           # human readable origin, e.g. "alias 'paint'"
    path: str = ""            # resolved absolute path (executables/files) when known

    def key(self) -> tuple[str, str, str]:
        return self.kind, (self.path or self.target).lower(), self.args

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target, "args": self.args, "label": self.label, "path": self.path}


@dataclass
class Attempt:
    method: str
    target: str
    ok: bool
    detail: str = ""
    code: Optional[int] = None      # Win32 error code when known
    not_found: bool = False         # "no such file/program" (as opposed to "blocked")
    pid: Optional[int] = None

    def __str__(self) -> str:
        return f"{self.method} {self.target!r}: {self.detail}"


@dataclass
class LaunchReport:
    name: str
    ok: bool = False
    candidate: Optional[Candidate] = None
    attempts: list[Attempt] = field(default_factory=list)
    pid: Optional[int] = None
    method: str = ""
    fatal: bool = False   # policy block / user cancelled: further attempts are pointless

    @property
    def resolved(self) -> str:
        if not self.candidate:
            return ""
        return self.candidate.path or self.candidate.target

    def message(self) -> str:
        c = self.candidate
        if c is None:
            return f"Launched {self.name!r}."
        what = c.target if not c.path or c.path.lower() == c.target.lower() else f"{c.target} ({c.path})"
        via = f" via {self.method}" if self.method else ""
        pid = f", pid {self.pid}" if self.pid else ""
        origin = f" [{c.label}]" if c.label and c.label != "path" else ""
        args = f" with arguments {c.args!r}" if c.args else ""
        return f"Launched {self.name!r} -> {what}{args}{via}{pid}{origin}."

    def error_message(self) -> str:
        lines = [f"Could not launch {self.name!r}."]
        failed = [a for a in self.attempts if not a.ok]
        if not failed:
            lines.append("Nothing matched this name: no executable, Start-menu entry, Store app, file or URI.")
        else:
            lines.append("Attempts:")
            for a in failed[:12]:
                lines.append(f"  - {a}")
            if len(failed) > 12:
                lines.append(f"  ... and {len(failed) - 12} more")
        lines.append("Diagnosis: " + self.diagnosis())
        lines.append(
            "Next steps: use the exact executable name (e.g. mspaint.exe, notepad.exe, calc.exe, msedge.exe, winword.exe), "
            "a full path, a ms-settings: URI, or ask the user what the program is called. Never guess legacy names such as "
            "'pbrush'. As a last resort press win, type the name, verify the highlighted result before pressing Enter, and "
            "check the window title afterwards."
        )
        return "\n".join(lines)

    def diagnosis(self) -> str:
        failed = [a for a in self.attempts if not a.ok]
        codes = {a.code for a in failed if a.code is not None}
        if failed and codes and codes <= {1155, 31}:
            return ("No application is registered for this URI scheme / file type on this machine (the app is not "
                    "installed). Use a different program or ask the user.")
        if failed and all(a.not_found for a in failed):
            hint = "No program with this name is installed (or it is spelled differently)."
            low = self.name.lower()
            if low in ("wordpad", "write", "write.exe", "wordpad.exe"):
                hint += " WordPad was removed from Windows 11 24H2 and later; use Notepad or Word instead."
            return hint
        if 1260 in codes:
            return ("Windows blocked the program by policy (ERROR_ACCESS_DISABLED_BY_POLICY / AppLocker / Software Restriction "
                    "Policies). The user or an administrator must allow it; do not keep retrying.")
        if 740 in codes and 1223 in codes:
            return "The program requires administrator rights and the user cancelled the UAC prompt."
        if 740 in codes:
            return ("The program requires administrator rights. A UAC prompt was requested; ask the user to confirm it, "
                    "then take a screenshot.")
        if 1223 in codes:
            return "The user cancelled the UAC / security prompt."
        if 5 in codes or any("denied" in a.detail.lower() for a in failed):
            return ("Windows refused to start the program (access denied). Typical causes: antivirus, Smart App Control, "
                    "AppLocker or a parental-control policy blocking it, or a corrupted installation. Tell the user; "
                    "an alternative program may be acceptable.")
        if 193 in codes:
            return "The file is not a valid Win32 application (corrupted, or built for another architecture)."
        if 31 in codes:
            return "No program is associated with this file type; open it with a specific program (pass the file as args)."
        if 267 in codes:
            return "The working directory is invalid."
        if failed:
            return "Every launch method failed with an unexpected error; see the attempts above."
        return "Unknown."


# ---------------------------------------------------------------------------
# Win32 error text (works off-Windows for the common codes)
# ---------------------------------------------------------------------------
_WIN32_ERRORS = {
    0: "success", 2: "the system cannot find the file specified", 3: "the system cannot find the path specified",
    5: "access is denied", 8: "not enough memory", 26: "sharing violation", 27: "file association incomplete",
    28: "DDE timeout", 29: "DDE transaction failed", 30: "DDE busy", 31: "no application is associated with this file type",
    32: "the process cannot access the file because it is being used by another process / DLL not found",
    193: "not a valid Win32 application", 267: "the directory name is invalid", 740: "the requested operation requires elevation",
    1155: "no application is associated with the specified file for this operation", 1223: "the operation was cancelled by the user",
    1260: "this program is blocked by group policy",
}


def describe_win32_error(code: Optional[int]) -> str:
    if code is None:
        return "unknown error"
    text = _WIN32_ERRORS.get(int(code))
    if text is None and IS_WINDOWS:
        try:
            text = ctypes.FormatError(int(code)).strip().rstrip(".").lower()
        except Exception:
            text = None
    return f"error {code}: {text or 'unknown error'}"


def _code_from_text(text: str) -> tuple[Optional[int], bool]:
    """Guess (win32 code, not_found) from the message of a failed shell command."""
    t = (text or "").lower()
    if "cannot find" in t or "not recognized" in t or "not found" in t or "could not find" in t or "no such" in t:
        return 2, True
    if "blocked by group policy" in t or "blocked by policy" in t:
        return 1260, False
    if "access is denied" in t or "access denied" in t or "unauthorized" in t:
        return 5, False
    if "elevation" in t or "requires elevation" in t or "administrator" in t:
        return 740, False
    if "cancel" in t:
        return 1223, False
    return None, False


# ---------------------------------------------------------------------------
# Environment (everything that touches the OS for *resolution*)
# ---------------------------------------------------------------------------
class LaunchEnv:
    """Default (mostly no-op) environment; the Windows implementation overrides everything."""

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def app_paths(self, exe: str) -> Optional[str]:
        return None

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def system_file(self, name: str) -> Optional[str]:
        return None

    def shortcuts(self) -> list[tuple[str, str]]:
        return []

    def store_apps(self) -> list[tuple[str, str]]:
        return []

    def default_browser(self) -> Optional[str]:
        return None

    def uri_registered(self, scheme: str) -> Optional[bool]:
        """True/False when it is known whether a URI scheme has a handler, None when unknown."""
        return None

    def resolve_exe(self, name: str) -> Optional[str]:
        """Absolute path of an executable given a bare name ('chrome', 'mspaint.exe')."""
        n = os.path.expandvars(name.strip())
        if _DRIVE_RE.match(n) or "\\" in n or "/" in n:
            return n if self.exists(n) else None
        base = n if n.lower().endswith(_EXE_EXT) else n + ".exe"
        for probe in (self.app_paths(base), self.which(n), self.which(base), self.system_file(base)):
            if probe:
                return probe
        return None


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------
Strategy = Callable[[Candidate, Optional[str]], Attempt]

# Which strategies to use, per kind of candidate (and whether it resolved to a path).
STRATEGY_ORDER: dict[str, list[str]] = {
    "exe_path": ["createprocess", "shellexecute", "cmd_start", "startfile", "powershell"],
    "exe_name": ["shellexecute", "cmd_start", "startfile", "powershell"],
    "file": ["shellexecute", "startfile", "cmd_start", "explorer", "powershell"],
    "uri": ["shellexecute", "startfile", "cmd_start", "powershell"],
    "storeapp": ["shellexecute", "explorer", "cmd_start", "powershell"],
}
STRATEGY_ORDER_ALL = {m for ms in STRATEGY_ORDER.values() for m in ms} | {"runas"}


class Launcher:
    def __init__(self, env: Optional[LaunchEnv] = None, strategies: Optional[dict[str, Strategy]] = None):
        self.env = env or LaunchEnv()
        self.strategies: dict[str, Strategy] = strategies if strategies is not None else (
            windows_strategies() if IS_WINDOWS else {})

    # ------------------------------------------------------------ resolution
    def resolve(self, name: str, args: str = "") -> list[Candidate]:
        """Strong candidates for ``name`` (no slow Start-menu / Store lookups)."""
        raw = (name or "").strip().strip('"').strip("'").strip()
        args = (args or "").strip()
        if not raw:
            return []
        out: list[Candidate] = []

        expanded = os.path.expandvars(os.path.expanduser(raw))
        if (_DRIVE_RE.match(expanded) or expanded.startswith("\\\\")) and self.env.exists(expanded):
            kind = "exe" if expanded.lower().endswith(_EXE_EXT) else "file"
            return [Candidate(kind, expanded, args, "path", path=expanded)]
        if self.env.exists(expanded) and ("\\" in expanded or "/" in expanded or os.path.splitext(expanded)[1]):
            p = os.path.abspath(expanded)
            kind = "exe" if p.lower().endswith(_EXE_EXT) else "file"
            return [Candidate(kind, p, args, "path", path=p)]
        if raw.lower().startswith("shell:"):
            return [Candidate("file", raw, args, "shell folder")]
        if _URI_RE.match(raw) and not _DRIVE_RE.match(raw):
            return [Candidate("uri", raw, args, "uri")]

        split = split_program_args(raw)
        if split:
            for c in self.resolve(split[0], (split[1] + " " + args).strip()):
                c.label = f"'{split[0]}' + arguments"
                self._add(out, c)
            if out:
                return out

        alias = lookup_alias(raw)
        if alias:
            out.extend(self._from_alias(alias, args, raw))

        for cand in self._exe_names(raw):
            if cand.lower().endswith(_DOC_EXT):
                self._add(out, self._doc_candidate(cand, args, "system file"))
                continue
            path = self.env.resolve_exe(cand)
            if path:
                self._add(out, Candidate("exe" if path.lower().endswith(_EXE_EXT) else "file", cand, args, "executable", path=path))

        return out

    def start_menu_candidates(self, name: str, args: str = "") -> list[Candidate]:
        """Candidates from Start-menu shortcuts and (slower) the Start apps list, best match first."""
        found: list[tuple[int, Candidate]] = []
        shortcuts = self.env.shortcuts()
        m = best_match(name, [s[0] for s in shortcuts])
        if m is not None:
            stem, path = shortcuts[m[1]]
            found.append((m[0], Candidate("file", path, args, f"Start-menu shortcut '{stem}'", path=path)))
        if not found or found[0][0] > 0:
            apps = self.env.store_apps()
            m = best_match(name, [a[0] for a in apps])
            if m is not None:
                app_name, app_id = apps[m[1]]
                found.append((m[0], Candidate("storeapp", app_id, "", f"Start app '{app_name}'")))
        found.sort(key=lambda t: t[0])
        return [c for _, c in found]

    def bare_candidates(self, name: str, args: str = "") -> list[Candidate]:
        """Unresolved executable names – the shell may still know them (App Paths, PATHEXT)."""
        raw = (name or "").strip()
        if not raw or _URI_RE.match(raw) or "\\" in raw or "/" in raw:
            return []
        split = split_program_args(raw)
        if split:
            raw, args = split[0], (split[1] + " " + args).strip()
        return [Candidate("exe", c, args, "unresolved name") for c in self._exe_names(raw)]

    def _from_alias(self, alias: str, args: str, origin: str) -> list[Candidate]:
        out: list[Candidate] = []
        for alt in [a.strip() for a in alias.split("|") if a.strip()]:
            if alt == BROWSER:
                alt = self.env.default_browser() or "msedge.exe"
            exe, _, alias_args = alt.partition(" ")
            all_args = (os.path.expandvars(alias_args) + " " + args).strip()
            label = f"alias '{origin}'"
            if _URI_RE.match(exe) and not _DRIVE_RE.match(exe):
                out.append(Candidate("uri", exe, all_args, label))
            elif exe.lower().endswith(_DOC_EXT):
                out.append(self._doc_candidate(exe, all_args, label))
            else:
                path = self.env.resolve_exe(exe)
                out.append(Candidate("exe", exe, all_args, label, path=path or ""))
        return out

    def _doc_candidate(self, name: str, args: str, label: str) -> Candidate:
        """.msc/.cpl/.lnk system files: Control Panel applets go through control.exe, the rest open directly."""
        if name.lower().endswith(".cpl"):
            control = self.env.resolve_exe("control.exe")
            return Candidate("exe", "control.exe", (name + " " + args).strip(), label, path=control or "")
        path = self.env.system_file(name)
        return Candidate("file", path or name, args, label, path=path or "")

    @staticmethod
    def _exe_names(raw: str) -> list[str]:
        names = [raw]
        low = raw.lower()
        if not low.endswith(_EXE_EXT + _DOC_EXT):
            names.append(raw + ".exe")
            if " " in raw:
                names.append(raw.replace(" ", "") + ".exe")
        return list(dict.fromkeys(names))

    @staticmethod
    def _add(out: list[Candidate], cand: Candidate) -> None:
        if all(c.key() != cand.key() for c in out):
            out.append(cand)

    # --------------------------------------------------------------- launch
    def launch(self, name: str, args: str = "", cwd: Optional[str] = None) -> LaunchReport:
        name = (name or "").strip().strip('"').strip("'").strip()
        if not name:
            raise BackendError("Application name is empty.")
        report = LaunchReport(name=name)
        tried: set[tuple[str, str, str]] = set()

        def run(cands: list[Candidate]) -> bool:
            for cand in cands:
                if cand.key() in tried or report.fatal:
                    continue
                tried.add(cand.key())
                if self._try(cand, cwd, report):
                    return True
            return False

        resolved = self.resolve(name, args)
        # candidates that point at something concrete (a path, URI, shell folder, Store app) come first; bare
        # executable names that nothing could locate are tried after the Start menu, because they mostly fail
        strong = [c for c in resolved if c.kind != "exe" or (c.path and not c.path.lower().endswith((".bat", ".cmd")))]
        weak = [c for c in resolved if c not in strong]
        for cands in (strong, self.start_menu_candidates(name, args) if not report.fatal else [],
                      weak + self.bare_candidates(name, args)):
            if report.fatal:
                break
            if run(cands):
                return report
        report.ok = False
        return report

    def _try(self, cand: Candidate, cwd: Optional[str], report: LaunchReport) -> bool:
        if cand.kind == "uri":
            scheme = cand.target.split(":", 1)[0]
            if self.env.uri_registered(scheme) is False:
                report.attempts.append(Attempt("uri check", cand.target, False,
                                               f"no application is registered for the '{scheme}:' protocol", code=1155, not_found=True))
                return False
        order_key = cand.kind
        if cand.kind == "exe":
            order_key = "exe_path" if cand.path else "exe_name"
        if cand.kind == "file" and not (cand.path or cand.target.lower().startswith("shell:") or _DRIVE_RE.match(cand.target)):
            # relative document / .msc name: explorer.exe would open the Documents folder instead
            order = [s for s in STRATEGY_ORDER["file"] if s != "explorer"]
        else:
            order = STRATEGY_ORDER[order_key]
        # strategies registered under names unknown to the order table (custom/simulated) run last
        order = [m for m in order if m in self.strategies] + [m for m in self.strategies if m not in STRATEGY_ORDER_ALL]
        not_found_votes = 0
        for method in order:
            strategy = self.strategies.get(method)
            if strategy is None:
                continue
            try:
                attempt = strategy(cand, cwd)
            except Exception as exc:  # a strategy must never take the whole launcher down
                attempt = Attempt(method, cand.path or cand.target, False, f"{type(exc).__name__}: {exc}")
            report.attempts.append(attempt)
            if attempt.ok:
                report.ok = True
                report.candidate = cand
                report.pid = attempt.pid
                report.method = attempt.method
                return True
            if attempt.code in (1260, 1223):
                report.fatal = True
                return False
            if attempt.code == 740 and "runas" in self.strategies:
                elevated = self.strategies["runas"](cand, cwd)
                report.attempts.append(elevated)
                if elevated.ok:
                    report.ok, report.candidate, report.pid, report.method = True, cand, elevated.pid, elevated.method
                    return True
                report.fatal = True
                return False
            if attempt.not_found:
                not_found_votes += 1
                # an unresolved name that two independent mechanisms cannot find does not exist
                if not cand.path and cand.kind in ("exe", "file") and not_found_votes >= 2:
                    return False
        return False


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------
_tls = threading.local()

if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessId.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        _ole32.CoInitializeEx.restype = ctypes.c_long
    except OSError:
        _ole32 = None

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010


def ensure_com_initialized() -> Optional[int]:
    """Initialise COM (STA) on the calling thread once; ShellExecuteEx relies on it for many targets."""
    if not IS_WINDOWS or _ole32 is None:
        return None
    if getattr(_tls, "com_hr", None) is not None:
        return _tls.com_hr
    try:
        hr = int(_ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE))
    except Exception as exc:
        log.debug("CoInitializeEx failed: %s", exc)
        hr = -1
    _tls.com_hr = hr  # S_OK, S_FALSE and RPC_E_CHANGED_MODE (already MTA) are all usable states
    return hr


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def run_hidden(argv: "str | list[str]", timeout: float = 30.0, cwd: Optional[str] = None) -> tuple[Optional[int], str, str]:
    """Run a console command without a window and return (exit code, stdout, stderr).

    Output goes to temporary files rather than pipes: a program started by the command (``start``,
    ``Start-Process``) may inherit the pipe handles and would then keep ``communicate()`` blocked
    until *it* exits.  With files we only wait for the shell itself.
    """
    flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
    try:
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f, cwd=cwd or None,
                                    creationflags=flags)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = None
            out_f.seek(0)
            err_f.seek(0)
            out = out_f.read().decode("utf-8", "replace")
            err = err_f.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, "", str(exc)
    if rc is None:
        err = (err + "\n" if err else "") + "timeout"
    return rc, out, err


def run_powershell(command: str, timeout: float = 30.0) -> tuple[Optional[int], str, str]:
    """Run a PowerShell command hidden, returning (exit code, stdout, stderr)."""
    prefix = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
              "$ErrorActionPreference='Stop'; ")
    encoded = base64.b64encode((prefix + command).encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text",
            "-EncodedCommand", encoded]
    return run_hidden(argv, timeout=timeout)


def windows_strategies() -> dict[str, Strategy]:  # pragma: no cover - Windows only
    """The real launch strategies (each one independent of the others)."""

    def shellexecute(cand: Candidate, cwd: Optional[str], verb: Optional[str] = None) -> Attempt:
        ensure_com_initialized()
        target = cand.path or cand.target
        if cand.kind == "storeapp":
            target = f"shell:AppsFolder\\{cand.target}"
        method = "ShellExecuteEx" if verb is None else f"ShellExecuteEx({verb})"
        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC | SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = None
        sei.lpVerb = verb          # None = the file type's default verb (what a double-click does)
        sei.lpFile = target
        sei.lpParameters = cand.args or None
        sei.lpDirectory = cwd or None
        sei.nShow = SW_SHOWNORMAL
        ctypes.set_last_error(0)
        ok = bool(_shell32.ShellExecuteExW(ctypes.byref(sei)))
        err = ctypes.get_last_error()
        pid = None
        if sei.hProcess:
            try:
                pid = int(_kernel32.GetProcessId(sei.hProcess)) or None
            finally:
                _kernel32.CloseHandle(sei.hProcess)
        if ok:
            return Attempt(method, target, True, "accepted by the shell" + (f", pid {pid}" if pid else ""), pid=pid)
        inst = int(sei.hInstApp or 0)          # SE_ERR_* codes (<= 32) mirror the Win32 codes we care about
        code = err or (inst if 0 < inst <= 32 else None)
        return Attempt(method, target, False, describe_win32_error(code), code=code, not_found=code in (2, 3))

    def runas(cand: Candidate, cwd: Optional[str]) -> Attempt:
        a = shellexecute(cand, cwd, verb="runas")
        if a.ok:
            a.detail += " (UAC prompt shown; the user must confirm it)"
        return a

    def createprocess(cand: Candidate, cwd: Optional[str]) -> Attempt:
        path = cand.path or cand.target
        cmdline = subprocess.list2cmdline([path]) + (f" {cand.args}" if cand.args else "")
        try:
            proc = subprocess.Popen(cmdline, cwd=cwd or None, close_fds=True, creationflags=CREATE_NEW_CONSOLE)
        except OSError as exc:
            code = getattr(exc, "winerror", None) or (2 if exc.errno == 2 else None)
            return Attempt("CreateProcess", path, False, f"[WinError {code}] {exc.strerror or exc}", code=code,
                           not_found=code in (2, 3))
        time.sleep(0.3)
        rc = proc.poll()
        if rc is not None and rc & 0xC0000000 == 0xC0000000:
            # NTSTATUS failure at start-up (missing DLL, blocked by a driver, bad image ...)
            return Attempt("CreateProcess", path, False, f"the process died at start-up with status 0x{rc:08X}", code=None)
        detail = f"started pid {proc.pid}"
        if rc not in (None, 0):
            detail += f" (the process exited immediately with code {rc}; it may have handed over to a running instance)"
        return Attempt("CreateProcess", path, True, detail, pid=proc.pid)

    def cmd_start(cand: Candidate, cwd: Optional[str]) -> Attempt:
        target = cand.path or cand.target
        if cand.kind == "storeapp":
            target = f"shell:AppsFolder\\{cand.target}"
        parts = ["start", '""']
        if cwd:
            parts.append(f'/d "{cwd}"')
        parts.append(f'"{target}"')
        if cand.args:
            parts.append(cand.args)
        cmd = f'cmd.exe /d /s /c "{" ".join(parts)}"'
        rc, out, err = run_hidden(cmd, timeout=20)
        text = (err or out).strip()
        if rc == 0:
            return Attempt("cmd start", target, True, "accepted")
        code, nf = _code_from_text(text)
        return Attempt("cmd start", target, False, f"exit {rc}: {text[:200] or 'no output'}", code=code, not_found=nf)

    def startfile(cand: Candidate, cwd: Optional[str]) -> Attempt:
        target = cand.path or cand.target
        if cand.kind == "storeapp":
            target = f"shell:AppsFolder\\{cand.target}"
        try:
            if cand.args or cwd:
                os.startfile(target, arguments=cand.args or None, cwd=cwd or None)  # type: ignore[call-arg]
            else:
                os.startfile(target)
        except TypeError:
            if cand.args:
                return Attempt("os.startfile", target, False, "this Python cannot pass arguments to os.startfile")
            try:
                os.startfile(target)
            except OSError as exc:
                code = getattr(exc, "winerror", None)
                return Attempt("os.startfile", target, False, f"[WinError {code}] {exc.strerror or exc}", code=code, not_found=code in (2, 3))
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            return Attempt("os.startfile", target, False, f"[WinError {code}] {exc.strerror or exc}", code=code, not_found=code in (2, 3))
        return Attempt("os.startfile", target, True, "accepted")

    def explorer(cand: Candidate, cwd: Optional[str]) -> Attempt:
        target = cand.path or cand.target
        if cand.kind == "storeapp":
            target = f"shell:AppsFolder\\{cand.target}"
        try:
            subprocess.Popen(["explorer.exe", target], close_fds=True)
        except OSError as exc:
            return Attempt("explorer.exe", target, False, str(exc))
        return Attempt("explorer.exe", target, True, "requested (explorer does not report launch errors)")

    def powershell(cand: Candidate, cwd: Optional[str]) -> Attempt:
        target = cand.path or cand.target
        if cand.kind == "storeapp":
            target = f"shell:AppsFolder\\{cand.target}"
        cmd = f"Start-Process -FilePath {_ps_quote(target)}"
        if cand.args:
            cmd += f" -ArgumentList {_ps_quote(cand.args)}"
        if cwd:
            cmd += f" -WorkingDirectory {_ps_quote(cwd)}"
        rc, out, err = run_powershell(cmd, timeout=30)
        if rc == 0:
            return Attempt("PowerShell Start-Process", target, True, "accepted")
        text = (err or out).strip().splitlines()
        first = text[0] if text else "no output"
        code, nf = _code_from_text(err or out)
        return Attempt("PowerShell Start-Process", target, False, f"exit {rc}: {first[:200]}", code=code, not_found=nf)

    return {
        "shellexecute": shellexecute, "runas": runas, "createprocess": createprocess, "cmd_start": cmd_start,
        "startfile": startfile, "explorer": explorer, "powershell": powershell,
    }


class WindowsLaunchEnv(LaunchEnv):  # pragma: no cover - Windows only
    """Resolution helpers backed by the registry, PATH, the Start menu and Get-StartApps."""

    def __init__(self) -> None:
        self._shortcuts: Optional[list[tuple[str, str]]] = None
        self._store: Optional[list[tuple[str, str]]] = None
        self._store_time = 0.0
        self._lock = threading.Lock()

    def app_paths(self, exe: str) -> Optional[str]:
        try:
            import winreg
        except ImportError:
            return None
        sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{}".format(exe)
        sub64 = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{}".format(exe)
        for root, key in ((winreg.HKEY_CURRENT_USER, sub), (winreg.HKEY_LOCAL_MACHINE, sub), (winreg.HKEY_LOCAL_MACHINE, sub64)):
            try:
                with winreg.OpenKey(root, key) as k:
                    value, _ = winreg.QueryValueEx(k, None)
            except OSError:
                continue
            path = os.path.expandvars(str(value).strip().strip('"'))
            if path and os.path.isfile(path):
                return path
        return None

    def system_file(self, name: str) -> Optional[str]:
        windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        dirs = [os.path.join(windir, "System32"), windir, os.path.join(windir, "SysNative"),
                os.path.join(windir, "SysWOW64")]
        dirs += [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        return None

    def shortcuts(self) -> list[tuple[str, str]]:
        with self._lock:
            if self._shortcuts is not None:
                return self._shortcuts
            roots = [
                Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
                Path(os.environ.get("USERPROFILE", "")) / "Desktop",
                Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
            ]
            found: list[tuple[str, str]] = []
            for root in roots:
                if not root.is_dir():
                    continue
                for dirpath, _dirs, files in os.walk(root):
                    for f in files:
                        if f.lower().endswith((".lnk", ".url", ".appref-ms")):
                            found.append((Path(f).stem, str(Path(dirpath) / f)))
            self._shortcuts = found
            return found

    def store_apps(self) -> list[tuple[str, str]]:
        with self._lock:
            if self._store is not None and time.time() - self._store_time < 600:
                return self._store
            apps: list[tuple[str, str]] = []
            rc, out, err = run_powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=25)
            if rc == 0 and out.strip():
                try:
                    data = json.loads(out)
                    if isinstance(data, dict):
                        data = [data]
                    apps = [(str(d.get("Name", "")), str(d.get("AppID", ""))) for d in data if d.get("AppID")]
                except ValueError as exc:
                    log.debug("Get-StartApps output unparsable: %s", exc)
            else:
                log.debug("Get-StartApps failed (rc=%s): %s", rc, (err or out)[:200])
            self._store = apps
            self._store_time = time.time()
            return apps

    def default_browser(self) -> Optional[str]:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
                progid = str(winreg.QueryValueEx(k, "ProgId")[0])
        except Exception:
            return None
        low = progid.lower()
        for marker, exe in (("chromehtml", "chrome.exe"), ("msedgehtm", "msedge.exe"), ("firefoxurl", "firefox.exe"),
                            ("bravehtml", "brave.exe"), ("opera", "opera.exe"), ("vivaldihtm", "vivaldi.exe"),
                            ("ie.http", "iexplore.exe"), ("appxq0fevzme2pys62n3e0fbqa7peapykr8v", "msedge.exe")):
            if marker in low:
                return exe
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as k:
                cmd = str(winreg.QueryValueEx(k, None)[0])
            m = re.search(r"([^\\\"/]+\.exe)", cmd, re.I)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    def uri_registered(self, scheme: str) -> Optional[bool]:
        """HKCR is the merged view of HKLM/HKCU classes, so packaged (Store) protocols show up here too."""
        try:
            import winreg
        except ImportError:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return None

    def installed_apps(self) -> dict[str, str]:
        """{display name: exe} for the KNOWN_APPS present on this machine (App Paths, PATH or a Start-menu shortcut)."""
        found: dict[str, str] = {}
        stems = [stem for stem, _ in self.shortcuts()]
        for display, exe in KNOWN_APPS:
            if self.app_paths(exe) or shutil.which(exe):
                found[display] = exe
                continue
            m = best_match(display, stems)
            if m is not None and m[0] <= 1:
                found[display] = exe
        return found

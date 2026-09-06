# WinAgent

**WinAgent** یک ایجنت هوش مصنوعی (Computer-Use Agent) برای ویندوز است که به هر مدل زبانی سازگار با API استاندارد OpenAI وصل می‌شود (`api_base_url` + `api_key`) و می‌تواند مثل یک کاربر انسانی با کامپیوتر کار کند: برنامه‌ها را باز کند، ماوس و کیبورد را کنترل کند، صفحه‌نمایش را ببیند و تحلیل کند، پنجره‌ها را مدیریت کند، دستورات PowerShell اجرا کند و … . رابط گرافیکی آن یک پنجرهٔ چت است که همراه با «دید ایجنت» (اسکرین‌شات لحظه‌ای) و لاگ اقدامات نمایش داده می‌شود.

**WinAgent** is an LLM-powered computer-use agent for Windows with a chat GUI. It connects to any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, DeepSeek, Gemini's OpenAI-compat endpoint, Ollama, LM Studio, vLLM, …), sees the screen through screenshots and acts with the mouse, keyboard, shell and window manager — everything a human can do on a Windows desktop.

![WinAgent main window](docs/screenshot-main.png)

---

## ویژگی‌ها / Features

| | |
|---|---|
| 🔌 **اتصال به هر مدل زبانی** | فقط `api_base_url`، `api_key` و نام مدل را وارد کنید. با OpenAI، OpenRouter، Groq، DeepSeek، Gemini، Ollama، LM Studio، vLLM و هر سرور سازگار با OpenAI کار می‌کند. |
| 📡 **پروتکل JSON استاندارد** | ارتباط ایجنت و مدل با ابزارهای JSON-Schema انجام می‌شود: پروتکل *native* (OpenAI `tools` / `tool_calls`) و در صورت پشتیبانی‌نشدن، پروتکل *JSON-in-text* به‌صورت خودکار. |
| 👁 **دیدن و تحلیل صفحه** | اسکرین‌شات با شبکهٔ مختصات و نشانگر ماوس برای مدل ارسال می‌شود؛ بزرگ‌نمایی ناحیه‌ای، چند مانیتور و DPI-awareness پشتیبانی می‌شود. مدل‌های بدون قابلیت بینایی هم با اطلاعات متنی پنجره‌ها کار می‌کنند. |
| 🖱 **کنترل کامل ماوس و کیبورد** | کلیک/دابل‌کلیک/راست‌کلیک، درگ، اسکرول، تایپ یونیکد (فارسی، ایموجی…)، کلیدهای ترکیبی (`ctrl+shift+esc`, `win+r`, …) از طریق `SendInput` ویندوز. |
| 🪟 **مدیریت برنامه‌ها و پنجره‌ها** | اجرای برنامه با نام (Notepad, Chrome, Excel, Settings…)، مسیر، منوی استارت یا اپ‌های UWP؛ فوکوس/کوچک/بزرگ/بستن/جابه‌جایی پنجره‌ها؛ فهرست کنترل‌های Win32. |
| 💻 **PowerShell / cmd، فایل و کلیپ‌بورد** | اجرای دستورات با خروجی کامل، خواندن/نوشتن فایل، فهرست پوشه، خواندن/نوشتن کلیپ‌بورد. |
| 🛡 **ایمنی** | تأیید کاربر پیش از دستورات مخرب و بازنویسی فایل، غیرفعال‌کردن shell/نوشتن فایل، سقف تعداد گام، کلید توقف اضطراری سراسری (`Ctrl+Alt+Esc`) و fail-safe گوشهٔ صفحه. |
| 💬 **رابط گرافیکی چت** | پنجرهٔ چت با پشتیبانی راست‌به‌چپ، نمایش استدلال و اقدامات مدل، دید زندهٔ ایجنت، لاگ اقدامات، اطلاعات سیستم و پنجره‌ها، دیالوگ تنظیمات با تست اتصال و دریافت فهرست مدل‌ها. |
| 🧪 **قابل تست بدون ویندوز** | یک بک‌اند شبیه‌سازی‌شده (`--demo`) امکان اجرای GUI و تست کامل حلقهٔ ایجنت را روی هر سیستم‌عاملی می‌دهد؛ ۷۰+ تست خودکار همراه با یک سرور OpenAI ساختگی. |

---

## نصب و اجرا / Installation

پیش‌نیاز: **Windows 10/11** و **Python 3.10+** ([python.org](https://www.python.org/downloads/) – گزینهٔ *Add python.exe to PATH* را فعال کنید).

```bat
git clone https://github.com/alihashemzadeh201-blip/WinAgent.git
cd WinAgent
run.bat
```

`run.bat` در اولین اجرا یک محیط مجازی می‌سازد، وابستگی‌ها را نصب می‌کند، `config.json` را از روی `config.example.json` می‌سازد و رابط گرافیکی را باز می‌کند. سپس از دکمهٔ **⚙ Settings** آدرس API، کلید و نام مدل را وارد کنید و با **Test connection** اتصال را بررسی کنید.

نصب دستی:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m winagent
```

### پیکربندی / Configuration

تنظیمات در `config.json` (کنار برنامه یا در `%APPDATA%\WinAgent\config.json`) ذخیره می‌شوند و از داخل GUI قابل ویرایش‌اند. متغیرهای محیطی بر فایل اولویت دارند:

```powershell
$env:WINAGENT_API_BASE_URL = "https://api.openai.com/v1"
$env:WINAGENT_API_KEY      = "sk-..."
$env:WINAGENT_MODEL        = "gpt-4o-mini"
python -m winagent
```

مهم‌ترین گزینه‌ها:

| کلید | توضیح |
|---|---|
| `api_base_url` | آدرس پایهٔ API سازگار با OpenAI (مثلاً `https://api.openai.com/v1`, `https://openrouter.ai/api/v1`, `http://localhost:11434/v1`) |
| `api_key` | کلید API (برای سرورهای محلی می‌تواند خالی باشد) |
| `model` | نام مدل. برای بهترین نتیجه از یک مدل **دارای بینایی (vision)** استفاده کنید: `gpt-4o`, `gpt-4o-mini`, `claude-3.5-sonnet` (از طریق OpenRouter), `gemini-2.0-flash`, `qwen2.5-vl`, `llama3.2-vision`… |
| `tool_protocol` | `auto` (پیش‌فرض)، `native` (OpenAI tools) یا `json` (پاسخ JSON در متن؛ برای مدل‌های بدون function calling) |
| `vision_enabled` | ارسال اسکرین‌شات به مدل (در صورت عدم پشتیبانی مدل، خودکار خاموش می‌شود) |
| `max_steps` | حداکثر تعداد گام (فراخوانی ابزار) برای هر درخواست |
| `confirm_dangerous_actions` | پرسیدن از کاربر قبل از دستورات مخرب (حذف، فرمت، خاموش‌کردن، …) و بازنویسی فایل |
| `allow_shell_commands`, `allow_file_write` | خاموش‌کردن کامل این قابلیت‌ها |
| `stop_hotkey`, `mouse_failsafe` | کلید توقف اضطراری سراسری و توقف با بردن ماوس به گوشهٔ بالا-چپ |
| `screenshot_max_width`, `screenshot_grid`, … | کیفیت و حاشیه‌نویسی اسکرین‌شات‌ها (برای کاهش هزینهٔ توکن) |
| `response_language` | زبان پاسخ (`auto` = زبان کاربر) |
| `extra_system_prompt` | دستورالعمل‌های اضافی برای ایجنت |

### حالت‌های اجرا

```bat
python -m winagent                      :: رابط گرافیکی
python -m winagent --cli                :: چت در ترمینال
python -m winagent --task "Notepad را باز کن و سلام دنیا بنویس"   :: اجرای یک وظیفه و خروج
python -m winagent --demo               :: GUI با دسکتاپ شبیه‌سازی‌شده (بدون تأثیر روی سیستم)
python -m winagent --init-config        :: ساخت config.json پیش‌فرض
```

---

## نمونه دستورها / Example prompts

- «Notepad را باز کن، متن *سلام دنیا* را بنویس و روی دسکتاپ با نام test.txt ذخیره کن»
- «Chrome را باز کن و قیمت دلار امروز را جستجو کن و نتیجه را بگو»
- «همهٔ پنجره‌ها را کوچک کن و ماشین‌حساب را باز کن»
- «یک اسکرین‌شات بگیر و بگو الان روی صفحه چه چیزی هست»
- «در پوشهٔ Downloads فایل‌های بزرگ‌تر از ۱۰۰ مگابایت را فهرست کن»
- "Open Excel, create a table with three columns Name/Age/City and fill it with 5 sample rows"
- "Take a screenshot of the active window and describe any error messages you see"

---

## معماری / Architecture

```
winagent/
├── agent.py            حلقهٔ ایجنت: مدل → فراخوانی ابزار → اجرا → بازخورد (اسکرین‌شات) → …
├── llm.py              کلاینت HTTP سازگار با OpenAI (retry، تشخیص قابلیت tools/vision)
├── protocol.py         پروتکل پیام: native tool_calls و JSON-in-text، پارس تحمل‌پذیر خطا
├── prompts.py          system prompt (قواعد کار، ایمنی، پروتکل JSON)
├── screenshot.py       مقیاس‌دهی، شبکهٔ مختصات، نشانگر ماوس، تبدیل مختصات مدل ↔ پیکسل فیزیکی
├── keys.py             نرمال‌سازی نام کلیدها (Ctrl/Control/^…) و کدهای virtual-key
├── config.py           پیکربندی JSON + متغیرهای محیطی
├── tools/
│   ├── definitions.py  تعریف ابزارها با JSON Schema (پروتکل مشترک با مدل)
│   └── executor.py     اجرای ابزارها، اعتبارسنجی آرگومان‌ها، سیاست‌های ایمنی
├── backends/
│   ├── base.py         اینترفیس انتزاعی دسکتاپ
│   ├── windows.py      پیاده‌سازی Win32 با ctypes (SendInput، EnumWindows، ShellExecuteEx، clipboard…)
│   └── fake.py         دسکتاپ شبیه‌سازی‌شده برای تست/دمو
└── gui/                رابط PySide6 (پنجرهٔ اصلی، ویجت‌های چت، دیالوگ تنظیمات، تم)
```

### پروتکل ارتباط ایجنت ↔ مدل

هر قابلیت ایجنت یک **ابزار** با نام، توضیح و JSON Schema است (`winagent/tools/definitions.py`). دو قالب انتقال پشتیبانی می‌شود:

**۱. Native (OpenAI function calling)** — ابزارها در فیلد `tools` درخواست ارسال می‌شوند و مدل با `tool_calls` پاسخ می‌دهد؛ نتیجهٔ هر ابزار با نقش `tool` و اسکرین‌شات جدید به‌صورت پیام `user` تصویری برمی‌گردد.

**۲. JSON-in-text** (برای مدل‌های بدون function calling؛ در حالت `auto` به‌طور خودکار انتخاب می‌شود) — مدل باید فقط یک شیء JSON برگرداند:

```json
{"thought": "ابتدا Notepad را باز می‌کنم",
 "actions": [{"tool": "open_app", "args": {"name": "notepad"}}]}
```

و برای پاسخ متنی: `{"message": "..."}`. نتایج به این شکل بازگردانده می‌شوند:

```json
{"tool_results": [{"tool": "open_app", "id": "call_1",
                   "result": {"ok": true, "message": "Launched 'notepad'", "screenshot": "Screenshot 1280x720 px ..."}}]}
```

ابزارهای موجود: `screenshot`, `get_screen_info`, `mouse_move`, `click`, `double_click`, `right_click`, `drag`, `scroll`, `type_text`, `press_keys`, `hotkey_sequence`, `open_app`, `open_url`, `run_command`, `list_windows`, `focus_window`, `window_action`, `get_window_controls`, `clipboard`, `read_file`, `write_file`, `list_directory`, `wait`, `ask_user`, `task_complete`.

مختصاتی که مدل می‌فرستد در فضای آخرین اسکرین‌شات (مقیاس‌شده) است و `ToolExecutor` آن را به پیکسل فیزیکی (با درنظرگرفتن DPI و چند مانیتور) تبدیل می‌کند.

---

## ایمنی / Safety

- ایجنت **کنترل واقعی** ماوس و کیبورد را در دست می‌گیرد؛ هنگام اجرا از کامپیوتر استفاده نکنید.
- دستورات مخرب (`Remove-Item`, `format`, `shutdown`, `reg delete`, …) و بازنویسی فایل‌ها به تأیید شما نیاز دارند (قابل تنظیم).
- **توقف اضطراری:** دکمهٔ Stop، کلید `Esc` در پنجرهٔ برنامه، کلید سراسری `Ctrl+Alt+Esc` یا بردن ماوس به گوشهٔ بالا-چپ صفحه.
- اسکرین‌شات‌ها به ارائه‌دهندهٔ مدل ارسال می‌شوند؛ اگر اطلاعات حساسی روی صفحه است از یک مدل محلی (Ollama/LM Studio) استفاده کنید.
- پنجره‌های با دسترسی Administrator فقط وقتی قابل کنترل‌اند که خود WinAgent هم با دسترسی Administrator اجرا شود.

---

## توسعه / Development

```bash
pip install -r requirements.txt pytest ruff
python -m pytest            # 70+ tests (protocol, executor, agent loop, HTTP end-to-end)
ruff check winagent tests
python -m tests.mock_server --port 8123 [--no-tools]   # a scripted OpenAI-compatible server for manual testing
python -m winagent --demo --api-base-url http://127.0.0.1:8123/v1 --api-key x --model mock
```

روی macOS/Linux با `./run.sh` رابط گرافیکی با دسکتاپ شبیه‌سازی‌شده اجرا می‌شود (بک‌اند واقعی فقط روی ویندوز فعال است).

![Settings dialog](docs/screenshot-settings.png)

## License

MIT

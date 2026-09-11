# Template for a WinAgent skill (not loaded – the leading underscore is ignored)

Copy this file to a NEW file (e.g. `skills/crm-export.md` or `skills/weekly-report.md`) and
fill it in.  Any `.md` / `.txt` file in a skills directory is installed automatically and
appears in the agent's system prompt as "Installed skills".

Skills directories (checked in this order; same-named skills in a later dir override):
  1. <project>/skills/                        (next to the WinAgent folder)
  2. %APPDATA%\WinAgent\skills on Windows     (per user; ~/.config/winagent/skills elsewhere)

Keep it under ~4000 characters.  Write concrete steps, exact control names, shortcuts and
verification points – the agent follows the skill when a task matches it.

--- example content ---

### When to use
The user asks to export invoices from the CRM (or similar).

### Steps
1. open_app "CRM" (wait for the window "Invoices – Acme CRM").
2. Use the `menu` tool: path ["Report", "Invoices", "Export…"] (keyboard only).
3. In the dialog: pick "PDF", folder "C:\Reports\Invoices", then click "Save".
4. Verify on the screenshot: a "Export finished" status line is visible.

### Notes
- The "Export" menu item is greyed out until a date range is chosen in the toolbar.
- If a "license expired" dialog appears, do not click anything – report it in task_complete
  with success=false.

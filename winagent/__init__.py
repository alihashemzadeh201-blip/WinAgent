"""WinAgent – an LLM-powered computer-use agent for Windows.

The agent talks to any OpenAI-compatible chat-completions endpoint
(``api_base_url`` + ``api_key``) using a JSON tool-calling protocol and drives
the Windows desktop: launching programs, moving/clicking the mouse, typing,
taking and analysing screenshots, managing windows, running commands, etc.
"""

__version__ = "0.1.0"
__app_name__ = "WinAgent"

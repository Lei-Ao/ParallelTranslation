<div align="center">

<p>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-%3E%3D3.10-3776AB?logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="#windows"><img src="https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white" alt="Platform: Windows"></a>
  <a href="#configure-the-ai-api"><img src="https://img.shields.io/badge/AI-OpenAI%20compatible-412991" alt="OpenAI-compatible AI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-663366" alt="License: AGPL-3.0-only"></a>
</p>

<h1>ParallelTranslation</h1>

<p><strong>Where understanding meets the page.</strong></p>

<p><sub>Local-first PDF translation workspace · bring your own AI · preserve the page layout</sub></p>

<p><a href="README.md">简体中文</a> · <a href="README.en.md">English</a></p>

</div>

---

A local-first PDF translation workspace with side-by-side reading, layout-aware typesetting, and custom AI APIs. The application displays the name “页间 Parallel”; this repository provides Chinese and English documentation.

## Features

- **Parallel reading**: continuous pages, bidirectional synchronized scrolling, paragraph navigation, zoom, and panning.
- **Desktop launcher**: on Windows, `start.bat` or `start.vbs` starts the app in the system tray without leaving a terminal window open. Closing the browser does not stop the service; the tray menu can open the workbench, restart, stop, or exit.
- **Layout-aware output**: preserve page dimensions and graphics, and typeset translations in safe regions using source typography, column geometry, and paragraph spacing.
- **Preserved links**: web links and in-document jumps are re-anchored to the translated text and stay clickable; link text keeps the source color, and all line fragments of a wrapped link highlight together as one link.
- **Bring your own AI**: OpenAI-compatible Chat Completions APIs or a configurable HTTP JSON agent.
- **Contextual translation**: surrounding paragraphs, completed translations, target language, terminology, and custom instructions.
- **Concurrency and recovery**: configurable parallel requests and same-page batches, pause/resume, bounded retries, and reuse of saved translations.
- **Editing and export**: edit or retranslate individual paragraphs, export bilingual Markdown, and automatically save a standalone translated PDF.

> Layout decisions use document geometry, typography, and structure. Complex regions may retain the complete original with an explanation. Successful translation of every region in every PDF is not guaranteed.

## Quick start

### Windows

1. Install **Python 3.10 or newer** and make sure `python` is available in your terminal.
2. Clone this repository, or use GitHub’s **Code → Download ZIP** and extract the archive.
3. Double-click **`start.bat`** in the project root. The first launch creates `.venv` and installs pinned dependencies (a terminal window is shown briefly during this step).
4. Afterwards, double-click `start.bat` or **`start.vbs`**. No terminal window stays open: a “页间 Parallel” tray icon appears and the workbench opens at <http://127.0.0.1:8765>.

```powershell
git clone https://github.com/Lei-Ao/ParallelTranslation.git
cd ParallelTranslation
.\start.bat
```

Double-click the tray icon to open the workbench. Its context menu offers **打开工作台**, **重新启动服务**, **启动服务 / 停止服务**, and **停止并退出**. Closing the browser does not stop the service; use **停止并退出** in the tray menu, or double-click **`stop.bat`**, to stop it completely. Server logs are written to `tmp/parallel.log`. Launching an already running instance only opens the workbench again.

The tray uses a green double-page icon, with “页间 Parallel” shown on hover. If Windows places it in the hidden area, click `⌃` near the clock and drag the icon onto the taskbar to keep it visible. After updating the icon, exit the old tray controller before launching `start.vbs` again. Browser tabs use the same icon.

Do not open `static/index.html` directly: PDF extraction, persistence, and API requests require the local backend. The current regression environment is Windows / Python 3.12.

### Manual installation

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe launch.py
```

Manual startup on macOS / Linux is also possible, but complete reader behavior and font rendering on those platforms have not been verified:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

Use `requirements-lock.txt` for pinned dependencies. `requirements.txt` records supported version ranges. Node.js is not required to run the application; it is used only for frontend development checks.

## Configure an AI endpoint

Open **Agent 设置** (Agent settings) and select the endpoint type.

### OpenAI-compatible API

- **Base URL**: your provider’s API URL. The app appends `/chat/completions`, unless you enter that full endpoint.
- **API Key**: your own credential. Local endpoints that do not require authentication may leave this blank.
- **Model**: enter the exact model ID provided by your service. No available model is assumed.
- **Test connection**: sends a real model request and may incur API charges.

The current prefilled Base URL is `https://api.apigoto.com/v1`; replace it as needed. This default does not guarantee service availability or quality. Integration tests use simulated endpoints.

### Custom HTTP JSON agent

Configure a full POST URL, a JSON request template, and a response field path. For example:

```json
{
  "input": "{{prompt}}",
  "model": "{{model}}",
  "metadata": { "target_language": "{{target}}" }
}
```

Available variables: `{{prompt}}`, `{{text}}`, `{{target}}`, and `{{model}}`. Response paths can be `data.text` or `choices.0.message.content`. Credentials are sent as `Authorization: Bearer ...`.

Only non-streaming JSON responses completed in one request are supported. SSE, polling, and special authentication require additional adapters.

## Workflow

1. Select a target language on the home page and optionally enter terminology and translation instructions.
2. Import a text-based PDF. The app extracts and translates its text, shows progress, and opens the reader when translation and typesetting finish.
3. Pause when needed. Resume after failures to skip already saved translations in the current target language.
4. Click a paragraph to locate its counterpart; click blank space to clear the selection. Double-click translated text to edit, or select the paragraph editing view from the top-right menu.
5. Use the menu to change language, concurrency, batch size, synchronized scrolling, or reading mode; regenerate the layout; download the PDF; or export bilingual Markdown.

The default is **3 concurrent requests**, configurable up to **12**. OpenAI-compatible endpoints combine up to **3 same-page paragraphs** per request by default, with 1 / 3 / 5 paragraph options. Custom endpoints use individual paragraph requests. Temporary failures receive at most two automatic retries; retries may incur additional charges.

Pausing stops new requests while letting in-flight requests finish and save their results. Keep the page open during translation. After a refresh, reopen a document from the library to resume saved progress.

## Storage and privacy

| Content | Location or behavior |
| --- | --- |
| Source PDFs, extracted text, translations, and layout caches | `.data/<document ID>/` |
| Completed standalone translated PDFs | `output/pdf/` under the project root |
| Agent configuration, API key, language, and terminology settings | The current browser’s localStorage |
| Content sent to the AI service | The current paragraph, bounded text context, terminology, and instructions |

- PDF binaries are not sent to the configured translation endpoint, but relevant text is sent to that service.
- Credentials in browser storage are **not encrypted**. A masked input only hides their display. Use the saved-key removal control to remove a credential.
- Do not put credentials in URLs, request templates, source files, or commits.
- Once all paragraphs are translated and typesetting succeeds, the finished PDF is automatically saved to `output/pdf/` under the project root. The folder is created on the first save, and PDFs can be opened independently. Successful saves and reopening documents do not display a save-path notification; save failures still display an error. Some regions may retain their original content; review the layout messages.
- A document currently stores one translation set. Translating into another language replaces previous translations paragraph by paragraph; export older versions first if needed.
- Back up the entire `.data/` directory to preserve documents. Personal documents, translations, credentials, caches, virtual environments, and local development screenshots are excluded from the repository and source package.

This is a single-user local application. It listens on `127.0.0.1` by default and is not intended to be exposed directly as a public multi-user service.

## Scope and limitations

- PDFs must have **1–300 pages** and be no larger than **50 MB**.
- Scans require OCR beforehand; automatic OCR is not included.
- Columns, rotation, headings, author information, inline symbols, drop caps, and references use general layout rules. Complex documents can still have extraction or typesetting errors.
- A cover title extracted as several text blocks is merged into one title group and typeset with the source title's size and weight. Author affiliation markers stay superscripted regardless of whether they follow the surname directly, a comma, a space, or a footnote symbol. Rotated covers and titles that do not start on the first page still follow the conservative per-block path.
- A bibliography that cannot be rebuilt into complete entries (multi-page lists, unquoted book titles, non-contiguous numbering) is retained as a whole and explained in the layout report, so one entry is never mixed between languages or overwritten by an expanded translation. The heading itself is still translated. Continuation pages without recognizable numbering are treated as ordinary text.
- Web links and in-document jumps (citations, figures, contents) are re-anchored onto the translation. When a rewritten anchor text cannot be located, an external link is no longer clickable and the layout report says so; internal jumps fall back to their original position. Link color is inherited from the source; decorative underlines are not rebuilt. The reader only draws a click overlay for links that can be reliably located or whole-block anchored; jumps kept at their original position remain clickable in the exported PDF.
- A model may return one paragraph as several lines; the layout and reader merge soft line breaks inside one source paragraph (line-structured lists and notation blocks keep their breaks). The edit dialog and saved translation text are not rewritten.
- Display equations, complex tables, vertical text, or text that cannot fit safely may retain the complete original. Tables generally retain their contents while their captions and notes are translated.
- Text inside images is not recognized or replaced. Translation progress counts saved paragraph translations, not the proportion of PDF regions successfully replaced.
- The source is displayed as rendered pages with paragraph interactions. Free text selection, full-text search, and embedded PDF annotations are not implemented in the source reader.
- Chinese typesetting first looks for locally installed Noto Serif SC or DengXian fonts, then uses a built-in fallback. Appearance may vary across systems.
- Review generated content and retained regions, especially equations, tables, important numbers, and technical terminology.

## Development

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_translation_queue.js tests/test_layout_updates.js tests/test_open_flow.js tests/test_reader_sync.js tests/test_reader_zoom.js tests/test_continuous_scroll.js tests/test_links_ui.js
```

Tests use temporary PDFs and simulated endpoints, without consuming real AI service credits. An optional audit of documents already translated on your machine is available:

```powershell
.\.venv\Scripts\python.exe tests/audit_corpus_layout.py
```

It writes reports and previews to `tmp/source-layout-audit/`. Personal paper samples are not distributed with the repository, so a fresh clone has none. Implementation notes and regression boundaries are documented in [the Chinese development guide](docs/development.zh-CN.md). General compatibility requirements are in [AGENTS.md](AGENTS.md).

```text
app.py                  FastAPI, persistence, agent adapters, exports
launch.py               Local startup and browser launch
start.bat / start.vbs   One-click Windows launch (hidden console, tray control)
stop.bat                Stop the local server and tray
tray.ps1                Windows tray menu (open/restart/stop/exit)
pdf_text.py             Drop-cap recovery and complete words
pdf_layout.py           Typesetting, obstacle handling, page caches
pdf_flow.py             Column reflow and equation relocation
pdf_lists.py            List reconstruction, numbering, hanging indents
pdf_typography.py       Font subsetting, weights, symbol fallback
pdf_references.py       Reference reconstruction and title translation
static/                 HTML, CSS, JavaScript without a build step
tests/                  Python and JavaScript regression tests
docs/                   Implementation and regression notes
```

When reporting a problem, include your operating system, Python version, reproduction steps, and a minimal PDF sample you are allowed to share. Do not submit credentials or private documents.

## License

Copyright (C) 2026 Lei-Ao. This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0-only)**. See [LICENSE](LICENSE) for the full terms. The software is provided without warranty.

Commercial use, modification, and redistribution are permitted subject to the license's source-availability and notice requirements. A modified version offered to users over a network must also offer its corresponding source as required by the license.

Third-party dependencies and fonts retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The project license does not grant rights to input papers, translated content, or third-party services.

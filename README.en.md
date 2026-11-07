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

<p><a href="README.md">简体中文</a> · <a href="README.en.md">English</a> · <a href="https://github.com/Lei-Ao/ParallelTranslation/graphs/contributors"><strong>Contributors</strong></a></p>

</div>

---

A local-first PDF translation workspace with side-by-side reading, layout-aware typesetting, and custom AI APIs. The application displays the name “页间 Parallel”; this repository provides Chinese and English documentation.

## Features

- **Parallel reading**: continuous pages, bidirectional synchronized scrolling, paragraph navigation, zoom, and word/phrase selection copying.
- **Desktop launcher**: on Windows, `start.bat` or `start.vbs` starts the app in the system tray without leaving a terminal window open. Closing the browser does not stop the service; the tray menu can open the workbench, restart, stop, or exit.
- **Layout-aware output**: preserve page dimensions and graphics, and typeset translations in safe regions using source typography, column geometry, and paragraph spacing.
- **Preserved links**: web links and in-document jumps are re-anchored to the translated text and stay clickable; link text keeps the source color, and all line fragments of a wrapped link highlight together as one link.
- **Bring your own AI**: OpenAI-compatible Chat Completions APIs or a configurable HTTP JSON agent.
- **Contextual translation**: surrounding paragraphs, completed translations, target language, terminology, and custom instructions.
- **Concurrency and recovery**: configurable parallel requests and same-page batches, pause/resume, bounded retries, and reuse of saved translations.
- **Editing and export**: edit or retranslate individual paragraphs, export bilingual Markdown, and automatically save a standalone translated PDF.
- **Full-paper visual reading**: open papers directly, browse the outline, and use full-paper analysis or section-by-section guided reading with the complete source text and all page images.
- **Citation Q&A**: select source or translated text to explain, quote, or highlight, including multiple selections and cross-page citations, with figure and formula region inspection.
- **Local notes**: save complete answers with questions, formulas, citations, and visual sources, then review or export them as Markdown.

> Layout decisions use document geometry, typography, and structure. Complex regions may retain the complete original with an explanation. Successful translation of every region in every PDF is not guaranteed.

## Changelog

### 2026-11-07 Page-spanning paragraphs, formula integrity, and performance

- **Paragraphs split across pages or columns:** paragraphs cut at a page or column boundary are detected automatically. The translated fragment at the page bottom is stretched within the source fragment's height to remove large gaps. The translation prompt asks the model to continue the previous fragment without repeating words already translated, and forbids inventing an ellipsis when the source has none, retrying once when needed.
- **Inline two-dimensional formulas:** when a fraction or superscript is split across extraction blocks, the whole formula is reproduced from its original glyphs (for example `e^{−‖x‖/δ}`). If it cannot be reproduced reliably, the complete source paragraph is kept and the reason is reported instead of printing a damaged formula such as `e δ`.
- **Layout performance:** duplicate full-page extraction and preflight work were removed from the layout pipeline. On the 14-page / 339-block sample, cold layout dropped from 6.14s to 4.82s with a warm font cache, one-block edits from 0.74s to 0.48s, and a single-page `typeset` from 0.34s to 0.22s. The layout report is field-for-field identical; the cache version is 75.
- **Reading and annotations:** source, translation, and paragraph views all support selecting text for citations. Colored highlights, cross-page quotations, an annotation summary, and local notes were added, while direct reading, full-paper visual reading, and section-by-section guided reading are preserved.
- **Branding and engineering:** the application, tray, and browser-tab icon now share a green book with a translation arrow. New modules and regression tests cover page-spanning paragraphs, figure labels, hanging lists, and the character selection layer.
- **Validation:** 333 Python and 80 JavaScript cases pass, together with an Edge browser smoke test. Two real samples were re-typeset with no missing Chinese characters, text collisions, font changes, bold-body errors, symbol errors, or link errors. The page-bottom leading fill makes the first-line gap of seven continuation paragraphs 0.2–0.6 pt smaller than the source reference; the corpus audit reports this as `spacing_errors`, which is an intentional leading adjustment to reclaim blank space, not a collision. Passing these samples does not mean every complex region in every paper has been adapted.

### 2026-11-06 Multimodal reading, contextual citations and local notes

- Analyze the complete original text, outline and all page images together using one multimodal model, with page-preparation progress and up to four rounds of targeted inspection.
- Add a dedicated citation workflow that reads complete paragraphs, nearby context and cross-referenced definitions, with original and translated PDF version mapping.
- Save complete answers and source snapshots as independent local notes, with Markdown export, individual answer deletion and conversation clearing.
- Standardize article-style responses and math formatting. Preserve LaTeX escapes through Markdown parsing to fix red formula-rendering errors.
- Support direct reading, original/translation view switching, multi-selection citations, local highlights and responsive reading panels.

Validation covers 277 Python tests, 73 JavaScript tests and browser regressions, plus isolated copies of three stored documents from two real papers totaling 36 pages. Real samples mainly use two columns. Synthetic tests cover single-column pages, scanned pages, varying sizes and all four rotations. Model-flow tests use simulated responses. Real multimodal understanding and adherence to the new output rules still require evaluation. See the detailed reading documentation. [Reading guide in Chinese](docs/skill-reading.zh-CN.md)

### 2026-11-06 Translation and layout speed

Batched translation now defaults to 5 blocks / 12,000 characters, keeps valid partial results, and no longer aborts the whole run for one failed paragraph. Parsed documents are cached in memory, untranslated documents skip typesetting, and merged redactions, page-image caching and a disk font cache remove repeated work. A follow-up pass removed duplicate page extraction and preflight work from the layout engine. On the 14-page / 339-block sample, cold layout dropped from 24.36s to 6.80s with an empty font cache (4.82s when warm), one-block edits from 1.88s to 0.48s, and grouped requests from 119 to 74. The layout report is field-for-field identical, so the cache version stays 68; see the [development notes in Chinese](docs/development.zh-CN.md).

### 2026-11 Layout and typography fixes

This repository improves on the upstream project [Lei-Ao/ParallelTranslation](https://github.com/Lei-Ao/ParallelTranslation); the improved version lives at [XIA-2005/ParallelTranslation](https://github.com/XIA-2005/ParallelTranslation). Compared with the original, this round fixes the following layout issues:

1. **Misplaced figure labels**: standalone tick and legend labels near graphics are separated from the prose flow and typeset at their original axis, font size, and baseline instead of displacing body text.
2. **Lost abstract bold**: a long bold label mixed with regular prose no longer bolds the whole paragraph; truly bold body text keeps its source weight, so abstract headings and prose are distinguished correctly.
3. **Shrunk short body text**: a paragraph shrunk by a tight local text box can borrow unused source height left by a shorter translation of the next paragraph in the same column, restoring its original size.
4. **Misaligned author blocks**: authors, affiliations, and emails on the cover page are pre-typeset as one region, keeping affiliation marks as superscripts and emails in a monospaced face instead of drifting downward line by line.
5. **Lost partial bold in captions and wrapped headings**: bold caption titles after figure/table numbers map to the matching first sentence of the translation, and bold labels spanning wrapped lines (including line-end hyphenation) are recognized continuously without bolding the following regular prose.
6. **Incomplete hover highlighting**: merged fragments of a source paragraph or equation keep their same-page group membership, so the reader highlights and selects the whole group on both sides instead of only the last few words or one equation line.
7. **Multiple selection frames around one equation**: a merged equation group draws one bounding frame and tint per side, instead of several small frames or an overly dark overlapping tint.
8. **Lost subheading italics and uniform bolding**: italic and weight follow the source font flags independently; numbering or heading structure no longer triggers uniform bolding. Chinese text gets synthesized 12° oblique faces from the embeddable font family.
9. **Headings typeset separately from body**: when the extractor merges a heading into a body block, the layout detects the structural boundary from source line geometry and typesets both parts separately, restoring a standalone centered heading with its own size, weight, italic, and spacing.
10. **Equation labels move with the equation**: punctuation and labels extracted into the following prose are split off and moved together with the original formula as vector content, keeping the label on the right at its original relative position.

The layout cache version advances from 61 to 66: reopen a document or run "Re-typeset" to apply, with no retranslation needed; saved translations, paragraph IDs, and the source PDF are unchanged. Regression tests cover synthetic single/double-column layouts, various page widths, 0/90/180/270° rotations, and local paper samples; complex regions may still keep the original text with a stated reason.

## Quick start

### Windows

1. Install **Python 3.10 or newer** and make sure `python` is available in your terminal.
2. Clone this repository, or use GitHub’s **Code → Download ZIP** and extract the archive.
3. Double-click **`start.bat`** in the project root. The first launch creates `.venv` and installs pinned dependencies (a terminal window is shown briefly during this step).
4. Afterwards, double-click `start.bat` or **`start.vbs`**. No terminal window stays open: a “页间 Parallel” tray icon appears and the workbench opens at <http://127.0.0.1:8765>.

```powershell
git clone https://github.com/XIA-2005/ParallelTranslation.git
cd ParallelTranslation
.\start.bat
```

Double-click the tray icon to open the workbench. Its context menu offers **打开工作台**, **重新启动服务**, **启动服务 / 停止服务**, and **停止并退出**. Closing the browser does not stop the service; use **停止并退出** in the tray menu, or double-click **`stop.bat`**, to stop it completely. Server logs are written to `tmp/parallel.log`. Launching an already running instance only opens the workbench again.

The tray uses a green double-page book with a translation arrow; hovering shows “页间 Parallel”. If Windows places it in the hidden area, click `⌃` near the clock and drag the icon onto the taskbar. After updating the icon, exit the old tray controller before launching `start.vbs` again. Browser tabs use the same icon.

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
- **Reasoning level**: controls thinking mode for OpenAI-compatible endpoints. “Provider default” sends no reasoning field at all; “Off” sends `thinking: {"type": "disabled"}`; Low / Medium / High / Max send the matching `reasoning_effort`. DeepSeek enables thinking at high effort by default, so switching it off saves time and cost, while raising it slows requests and may exceed the request timeout. Custom endpoints ignore this setting and follow their request template.
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

### Direct reading and the paper assistant

Choose **导入后直接阅读** before importing, or click **直接阅读** in the library to open a paper immediately. In the reader, open **论文助手**. Choose **文章速读** for a full-paper analysis, **逐节陪读** to work through a section, or select text in either pane to ask about it. Answers link back to their sources. Save an answer as a note to keep it after clearing the conversation or export it as Markdown.

Opening the panel or browsing the outline is local. Starting an AI task uses your configured endpoint and may incur charges. Full-paper reading sends all extracted text, the outline, and an image of every page. It requires a model with image input, tool calls, and sufficient capacity. Restart the service and refresh the reader after updating. [Reading guide in Chinese](docs/skill-reading.zh-CN.md)

## Storage and privacy

| Content | Location or behavior |
| --- | --- |
| Source PDFs, extracted text, translations, and layout caches | `.data/<document ID>/` |
| Completed standalone translated PDFs | `output/pdf/` under the project root |
| Agent configuration, API key, language, and terminology settings | The current browser’s localStorage |
| Reading sessions and notes | `reading.json` and `reading-notes.json` in the document directory. Highlights are stored in the current browser. |
| Content sent to the AI service | Translation uses paragraphs, bounded context, terminology, and instructions. Reading can also send source text, page or region images, questions, and relevant conversation history. |

- PDF binaries are not sent to the configured translation endpoint, but relevant text is sent to that service.
- Credentials in browser storage are **not encrypted**. A masked input only hides their display. Use the saved-key removal control to remove a credential.
- Do not put credentials in URLs, request templates, source files, or commits.
- Once all paragraphs are translated and typesetting succeeds, the finished PDF is automatically saved to `output/pdf/` under the project root. The folder is created on the first save, and PDFs can be opened independently. Successful saves and reopening documents do not display a save-path notification; save failures still display an error. Some regions may retain their original content; review the layout messages.
- A document currently stores one translation set. Translating into another language replaces previous translations paragraph by paragraph; export older versions first if needed.
- Back up the entire `.data/` directory to preserve documents. Personal documents, translations, credentials, caches, virtual environments, and local development screenshots are excluded from the repository and source package.

This is a single-user local application. It listens on `127.0.0.1` by default and is not intended to be exposed directly as a public multi-user service.

## Scope and limitations

- PDFs must have **1–300 pages** and be no larger than **50 MB**.
- Scans require OCR for translation and text selection; full-paper visual reading can inspect page images.
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

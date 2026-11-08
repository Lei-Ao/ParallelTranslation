# Third-party notices / 第三方依赖说明

Dependencies keep their own licenses. Versions below match `requirements-lock.txt`.

以下依赖保留各自许可，其条款不因本仓库的许可证而改变。版本对应 `requirements-lock.txt`。

| Dependency | Pinned version | Upstream license | Project |
| --- | --- | --- | --- |
| PyMuPDF / MuPDF | 1.28.2 | GNU AGPL v3 or an Artifex commercial license | <https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright> |
| FastAPI | 0.143.0 | MIT | <https://github.com/fastapi/fastapi> |
| Uvicorn | 0.54.0 | BSD-3-Clause | <https://github.com/Kludex/uvicorn> |
| HTTPX | 0.28.1 | BSD-3-Clause | <https://github.com/encode/httpx> |
| python-multipart | 0.0.32 | Apache-2.0 | <https://github.com/Kludex/python-multipart> |
| FontTools | 4.67.0 | MIT; external components retain their notices | <https://github.com/fonttools/fonttools> |

## PyMuPDF / MuPDF

The installed PyMuPDF distribution declares: “Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License”. Use, redistribution, and deployment must comply with the applicable upstream terms. ParallelTranslation's license does not remove PyMuPDF's requirements or grant an Artifex commercial license.

PyMuPDF 的发行包声明采用 GNU AGPL v3 / Artifex 商业双授权。使用、再分发或部署时，需遵守适用的上游条款。本项目的许可证不解除 PyMuPDF 的要求，也不授予 Artifex 商业授权。

## Other dependencies / 其他依赖

Transitive dependencies, including the other packages in `requirements-lock.txt`, retain their package-specific licenses. Their distributions include authoritative license and copyright files. Python dependencies are installed from their packages.

间接依赖（包括 `requirements-lock.txt` 中的其他包）保留各自许可。具体条款及版权声明以各包发行文件为准。Python 依赖由包管理器安装。

## Paper assistant / 论文助手

These reading guides and browser assets are distributed with the application and served locally.

| Component | License and attribution |
| --- | --- |
| [paper-reading-guide](https://github.com/fuxiao13/paper-reading-guide) | MIT, © 2026 拂晓 (fu xiao). The contribution includes the upstream [license](skills/paper-reading-guide/LICENSE) and [source manifest with SHA-256 hashes](skills/paper-reading-guide/UPSTREAM.json). |
| [marked](https://github.com/markedjs/marked) 15.0.12 | MIT. [Bundled license](static/vendor/marked-LICENSE) |
| [DOMPurify](https://github.com/cure53/DOMPurify/releases/tag/3.4.17) 3.4.17 | Cure53 and contributors. [Apache-2.0](static/vendor/purify-LICENSE) or [MPL-2.0](static/vendor/purify-LICENSE-MPL). |
| [KaTeX](https://github.com/KaTeX/KaTeX/releases/tag/v0.18.2) 0.18.2 | The renderer uses the [MIT license](static/vendor/katex/LICENSE). The packaged math fonts carry separate copyright and reserved-name notices under the [SIL Open Font License 1.1](static/vendor/katex/fonts/OFL.txt). |

## Fonts / 字体

No system font files are redistributed in this repository. The app may read locally installed Noto Serif SC or Microsoft DengXian fonts, subset them, and embed supported glyphs into generated PDFs. Relevant font licenses and embedding permissions still apply. PyMuPDF's built-in fonts retain their upstream terms.

本仓库不分发系统字体。应用可能读取本机的 Noto Serif SC 或微软等线字体，对字形做子集处理并嵌入生成的 PDF。相关字体许可与嵌入权限仍然适用；PyMuPDF 内置字体资源也保留其上游条款。

## Translation workflow references / 翻译工作流参考

The document-quality workflow was independently implemented after studying
[BabelDOC](https://github.com/funstory-ai/BabelDOC) (AGPL-3.0),
[PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) (AGPL-3.0), and
[translation-agent](https://github.com/andrewyng/translation-agent) (MIT).
Their runtime packages and source files are not bundled by this change. The examined local file hashes,
adopted mechanisms, and evaluation limitations are recorded in [translation-quality.zh-CN.md](docs/translation-quality.zh-CN.md).

## Documents and services / 文档与服务

Input PDFs, their content, translations, and third-party AI services are not licensed by this notice. This archive does not include personal papers, translations, credentials, or local development screenshots.

输入 PDF、文档内容、译文及第三方 AI 服务不因本说明而获得授权。源码包不包含私人论文、译文、密钥或本机开发截图。

## Document structure and visual API / 文档结构与视觉接口

BabelDOC 0.7.1 (AGPL-3.0) informed the typed intermediate representation and formula/style placeholder design in `pdf_document.py` and `pdf_translation.py`. The final implementation uses the user's configured translation API for layout detection and visual acceptance. It does not bundle or use BabelDOC runtime packages, Rust extensions, DocLayout weights, or ONNX inference. This project remains AGPL-3.0-only. Reference file hashes and provenance are recorded in [babeldoc-reference.json](docs/babeldoc-reference.json).

Playwright is used only for optional developer browser tests, not added as an application dependency.

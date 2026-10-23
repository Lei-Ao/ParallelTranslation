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

Transitive dependencies, including the other packages in `requirements-lock.txt`, retain their package-specific licenses. Their distributions include authoritative license and copyright files. Dependencies are installed from Python packages rather than vendored into this source archive.

间接依赖（包括 `requirements-lock.txt` 中的其他包）保留各自许可。具体条款及版权声明以各包发行文件为准。源码包不捆绑虚拟环境或第三方库副本，运行时通过 Python 包安装。

## Fonts / 字体

No system font files are redistributed in this repository. The app may read locally installed Noto Serif SC or Microsoft DengXian fonts, subset them, and embed supported glyphs into generated PDFs. Relevant font licenses and embedding permissions still apply. PyMuPDF's built-in fonts retain their upstream terms.

本仓库不分发系统字体。应用可能读取本机的 Noto Serif SC 或微软等线字体，对字形做子集处理并嵌入生成的 PDF。相关字体许可与嵌入权限仍然适用；PyMuPDF 内置字体资源也保留其上游条款。

## Documents and services / 文档与服务

Input PDFs, their content, translations, and third-party AI services are not licensed by this notice. This archive does not include personal papers, translations, credentials, or local development screenshots.

输入 PDF、文档内容、译文及第三方 AI 服务不因本说明而获得授权。源码包不包含私人论文、译文、密钥或本机开发截图。

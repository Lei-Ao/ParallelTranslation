# ParallelTranslation

**简体中文** | [English](README.en.md)

本地 PDF 对照翻译工作台。接入自己的 AI 接口，在原文与译文之间同步阅读，并尽可能保留页面版式。

界面中的名称为「页间 Parallel」。目前界面使用中文，项目文档提供中英文版本。

## 功能

- **对照阅读**：原文与译文并排展示，支持连续翻页、双向同步滚动、段落定位、缩放与拖动。
- **版式排印**：保留页面尺寸和图形，在安全区域内排印译文，参考原文的字体样式、段间距与栏结构。
- **自带 AI**：支持 OpenAI 兼容的 Chat Completions 接口，也可配置自定义 HTTP JSON Agent。
- **上下文翻译**：携带前后段落、已完成译文、目标语言、术语表和翻译要求。
- **并发与恢复**：可调请求并发和同页批量翻译；支持暂停、失败重试及跳过已有译文。
- **修订与导出**：支持逐段编辑、重新翻译、导出对照 Markdown，以及自动保存独立译文 PDF。

> 项目针对文档的几何、字体及版式特征做通用判断。复杂区域可能保留原文并给出原因，不能保证所有 PDF 的每个区域都能完成翻译排版。

## 快速开始

### Windows

1. 安装 **Python 3.10 或更新版本**，确保终端可以运行 `python`。
2. 克隆仓库，或通过 GitHub 的 **Code → Download ZIP** 下载并解压。
3. 双击项目根目录的 **`start.bat`**。
4. 首次启动会创建 `.venv` 并安装固定版本依赖。服务就绪后自动打开 <http://127.0.0.1:8765>。

```powershell
git clone https://github.com/Lei-Ao/ParallelTranslation.git
cd ParallelTranslation
.\start.bat
```

使用期间保留启动窗口，关闭服务按 **Ctrl+C**。再次启动会优先打开已运行的工作台。

不要直接打开 `static/index.html`；PDF 解析、译文保存与接口调用需要本机后端服务。当前测试环境为 Windows / Python 3.12。

### 手动安装与启动

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe launch.py
```

macOS / Linux 可使用手动启动方式；这些系统的完整阅读及字体效果尚未验证：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

`requirements-lock.txt` 用于安装已固定的依赖版本；`requirements.txt` 记录允许的版本范围。运行应用不需要 Node.js，Node.js 仅用于前端开发检查。

## 配置 AI 接口

打开「Agent 设置」，选择接口类型。

### OpenAI 兼容接口

- **Base URL**：填写服务商的 API 地址。程序会追加 `/chat/completions`，也支持填写完整端点。
- **API Key**：填写自己的密钥；无需认证的本机服务可以留空。
- **模型名称**：按服务商实际提供的模型 ID 填写，项目不预设可用模型。
- **测试连接**：发送一次真实模型请求，可能产生接口费用。

界面当前预填的 Base URL 是 `https://api.apigoto.com/v1`，可按需更换。该预填地址不代表服务可用性或质量保证；集成测试使用模拟接口。

### 自定义 HTTP JSON Agent

填写完整 POST 地址、JSON 请求模板和译文响应字段路径，例如：

```json
{
  "input": "{{prompt}}",
  "model": "{{model}}",
  "metadata": { "target_language": "{{target}}" }
}
```

支持 `{{prompt}}`、`{{text}}`、`{{target}}`、`{{model}}`。响应字段路径可填写 `data.text` 或 `choices.0.message.content`。密钥通过 `Authorization: Bearer ...` 发送。

目前仅支持一次请求完成的非流式 JSON 响应；SSE、异步轮询及特殊认证需另行适配。

## 使用流程

1. 在首页选择目标语言，按需填写「术语与要求」。
2. 导入文字型 PDF。程序解析后自动翻译，首页显示进度；全文译完并完成排版后进入阅读界面。
3. 翻译期间可暂停；失败后点击「继续翻译」，跳过已有当前语言译文。
4. 两侧单击段落可同步定位，双击译文文字可编辑。也可在右上角菜单切换「段落（编辑）」视图。
5. 在菜单中调整语言、并发、批量段数、同步滚动与阅读模式，或重新排版、下载 PDF、导出对照 Markdown。

默认并发 **3** 个请求，最高 **12** 个；OpenAI 兼容接口默认每次合并同页最多 **3** 段，可选 1 / 3 / 5 段。自定义接口使用单段请求。接口临时错误最多自动重试两次，重试可能产生额外费用。

暂停会停止安排新请求，已发送请求会继续返回并保存。翻译期间请保持页面打开；刷新后可以从文档库重新打开并继续。

## 保存与隐私

| 内容 | 保存位置或行为 |
| --- | --- |
| 原始 PDF、提取文本、译文与布局缓存 | `.data/<文档 ID>/` |
| 完成的独立译文 PDF | 项目根目录下的 `output/pdf/` |
| Agent 配置、API Key、语言及术语设置 | 当前浏览器的 localStorage |
| 发给 AI 服务的内容 | 当前段及受限长度的文本上下文、术语表和翻译要求 |

- PDF 二进制文件不会上传给配置的翻译接口，但相关文本会发送给该服务。
- 浏览器中的密钥存储**未加密**；输入框隐藏仅遮挡显示。可使用「清除已保存密钥」移除密钥。
- 不要把密钥写入 URL、请求模板、源码或提交记录。
- 完整翻译并排版成功后，成品 PDF 自动保存到项目根目录下的 `output/pdf/` 文件夹（首次保存时自动创建），可脱离应用直接打开。保存成功或重新打开文档时，不再弹出保存路径；保存失败仍会提示。排版仍可能包含保留原文的区域，请查看排版提示。
- 每份文档当前仅保存一套译文；切换语言并重新翻译会逐段替换原有译文，需要旧版本时请先导出。
- 备份数据可复制整个 `.data/`。仓库及源码包不包含个人文档、译文、密钥、缓存、虚拟环境或本机开发截图。

本项目是单用户本地工具，默认只监听 `127.0.0.1`，不适合直接部署为公开的多用户服务。

## 支持范围与限制

- 支持 **1–300 页、最大 50 MB** 的 PDF。
- 扫描件需要先进行 OCR；本项目不提供自动 OCR。
- 多栏、旋转、标题、作者信息、行内符号、首字下沉和参考文献等使用版式规则处理，复杂文档仍可能存在提取或排版误差。
- 独立公式、复杂表格、竖排文字或无法安全容纳的区域可能保留完整原文；表格通常只翻译表题和表注。
- 图片内的文字不进行 OCR 或替换。翻译进度表示已生成的段落译文比例，不代表所有区域均已替换进 PDF。
- 原文以渲染页面显示，支持段落交互，暂不支持原文自由选字、全文搜索或 PDF 内嵌批注。
- 中文排印会优先查找本机 Noto Serif SC、等线字体；没有可用字体时使用内置回退。不同系统的字体效果可能不同。
- 生成内容和保留区域应人工审核，特别是公式、表格、关键数字和专业术语。

## 开发

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_translation_queue.js tests/test_layout_updates.js tests/test_open_flow.js tests/test_reader_sync.js tests/test_reader_zoom.js tests/test_continuous_scroll.js
```

测试使用临时 PDF 与模拟接口，不消耗真实 AI 服务额度。针对本机已有译文的可选排版检查：

```powershell
.\.venv\Scripts\python.exe tests/audit_corpus_layout.py
```

该检查在 `tmp/source-layout-audit/` 写入报告和预览。个人论文样本不随仓库分发；新克隆的项目没有这些样本。排版实现和回归边界见 [开发与排版说明](docs/development.zh-CN.md)，通用适配要求见 [AGENTS.md](AGENTS.md)。

```text
app.py                  FastAPI、文档保存、Agent 适配与导出
launch.py               本机服务启动与浏览器打开
pdf_text.py             首字下沉与完整单词恢复
pdf_layout.py           PDF 排版、图表避让与按页缓存
pdf_flow.py             分栏重排与公式区域移动
pdf_typography.py       字体子集、字重与符号回退
pdf_references.py       参考文献恢复、题名翻译与排版
static/                 无构建依赖的 HTML、CSS、JavaScript
tests/                  Python / JavaScript 回归测试
docs/                   实现与回归说明
```

提交问题时请说明系统、Python 版本和复现步骤，并提供有权分享的最小 PDF 样本。请勿提交密钥或私人文档。

## 开源协议

Copyright (C) 2026 Lei-Ao. 本项目以 **GNU Affero General Public License v3.0（AGPL-3.0-only）** 发布，完整条款见 [LICENSE](LICENSE)。软件按现状提供，不附带担保。

允许商用、修改和再分发，但需遵守相应的源码提供与版权保留要求；通过网络提供修改版服务时，也须按协议向服务用户提供相应源码。

第三方依赖和字体保留各自许可，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目的开源许可不授予输入论文、译文内容或第三方服务的使用权。

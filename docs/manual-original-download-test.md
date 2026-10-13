# 原始主文 PDF / SI：提交前手动验收

适用于当前源码中的 `download_paper_pdfs(query, output_dir, include_si=True)`。这份清单是待执行方案，**不是测试通过记录**；不涉及发布版本或修改 CI。

> 当前原格式 SI 功能仍有已知缺陷，**暂时不能承诺完整可用**。ACS 有常规 MCP 主文＋SI 成功样例，但部分附件仍受 challenge 限制；Wiley 主文可用、SI 尚未通过。`texmath 0.13.3` 的本机验证见第 6 节。论文、运行日志与本地报告不纳入提交，也不能把 `partial`、配置阻塞或 skip 当作全部通过。

## 当前已知缺陷与使用限制

- **Wiley 尚未修好**：真实 MCP 诊断显示主文阶段约 119.7 秒后共享 120 秒预算已耗尽，SI 在导航前失败（`request_deadline_exceeded`）。这不是该次附件网络请求失败；主文内部 warm/HTTP/browser 耗时原因仍需细分。不用加超时、重置预算或原样重试掩盖。
- **ACS 合法访问限制**：已有新样例完整下载，但旧目标 SI 曾返回 Cloudflare challenge。新样例成功不能证明旧目标或所有论文均恢复。
- `partial/overall=degraded` 表示已保留主文但请求的 SI 未完成；discovery 的 `failed/unsupported` 不等于 `not_found`，不要误报没有附件。
- 非 PDF/多附件 SI、真实宿主取消及原生 macOS 的新增 live 验收仍有未执行项；受控回归不代替这些证据。只需要主文可传 `include_si=false`；旧 `fetch_paper` 阅读路线不被替换。
- Elsevier 缺 key 的成功路线未验证，本轮不处理其 key/权限配置。声明限制后提交功能分支不等于通过全部门禁，不创建 release 或发布包。

维护者本地证据：`papers/si-repair-20261013T010246Z/REPORT.md`、`papers/mcp-normal-20261012T235055Z/REPORT.md`，历史下载见 `papers/live-smoke/REPORT.md`，公式工具验证见 `papers/texmath-validation/REPORT.md`。这些 ignored 记录不会随源码分发；上述公开限制不依赖读者持有本地记录。

## 1. 准备环境与独立输出目录

以下命令用于 Linux/macOS Bash；从仓库根目录执行：

```bash
uv sync --frozen --extra dev --extra full
export PYTHONPATH="$PWD/src"
export PF_MANUAL_ROOT="$PWD/papers/manual-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$PF_MANUAL_ROOT"
printf '本次验收目录：%s\n' "$PF_MANUAL_ROOT"
uv run --no-sync python - <<'PY'
from pathlib import Path
import paper_fetch.service as service
from paper_fetch.mcp.server import build_server
print('service:', Path(service.__file__).resolve())
print('download entry:', service.download_paper_pdfs.__name__)
print('MCP server:', type(build_server()).__name__)
PY
```

**通过标准**：导入路径位于当前 checkout，依赖来自锁文件；不是另一个旧安装副本。每轮使用新目录，不删除或覆盖之前的论文文件。这里只准备已有声明的依赖，不新增依赖、修改锁文件或安装无关工具。

### 1.1 配置与合法访问

```bash
uv run --no-sync paper-fetch doctor --provider acs --detail full --json
uv run --no-sync paper-fetch doctor --provider wiley --detail full --json
uv run --no-sync paper-fetch doctor --provider elsevier --detail full --json
```

- `doctor` 只证明本地配置，不证明出版社可访问。首次缺 managed Camoufox 时，正常下载会按已有机制准备；准备失败应记录为环境阻塞，不换浏览器 backend 强行通过。
- Elsevier 必须有合法的 `ELSEVIER_API_KEY` 及对应全文权限；开放获取页面不等于匿名 API 权限。使用已有私有配置，或在自己的终端隐藏输入：

  ```bash
  read -rsp 'ELSEVIER_API_KEY: ' ELSEVIER_API_KEY; printf '\n'
  export ELSEVIER_API_KEY
  ```

- 若用私有 env 文件，显式设置 `PAPER_FETCH_ENV_FILE=/absolute/path/to/private.env`；仓库 `.env` **不会隐式加载**。不要把 key、Cookie、storage-state、私有 env 文件提交或发到聊天中。
- 浏览器明确要求人工认证时，才由你运行已有入口，例如：

  ```bash
  uv run --no-sync paper-fetch auth wiley \
    --url 'https://chemistry-europe.onlinelibrary.wiley.com/doi/10.1002/open.202500340'
  ```

  自己完成合法操作；不绕过付费墙/验证码。配置或登录态变化后，重启 MCP 宿主的 server，使其使用正确环境。
- 同一论文、同一意图最多初次＋两次有意义重试；记录参数/状态变化。不要立即原样重复，更不要关闭身份、文件、URL 或预算校验。

## 2. 确认真实 MCP 入口

在 MCP 宿主中让 server 指向当前源码。常见配置的 command/args 可用：

```json
{
  "command": "uv",
  "args": [
    "--directory", "/home/memcon/git_repos/paper-fetch-skill",
    "run", "--no-sync", "paper-fetch-mcp"
  ]
}
```

路径按你的 checkout 修改；这是 stdio 服务，由宿主启动，不能把下面的工具参数 JSON 当作原始 stdin 消息粘进去。

**M0 通过标准**：重启后 `tools/list` 有 `download_paper_pdfs`，`query` 和 `output_dir` 必填，`include_si` 默认 true；旧 `fetch_paper` 参数仍在。至少从你的实际 MCP 宿主调用一次：

```json
{
  "query": "10.1021/acsomega.6c08841",
  "output_dir": "./papers/manual-mcp/acs-first-run",
  "include_si": true
}
```

相对路径基于 server 工作目录；建议使用本次实际的绝对路径。再次测试换新目录。旧 server 看不到新工具是配置问题，不能称为新接口已经测过。

### 2.1 可复制的终端调用助手

没有方便的工具调用 UI 时，在第 1 节同一终端定义下列函数。它使用当前**真实注册 MCP server 的进程内 transport**，没有 Mock，也不绕过工具校验；但不能代替上述宿主 stdio/配置检查。

```bash
pf_raw() {
  PF_DOI="$1" PF_CASE="$2" PF_INCLUDE_SI="${3:-true}" \
    uv run --no-sync python - <<'PY'
import asyncio
import json
import os
from pathlib import Path
from mcp import Client
from paper_fetch.mcp.server import build_server

async def main():
    case = Path(os.environ['PF_MANUAL_ROOT']) / os.environ['PF_CASE']
    case.mkdir(parents=True, exist_ok=True)
    async with Client(build_server()) as session:
        listed = await session.list_tools()
        assert any(t.name == 'download_paper_pdfs' for t in listed.tools)
        result = await session.call_tool('download_paper_pdfs', {
            'query': os.environ['PF_DOI'],
            'output_dir': str(case / 'files'),
            'include_si': os.environ['PF_INCLUDE_SI'].lower() == 'true',
        })
        payload = result.structured_content
        assert isinstance(payload, dict), 'Missing structured MCP result'
        (case / 'result.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print('MCP is_error:', result.is_error)
        print('结果文件:', case / 'result.json')

asyncio.run(main())
PY
}
```

助手进程正常退出只表示收到结果；**验收看 `status`、`acceptance` 和实际文件**，不是进程 exit 0。

## 3. 三家真实下载：逐篇执行

开始前在浏览器打开目标论文，确认 DOI 和 Supporting/Supplementary Information 清单。可换成你有合法权限的论文；换了候选就在记录中写明 DOI、页面和附件类型/数量。

| ID | 命令 | 通过标准 |
|---|---|---|
| F1 ACS | `pf_raw '10.1021/acsomega.6c08841' acs-all true` | 主文＋该页面的 1 个 canonical SI PDF 均 downloaded；overall=complete。 |
| F2 Wiley | `pf_raw '10.1002/open.202500340' wiley-all true` | 主文＋页面 Supporting Information 中的附件均 downloaded；overall=complete。**此前此项只到 partial，须重新验收。** |
| F3 Elsevier | `pf_raw '10.1016/j.heliyon.2023.e18493' elsevier-all true` | 已合法配置 API key；主文为完整、非 first-page-only PDF；SI 与实际页面/API 清单一致。若该文无 SI，另选有 SI 的目标验证附件路线。 |
| F4 仅主文 | `pf_raw '10.1021/acsomega.6c08841' acs-main-only false` | 主文 downloaded，`supplementary=[]`、discovery=not_requested；该 case 不生成 SI、文章 Markdown 或正文图片。 |

**共同判断**：

- 主文成功：`main_pdf.status=downloaded`；其 `validation.format/structure/size/role/identity` 全为 true。
- SI 成功：每个附件都有独立 ID、状态、path、size_bytes、sha256、format、provider/route 与来源；`validation.format/size/source=true`，PDF SI 的 structure=true。其他格式的 structure 可为 null，不伪称已解析全部内容。
- 主文成功而 SI 失败：应为 `partial`、`is_error=false`、overall=degraded，主文保留。这是**失败报告契约正确**，不是 F2/F3 的 SI 成功项通过。
- `not_found` 只能在完成发现且页面确实无附件时接受；`failed/unsupported` 不等于“没有 SI”。多附件不能只保存第一个。ACS 的 viewer/重复 Figshare 入口不另计为 canonical 文件。
- 主文失败：MCP is_error=true，不能以 HTML/摘要/metadata 冒充原 PDF 成功；SI 不再启动。
- 401/403、验证码、登录、API key/entitlement 缺失记 **BLOCKED**；身份错、坏文件仍被保存为成功、漏附件或协议字段错误记 **FAIL**。
- 输出可以有脱敏 diagnostics、文件锁及本助手的结果 JSON；不得把这些当作论文。新工具不应生成正文 Markdown、转换器导出的正文图片或 fetch-envelope/cache-index。

至少找一篇有多个附件或非 PDF SI 的合法目标进行相同测试：ZIP/CSV/DOCX/XLSX 应保存原文件，不因“非 PDF”跳过。未做该 live 项时如实记录；第 5 节机制测试不能冒充出版社 live 成功。

## 4. 文件与来源复核

先手工打开 PDF：主文标题/DOI对应目标且不是预览或 SI；SI 对应目标论文附件面板的文件标签。脱敏 URL 可能不保留 query，**不要据此猜测或重放签名参数**；以原页面明确的附件清单核对。

对第 3 节每个成功 case 执行以下检查（示例 ACS）：

```bash
PF_AUDIT_CASE=acs-all uv run --no-sync python - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import pymupdf

case = Path(os.environ['PF_MANUAL_ROOT']) / os.environ['PF_AUDIT_CASE']
payload = json.loads((case / 'result.json').read_text(encoding='utf-8'))
root = (case / 'files').resolve()
checked = 0
for item in [payload['main_pdf'], *payload['supplementary']]:
    if item['status'] != 'downloaded':
        print('未成功:', item.get('id'), item.get('code'), item.get('reason'))
        continue
    path = Path(item['path']).resolve()
    assert path.is_relative_to(root), 'Wrong output scope'
    data = path.read_bytes()
    assert data and len(data) == item['size_bytes']
    assert hashlib.sha256(data).hexdigest() == item['sha256']
    if item['format'] == 'pdf':
        assert data.startswith(b'%PDF-')
        with pymupdf.open(path) as doc:
            assert doc.page_count > 0
            print(item['role'], doc.page_count, 'pages')
    checked += 1
    print('文件事实通过:', path, item['sha256'])
assert checked > 0, 'No downloaded files to audit'
assert not list(root.rglob('*.part')), 'Unfinished staging files remain'
PY
```

依次把 `PF_AUDIT_CASE` 换成 `wiley-all`、`elsevier-all`、`acs-main-only`。这个脚本只检查结构/hash/位置，不替代页面归属或人工主文身份核对，也不改写文件。

**V1 通过标准**：工具关闭后文件仍存在；每个 hash/size 一致；源文件保持原格式，ZIP 没有被解包。没有文件可审计时不能据脚本无断言错误宣称下载成功。

## 5. 负面路径、原字节与旧接口

不通过破坏真实网站、伪造登录或修改生产配置制造错误；在终端手动运行已有受控回归：

```bash
PYTHONPATH=src uv run python -m pytest \
  tests/unit/test_download_paper_pdfs.py \
  tests/unit/test_original_downloads_provider.py \
  tests/integration/test_download_paper_pdfs.py \
  tests/integration/test_original_downloads_provider.py -q
```

应全部通过。它覆盖：无正文转换 tripwire；PDF 结构/大小/身份/主文角色；HTML错误页、403、非法JSON/XML/RTF；合法文本/ZIP/Office 原字节；多 SI 部分失败及缺终态对账；校验失败不发布；冲突、预算、取消 fence；公开结果不泄漏认证 seed；缺 PyMuPDF 的 action_required。

手工补核对：

- **R1 拒绝覆盖**：同一 case 再运行 F4。同字节可以幂等成功；出版社本次返回字节不同则应 `artifact_conflict`，原文件 hash 不变。工具没有公开 `overwrite` 参数，不要传不存在的参数或删除原件强行通过。同 DOI 两次 hash 不同不自动等于错论文，仍需身份核验；不得因此静默覆盖。
- **R2 取消**：在支持取消的真实 MCP 宿主中取消尚未结束的调用。已完成文件可以保留；取消后不得继续提交未完成文件，staging 应清理。不要用 `kill -9` 代替 cooperative cancellation；宿主不支持时明确记录，并使用既有受控取消测试作机制证据。
- **R3 旧 fetch**：在同一 MCP 宿主调用 `fetch_paper`：

  ```json
  {
    "query": "10.1021/acsomega.6c08841",
    "modes": ["markdown", "metadata"],
    "strategy": {"asset_profile": "none"},
    "save_markdown": false,
    "no_download": true,
    "prefer_cache": false,
    "artifact_mode": "none"
  }
  ```

  合法可访问时仍提供阅读用 Markdown/metadata 与既有七分面 acceptance，不能被改为只返回 PDF 文件路径。全文、摘要、metadata 降级要区分；不验收 PDF 排版/转换质量，不据格式差异修改转换内容。原始下载的 acceptance 是 main_pdf/supplementary 摘要，不套用旧 fetch 的内容验收。

## 6. `texmath` 与提交前自动检查

### 6.1 使用项目解释器安装，不向系统 Python 逐个补包

`rapidfuzz` 已是项目依赖。`uv add` 安装进 `.venv`，不能让安装脚本默认选择的系统 `python3` 获得该包；而且它可能重建 venv，只保留 core 依赖。使用已有安装器的解释器选择入口：

```bash
uv sync --frozen --extra dev --extra full
PAPER_FETCH_INSTALL_PYTHON_BIN="$PWD/.venv/bin/python" \
  ./install-formula-tools.sh
```

若 `.formula-tools/bin/texmath --version` 已显示 `Version 0.13.3`，无需重复编译。**安装器 exit 0 不等于 texmath 成功**：Node 或内建 Python 回退也会退出 0，必须执行第 6.2 节。

**当前 Arch Linux 的特殊情况**：GHC 9.6.7 有 `Prelude.dyn_hi`，没有 vanilla `Prelude.hi`；默认 Cabal 编译因此报告缺 `Prelude/base/containers/text`，不代表再 `uv add` Python 包能修复。已验证以下临时动态构建配置；只在这一类动态库-only GHC 环境使用，不改系统 Cabal 配置或仓库安装器：

```bash
# PF_MANUAL_ROOT 由第 1 节建立；config 路径必须是新文件。
export PF_CABAL_CONFIG="$PF_MANUAL_ROOT/cabal-arch-dynamic.config"
cabal --config-file="$PF_CABAL_CONFIG" user-config init \
  --augment='library-vanilla: False' \
  --augment='shared: True' \
  --augment='executable-dynamic: True' \
  --augment='library-for-ghci: False' \
  --augment='jobs: 2'
CABAL_CONFIG="$PF_CABAL_CONFIG" \
PAPER_FETCH_INSTALL_PYTHON_BIN="$PWD/.venv/bin/python" \
  ./install-formula-tools.sh --no-node
```

`jobs: 2` 仅限制这次 Haskell 编译，不改变 pytest 并行配置。二进制安装在 ignored `.formula-tools/bin/texmath`；它依赖本机 GHC/Cabal store 的动态库，不能当作 portable/offline release 资产复制或提交。GHC 升级后需重新验证或重建。

### 6.2 配置发现路径并强制验证真实 texmath

当前源码会自动发现 repo-local `.formula-tools/bin/texmath`，无需修改全局环境文件。需要显式指定时，在本次终端使用：

```bash
export TEXMATH_BIN="$PWD/.formula-tools/bin/texmath"
test -x "$TEXMATH_BIN" && "$TEXMATH_BIN" --version
export PATH="$(dirname "$TEXMATH_BIN"):$PATH"
export PAPER_FETCH_FORMULA_TOOLS_DIR="$PWD/.formula-tools"
uv run --no-sync python - <<'PY'
from pathlib import Path
from paper_fetch.formula.install import have_working_texmath
from paper_fetch.formula.convert import convert_mathml_string
assert have_working_texmath(Path('.formula-tools/bin/texmath'))
raw = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mfrac><mi>x</mi><mi>y</mi></mfrac></math>'
r = convert_mathml_string(raw, display_mode=False, backend='texmath')
assert r.status == 'ok' and r.backend == 'texmath', r.error
assert r.raw_mathml == raw and r'\frac{x}{y}' in r.latex
print(r.backend, r.status, r.latex)
PY
PYTHONPATH=src uv run python -m pytest \
  tests/integration/test_mathml_semantics.py \
  tests/integration/test_formula_conversion.py \
  tests/integration/test_formula_install.py -q
```

**G0 标准**：版本为 `0.13.3`，转换明确返回 `backend=texmath/status=ok`，此前 `[texmath]` 用例 PASS，不是 skip。测试 helper 构造独立环境，只设置 `TEXMATH_BIN` 不足；保持公式工具目录可见，或让名为 `texmath` 的可执行文件在 PATH 中可见。

本轮自动证据（Arch Linux、GHC 9.6.7、项目 Python 3.12.13）：

- 项目解释器已恢复锁定 dev/full 依赖；未修改依赖声明/锁文件、安装器或公式源码。
- 实际二进制版本、动态库加载、repo-local 自动发现及分式转换通过，未使用 Node 回退；公式 integration **8 passed，无 skip**。
- 完整 integration 首次为 **209 passed / 1 skipped / 2 failed**：两项失败均为重建 venv 后 fpgen 模型下载中断，不是 texmath 失败。按既有 Camoufox 准备命令补齐模型后，这两个失败节点复跑 **2 passed**；其余通过结果复用，**未重跑完整 integration，不称首轮退出成功**。
- 本轮没有重新跑完整 unit/golden 或 publisher live；Wiley SI/Elsevier 的人工门禁仍未通过。

### 6.3 重建 venv 后的浏览器模型准备与提交检查

若重建 venv 后要运行真实本地浏览器 integration，先按项目既有命令准备匹配 runtime/fpgen 模型，避免并行测试首次竞争下载：

```bash
uv run --no-sync python -m camoufox fetch
```

这是已有浏览器依赖的准备，不访问出版社页面，不替代 live 授权或原生 macOS 验证。准备失败时记录环境阻塞，不修改测试让它 skip。

提交前执行项目规定的回归；不是发布候选，无需新增 build/install 或全平台 release 矩阵：

```bash
PYTHONPATH=src uv run python -m pytest tests/unit -q
PYTHONPATH=src uv run python -m pytest tests/integration -q
PYTHONPATH=src uv run python -m pytest \
  tests/golden/test_acs_provider.py \
  tests/golden/test_wiley_provider.py \
  tests/golden/test_elsevier_markdown.py \
  tests/golden/test_html_shared_helpers.py \
  tests/golden/test_pdf_document_acceptance.py -q
uv run ruff check .
uv run mypy src/paper_fetch
python scripts/validate_macos_adaptation.py
```

Linux/WSL 另执行 `bash scripts/test-macos-contract.sh --python .venv/bin/python`，必须为原生 Linux venv；原生 Windows 用既有 `.ps1`。Portable 绿灯不等于 native macOS gate 已通过；未做原生测试如实记录，不宣称全平台发布验收完成。

常规 unit/integration/golden 保留 `pyproject.toml` 的并行设置，不加 `-n 0`。若 xdist INTERNALERROR，先检查日志、依赖及 evidence ledger，不当作测试通过；证实本机资源不足后才临时限制 auto workers 并记录原因。Live 出版社调用按本清单串行，不混入默认离线套件。

## 7. Go / No-Go 与提交范围

**正常提交门禁**：M0、F1–F4、V1、R1–R3、G0及第 6 节适用检查通过；非 PDF/多附件覆盖及 skips 的证据边界记录清楚。Wiley SI 仍 failed/partial 或 Elsevier 仍缺配置时，不能签为对应成功项通过。若你明确接受某个环境/live 项无法执行，逐项写明 **BLOCKED/未执行、原因、接受人和限制**，这是有条件接受，不是“全部测试通过”。

提交前仅做检查与人工选文件，不 `git add .`：

```bash
git status --short
git diff --check
git diff --stat
# 普通 diff 不显示新增文件，必须检查这一列。
git ls-files --others --exclude-standard -- src/paper_fetch tests docs skills
# 正常情况下下面没有变化；若有，先核对是否越过本次范围。
git diff --name-only -- .github pyproject.toml uv.lock
```

- 应纳入：本功能的 source、tests（含 `tests/test-evidence.json`）、必要 Skill/说明文档和本清单；特别检查新增的 downloads/originals/MCP handler、测试及 support 文件。
- 不纳入：`.pi/`、`papers/`、工具安装目录、venv、token/env/storage-state、调试日志或新抓的整篇论文/资产。即使目录被 ignore，也必须按第 4 节核验文件。
- 逐项暂存核对后的文件，再看 `git diff --cached --stat`、`--check`、`--name-only`；确认没有漏掉新增源码或混入其它工作。
- 通过后由你自行提交。按仓库约定不触发 CI，提交消息可用 `feat: download original PDFs and supplements [skip ci]`；本地 commit 本身不启动 GitHub CI。Push 前核对触发规则，不创建 release tag、不手动触发 workflow，不把源码提交等同于发布。

## 验收记录模板

在本次 `PF_MANUAL_ROOT` 下保存记录，不把凭据或论文文件提交：

```text
源码路径 / git HEAD / 操作系统 / Python：
本次输出目录：
M0 新工具与旧 schema / 实际 MCP 宿主调用：PASS / FAIL / BLOCKED
F1 ACS 主文+SI（DOI、数量、结果JSON）：
F2 Wiley 主文+SI（DOI、数量、结果JSON）：
F3 Elsevier 主文+SI（配置只写有/无，不写key）：
F4 include_si=false：
V1 文件位置、结构、身份/附件来源、hash、关闭后持久存在：
非PDF/多附件：live / 受控机制 / 未执行，具体格式与证据：
R1 幂等或冲突不覆盖 / R2 取消 / R3 旧fetch：
G0 texmath：PASS / FAIL / SKIP（skip不算通过）
unit / integration / 指定golden / Ruff / mypy / macOS portable：
skips、未执行的native/live项与理由：
遗留FAIL/BLOCKED；是否明确有条件接受、接受人：
暂存文件检查 / 无凭据数据 / 无CI版本依赖变更：
最终：GO / NO-GO（原因）：
```

# FASTA UniProt → NCBI Taxonomy 映射工具

[English](README.en.md) | 简体中文

`fasta_taxonomy_mapper.py` 使用 Biopython 解析 FASTA，通过 UniProt 官方 REST API 建立 accession 到当前数据库物种的映射。只查询唯一 accession，但输出每条原始 FASTA 记录，保留重复项和顺序。

## 运行环境

- Python 3.11+。
- 仅依赖 `requests`、`biopython` 和标准库；依赖范围列于 `requirements-fasta-taxonomy.txt`。
- 脚本不会自动安装依赖、重建虚拟环境或升级 Python。
- 验证环境：Python 3.11.9、requests 2.34.2、Biopython 1.88。

所有示例均使用相对路径，不依赖个人用户名、盘符或固定的项目存放位置。以下命令从项目根目录执行，并使用项目已有的虚拟环境。当前目录结构如下：

```text
project-root/
├── .venv/
├── 重写/
│   ├── fasta_taxonomy_mapper.py
│   ├── README.md
│   └── README.en.md
├── 配置文件/
│   └── requirements-fasta-taxonomy.txt
├── examples/
│   └── taxonomy_smoke.fasta
├── tests/
└── validation/
```

Windows PowerShell，查看帮助（命令均为单行，不使用续行符）：

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --help
```

用随附的小型 FASTA 建立映射：

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --input examples/taxonomy_smoke.fasta --output results/taxonomy_mapping.tsv
```

脚本使用 `pathlib.Path`；相对路径以启动命令时的工作目录为准，不绑定到脚本目录。若调整目录结构，相应修改脚本、输入和输出的相对路径即可。

Linux/WSL 使用该平台的项目环境，例如：

```bash
.venv/bin/python ./重写/fasta_taxonomy_mapper.py --input examples/taxonomy_smoke.fasta --output results/taxonomy_mapping.tsv
```

## 使用示例

以下 `data/proteins.fasta` 为通用输入示例，请替换为自己的 FASTA 路径。

建立所有物种的映射，不限定目标物种：

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv --cache results/uniprot_taxonomy.cache.sqlite3
```

按精确 NCBI Taxonomy ID `9606` 筛选智人：

```powershell
.\.venv\Scripts\python.exe .\重写\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv --cache results/uniprot_taxonomy.cache.sqlite3 --batch-size 50 --timeout 30 --retries 3 --taxon 9606 --filtered-fasta results/human.fasta
```

`--output` 始终输出完整映射表。指定 `--taxon` 后，表内额外记录目标物种判断、报告增加目标数量；`--filtered-fasta` 才生成仅包含目标物种的 FASTA。黑猩猩 `9598`、倭黑猩猩 `9597` 等不会因为属于灵长类就被纳入 `9606`。

筛选直接复制原始 FASTA 记录的字节，保留标题、序列、换行和重复记录。不会用 API 返回的序列、标题或 GO 注释替换输入，也不读取或重写任何历史 GO 标签文件。

## 官方接口与匹配规则

实现前核查了官方文档，并实际请求接口验证参数：

- [UniProtKB 查询字段](https://www.uniprot.org/help/query-fields)：`accession:` 查询当前主 accession；`sec_acc:` 查询历史 secondary accession。
- [查询与分页](https://www.uniprot.org/help/api_queries)、[分页说明](https://www.uniprot.org/help/pagination)：使用 `/uniprotkb/search`，沿响应头 `Link: ...; rel="next"` 读取后续页。
- [accession 规则与合并/拆分](https://www.uniprot.org/help/accession_numbers)：历史 accession 可能对应多个当前条目。
- [官方返回字段配置](https://rest.uniprot.org/configure/uniprotkb/result-fields)。

查询流程固定如下：

1. 用 Biopython 提取 `record.id`；保留原始 ID，规范化 accession 为大写，按首次出现顺序去重查询。
2. 优先复用未过期的本地缓存。
3. 批量 GET `https://rest.uniprot.org/uniprotkb/search`，参数为：

   ```text
   query=active:true AND (accession:P69905 OR accession:P68871)
   format=json
   fields=accession,organism_id,organism_name
   size=500
   ```

4. 精确核对 JSON 的 `primaryAccession` 与输入 accession；核对 `organism.taxonId` 和 `organism.scientificName`。完成主 accession 查询后才对剩余 ID 执行 `sec_acc:` 查询。
5. secondary 查询不传 `fields`，从完整 JSON 检查输入 accession 是否确实位于 `secondaryAccessions`。`sec_acc` 是有效查询字段，但作为 `fields=sec_acc` 会返回 HTTP 400，不能混用。其他注释随响应读取后丢弃，不进入结果或缓存。
6. secondary 仅有一个当前候选条目时接受映射；多个候选一律保留为歧义，即使候选物种相同。
7. 两种查询都成功且完整、仍无匹配，才写入 `unmapped`。

这是限定 accession 字段并核对 JSON 证据的精确匹配，不采用普通文本搜索排名或相似名称推断。在查询阶段不加入物种限制，避免掩盖跨物种的 secondary 歧义；物种筛选只在匹配完成后执行精确整数比较。

所选接口为同步分页查询，不需要异步任务轮询。官方每页最多 500 条；工具默认每批 50 个 accession，允许 1–100，后者是工具控制 GET URL 长度的保守限制，不是官方 accession 总数限制。默认请求间隔 0.25 秒是工具策略，不代表官方保证的速率配额。

每次查询检查 `X-Total-Results`、重复条目、重复分页链接和版本一致性。缺少总数或分页不完整会记作 `api_error`，不会生成确定的未匹配结论。`X-UniProt-Release`、`X-UniProt-Release-Date` 若存在则原样记录；缺失时保留空值，不自行推测版本。

## 标题支持与状态

支持 `>P12345`、`>sp|P12345|PROTEIN_HUMAN ...`、`>tr|A0A024RBG1|ENTRY ...` 三类标题。`original_id` 是 Biopython 的 `record.id`，即标题首个空白前的 ID；完整标题保留在原始/筛选 FASTA 中。`record_index` 从 1 开始，区分重复 ID。

accession 使用官方 6/10 位格式校验。`tr|A0A1234567|...` 的分隔格式可解析，但其中示例 accession `A0A1234567` 不符合官方十位规则，会保留为 `invalid_id`，不送入 API。真实十位格式示例为 `A0A024RBG1`。不自动剥离 `-2` 等 isoform 后缀或 `.1` 等版本后缀，也不把 `PROTEIN_HUMAN` 条目名称猜成 accession。

| mapping_status | 含义 | 可用于物种筛选 |
|---|---|---|
| `primary_match` | 当前主 accession 精确匹配 | 是 |
| `secondary_match` | secondary 精确映射至唯一当前条目 | 是，保留原 accession 与映射关系 |
| `ambiguous` | secondary 对应多个当前条目 | 否 |
| `unmapped` | 完整查询当前活动 UniProtKB 后没有精确匹配 | 否 |
| `invalid_id` | 标题 ID 不受支持或 accession 格式不合法 | 否 |
| `api_error` | HTTP、网络、响应内容或分页验证失败 | 否，下次重查 |
| `interrupted` | 中断时尚未完成查询 | 否，下次重查 |

歧义、未匹配、错误记录的 `taxonomy_id`、`organism_name`、`matched_accession` 留空。歧义候选完整写入 `candidate_matches` JSON；不会取第一个候选，也不会把有候选的人类条目直接当作唯一匹配。

## 输出文件

以 `--output results/taxonomy_mapping.tsv` 为例，默认生成：

| 文件 | 内容 |
|---|---|
| `taxonomy_mapping.tsv` | 全部输入记录，顺序与 FASTA 一致 |
| `taxonomy_mapping.unmatched.tsv` | 全部未确定记录，包含 unmapped、ambiguous、invalid_id、api_error、interrupted |
| `taxonomy_mapping.report.json` | 记录/唯一 accession 状态统计、请求与重试数、缓存命中数、版本、输入 SHA-256、查询时间和参数 |
| `taxonomy_mapping.cache.sqlite3` | 增量缓存；可用 `--cache` 指定共享路径 |

TSV 前七列为请求的 `original_id`、`accession`、`taxonomy_id`、`organism_name`、`matched_accession`、`mapping_status`、`mapping_source`。

其他列：

- `record_index`：原始 FASTA 记录序号。
- `query_date_utc`：实际查询时刻，使用带时区的 ISO 8601 UTC 时间；读缓存不改写此值，本地非法 ID 留空。
- `uniprot_release`、`uniprot_release_date`：API 返回的版本与发布日期。
- `taxonomy_context`：`current_uniprotkb`，表示查询当时活动数据库中的归属。
- `candidate_matches`：候选当前 accession、taxonomy ID、物种学名的 JSON 数组。
- `error`：非法 ID、歧义或请求失败的具体说明。
- `target_taxonomy_id`：请求的 `--taxon`，未指定则为空。
- `taxon_filter_status`：`target_taxon`、`other_taxon`、`unresolved` 或 `not_requested`。不确定记录始终为 `unresolved`，不会标成其他物种。

TSV 使用 UTF-8 与标准 CSV 引号规则；程序读取时建议 `csv.DictReader(..., delimiter="\t")`。

当前数据库归属不能证明历史数据集建立时的归属。`secondary_match` 也只证明 accession 关联，不证明历史 FASTA 序列与当前条目序列完全一致。历史归属应结合原数据集元数据或历史数据库版本另行核查。本工具不推断它，也不更改输入数据集标签。

## 参数与恢复

| 参数 | 默认值/约束 |
|---|---|
| `--input`、`--output` | 必填 |
| `--cache` | 输出文件同 stem 的 `.cache.sqlite3` |
| `--batch-size` | 50，范围 1–100 |
| `--timeout` | 30 秒；requests 每次连接/读取的超时，不是整个任务的总期限 |
| `--retries` | 初次请求后最多再重试 3 次 |
| `--taxon` | 不限制物种；提供时必须为正整数 |
| `--filtered-fasta` | 可选，需同时给出 `--taxon` |
| `--request-interval` | 0.25 秒，非负 |
| `--page-size` | 500，范围 1–500；设为 1 可检查分页 |
| `--cache-max-age-days` | 30 天；设为 0 会重查 |
| `--refresh-cache` | 强制重新查询，仍增量提交缓存 |
| `--unmatched-output`、`--report` | 覆盖默认未确定 TSV / JSON 报告路径 |
| `--verbose`、`--version` | 调试日志 / 版本 |

网络异常和 HTTP 408、429、500、502、503、504 会有限重试；HTTP 400 等请求错误直接记录。重试采用指数退避与少量随机抖动，并等待不短于 `Retry-After` 指定的秒数或 HTTP 日期。不关闭 TLS 证书验证。

缓存使用 SQLite WAL、完整同步与事务，分别提交每个完成的主 accession / secondary 批次。进程中断后，已提交结果保留；未完成批次可能重新请求。查询错误可写入缓存供审计，但永不作为缓存命中重用。缓存损坏的单条结果会警告并重查，数据库整体损坏则明确报错，不自动删除或覆盖。

重新运行相同命令即可恢复。缓存保留原查询日期和版本；若合并了多个发行版结果，报告与日志会明确提示。需要重新获取较新的数据库快照时使用 `--refresh-cache`；活动数据库不能保证跨日期固定不变，请保留完整映射表、报告和缓存以便审计。

输出文件逐文件使用临时文件加原子替换。完整映射和未确定记录始终保留，不能只根据筛选后 FASTA 的缺失判断某条记录是非目标物种。

退出码：`0` 表示查询流程完成（可以包含歧义、未匹配或非法 ID）；`1` 为输入、文件、缓存等错误；`2` 为存在 API 错误或命令行参数错误；`130` 为用户中断。在映射阶段按 Ctrl+C，会尽量生成已完成记录和标为 `interrupted` 的未完成记录；强制结束进程时依靠已提交缓存恢复。

## 测试与小规模在线验证

测试文件位于 `tests/`，脚本位于 `重写/`。从项目根目录运行测试前，将脚本目录加入当前 PowerShell 会话的模块搜索路径；保留已有的 `PYTHONPATH`，不更改系统设置或虚拟环境文件：

```powershell
$env:PYTHONPATH = (Resolve-Path './重写').Path + [IO.Path]::PathSeparator + $env:PYTHONPATH
```

离线回归测试，无网络访问：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_fasta_taxonomy_mapper.py' -v
```

涵盖 ID 解析、重复与顺序、缓存往返与过期、错误缓存重查、secondary 证据和歧义、429/超时、分页完整性、Ctrl+C、TSV/报告、精确物种筛选和原始 FASTA 字节保留。

显式在线验证，仅查询 `examples/taxonomy_smoke.fasta` 的 7 个唯一合法 accession：

```powershell
.\.venv\Scripts\python.exe -B tests/live_smoke_test.py
```

该验证强制每页 1 条，检查 9 条原始记录、4 条人类记录、secondary 映射、跨物种歧义和筛选结果原始字节；随后再次运行，要求 HTTP 请求数为 0。产物保存在 `validation/taxonomy_smoke/`，包括完整 TSV、未确定 TSV、人类 FASTA、两次统计报告、缓存和 `verification.json`。

在线测试的参考预期核查自 UniProt `2026_03`；未来数据库更新可能改变映射，届时需根据实际结果复核参考预期。示例序列是截短的测试字符串，只用于验证 ID 映射与文件保留，不适合生物学分析。

基线验证日期为 **2026-10-18**：29 项离线测试全部通过；在线首次运行 9 次请求，重复运行 0 次请求；`verification.json` 的结果为 `passed`。9 条记录的状态为主匹配 4、secondary 匹配 1、歧义 2、未匹配 1、非法 ID 1，精确筛出 4 条人类记录。也验证了从其他工作目录调用脚本，以及不指定 `--taxon` 的完整映射模式，退出码为 0。

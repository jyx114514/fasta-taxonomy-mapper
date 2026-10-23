# FASTA UniProt → NCBI Taxonomy 映射工具

[English](README.en.md) | 简体中文

`fasta_taxonomy_mapper.py` 使用 Biopython 解析 FASTA，通过 UniProt 官方 REST API 建立 accession 到当前数据库物种的映射。只查询唯一 accession，但输出每条原始 FASTA 记录，保留重复项和顺序。

## 运行环境

- Python 3.11+。
- 仅依赖 `requests`、`biopython` 和标准库；依赖范围列于 requirements-fasta-taxonomy.txt
- 脚本不会自动安装依赖、重建虚拟环境或升级 Python。
- 验证环境：Python 3.11.9、requests 2.34.2、Biopython 1.88。

所有示例均使用相对路径，不依赖个人用户名、盘符或固定的项目存放位置。以下命令从项目根目录执行，并使用项目已有的虚拟环境。目录结构示意如下，其中 `data/` 是需自行准备的输入位置：

```text
project-root/
├── .venv/
├── 重写/
│   └── taxonomy脚本/
│       ├── fasta_taxonomy_mapper.py
│       ├── README.md
│       └── README.en.md
├── 配置文件/
│   └── requirements-fasta-taxonomy.txt
├── data/                 # 自备输入，按需创建
│   └── proteins.fasta
└── results/              # 生成的输出
```

Windows PowerShell，查看帮助（命令均为单行，不使用续行符）：

```powershell
.\.venv\Scripts\python.exe .\重写\taxonomy脚本\fasta_taxonomy_mapper.py --help
```

用自己的 FASTA 建立映射（`data/proteins.fasta` 为占位路径，需先准备输入文件）：

```powershell
.\.venv\Scripts\python.exe .\重写\taxonomy脚本\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv
```

脚本使用 `pathlib.Path`；相对路径以启动命令时的工作目录为准，不绑定到脚本目录。若调整目录结构，相应修改脚本、输入和输出的相对路径即可。

Linux/WSL 使用该平台的项目环境，例如：

```bash
.venv/bin/python ./重写/taxonomy脚本/fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv
```

## 使用示例

以下 `data/proteins.fasta` 为通用输入示例，请替换为自己的 FASTA 路径。

建立所有物种的映射，不限定目标物种：

```powershell
.\.venv\Scripts\python.exe .\重写\taxonomy脚本\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv --cache results/uniprot_taxonomy.cache.sqlite3
```

按精确 NCBI Taxonomy ID `9606` 筛选智人：

```powershell
.\.venv\Scripts\python.exe .\重写\taxonomy脚本\fasta_taxonomy_mapper.py --input data/proteins.fasta --output results/taxonomy_mapping.tsv --cache results/uniprot_taxonomy.cache.sqlite3 --batch-size 50 --timeout 30 --retries 3 --taxon 9606 --filtered-fasta results/human.fasta
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

完整映射表与未确定记录表使用相同的 16 列表头。前七列固定按下表顺序输出；每条 FASTA 记录对应一行，重复 ID 不合并。

| 列序 | 字段 | 预计输出及空值规则 |
|---|---|---|
| 1 | `original_id` | Biopython 解析的原始 `record.id`，即 `>` 后首个空白前的标识符；保留大小写和 `sp\|…\|…` / `tr\|…\|…` 包装，不含 `>` 或完整描述。 |
| 2 | `accession` | 从受支持标题中提取并转为大写的查询 accession。例如 `sp\|p69905\|HBA_HUMAN` 输出 `P69905`。非法普通 ID 也会大写；不受支持的管道分隔 ID 则原样保留去除首尾空白后的 token，供排查，不保证是合法 accession。 |
| 3 | `taxonomy_id` | 仅在 `primary_match` / `secondary_match` 时输出唯一当前条目的正整数 NCBI Taxonomy ID，例如 `9606`；其余状态为空字符串，不输出 `0`、`None` 或 `NA`。 |
| 4 | `organism_name` | 同一确定条目的 API `organism.scientificName`，例如 `Homo sapiens`；非确定状态留空。不从 FASTA 描述推断物种。 |
| 5 | `matched_accession` | 确定匹配的当前主 accession。主匹配时等于第 2 列；secondary 匹配时输出关联的当前主 accession，第 2 列仍保留输入 accession；非确定状态留空。 |
| 6 | `mapping_status` | 固定状态字符串：`primary_match`、`secondary_match`、`ambiguous`、`unmapped`、`invalid_id`、`api_error` 或 `interrupted`，含义见上表。 |
| 7 | `mapping_source` | 匹配证据或失败阶段的文字标记，具体取值见下文；缓存命中保留原标记，不改为 `cache`。 |

`mapping_source` 的当前固定取值为：

- 主 accession 查询的确定结果：`UniProtKB REST: exact accession + primaryAccession verification`。
- secondary 查询的确定结果或歧义：`UniProtKB REST: exact sec_acc + secondaryAccessions verification`。
- `unmapped`：以上两个字符串按主查询、secondary 查询顺序用 `; ` 连接，表示两种查询均完成。
- `invalid_id`：`local_id_validation`；`api_error`：`UniProtKB REST: incomplete query`；`interrupted`：`not_completed`。

示意输出仅展示前七列，字段间为制表符；空字段由连续制表符表示。示例不代表本轮实时 API 查询，实际文件还包含后续九列：

```tsv
original_id	accession	taxonomy_id	organism_name	matched_accession	mapping_status	mapping_source
sp|p69905|HBA_HUMAN	P69905	9606	Homo sapiens	P69905	primary_match	UniProtKB REST: exact accession + primaryAccession verification
P69905-2	P69905-2				invalid_id	local_id_validation
```

`ambiguous` 即使多个候选具有相同物种，也将第 3–5 列留空，候选写入 `candidate_matches`；`api_error` / `interrupted` 的空值表示查询未完成，不能视作数据库不存在该 accession。筛选目标不会改变前七列：确定的其他物种仍输出自己的 taxonomy ID 和学名。

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

## 迁移与复用检查（2026-10-24）

脚本中未发现个人盘符、用户名、固定输入数据或固定目标物种。输入、输出、缓存及可选筛选文件均由命令行指定，默认伴随文件由 `--output` 推导；路径支持 `~`，相对路径以启动时的工作目录为准。报告中的绝对路径是运行时记录，不是下次运行要读取的配置。

迁移时复制 `fasta_taxonomy_mapper.py` 即可复用逻辑；建议同时带上两份 README 和项目的依赖清单。目标机器需准备自己的 Python 3.11+ 环境及已有依赖。不要直接复制 Windows `.venv` 到其他机器或 Linux/WSL；本项目环境引用本机基础解释器，不是可迁移运行时。文档中的 `.venv` 只是调用示例，脚本本身没有绑定解释器路径。

复用边界与注意事项：

- UniProt API 地址写在 `API_URL` 中，分页校验也固定为同一 HTTPS 主机与 `/uniprotkb/search` 路径。这是服务约束，不能仅更改一个常量就切换镜像或其他数据库；正常新查询需网络和有效 TLS 证书，只有全部命中有效缓存时才不发请求，没有专用离线模式。
- 工具只接受上述 UniProt accession 格式，按精确 taxonomy ID 筛选；不支持任意数据库 ID、isoform 自动合并或子类群筛选。
- 依赖清单给出兼容范围，未锁定全部版本；UniProt 活动数据也随版本变化。严格复现应保存实际依赖版本、TSV、缓存和 JSON 报告。
- 已存在的输出文件会被替换；每个文件单独原子写入，但整组输出不是一个事务。失败后可能出现本轮 TSV 与旧报告并存，应检查退出码和报告时间。路径去重会解析符号链接，但不能识别所有硬链接别名；为输入、缓存及输出使用独立文件名，并避开缓存的 `-wal` / `-shm` 伴随文件。
- FASTA 记录元数据、唯一 accession 结果及单次查询的分页结果保存在内存中；不是全流程恒定内存。超大输入或 secondary 候选很多时需评估内存。

## 当前验证与历史记录

当前目录没有旧文档提到的 `tests/`、`examples/taxonomy_smoke.fasta` 或 `validation/`，因此旧版 29 项测试和在线 smoke 命令不能直接重跑。2026-10-18 的通过记录属于原文档的历史声明，本次未复核其产物。

2026-10-24 本次检查使用项目现有环境（Python 3.11.9、requests 2.34.2、Biopython 1.88），在沙盒外成功启动。离线验证检查独立复制脚本后从其他工作目录调用、缓存驱动的完整 TSV / 报告 / 精确物种筛选，以及前七列的状态和空值规则；未请求实时 UniProt 接口，未安装依赖。

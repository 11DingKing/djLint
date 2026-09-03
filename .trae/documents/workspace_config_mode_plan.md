# 工作区配置模式（Workspace Config Mode）实现计划

## Repository Research

djLint 当前一次运行只构建**一个** `Config`：

- 入口 [main()](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/__init__.py#L459-L593)：用 `src[0]` 构造 `Config(src[0], **cli_options)`；stdin 分支与文件分支共用它。
- [Config.__init__](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/settings.py#L1291-L1359)：`find_project_root(src)` 向上找到第一个含 `.git/.hg/pyproject.toml/djlint.toml/.djlintrc` 的目录作为 `project_root`；`load_project_settings()` 只读取**该目录自身**的一个配置文件（pyproject `[tool.djlint]` 非空 → `djlint.toml/.djlint.toml` → `.djlintrc`，先到先得）；`--configuration` 全局文件与项目文件按 `{**named, **project}` 合并（项目优先；`--prefer-configuration` 反转）。
- 规则：默认 `rules.yaml` + `--rules` 或 `project_root/.djlint_rules.yaml`；`validate_rules()` 对无效规则只打 warning 并跳过。
- 文件发现 [get_src()](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/src.py#L82-L108)：目录用单一 `config.extension` 做 `**/*.{ext}` glob；用单一 `config.exclude_pattern`、`config.gitignore`（仅 project_root 一个 `.gitignore`）、`require_pragma` 过滤；resolved path 字典去重；`SrcFiles.excluded` 区分"全部被配置跳过"（exit 0）与"什么都没匹配"（exit 2）。
- 处理：`process(config, this_file)` → `reformat_file` / `lint_file`；多进/线程时 config 被 pickle 传给 worker。
- 输出：[print_output](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/output.py#L49-L121) / [print_github_output](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/github_output.py#L42-L67) 用单一 config 做路径相对化（`build_relative_path(..., config.project_root)`）、`linter_output_format`、统计消息表；测试直接调用 `build_stats_output((msg,), config)`。
- stdin 分支：`"-" in src` 时读 stdin；`files` 配置项仅用于 stdin 工作流（`djlint -` 时改处理配置里列出的文件）。

结论：多子项目一次运行时，所有文件共用首路径的 profile/extension/exclude/gitignore/规则/格式选项。需要新增**可选**工作区模式，按目录就近解析、逐层继承，且发现、lint、重排、规则、输出全部使用同一有效配置。

## 设计决策

1. **启用方式**：新增 CLI flag `--workspace`，同时支持配置键 `workspace = true`（由首路径基准 config 读取）。默认关闭，行为完全不变。
2. **工作区边界**：每个**目录参数**自身就是边界 root。某文件的有效配置 = 从边界目录向下到该文件所在目录，逐层收集配置文件合并（外层 → 内层，同键内层覆盖外层）；**边界之上的配置不读取**（隔离 monorepo 中无关的上层配置）。显式文件参数：归入包含它的最深目录 root；否则边界为 cwd（cwd 是其祖先时）或文件父目录。
3. **单层内文件优先级**：与现状一致（pyproject `[tool.djlint]` 非空 → `djlint.toml/.djlint.toml` → `.djlintrc`）。
4. **全局/CLI 优先级不变**：合并后的项目层与 `--configuration` 仍按现有规则合并（默认项目层胜，`--prefer-configuration` 时全局胜）；CLI 选项始终最高。逐层合并为按键覆盖（`extend_exclude` 用于跨层累加排除）。
5. **发现阶段作用域化**：`os.walk` 自上而下遍历每个 root；每个目录的有效 `Config` 缓存（仅当目录含配置文件/`.djlint_rules.yaml`/`.gitignore` 时才重建）；扩展名匹配、exclude、gitignore、require_pragma 全部用该作用域 config；被 exclude/gitignore 命中的子目录**剪枝**（不读取其中配置，避免 node_modules 等垃圾目录里的配置被严格校验），剪枝非空目录时置 `excluded=True` 以保持空结果 exit code 语义。
6. **gitignore 作用域**：开启 `use_gitignore`（CLI 或作用域配置）时，加载边界到该目录之间每层的 `.gitignore`，按各自所在目录相对匹配（git 语义）；边界层的 `.gitignore` 仍走 `config.gitignore`。
7. **自定义规则作用域**：边界→文件目录每层的 `.djlint_rules.yaml` 全部加载（外→内），CLI `--rules` 最后；多文件按规则名去重，近者/显式者胜。
8. **失败前置（严格模式）**：工作区模式下，配置文件解析失败、规则 YAML 解析失败、规则缺 name/patterns/python_module/message、非法 profile/quote-style、`.gitignore` 解析失败，均抛出带**文件路径**的 `UsageError`/`BadParameter`（exit 2）。所有作用域 config 在**发现阶段**（遍历整树时）全部构建完成，早于任何 reformat 写文件。非工作区模式保持现有"打 warning 后跳过/继续"行为。
9. **输出一致**：每个文件携带自己的作用域 config 进入处理与输出（路径相对化、`linter_output_format` 用作用域 config；统计消息表合并所有作用域规则）；运行级选项（quiet/progress/check/reformat）用基准 config。
10. **去重**：resolved-path 字典跨所有输入去重，重叠路径只处理一次（config 一致，因缓存按 (boundary, directory)）。
11. **stdin 不变**：`"-" in src` 时 `--workspace` 不生效，走既有 stdin / `files` 流程。

## Files and Modules

- [src/djlint/settings.py](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/settings.py)：
  - `Config`：新增 slots/参数 `workspace`、`gitignore_scopes`；新增内部参数 `_settings`、`_root`、`_extra_rules`、`_gitignore_scopes`、`_strict`（绕过向上查找、直接使用合并好的设置构建作用域 config，巨型 `__init__` 主体不动）。
  - `load_project_settings` / `_named_settings` / 目录配置查找：增加 `strict` 参数，严格时抛 `UsageError`（含文件路径），非严格保持 echo 继续。
  - `validate_rules`：增加 `strict`/`source` 关键字，严格时抛 `UsageError`（含规则文件路径与原因）。
  - 规则加载段：支持 `_extra_rules`（多规则文件外→内 + CLI `--rules` 最后），多文件时按规则名去重（近者胜）；严格模式 YAML 解析失败抛错。
  - 新增 `find_scope_config_file(directory, *, strict)` 与 `build_workspace_config(boundary, directory, cli_kwargs)`：逐层收集配置/规则/.gitignore，合并后构造作用域 `Config`。
- [src/djlint/src.py](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/src.py)：
  - 新增 `get_workspace_src(src, config, build_scope)`：os.walk 遍历 + 每目录作用域 config 缓存 + 剪枝 + 扩展名/排除/gitignore/pragma 过滤 + 显式文件边界处理 + 去重，返回 `list[tuple[Path, Config]]` 与 `excluded`。
  - `_gitignore_match`：除 project_root  spec 外，依次匹配 `config.gitignore_scopes` 中各层 `.gitignore`（非工作区模式为空，行为不变）。
- [src/djlint/__init__.py](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/__init__.py)：
  - 新增 `--workspace` click 选项与参数；Config 调用改为先组装 `config_kwargs` 字典再构造（供作用域构建复用）。
  - 文件分支：工作区模式调用 `get_workspace_src`，否则原 `get_src`；统一为 `entries: list[tuple[Path, Config]]`；worker 提交 `process(cfg, path)`；结果收集为 `(ProcessResult, Config)` 对。
  - stdin 分支完全不变。
- [src/djlint/output.py](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/output.py) 与 [src/djlint/github_output.py](file:///Users/huangding/Documents/new-SWE/09032/project-01/src/djlint/github_output.py)：
  - `file_errors` 改为 `Sequence[tuple[ProcessResult, Config]]`；每个文件用自己的 config 做 `build_output`/`build_check_output`/`print_lint_errors`/`print_format_errors`/`report_on_stderr`；统计消息表合并所有作用域 `linter_rules`（SimpleNamespace shim，`build_stats_output` 签名不变，直接调用方测试不受影响）。
- 测试：新增 `tests/test_config/test_workspace/test_workspace.py`（用 `tmp_path` + CliRunner 动态建树，不入库固定夹具）。
- 文档：在 [docs/src/docs/configuration.md](file:///Users/huangding/Documents/new-SWE/09032/project-01/docs/src/docs/configuration.md) 增加 `--workspace` 简短说明（英文主文档）。

## Implementation Steps

1. **settings.py 基础**：`Config` 增加 `workspace`、`gitignore_scopes` slot 与 `_settings/_root/_extra_rules/_gitignore_scopes/_strict` 内部参数；项目设置加载支持 strict；规则加载支持多文件 + 严格校验 + 去重；gitignore scopes 赋值。
2. **settings.py 作用域构建**：`find_scope_config_file` 与 `build_workspace_config`（逐层配置合并、`--configuration`/`--prefer-configuration` 优先级、逐层 `.djlint_rules.yaml`、逐层 `.gitignore`）。
3. **src.py**：`_gitignore_match` 支持 scopes；新增 `get_workspace_src`（walk、缓存、剪枝、过滤、显式文件、去重、excluded 语义）。
4. **__init__.py**：`--workspace` 选项、`config_kwargs` 复用、工作区分支与 `(result, config)` 结果流。
5. **output.py / github_output.py**：按文件 config 输出与统计合并。
6. **测试**：新增工作区测试用例（见 Validation）；跑全量 pytest、mypy、ruff。
7. **文档**：configuration.md 增补一节。

## Dependencies and Considerations

- Config 被 pickle 到进程池：新增字段（Path、PathSpec、编译后正则、tuple）均可 pickle。
- `build_stats_output((msg,), config)` 被测试直接调用——签名保持不变。
- `print_output` 被 monkeypatch（`hang_up(*_args, **_kwargs)`）——签名宽松，兼容。
- 剪枝策略：被作用域 exclude/gitignore 命中的目录整棵跳过（含其中的配置文件）；"在被 exclude 的目录里放配置试图复活文件"不支持（与 Black/prettier 一致），计划在文档中说明。
- 空结果语义：剪枝非空目录置 `excluded=True`（"全部被跳过"→exit 0）；空目录/无模板文件不计，保持 exit 2 语义（参见 test_no_files）。
- 边界之上配置不读取：基准 config 仍按现有方式加载用于运行级开关；文件级发现/处理/输出只认边界内作用域 config。
- `os.walk(followlinks=False)` 防符号链接环。

## Validation

新增测试（tmp_path 动态建树）覆盖：

1. 两子项目不同 profile/extension：发现与 lint 结果各自正确（如 a 用 django + `.html`，b 用 golang + `.tmpl`）。
2. 逐层继承：根配置 `ignore`/indent 等被无子配置的子目录继承，子配置按键覆盖。
3. 发现作用域：a 的 exclude 只影响 a；a 的 `use_gitignore` + `.gitignore` 只忽略 a 中文件。
4. 自定义规则作用域：a/b 各自 `.djlint_rules.yaml` 只在本作用域生效。
5. CLI 覆盖：`--profile` 等对所有作用域生效；`--configuration` 作用于全部作用域，`--prefer-configuration` 反转优先级。
6. 重叠输入（root 与子目录同传）只处理一次（文件计数）。
7. 无效配置（坏 TOML / 非法 profile）与无效规则（缺字段/坏 YAML）：exit 2 且错误信息含具体文件路径；`--reformat` 模式下目标文件未被修改。
8. 未启用 `--workspace`：维持旧行为（子目录配置被忽略）。
9. `--workspace` 与 stdin（`-`）同用：stdin 行为不变。
10. 单项目 + `--workspace`：与普通模式结果一致。

回归：`uv run pytest` 全量通过；`mypy src/djlint` 与 `ruff check` 无新增问题。

## Risks

- **巨型 `Config.__init__` 改动风险**：采用内部 kwargs 注入合并后设置，主体逻辑零改动，非工作区路径逐行不变；以全量现有测试兜底。
- **性能**：每目录构建 Config（编译正则）有成本；通过"仅含配置/规则/gitignore 的目录才重建、其余继承父级"缓存控制，且剪枝避免遍历 node_modules 等。
- **输出签名变更**波及 github_output：两处同步改为 `(result, config)` 对流，stdin 分支 config 同一对象，输出字节级不变。
- **严格模式误报**：只对边界内、且未被剪枝的目录生效；第三方垃圾目录（node_modules 等）被默认 exclude 剪枝，不会触发严格校验。

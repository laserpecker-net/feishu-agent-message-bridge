"""WSL 侧装 DSH 时写的 `models:` 必须是**网关的整套清单**，不是只有默认模型那一个。

真机踩过（docs/HISTORY.md 踩坑 22）：网关服务 3 个模型，`install-dsh.sh` 只写 `--model` 那一个，
同事的模型选择器里就只剩一个。清单是网关的事实，脚本里不该有第二份；取不到才退回默认模型。

这里不跑脚本（它要一台真的 WSL 机器），锁的是**契约**：清单从网关问、每个 id 都落进 yaml、
取不到时有 warn 兜底、断言阶段逐个 id 检查。Windows 侧同一套语义由
`tools/windows/installer/apply.Tests.ps1` 的纯函数测试覆盖。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'dist' / 'wsl2' / 'wsl' / 'install-dsh.sh'
TEXT = SCRIPT.read_text(encoding='utf-8')


def test_清单来自网关而不是脚本里的常量():
    # 网关的 OpenAI 约定端点：用渠道 key 问，网关服务哪些就写哪些
    assert '/v1/models' in TEXT
    assert 'Authorization: Bearer $API_KEY' in TEXT
    # `--model` 只是默认模型（帮助文本与校验信息都要说清楚，别让人以为它是清单）
    assert '--model 只是**默认模型**' in TEXT
    assert '缺少 --model（默认模型 id；可选清单由网关 /v1/models 决定）' in TEXT


def test_每个模型都真的写进_yaml():
    # 逐个 id 生成两行（id + name），而不是只写一个
    assert 'while IFS= read -r _mid' in TEXT
    assert '- id: ${_mid}' in TEXT
    assert 'name: ${_mid}' in TEXT
    # 生成的片段接在 models: 下面，而不是另起一个根节点
    assert '${MODEL_LINES}- id: agent-default-model' in TEXT


def test_默认模型一定在清单里():
    # agent-default-model 指向清单外的 id = 选择器里一个永远选不中的模型
    assert "case $'\\n'\"$MODELS\"$'\\n' in" in TEXT
    assert 'MODELS="$MODEL"$\'\\n\'"$MODELS"' in TEXT
    # 清单本身去重
    assert "awk 'NF && !seen[$0]++'" in TEXT


def test_取不到清单时退回默认模型并且吵出来():
    # 静默降级被禁止：走兜底必须打 warn（网关不提供 /v1/models 时会看到这条）
    assert '取不到 ${BASE_URL%/v1}/v1/models 的模型清单，只写默认模型 $MODEL' in TEXT
    assert '>&2' in TEXT


def test_装完逐个模型在_dump_里验一遍():
    # 「文件里写了」不等于「合成进配置了」—— 断言阶段要逐个 id 在 dump 里出现
    assert 'dump 里没有模型 $_mid' in TEXT
    assert '[[ $DUMP == *"$_mid"* ]]' in TEXT


def test_摘要行带上模型个数():
    assert 'models=$(printf ' in TEXT

"""第三道闸门的**默认值**是安全姿态的选择（设计文档 11.8.1）。

## 为什么单独给默认值开一组测试

闸门开 = QM 意义上的 Strict，关 = Dangerous。**默认值决定的是失效方向**：
默认关意味着「忘了加旗标就静默进入无人值守」，而 postmortem/0001 实测
opencode 默认 allow —— 不传 ``--auto`` 也全部放行，写出目录外都执行。
那种情况下「跑起来了」比「跑不起来」危险得多。

所以「默认是什么」必须被钉住，不能靠读代码确认。

## 为什么 parser 得先被提出来

``main()`` 会去探 opencode 版本、真的派发。直接测它就得先装 opencode、
造飞书凭据 —— 测的就不是默认值，而是环境。默认值必须能被单独钉住。
"""

import pytest

from freeagent.delegate import build_parser


def test_gate_is_on_by_default():
    """**不传任何旗标也必须开闸。**

    这是本组测试存在的主要理由：默认值是安全姿态，而安全姿态不能是
    「记得加旗标」。
    """
    assert build_parser().parse_args([]).tool_gate is True


def test_explicit_flag_still_accepted():
    """``--tool-gate`` **必须仍然可用** —— 兼容性硬要求。

    它写在 README、``tools\\_svc_executor.cmd`` 与既有运维习惯里。
    删掉会把这些命令行变成**报错**（而不是变安全）—— 而一个报错的新参数
    会逼着人在排障时去加旗标，那是倒退。
    """
    assert build_parser().parse_args(["--tool-gate"]).tool_gate is True


def test_opt_out_is_explicit_and_works():
    """关闸必须是**显式**动作，且真的关掉。"""
    assert build_parser().parse_args(["--no-tool-gate"]).tool_gate is False


def test_the_two_gate_flags_are_mutually_exclusive():
    """同时给两个必须报错 —— 不能「后一个悄悄赢」。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--tool-gate", "--no-tool-gate"])


def test_extracting_the_parser_kept_every_other_flag():
    """提取 :func:`build_parser` 时别把别的旗标弄掉了。

    这条是**回归**：提取是个纯结构改动，但它动的是所有运维命令行的入口，
    少一个 ``--db`` / ``--dry-run`` / ``--watch`` / ``--model`` 都是
    生产事故。逐个断言，不靠「跑一遍看看没报错」。
    """
    args = build_parser().parse_args(
        ["--db", "x.db", "--dry-run", "--watch", "5", "--model", "m"]
    )
    assert str(args.db) == "x.db"
    assert args.dry_run is True
    assert args.watch == 5.0
    assert args.model == "m"
"""``check_project_allowed`` 的 ``known_names`` **提示**行为。

## 守的是一条安全边界

``known_names`` 只出现在**错误文案**里，**绝不参与放行判定**。一旦让提示里
的名字影响到准入，白名单就成了摆设——所以下面有一条测试专门盯这件事。
"""

import pytest

from freeagent.domain import FreeAgentError
from freeagent.services.delegate import DelegationPolicy, check_project_allowed

POLICY = DelegationPolicy(projects=("D:/PycharmProjects/openmos",))


def _err(fn, *a, **kw):
    with pytest.raises(FreeAgentError) as ei:
        fn(*a, **kw)
    return str(ei.value)


def test_hint_lists_names_when_not_allowed():
    msg = _err(check_project_allowed, POLICY, "D:/nope",
               known_names=["FreeAgent", "OpenMOS"])
    assert "FreeAgent" in msg and "OpenMOS" in msg


def test_hint_absent_when_no_names():
    """没有可授权项目时**不加那句**——那时用户要解决的是授权，不是选名字。"""
    msg = _err(check_project_allowed, POLICY, "D:/nope")
    assert "项目名代替路径" not in msg


def test_blank_names_are_ignored():
    msg = _err(check_project_allowed, POLICY, "D:/nope",
               known_names=["", "  "])
    assert "项目名代替路径" not in msg


def test_empty_input_also_gets_hint():
    msg = _err(check_project_allowed, POLICY, "",
               known_names=["OpenMOS"])
    assert "OpenMOS" in msg


def test_relative_path_error_also_gets_hint():
    msg = _err(check_project_allowed, POLICY, "rel/path",
               known_names=["OpenMOS"])
    assert "绝对路径" in msg and "OpenMOS" in msg


def test_names_never_grant_access():
    """**名字只是提示。** 只给名字、路径不在白名单 → 必须仍然拒。"""
    with pytest.raises(FreeAgentError):
        check_project_allowed(POLICY, "OpenMOS", known_names=["OpenMOS"])


def test_names_never_grant_access_via_subdir():
    with pytest.raises(FreeAgentError):
        check_project_allowed(POLICY, "D:/PycharmProjects/other",
                              known_names=["OpenMOS"])
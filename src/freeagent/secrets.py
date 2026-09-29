"""``SecretStr``：**在类型层面**让密钥无法被顺手打印出去。

## 要解决的问题

「密钥不进日志」这种事，靠自觉是会漏的。真实踩过的坑就写在
``feishu/config.py`` 的 ``__repr__`` 注释里：``print(cfg)``、
``log.info("%s", cfg)``、异常里带 config，都是极常见的写法，于是
「只从环境变量读、不落盘」这条承诺会被**一次日志输出**破掉。

在容器上覆写 ``__repr__`` 是**约定** —— 它只保护那一个容器。
而密钥值本身是可以到处传的：``f"{cfg.app_secret}"``、``log.info("key=%s", cfg.app_secret)``、
``raise ValueError(f"bad key {secret}")``，这些写法新写一处就可能漏。

``SecretStr`` 把取值收窄到 :meth:`use` **一个出口**，并且
``__repr__`` / ``__str__`` 都只能给出遮蔽预览。于是密钥**离开这个类型时
必然是显式的**：代码里出现 ``.use()`` 就说明作者知道自己在拿原文。

## 遮蔽强度：比参照实现更保守

对照项目（GenericAgent ``memory/keychain.py``）的预览是「前 6 + 后 6 + 长度」，
32 位的 App Secret 会露出 12 个字符。这里**只露前 3 位 + 长度**：

- 前 3 位足够用来判断「控制台上那条和我这份是不是同一个」—— 这是遮蔽的正当用途；
- 尾部往往是熵最高的一段，没必要露；
- 长度本身已经在现有输出里出现过（doctor 会打印），不算新增泄露。

## 刻意不做的事

**不提供任何密钥持久化机制。** 进程环境变量随进程消亡，比任何「加密文件」都可靠 ——
参照项目用 ``sha256(用户名 + 常量)`` 做 XOR 混淆，那不是加密：常量写死在源码里，
知道用户名就能解，反而因为看起来安全而更危险。密钥只从环境变量读（见 12.3）。
"""

from __future__ import annotations

__all__ = ["SecretStr", "as_secret", "secret_use"]

#: 预览里露出的前缀长度。够确认「是不是同一条」，不够还原。
PREFIX_CHARS = 3


class SecretStr:
    """一个刻意不便于打印的字符串。

    刻意**不**继承 ``str``：子类化 ``str`` 会让它在格式化、拼接、
    字典键等场景里**表现得和普通字符串一样**，那正是要防的事。
    """

    __slots__ = ("_value", "_label")

    def __init__(self, value: str, *, label: str = "secret") -> None:
        if not isinstance(value, str):
            raise TypeError(f"SecretStr 只接受 str，收到 {type(value).__name__}")
        self._value = value
        self._label = label

    # -- 取值：唯一出口 ---------------------------------------------------- #
    def use(self) -> str:
        """**唯一**拿到原文的出口。

        出现在代码里就等于明确表态「这里真的要发请求」，
        审阅时能一眼看见密钥流向了哪里。
        """
        return self._value

    # -- 遮蔽 -------------------------------------------------------------- #
    def preview(self) -> str:
        """人看的遮蔽预览。不含尾部。"""
        n = len(self._value)
        if n == 0:
            return "（空）"
        head = self._value[:PREFIX_CHARS]
        return f"{head}…len={n}"

    def __repr__(self) -> str:
        return f"SecretStr({self._label}={self.preview()})"

    # str() 也走遮蔽：f"{secret}" 与 print(secret) 是最常见的两种泄露方式
    __str__ = __repr__

    # -- 行为兼容 ---------------------------------------------------------- #
    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        """和裸字符串也能比。

        不做的话 ``cfg.app_secret == "xxx"`` 会静默为 ``False``，
        那是个很难查的坑 —— 而比较本身不泄露任何东西。
        """
        if isinstance(other, SecretStr):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


def as_secret(value: str | SecretStr | None, *, label: str = "secret") -> SecretStr | None:
    """把可能是裸字符串的值收敛成 ``SecretStr``；``None`` 与空串透传为 ``None``。

    给「从环境变量读、可能还没配」的场合用：未配置时返回 ``None``，
    调用方直接判空即可，不必到处写 ``or SecretStr("")``。
    """
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return value
    text = value.strip() if isinstance(value, str) else value
    return SecretStr(text, label=label) if text else None


def secret_use(value: str | SecretStr) -> str:
    """把两种形态统一成裸字符串。

    只在**真的要发请求**的边界用（比如拼 ``Authorization`` 头）。
    偏好直接写 ``.use()``；这个函数是为了让「调用方可能传裸字符串」
    的公开构造函数不必到处判类型。
    """
    return value.use() if isinstance(value, SecretStr) else value

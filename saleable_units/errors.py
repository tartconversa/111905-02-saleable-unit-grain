"""仓库内统一的错误类型。"""


class SourceError(ValueError):
    """来源资料违反格式或约定（阻断整次入仓）。"""


class DomainError(RuntimeError):
    """领域状态无法按当前口径解释（应转成 blocker，而不是崩溃）。"""

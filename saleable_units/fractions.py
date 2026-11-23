"""精确有理数与左闭右开有理区间的解析、运算与规范化。

来源约定：
* 份额以 ``分子/分母`` 十进制字符串表达，必须精确；
* 面积是平方米十进制字符串，只在展示阶段才允许舍入；
* 区间端点是无理数意义上的有理数，区间一律左闭右开 ``[a, b)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from .errors import SourceError


def parse_fraction(text: object, *, label: str = "fraction") -> Fraction:
    """把 ``a/b`` 或整数字符串解析为精确 Fraction。"""

    if not isinstance(text, str):
        raise SourceError(f"{label}: 份额必须是字符串，实际为 {type(text).__name__}")
    body = text.strip()
    if "/" in body:
        numerator, _, denominator = body.partition("/")
    else:
        numerator, denominator = body, "1"
    try:
        num = int(numerator.strip())
        den = int(denominator.strip())
    except ValueError as exc:
        raise SourceError(f"{label}: 无法解析份额 {text!r}") from exc
    if den == 0:
        raise SourceError(f"{label}: 分母为零 {text!r}")
    if den < 0:
        num, den = -num, -den
    return Fraction(num, den)


def parse_interval(values: object, *, label: str = "source_slice") -> tuple[Fraction, Fraction]:
    """把 ``["a/b","c/d"]`` 解析为左闭右开区间并校验非空。"""

    if not isinstance(values, list) or len(values) != 2:
        raise SourceError(f"{label}: source_slice 必须是含两个端点的数组")
    start = parse_fraction(values[0], label=f"{label}[0]")
    end = parse_fraction(values[1], label=f"{label}[1]")
    if not (0 <= start < end <= 1):
        raise SourceError(f"{label}: 区间必须满足 0 <= start < end <= 1，实际为 {values!r}")
    return (start, end)


def parse_decimal(text: object, *, label: str = "decimal") -> Decimal:
    if not isinstance(text, (str, int)) or isinstance(text, bool):
        raise SourceError(f"{label}: 必须以十进制字符串表达，实际为 {type(text).__name__}")
    try:
        value = Decimal(str(text))
    except InvalidOperation as exc:
        raise SourceError(f"{label}: 无法解析十进制数值 {text!r}") from exc
    if not value.is_finite():
        raise SourceError(f"{label}: 数值必须有限，实际为 {text!r}")
    return value


def format_fraction(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def format_interval(start: Fraction, end: Fraction) -> str:
    return f"[{format_fraction(start)},{format_fraction(end)})"


def interval_intersection(
    left: tuple[Fraction, Fraction], right: tuple[Fraction, Fraction]
) -> tuple[Fraction, Fraction] | None:
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    if start >= end:
        return None
    return (start, end)


def interval_measure(interval: tuple[Fraction, Fraction]) -> Fraction:
    return interval[1] - interval[0]


def interval_within(
    inner: tuple[Fraction, Fraction], outer: tuple[Fraction, Fraction]
) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def aggregate_intervals(
    intervals: list[tuple[Fraction, Fraction]],
) -> tuple[list[tuple[Fraction, Fraction]], Fraction]:
    """合并重叠/相接区间，返回规范区间列表与总长度。

    重叠本身不是错误：沿革的 source_slice 互相重叠时由调用方按区间并集判定
    “来源重叠”，并按并集计算份额，避免把同一段旧空间重复计入。
    """

    ordered = sorted(intervals)
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total = sum((end - start for start, end in merged), Fraction(0))
    return merged, total


def covers_without_overflow(merged: list[tuple[Fraction, Fraction]]) -> bool:
    return all(start < end for start, end in merged)


def decimal_to_fraction(value: Decimal) -> Fraction:
    return Fraction(value)


def fraction_to_decimal(value: Fraction, places: str = "0.0001") -> Decimal:
    """按给定精度量化，仅用于对外展示，不参与后续计算。"""

    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal(places))

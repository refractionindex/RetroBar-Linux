""".NET DateTime format strings ("t", "D", "ddd MMM d  h:mm:ss tt", ...) rendered with the system locale."""

from __future__ import annotations

import re
from datetime import datetime

from PyQt6.QtCore import QDate, QDateTime, QLocale, QTime

_TOKENS = re.compile(r"dddd|ddd|dd|d|MMMM|MMM|MM|M|yyyy|yyy|yy|y|HH|H|hh|h|mm|m|ss|s|tt|t|fff|ff|f|zzz|zz|z|'[^']*'|\"[^\"]*\"|\\.|.", re.S)


_short_time_override: str | None = None
_designators: tuple[str | None, str | None] = (None, None)


def set_short_time_pattern(pattern: str | None, am: str | None = None, pm: str | None = None) -> None:
    """Replace the culture's short time pattern ("t"), as the clock settings do on Windows."""
    global _short_time_override, _designators
    _short_time_override = pattern or None
    _designators = (am or None, pm or None)


def _qdt(value: datetime) -> QDateTime:
    return QDateTime(QDate(value.year, value.month, value.day), QTime(value.hour, value.minute, value.second))


def format_datetime(value: datetime, fmt: str | None, am: str | None = None, pm: str | None = None) -> str:
    locale = QLocale()
    if not fmt:
        fmt = "G"
    am = am or _designators[0]
    pm = pm or _designators[1]
    if fmt == "t" and _short_time_override:
        fmt = _short_time_override
    if len(fmt) == 1:
        standard = _standard(locale, value, fmt)
        if standard is not None:
            # CLDR puts a narrow no-break space before AM/PM; Windows patterns use a plain space.
            return standard.replace("\u202f", " ")
    return _custom(locale, value, fmt, am, pm)


def _standard(locale: QLocale, value: datetime, fmt: str) -> str | None:
    qdt = _qdt(value)
    short, long_ = QLocale.FormatType.ShortFormat, QLocale.FormatType.LongFormat
    match fmt:
        case "t":
            return locale.toString(qdt.time(), short)
        case "T":
            # Qt's long time format ends with a time zone ("t"), which .NET's "T" does not show.
            long_time = re.sub(r"\s*t+\s*$", "", locale.timeFormat(long_)).strip()
            return locale.toString(qdt.time(), long_time)
        case "d":
            return locale.toString(qdt.date(), short)
        case "D":
            return locale.toString(qdt.date(), long_)
        case "f":
            return f"{locale.toString(qdt.date(), long_)} {locale.toString(qdt.time(), short)}"
        case "g":
            return f"{locale.toString(qdt.date(), short)} {locale.toString(qdt.time(), short)}"
        case "G":
            return locale.toString(qdt, short)
    return None


def _custom(locale: QLocale, value: datetime, fmt: str, am: str | None, pm: str | None) -> str:
    out = []
    for tok in _TOKENS.findall(fmt):
        match tok:
            case "dddd":
                out.append(locale.dayName(value.isoweekday(), QLocale.FormatType.LongFormat))
            case "ddd":
                out.append(locale.dayName(value.isoweekday(), QLocale.FormatType.ShortFormat))
            case "dd":
                out.append(f"{value.day:02d}")
            case "d":
                out.append(str(value.day))
            case "MMMM":
                out.append(locale.standaloneMonthName(value.month, QLocale.FormatType.LongFormat))
            case "MMM":
                out.append(locale.monthName(value.month, QLocale.FormatType.ShortFormat))
            case "MM":
                out.append(f"{value.month:02d}")
            case "M":
                out.append(str(value.month))
            case "yyyy" | "yyy":
                out.append(f"{value.year:04d}")
            case "yy":
                out.append(f"{value.year % 100:02d}")
            case "y":
                out.append(str(value.year % 100))
            case "HH":
                out.append(f"{value.hour:02d}")
            case "H":
                out.append(str(value.hour))
            case "hh":
                out.append(f"{(value.hour % 12) or 12:02d}")
            case "h":
                out.append(str((value.hour % 12) or 12))
            case "mm":
                out.append(f"{value.minute:02d}")
            case "m":
                out.append(str(value.minute))
            case "ss":
                out.append(f"{value.second:02d}")
            case "s":
                out.append(str(value.second))
            case "tt" | "t":
                text = (am or locale.amText()) if value.hour < 12 else (pm or locale.pmText())
                out.append(text if tok == "tt" else text[:1])
            case "fff" | "ff" | "f":
                out.append(f"{value.microsecond // 1000:03d}"[: len(tok)])
            case _ if tok[:1] in "'\"":
                out.append(tok[1:-1])
            case _ if tok.startswith("\\"):
                out.append(tok[1:])
            case _:
                out.append(tok)
    return "".join(out)

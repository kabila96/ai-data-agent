"""Safe CSV loading, type inference and user-approved type overrides.

Nothing is silently dropped: every coercion reports how many values failed to
convert so the user can decide what to do.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from security.errors import DataSourceError

SemanticType = Literal["integer", "numeric", "datetime", "boolean", "category", "text", "identifier"]
TYPE_CHOICES: tuple[SemanticType, ...] = (
    "integer", "numeric", "datetime", "boolean", "category", "text", "identifier",
)

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}
_DATE_NAME_HINT = re.compile(r"(date|time|day|month|period|_at$|_on$|timestamp)", re.I)
_ID_NAME_HINT = re.compile(r"(^id$|_id$|^id_|code$|_code$|_key$|^key$|uuid|postcode|zip)", re.I)


@dataclass
class LoadedCSV:
    name: str
    data: pd.DataFrame
    raw: pd.DataFrame  # original values as trimmed text, used to re-apply type overrides
    inferred_types: dict[str, SemanticType]
    encoding: str
    delimiter: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class CoercionReport:
    column: str
    target: SemanticType
    failed: int
    examples: list[str]


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise DataSourceError("Could not decode the file. Save it as UTF-8 CSV and try again.")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def load_csv(name: str, raw: bytes, max_mb: int = 200) -> LoadedCSV:
    """Parse uploaded bytes into a DataFrame with every column read as text first."""
    if len(raw) > max_mb * 1024 * 1024:
        raise DataSourceError(f"File is larger than the {max_mb} MB upload limit.")
    if not raw.strip():
        raise DataSourceError("The file is empty.")
    text, encoding = _decode(raw)
    delimiter = _sniff_delimiter(text[:20_000])
    try:
        # header=None so pandas does not silently mangle duplicate names ("a", "a.1");
        # the header row is cleaned explicitly below.
        df = pd.read_csv(
            io.StringIO(text), sep=delimiter, dtype=str, keep_default_na=False, header=None,
            na_values=["", "NA", "N/A", "NULL", "null", "NaN", "nan", "None", "#N/A"],
            skipinitialspace=True, on_bad_lines="warn", engine="python",
        )
    except Exception as exc:  # pandas raises several parser error types
        raise DataSourceError(f"Could not parse CSV: {str(exc).splitlines()[0][:200]}") from exc
    if df.empty and len(df.columns) == 0:
        raise DataSourceError("No columns found in the file.")

    warnings: list[str] = []
    header = ["" if pd.isna(v) else str(v) for v in df.iloc[0].tolist()]
    df = df.iloc[1:].reset_index(drop=True)
    df.columns = _clean_column_names(header, warnings)
    for col in df.columns:
        df[col] = df[col].astype("string").str.strip()
    inferred = {col: infer_semantic_type(col, df[col]) for col in df.columns}
    converted, reports = apply_types(df, inferred)
    for rep in reports:
        if rep.failed:
            warnings.append(
                f"{rep.failed} value(s) in '{rep.column}' could not be read as {rep.target} "
                f"and were left blank (e.g. {', '.join(rep.examples[:3])})."
            )
    return LoadedCSV(name=name, data=converted, raw=df, inferred_types=inferred,
                     encoding=encoding, delimiter=delimiter, warnings=warnings)


def _clean_column_names(columns: list[str], warnings: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: dict[str, int] = {}
    for i, col in enumerate(columns):
        name = str(col).strip() or f"column_{i + 1}"
        if name.lower().startswith("unnamed:"):
            name = f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            new = f"{name}_{seen[name]}"
            warnings.append(f"Duplicate column name '{name}' renamed to '{new}'.")
            name = new
        else:
            seen[name] = 0
        cleaned.append(name)
    return cleaned


def _parse_dates(values: pd.Series) -> pd.Series:
    """Parse dates, preferring ISO then day-first (UK) conventions."""
    iso = pd.to_datetime(values, errors="coerce", format="ISO8601")
    if iso.notna().sum() >= values.notna().sum() * 0.9:
        return iso
    uk = pd.to_datetime(values, errors="coerce", dayfirst=True, format="mixed")
    return uk if uk.notna().sum() >= iso.notna().sum() else iso


def infer_semantic_type(name: str, series: pd.Series) -> SemanticType:
    values = series.dropna()
    values = values[values.astype(str).str.len() > 0]
    if values.empty:
        return "text"
    sample = values.astype(str).head(5000)
    lowered = sample.str.lower()
    n = len(sample)

    if lowered.isin(_TRUE | _FALSE).mean() > 0.98 and lowered.nunique() <= 2 and not sample.str.fullmatch(r"[01]").all():
        return "boolean"

    numeric = pd.to_numeric(sample.str.replace(",", "", regex=False).str.replace("£", "", regex=False), errors="coerce")
    numeric_ratio = numeric.notna().mean()
    if numeric_ratio >= 0.95:
        leading_zero = sample.str.match(r"^0\d").mean() > 0.05
        if _ID_NAME_HINT.search(name) or leading_zero:
            return "identifier"
        if (numeric.dropna() % 1 == 0).all():
            return "integer"
        return "numeric"

    if _DATE_NAME_HINT.search(name) or sample.str.match(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}").mean() > 0.8:
        parsed = _parse_dates(sample)
        if parsed.notna().mean() >= 0.8:
            return "datetime"

    if _ID_NAME_HINT.search(name):
        return "identifier"
    unique = sample.nunique()
    if unique <= max(30, int(n * 0.05)) and sample.str.len().mean() < 60:
        return "category"
    return "text"


def apply_types(
    df: pd.DataFrame, types: dict[str, SemanticType]
) -> tuple[pd.DataFrame, list[CoercionReport]]:
    """Return a converted copy plus a report of values that failed to convert."""
    out = df.copy()
    reports: list[CoercionReport] = []
    for col, target in types.items():
        if col not in out.columns:
            continue
        original = out[col].astype("string")
        present = original.notna() & (original.str.len() > 0)
        if target in ("integer", "numeric"):
            cleaned = original.str.replace(",", "", regex=False).str.replace("£", "", regex=False).str.strip()
            converted = pd.to_numeric(cleaned, errors="coerce")
            if target == "integer" and (converted.dropna() % 1 == 0).all():
                converted = converted.astype("Int64")
            else:
                converted = converted.astype("Float64")
        elif target == "datetime":
            converted = _parse_dates(original)
        elif target == "boolean":
            lowered = original.str.lower()
            converted = pd.Series(pd.NA, index=original.index, dtype="boolean")
            converted[lowered.isin(_TRUE)] = True
            converted[lowered.isin(_FALSE)] = False
        elif target == "category":
            converted = original.astype("category")
        else:  # text / identifier
            converted = original
        failed_mask = present & pd.Series(converted, index=original.index).isna()
        failed = int(failed_mask.sum())
        examples = original[failed_mask].dropna().astype(str).unique()[:5].tolist()
        reports.append(CoercionReport(col, target, failed, examples))
        out[col] = converted
    return out, reports


def to_raw_strings(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a typed frame back to strings so types can be re-applied."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M:%S").str.replace(" 00:00:00", "", regex=False)
        out[col] = out[col].astype("string")
    return out.replace({np.nan: pd.NA})

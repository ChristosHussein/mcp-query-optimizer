import re
from dataclasses import dataclass
from typing import Any

_PLAIN_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass(frozen=True)
class IndexDefinition:
    """Immutable index configuration for hashing."""

    table: str
    columns: tuple[str, ...]
    using: str = "btree"
    where: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "table": self.table,
            "columns": list(self.columns),
            "using": self.using,
            "definition": self.definition,
        }
        if self.where:
            out["where"] = self.where
        return out

    @property
    def definition(self) -> str:
        formatted = [
            c if _PLAIN_IDENT.match(c.strip()) or (c.strip().startswith("(") and c.strip().endswith(")")) else f"({c.strip()})"
            for c in self.columns
        ]
        where_sql = f" WHERE {self.where.strip()}" if (self.where and self.where.strip()) else ""
        return f"CREATE INDEX {self.name} ON {self.table} USING {self.using} ({', '.join(formatted)}){where_sql}"

    @property
    def name(self) -> str:
        # Clean column names for use in index naming
        # Replace special characters with underscores to avoid issues with
        # functional expressions
        cleaned_columns = []
        for col in self.columns:
            cleaned_col = re.sub(r"[^a-zA-Z0-9_]+", "_", col).strip("_")
            cleaned_columns.append(cleaned_col or "expr")

        column_part = "_".join(cleaned_columns)
        suffix = "" if self.using == "btree" else f"_{self.using}"
        base = f"crystaldba_idx_{self.table}_{column_part}_{len(self.columns)}"
        return f"{base}{suffix}"

    def __str__(self) -> str:
        return self.definition

    def __repr__(self) -> str:
        return f"IndexConfig(table='{self.table}', columns={self.columns}, using='{self.using}')"

"""Adapt Markdown tables to Discord's supported text formatting."""

import re


def _cells(line):
    """Split unescaped pipes outside inline code, with optional outer pipes."""
    line = line.strip()
    cells, start, index = [], 0, 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            run = re.match(r"`+", line[index:])[0]
            end = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", line[index + len(run):])
            if end:
                index += len(run) + end.end()
                continue
            index += len(run)
            continue
        if char == "|":
            cells.append(line[start:index].strip())
            start = index + 1
        index += 1
    if not cells:
        return None
    cells.append(line[start:].strip())
    if line.startswith("|"):
        cells.pop(0)
    if start == len(line):
        cells.pop()
    return cells


def discord_tables(text):
    """Render complete pipe tables as labeled rows before message chunking.

    Prose and code examples stay verbatim. Requiring a matching delimiter row
    avoids treating ordinary pipes as tables; malformed rows remain visible.
    """
    lines = text.splitlines(keepends=True)
    out = []
    index = 0
    fence = None
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        elif not line.startswith(("    ", "\t")) and index + 1 < len(lines):
            headers = _cells(line)
            separator = _cells(lines[index + 1])
            if (headers and separator and len(headers) == len(separator)
                    and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)):
                end = index + 2
                rows = []
                while end < len(lines):
                    cells = _cells(lines[end])
                    if cells is None or len(cells) != len(headers):
                        break
                    rows.append(cells)
                    end += 1
                if rows:
                    rendered = []
                    for row in rows:
                        fields = []
                        for column, (header, value) in enumerate(zip(headers, row), 1):
                            label = header or f"Column {column}"
                            if not (label.startswith("**") and label.endswith("**")):
                                label = f"**{label}**"
                            fields.append(f"{label}: {value}")
                        rendered.append("- " + "\n  ".join(fields))
                    out.append("\n\n".join(rendered))
                    if lines[end - 1].endswith("\n"):
                        out.append("\n")
                    index = end
                    continue
        out.append(line)
        index += 1
    return "".join(out)

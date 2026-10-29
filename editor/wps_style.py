"""Convert standard LaTeX into the subset WPS's formula editor emits.

WPS writes binomials as ``\\left(\\frac{n}{k}\\right)`` and puts summation
limits as ``\\sum \\limits^{n}_{k=0}``.  Standard LaTeX (and our OCR output)
uses ``\\binom{n}{k}`` and ``\\sum_{k=0}^{n}``.  Both render the same; this
conversion is for pasting into WPS.
"""

from __future__ import annotations

import re


def _convert_braced_aligned(latex: str) -> str:
    """`\\left\\{\\begin{aligned}...\\end{aligned}\\right.` -> `cases`.

    OCR models often build piecewise definitions from aligned + empty groups
    (`{} & {}`).  WPS renders those empty groups as dotted placeholder boxes,
    so collapse them into a plain cases block.
    """
    pattern = re.compile(
        r"\\left\\\{\\begin\{aligned\}(.*?)\\end\{aligned\}\s*\\right\.", re.S
    )

    def repl(match: re.Match) -> str:
        rows = []
        for raw in match.group(1).split("\\\\"):
            raw = raw.strip()
            if not raw:
                continue
            cells = []
            for cell in raw.split("&"):
                cell = re.sub(r"^(?:\{\s*\})+\s*", "", cell.strip())
                if cell:
                    cells.append(cell)
            if cells:
                rows.append(" ".join(cells))
        if not rows:
            return match.group(0)
        return "\\begin{cases} " + " \\\\ ".join(rows) + " \\end{cases}"

    return pattern.sub(repl, latex)


def _read_group(s: str, i: int):
    if i >= len(s) or s[i] != "{":
        return None, i
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
    return None, i


def _read_script(s: str, i: int):
    if i + 1 < len(s) and s[i + 1] == "{":
        return _read_group(s, i + 1)
    if i + 1 < len(s):
        return s[i + 1], i + 2
    return None, i


def _strip_after(s: str, i: int):
    """If a redundant ``{...}`` group starts here, return its content."""
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    if i < len(s) and s[i] == "{":
        content, j = _read_group(s, i)
        if content is not None and "\\\\" not in content and "&" not in content:
            return content, j
    return None, i


def strip_row_braces(latex: str) -> str:
    """Drop ``{...}`` wrappers around full cases rows/cells.

    OCR models often emit ``\\begin{cases} {row1} \\\\ {row2} \\end{cases}``.
    Standard LaTeX tolerates the extra group, but WPS's parser drops the first
    row and renders only the second.  Removing the wrappers fixes it.
    """
    out = []
    i = 0
    n = len(latex)
    while i < n:
        if latex.startswith("\\begin{cases}", i):
            out.append("\\begin{cases}")
            i += len("\\begin{cases}")
            content, j = _strip_after(latex, i)
            if content is not None:
                out.append(" " + content)
                i = j
            continue
        if latex.startswith("\\\\", i):
            out.append("\\\\")
            i += 2
            if i < n and latex[i] == "[":
                k = latex.find("]", i)
                if k != -1:
                    out.append(latex[i:k + 1])
                    i = k + 1
            content, j = _strip_after(latex, i)
            if content is not None:
                out.append(" " + content)
                i = j
            continue
        if latex[i] == "&":
            out.append("&")
            i += 1
            content, j = _strip_after(latex, i)
            if content is not None:
                out.append(" " + content)
                i = j
            continue
        out.append(latex[i])
        i += 1
    return "".join(out)


def _clean_env_rows(latex: str) -> str:
    """Drop empty rows/cells inside cases/aligned.

    WPS draws a dotted placeholder for every empty row or cell, so a trailing
    ``\\\\`` or ``& {}`` shows up as a box even after the braces are gone.
    """
    pattern = re.compile(r"\\begin\{(cases|aligned)\}(.*?)\\end\{\1\}", re.S)

    def repl(match: re.Match) -> str:
        env, body = match.group(1), match.group(2)
        rows = []
        for raw in body.split("\\\\"):
            cells = [c.strip() for c in raw.split("&")]
            cells = [c for c in cells if c and c not in ("{}", "{ }")]
            if not cells:
                continue
            rows.append(" ".join(cells) if env == "cases" else " & ".join(cells))
        if not rows:
            return ""
        return "\\begin{" + env + "} " + " \\\\ ".join(rows) + " \\end{" + env + "}"

    return pattern.sub(repl, latex)


def to_wps_style(latex: str) -> str:
    latex = _convert_braced_aligned(latex)
    latex = strip_row_braces(latex)
    latex = re.sub(r"\{\s*\}", "", latex)
    # A trailing \\ before \end{...} adds an empty row (WPS draws a dotted
    # placeholder for it); drop all of them.
    latex = re.sub(
        r"(?:\\\\\s*)+(?=\\end\{(?:cases|aligned|array|matrix)\})", "", latex
    )
    latex = _clean_env_rows(latex)
    out = []
    i = 0
    n = len(latex)
    while i < n:
        if latex.startswith("\\binom", i):
            a, j = _read_group(latex, i + 6)
            if a is not None:
                b, j2 = _read_group(latex, j)
                if b is not None:
                    out.append("\\left(\\frac{" + a + "}{" + b + "}\\right)")
                    i = j2
                    continue

        cmd = None
        if latex.startswith("\\sum", i):
            cmd = "\\sum"
        elif latex.startswith("\\prod", i):
            cmd = "\\prod"
        if cmd:
            j = i + len(cmd)
            if latex.startswith("\\limits", j):
                j += 7
            low = up = None
            k = j
            for _ in range(2):
                if k < n and latex[k] in "_^":
                    value, k2 = _read_script(latex, k)
                    if value is None:
                        break
                    if latex[k] == "_":
                        low = value
                    else:
                        up = value
                    k = k2
                else:
                    break
            if low is not None or up is not None:
                piece = cmd + " \\limits"
                if up is not None:
                    piece += "^{" + up + "}"
                if low is not None:
                    piece += "_{" + low + "}"
                out.append(piece)
                i = k
                continue

        out.append(latex[i])
        i += 1
    latex = "".join(out)
    # A \\ *after* \end{cases} is an empty equation line; WPS shows it as a
    # dotted box below the brace.
    return re.sub(r"\s*\\\\\s*$", "", latex).strip()


if __name__ == "__main__":
    sample = r"\left(x+a\right)^{n}=\sum_{k=0}^{n}\binom{n}{k}x^{k}a^{n-k}"
    print(to_wps_style(sample))

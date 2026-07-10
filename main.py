import os
import re
import uuid
import httpx
import json
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Mount static files only if the build directory exists
if os.path.isdir("frontend/build"):
    app.mount("/static", StaticFiles(directory="frontend/build/static"), name="static")

# In-memory store of the last converted file per job id, so /api/download
# can hand back a real .py file after a conversion instead of only JSON.
_CONVERSIONS: dict[str, str] = {}


@app.get("/api/llm/providers")
async def get_llm_providers():
    return JSONResponse(content={"providers": ["gemini", "openai", "anthropic"]})


def _strip_sas_comments(sas_code: str) -> str:
    """Removes SAS comments so they're never mistaken for code or reported
    as unsupported statements: block comments (/* ... */, possibly spanning
    multiple lines, including a trailing comment on the same line as real
    code) and line comments (a statement starting with "*" up to the next
    ";"). A "*" is only treated as a comment starter at the beginning of a
    statement (start of string or right after a prior ";"), so a literal
    "*" used as multiplication inside an expression is left untouched.
    """
    without_block_comments = re.sub(r"/\*.*?\*/", "", sas_code, flags=re.DOTALL)

    def _strip_star_comments(text: str) -> str:
        out = []
        i = 0
        at_stmt_start = True
        while i < len(text):
            ch = text[i]
            if at_stmt_start and ch == "*":
                end = text.find(";", i)
                i = end + 1 if end != -1 else len(text)
                at_stmt_start = True
                continue
            out.append(ch)
            if ch == ";":
                at_stmt_start = True
            elif not ch.isspace():
                at_stmt_start = False
            i += 1
        return "".join(out)

    return _strip_star_comments(without_block_comments)


def _expand_sas_macros(sas_code: str) -> str:
    """Expands %let variable assignments and non-recursive %macro/%mend
    blocks by textual substitution, so the main line-by-line parser sees
    plain SAS code instead of macro syntax it has no way to interpret.

    Supported: "%let name = value;" definitions, "&name" / "&name." token
    references, and "%macro name(params) ... %mend;" definitions expanded
    at each "%name(args);" call site by substituting positional parameters
    into the macro body. Nested macro calls, conditional macro logic
    (%if/%then at compile time), and macro functions (%sysfunc, etc.) are
    NOT expanded — a call to an unrecognized "%something" is left as-is,
    which then falls through to the main parser and is reported as
    unsupported like any other unrecognized statement.
    """
    let_re = re.compile(r"%let\s+(\w+)\s*=\s*(.*?);", re.IGNORECASE | re.DOTALL)
    macro_def_re = re.compile(
        r"%macro\s+(\w+)\s*(\(([^)]*)\))?\s*;(.*?)%mend\s*(\w+)?\s*;",
        re.IGNORECASE | re.DOTALL,
    )

    macro_vars: dict[str, str] = {}
    for m in let_re.finditer(sas_code):
        macro_vars[m.group(1)] = m.group(2).strip()
    sas_code = let_re.sub("", sas_code)

    macros: dict[str, tuple[list[str], str]] = {}

    def _capture_macro(m: re.Match) -> str:
        name = m.group(1)
        params_raw = m.group(3) or ""
        params = [p.strip() for p in params_raw.split(",") if p.strip()]
        body = m.group(4)
        macros[name.lower()] = (params, body)
        return ""

    sas_code = macro_def_re.sub(_capture_macro, sas_code)

    def _substitute_vars(text: str, local_vars: dict[str, str]) -> str:
        all_vars = {**macro_vars, **local_vars}
        for name in sorted(all_vars, key=len, reverse=True):
            text = re.sub(rf"&{re.escape(name)}\.", all_vars[name], text, flags=re.IGNORECASE)
            text = re.sub(rf"&{re.escape(name)}\b", all_vars[name], text, flags=re.IGNORECASE)
        return text

    def _expand_calls(text: str) -> str:
        call_re = re.compile(r"%(\w+)\s*(\(([^)]*)\))?\s*;")

        def _replace(m: re.Match) -> str:
            name = m.group(1).lower()
            if name not in macros:
                return m.group(0)
            params, body = macros[name]
            args_raw = m.group(3) or ""
            args = [a.strip() for a in args_raw.split(",")] if args_raw.strip() else []
            local_vars = dict(zip(params, args))
            expanded_body = _substitute_vars(body, local_vars)
            return expanded_body

        prev = None
        while prev != text:
            prev = text
            text = call_re.sub(_replace, text)
        return text

    sas_code = _expand_calls(sas_code)
    sas_code = _substitute_vars(sas_code, {})
    return sas_code


def _convert_proc_sql_block(sql: str) -> tuple[list[str], list[str]]:
    """Translates a constrained subset of PROC SQL to pandas: a single
    SELECT with optional WHERE / GROUP BY / ORDER BY, an optional single
    INNER/LEFT JOIN ... ON, and an optional "INTO :var" clause capturing
    one column's values into a Python list. Anything outside this shape
    (subqueries, multiple joins, HAVING, UNION, etc.) is reported as
    unsupported and left as a SQL comment in the output.
    """
    py_lines: list[str] = []
    unsupported: list[str] = []

    select_re = re.compile(
        r"select\s+(distinct\s+)?(.+?)\s+"
        r"(into\s+:(\w+)\s*(separated\s+by\s+'([^']*)')?\s+)?"
        r"from\s+([\w.]+)(\s+as\s+(\w+)|\s+(\w+))?"
        r"(\s+(inner|left)\s+join\s+([\w.]+)(\s+as\s+(\w+)|\s+(\w+))?\s+on\s+(.+?))?"
        r"(\s+where\s+(.+?))?"
        r"(\s+group\s+by\s+(.+?))?"
        r"(\s+order\s+by\s+(.+?))?"
        r"\s*;",
        re.IGNORECASE | re.DOTALL,
    )

    create_table_re = re.compile(r"^\s*create\s+table\s+(\w+)\s+as\s+(.*)$", re.IGNORECASE | re.DOTALL)

    def sql_expr_to_py(expr: str) -> str:
        expr = re.sub(r"\bne\b", "!=", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\beq\b", "==", expr, flags=re.IGNORECASE)
        expr = re.sub(r"=(?!=)", "==", expr)
        expr = re.sub(r"\band\b", "and", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bor\b", "or", expr, flags=re.IGNORECASE)
        return expr.strip()

    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for stmt in statements:
        target_table: str | None = None
        if ct := create_table_re.match(stmt):
            target_table = ct.group(1)
            stmt = ct.group(2)

        # Constructs this single-table/single-join translator can't
        # represent correctly: nested "select" (subquery), HAVING, UNION,
        # and aggregate function calls in the column list (count(*), sum(),
        # etc., which need groupby().agg() rather than a plain column
        # projection). Flagging these as unsupported is safer than silently
        # emitting a query string that looks plausible but is wrong or
        # invalid pandas syntax at runtime.
        unsupported_markers = [
            (r"\bselect\b.*\bselect\b", "subquery"),
            (r"\bhaving\b", "HAVING clause"),
            (r"\bunion\b", "UNION"),
            (r"\w+\s*\([^)]*\)\s*(as\s+\w+)?\s*(,|from\b)", "aggregate/function call in SELECT list"),
        ]
        matched_marker = next(
            (label for pattern, label in unsupported_markers if re.search(pattern, stmt, re.IGNORECASE | re.DOTALL)),
            None,
        )
        if matched_marker:
            unsupported.append(f"proc sql: {stmt};")
            py_lines.append(f"# UNSUPPORTED SQL ({matched_marker} not supported): {stmt};")
            continue

        m = select_re.match(stmt + ";")
        if not m:
            unsupported.append(f"proc sql: {stmt};")
            py_lines.append(f"# UNSUPPORTED SQL: {stmt};")
            continue

        (_distinct, cols, _into_full, into_var, _sep_full, _sep, from_table,
         _alias1, alias1a, alias1b, _join_full, join_type, join_table,
         _alias2, alias2a, alias2b, join_on, _where_full, where_clause,
         _groupby_full, groupby, _orderby_full, orderby) = m.groups()

        alias1 = alias1a or alias1b
        alias2 = alias2a or alias2b
        left_ref = alias1 or from_table
        result_var = target_table or "sql_result"

        def strip_aliases(text: str) -> str:
            for alias in (a for a in (alias1, alias2, from_table) if a):
                text = re.sub(rf"\b{re.escape(alias)}\.(\w+)", r"\1", text)
            return text

        expr = f"{from_table}.copy()"
        if alias1:
            py_lines.append(f"{alias1} = {from_table}")

        if join_table:
            join_alias = alias2 or join_table
            if alias2:
                py_lines.append(f"{alias2} = {join_table}")
            how = "inner" if (join_type or "").lower() == "inner" else "left"
            on_parts = re.split(r"\s+and\s+", join_on, flags=re.IGNORECASE)
            left_cols, right_cols = [], []
            for part in on_parts:
                lhs, rhs = [p.strip() for p in part.split("=")]
                left_cols.append(lhs.split(".")[-1])
                right_cols.append(rhs.split(".")[-1])
            expr = (
                f"{left_ref}.merge({join_alias}, how='{how}', "
                f"left_on={left_cols!r}, right_on={right_cols!r})"
            )

        py_lines.append(f"{result_var} = {expr}")

        if where_clause:
            where_py = sql_expr_to_py(strip_aliases(where_clause))
            py_lines.append(f"{result_var} = {result_var}.query('{where_py}')")

        if groupby:
            group_cols = [strip_aliases(c.strip()) for c in groupby.split(",")]
            cols_py = ", ".join(f'"{c}"' for c in group_cols)
            py_lines.append(f"{result_var} = {result_var}.groupby([{cols_py}]).first().reset_index()")

        if orderby:
            order_cols = [strip_aliases(c.strip()) for c in orderby.split(",")]
            cols_py = ", ".join(f'"{c}"' for c in order_cols)
            py_lines.append(f"{result_var} = {result_var}.sort_values([{cols_py}])")

        if cols.strip() != "*":
            col_list = [c.strip().split(" as ")[-1].split(".")[-1].strip() for c in cols.split(",")]
            cols_py = ", ".join(f'"{c}"' for c in col_list)
            py_lines.append(f"{result_var} = {result_var}[[{cols_py}]]")

        if into_var:
            col_name = cols.strip().split(" as ")[-1].split(".")[-1].strip()
            py_lines.append(f"{into_var} = {result_var}['{col_name}'].drop_duplicates().tolist()")

    return py_lines, unsupported


def _convert_sas_rule_based(sas_code: str) -> tuple[str, list[str], list[str]]:
    """Line-oriented rule-based translator for common SAS constructs.

    Handles: DATA/SET, simple column assignments, IF/THEN/ELSE (single
    statement), DROP/KEEP, PROC PRINT, PROC SORT BY, PROC MEANS. Anything
    else is preserved as a commented-out original line and reported in
    unsupported_statements.
    """
    sas_code = _strip_sas_comments(sas_code)
    sas_code = _expand_sas_macros(sas_code)
    lines = [l.rstrip() for l in sas_code.splitlines() if l.strip()]
    py_lines: list[str] = ["import pandas as pd", ""]
    unsupported: list[str] = []
    current_df: str | None = None
    source_df: str | None = None

    data_re = re.compile(r"^\s*data\s+(\w+)\s*;", re.IGNORECASE)
    set_re = re.compile(r"^\s*set\s+(\w+)\s*;", re.IGNORECASE)
    assign_re = re.compile(r"^\s*(\w+)\s*=\s*(.+?);\s*$")
    if_then_re = re.compile(r"^\s*if\s+(.+?)\s+then\s+(.+?);\s*(else\s+(.+?);)?\s*$", re.IGNORECASE)
    else_only_re = re.compile(r"^\s*else\s+(.+?);\s*$", re.IGNORECASE)
    drop_re = re.compile(r"^\s*drop\s+(.+?);", re.IGNORECASE)
    keep_re = re.compile(r"^\s*keep\s+(.+?);", re.IGNORECASE)
    run_re = re.compile(r"^\s*run\s*;", re.IGNORECASE)
    proc_print_re = re.compile(r"^\s*proc\s+print\s+data\s*=\s*(\w+)", re.IGNORECASE)
    proc_sort_re = re.compile(r"^\s*proc\s+sort\s+data\s*=\s*(\w+)", re.IGNORECASE)
    by_re = re.compile(r"^\s*by\s+(.+?);", re.IGNORECASE)
    proc_means_re = re.compile(r"^\s*proc\s+means\s+data\s*=\s*(\w+)", re.IGNORECASE)
    proc_sql_re = re.compile(r"^\s*proc\s+sql\b", re.IGNORECASE)
    quit_re = re.compile(r"^\s*quit\s*;", re.IGNORECASE)

    def sas_expr_to_py(expr: str) -> str:
        expr = re.sub(r"\bne\b", "!=", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\beq\b", "==", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bgt\b", ">", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\blt\b", "<", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bge\b", ">=", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\ble\b", "<=", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\band\b", "and", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bor\b", "or", expr, flags=re.IGNORECASE)
        return expr.strip()

    i = 0
    pending_proc: str | None = None
    while i < len(lines):
        line = lines[i]

        if m := data_re.match(line):
            current_df = m.group(1)
            py_lines.append(f"# DATA {current_df};")
            i += 1
            continue

        if m := set_re.match(line):
            source_df = m.group(1)
            if current_df:
                py_lines.append(f"{current_df} = {source_df}.copy()")
            i += 1
            continue

        if proc_sql_re.match(line):
            i += 1
            sql_lines: list[str] = []
            while i < len(lines) and not quit_re.match(lines[i]):
                sql_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # consume the "quit;" line
            sql_text = " ".join(sql_lines)
            sql_py_lines, sql_unsupported = _convert_proc_sql_block(sql_text)
            py_lines.extend(sql_py_lines)
            py_lines.append("")
            unsupported.extend(sql_unsupported)
            continue

        if run_re.match(line) or quit_re.match(line):
            py_lines.append("")
            current_df = None
            pending_proc = None
            i += 1
            continue

        if m := proc_print_re.match(line):
            pending_proc = m.group(1)
            py_lines.append(f"print({pending_proc})")
            i += 1
            continue

        if m := proc_sort_re.match(line):
            pending_proc = m.group(1)
            i += 1
            continue

        if m := by_re.match(line):
            by_cols = [c.strip() for c in m.group(1).split()]
            if pending_proc:
                cols_py = ", ".join(f'"{c}"' for c in by_cols)
                py_lines.append(f"{pending_proc} = {pending_proc}.sort_values([{cols_py}])")
            i += 1
            continue

        if m := proc_means_re.match(line):
            pending_proc = m.group(1)
            py_lines.append(f"print({pending_proc}.describe())")
            i += 1
            continue

        if m := drop_re.match(line):
            cols = [c.strip() for c in m.group(1).split()]
            cols_py = ", ".join(f'"{c}"' for c in cols)
            target = current_df or pending_proc
            if target:
                py_lines.append(f"{target} = {target}.drop(columns=[{cols_py}])")
            i += 1
            continue

        if m := keep_re.match(line):
            cols = [c.strip() for c in m.group(1).split()]
            cols_py = ", ".join(f'"{c}"' for c in cols)
            target = current_df or pending_proc
            if target:
                py_lines.append(f"{target} = {target}[[{cols_py}]]")
            i += 1
            continue

        if m := if_then_re.match(line):
            cond, then_stmt, _, else_stmt = m.groups()
            cond_py = sas_expr_to_py(cond)
            target = current_df or "df"
            if am := assign_re.match(then_stmt + ";"):
                var, val = am.group(1), sas_expr_to_py(am.group(2))
                py_lines.append(f"{target}.loc[{target}.eval('{cond_py}'), '{var}'] = {val}")
            else:
                unsupported.append(line.strip())
                py_lines.append(f"# UNSUPPORTED: {line.strip()}")

            if not else_stmt and i + 1 < len(lines) and (em := else_only_re.match(lines[i + 1])):
                else_stmt = em.group(1) + ";"
                i += 1

            if else_stmt and (am := assign_re.match(else_stmt if else_stmt.endswith(";") else else_stmt + ";")):
                var, val = am.group(1), sas_expr_to_py(am.group(2))
                py_lines.append(f"{target}.loc[~{target}.eval('{cond_py}'), '{var}'] = {val}")
            i += 1
            continue

        if m := assign_re.match(line):
            var, val = m.group(1), sas_expr_to_py(m.group(2))
            target = current_df or "df"
            py_lines.append(f"{target}['{var}'] = {target}.eval('{val}') if any(c.isalpha() for c in '{val}') else {val}")
            i += 1
            continue

        # Anything else (LABEL, FORMAT, macros, PROC SQL, PROC TABULATE, etc.)
        unsupported.append(line.strip())
        py_lines.append(f"# UNSUPPORTED: {line.strip()}")
        i += 1

    notes = [
        "This is a rule-based, pattern-driven translation covering common "
        "DATA step and PROC constructs. Statements it doesn't recognize are "
        "preserved as comments and listed under unsupported_statements — "
        "review and complete those manually, or use the AI-agent conversion "
        "for a full LLM-based attempt."
    ]
    return "\n".join(py_lines), unsupported, notes


@app.post("/api/convert/sas-to-python/rule-based")
async def convert_sas_to_python_rule_based(file: UploadFile = File(...)):
    if not file.filename.endswith(".sas"):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload a .sas file.")

    try:
        raw = await file.read()
        sas_code = raw.decode("utf-8", errors="replace")

        python_code, unsupported_statements, notes = _convert_sas_rule_based(sas_code)
        summary = (
            f"Converted {len(sas_code.splitlines())} source line(s); "
            f"{len(unsupported_statements)} statement(s) require manual review. "
            + " ".join(notes)
        )

        job_id = uuid.uuid4().hex
        _CONVERSIONS[job_id] = python_code

        return JSONResponse(content={
            "job_id": job_id,
            "python_code": python_code,
            "summary": summary,
            "unsupported_statements": unsupported_statements,
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")


AI_AGENT_PROMPT_TEMPLATE = """Convert the following SAS code to equivalent Python (using pandas where appropriate).

Respond with ONLY a single JSON object, no markdown code fences, no prose before or after, matching exactly this shape:
{{"python_code": "<the full converted Python source as a string, with \\n for newlines>", "migration_report": "<a concise plain-text migration report>", "execution_timeline": "<a concise plain-text estimated timeline>"}}

SAS Code:
{sas_code}
"""


@app.post("/api/convert/sas-to-python/ai-agent")
async def convert_sas_to_python_ai_agent(
    file: UploadFile = File(...),
    llm_provider: str = Form(...),
    api_key: str = Form(...)
):
    if not file.filename.endswith(".sas"):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload a .sas file.")

    sas_code = (await file.read()).decode("utf-8", errors="replace")
    prompt = AI_AGENT_PROMPT_TEMPLATE.format(sas_code=sas_code)

    if llm_provider.lower() != "gemini":
        raise HTTPException(status_code=400, detail=f"LLM provider '{llm_provider}' not supported by this endpoint.")

    api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent"
    headers = {"Content-Type": "application/json"}
    json_body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    params = {"key": api_key}

    generated_python_code = "# Fallback: LLM call did not return usable content."
    migration_report = "Fallback: LLM call did not return usable content."
    execution_timeline = "Fallback: LLM call did not return usable content."

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, headers=headers, json=json_body, params=params)
            response.raise_for_status()
            result = response.json()

            candidates = result.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                generated_text = parts[0].get("text", "") if parts else ""
                cleaned = generated_text.strip()
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```(json)?\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)

                ai_output = json.loads(cleaned)
                generated_python_code = ai_output.get("python_code", generated_python_code)
                migration_report = ai_output.get("migration_report", migration_report)
                execution_timeline = ai_output.get("execution_timeline", execution_timeline)

    except httpx.TimeoutException:
        generated_python_code = "# Fallback: LLM API timed out. Cannot convert."
        migration_report = "Fallback: LLM API timed out."
        execution_timeline = "Fallback: LLM API timed out."
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        generated_python_code = f"# Fallback: LLM request error - {exc!r}. Cannot convert."
        migration_report = f"Fallback: LLM request error - {exc!r}."
        execution_timeline = f"Fallback: LLM request error - {exc!r}."
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        generated_python_code = f"# Fallback: could not parse LLM response - {exc!r}. Cannot convert."
        migration_report = f"Fallback: could not parse LLM response - {exc!r}."
        execution_timeline = f"Fallback: could not parse LLM response - {exc!r}."

    job_id = uuid.uuid4().hex
    _CONVERSIONS[job_id] = generated_python_code

    return JSONResponse(content={
        "job_id": job_id,
        "python_code": generated_python_code,
        "migration_report": migration_report,
        "execution_timeline": execution_timeline,
    })


@app.get("/api/download/{job_id}")
async def download_python_file(job_id: str):
    python_code = _CONVERSIONS.get(job_id)
    if python_code is None:
        raise HTTPException(status_code=404, detail="No conversion found for this job id.")
    return PlainTextResponse(
        content=python_code,
        media_type="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="converted_{job_id[:8]}.py"'},
    )


# Catch-all route for serving frontend files
# This must be the last route defined.
if os.path.isdir("frontend/build"):
    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if ".." in full_path:
            raise HTTPException(status_code=400, detail="Invalid path")

        if os.path.isfile(f"frontend/build/{full_path}"):
            return FileResponse(f"frontend/build/{full_path}")
        elif os.path.isfile("frontend/build/index.html"):
            return FileResponse("frontend/build/index.html")
        else:
            raise HTTPException(status_code=404, detail="Frontend build not found")

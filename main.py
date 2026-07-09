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


def _convert_sas_rule_based(sas_code: str) -> tuple[str, list[str], list[str]]:
    """Line-oriented rule-based translator for common SAS constructs.

    Handles: DATA/SET, simple column assignments, IF/THEN/ELSE (single
    statement), DROP/KEEP, PROC PRINT, PROC SORT BY, PROC MEANS. Anything
    else is preserved as a commented-out original line and reported in
    unsupported_statements.
    """
    sas_code = _strip_sas_comments(sas_code)
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

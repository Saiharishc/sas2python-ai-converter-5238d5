import os
import httpx
import json
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Mount static files only if the build directory exists
if os.path.isdir("frontend/build"):
    app.mount("/static", StaticFiles(directory="frontend/build/static"), name="static")

@app.get("/api/llm/providers")
async def get_llm_providers():
    # In a real application, this list might be dynamic or configurable.
    # For now, we provide a static list.
    return JSONResponse(content={"providers": ["gemini", "openai", "anthropic"]})

@app.post("/api/convert/sas-to-python/rule-based")
async def convert_sas_to_python_rule_based(file: UploadFile = File(...)):
    if not file.filename.endswith(".sas"):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload a .sas file.")

    try:
        sas_code = await file.read()
        # Placeholder for actual rule-based conversion logic
        # This would involve parsing SAS code and applying translation rules.
        # For demonstration, we'll return a mock response.
        mock_python_code = "# Converted Python code (rule-based)\nprint('Hello, world!')"
        mock_summary = "Basic SAS to Python conversion logic applied."
        mock_unsupported = ["PROC TABULATE", "Gplot"]

        return JSONResponse(content={
            "python_code": mock_python_code,
            "summary": mock_summary,
            "unsupported_statements": mock_unsupported
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")


@app.post("/api/convert/sas-to-python/ai-agent")
async def convert_sas_to_python_ai_agent(
    file: UploadFile = File(...),
    llm_provider: str = Form(...),
    api_key: str = Form(...)
):
    if not file.filename.endswith(".sas"):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload a .sas file.")

    sas_code = await file.read()
    prompt = f"Convert the following SAS code to Python. Provide the generated Python code, a migration report, and an execution timeline. SAS Code:\n{sas_code.decode('utf-8')}"

    api_url = ""
    if llm_provider.lower() == "gemini":
        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent"
    else:
        # Placeholder for other LLM providers. This would require specific SDKs/APIs.
        raise HTTPException(status_code=400, detail=f"LLM provider '{llm_provider}' not supported by this endpoint.")

    headers = {"Content-Type": "application/json"}
    json_body = {"contents": [{"parts": [{"text": prompt}]}]}
    params = {"key": api_key}

    generated_python_code = "# Placeholder for AI-generated Python code."
    migration_report = "Placeholder migration report."
    execution_timeline = "Placeholder execution timeline."

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(api_url, headers=headers, json=json_body, params=params)
            response.raise_for_status() # Raise an exception for bad status codes
            result = response.json()

            if "candidates" in result and result["candidates"]:
                content = result["candidates"][0].get("content", {})
                parts = content.get("parts", [])
                if parts:
                    generated_text = parts[0].get("text", "")
                    # Attempt to parse JSON from the generated text
                    try:
                        # Remove markdown code fences if present
                        if generated_text.strip().startswith("```json") and generated_text.strip().endswith("```"):
                           generated_text = generated_text.strip()[7:-3].strip()
                        
                        ai_output = json.loads(generated_text)
                        generated_python_code = ai_output.get("python_code", generated_python_code)
                        migration_report = ai_output.get("migration_report", migration_report)
                        execution_timeline = ai_output.get("execution_timeline", execution_timeline)

                    except json.JSONDecodeError:
                        # If it's not JSON, try to extract Python code, report, timeline manually if possible
                        # This is a basic fallback, more robust parsing might be needed.
                        lines = generated_text.split('\n')
                        current_section = None
                        for line in lines:
                            if "Generated Python Code:" in line:
                                current_section = "python_code"
                                continue
                            elif "Migration Report:" in line:
                                current_section = "migration_report"
                                continue
                            elif "Execution Timeline:" in line:
                                current_section = "execution_timeline"
                                continue
                            
                            if current_section == "python_code":
                                generated_python_code += line + '\n'
                            elif current_section == "migration_report":
                                migration_report += line + '\n'
                            elif current_section == "execution_timeline":
                                execution_timeline += line + '\n'
                        # Clean up potential extra newlines
                        generated_python_code = generated_python_code.strip()
                        migration_report = migration_report.strip()
                        execution_timeline = execution_timeline.strip()

            else:
                print(f"Warning: No candidates found in LLM response: {result}")

    except httpx.TimeoutException:
        print("LLM API call timed out.")
        # Use fallback data
        generated_python_code = "# Fallback: LLM API timed out. Cannot convert."
        migration_report = "Fallback: LLM API timed out."
        execution_timeline = "Fallback: LLM API timed out."
    except httpx.RequestError as exc:
        print(f"An HTTP error occurred: {exc!r}")
        # Use fallback data
        generated_python_code = f"# Fallback: LLM request error - {exc!r}. Cannot convert."
        migration_report = f"Fallback: LLM request error - {exc!r}. Cannot convert."
        execution_timeline = f"Fallback: LLM request error - {exc!r}. Cannot convert."
    except Exception as e:
        print(f"An unexpected error occurred: {e!r}")
        # Use fallback data
        generated_python_code = f"# Fallback: Unexpected error during AI conversion - {e!r}. Cannot convert."
        migration_report = f"Fallback: Unexpected error during AI conversion - {e!r}. Cannot convert."
        execution_timeline = f"Fallback: Unexpected error during AI conversion - {e!r}. Cannot convert."

    return JSONResponse(content={
        "python_code": generated_python_code,
        "migration_report": migration_report,
        "execution_timeline": execution_timeline
    })


# Catch-all route for serving frontend files
# This must be the last route defined.
if os.path.isdir("frontend/build"):
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        from fastapi.responses import FileResponse
        # Prevent serving files from outside the build directory
        if ".." in full_path:
            raise HTTPException(status_code=400, detail="Invalid path")
        
        # Serve index.html for root and sub-paths not matching API routes
        if os.path.isfile(f"frontend/build/{full_path}"):
            return FileResponse(f"frontend/build/{full_path}")
        elif os.path.isfile("frontend/build/index.html"):
             return FileResponse("frontend/build/index.html")
        else:
            raise HTTPException(status_code=404, detail="Frontend build not found")


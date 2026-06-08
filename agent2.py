import asyncio
import uuid
import os
import shutil
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing_extensions import TypedDict
from dotenv import load_dotenv

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, AnyMessage, AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph_sdk import get_client

load_dotenv()

# ── Config ───────────────────────────────────────
INCIDENTS_DIR  = Path("incidents")
PROCESSED_DIR  = Path("incidents/processed")
FAILED_DIR     = Path("incidents/failed")
WATCH_INTERVAL = 60

INCIDENTS_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(exist_ok=True)
FAILED_DIR.mkdir(exist_ok=True)

model = ChatGroq(model="meta-llama/llama-4-scout-17b-16e-instruct")

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

SYSTEM_PROMPT = SystemMessage(content="""
You are a Windows, GCP, and infrastructure assistant.

TOOL ROUTING:

STEP 1: Remote machine query?
  → user mentions IP, hostname, server name
  → USE: ssh_command(host=<ip>, command=<cmd>)

STEP 2: IT systems or processes query?
  → keywords: gcp, colt, striim, onboard, permission,
               disk, memory, network, cpu, folder
  → USE: search_knowledge_base ONLY
  → NEVER answer from own knowledge

STEP 3: Live local system state?
  → keywords: hostname, IP, uptime, processes, disk size
  → USE: run_command directly

──────────────────────────────────────────────────

── MODE 1: RESOLVE ISSUE ─────────────────────────
Trigger: fix, stop, start, restart, resolve, error, down

1. Call search_knowledge_base → get ACTION PLAN
2. Print plan BEFORE executing:

   📋 Action Plan: <topic>
   ───────────────────────
   Step 1: <command>
   Step 2: <command>
   ───────────────────────
   Executing now...

3. Call run_command or ssh_command for each step
4. Print each result:
   ✅ Step 1 result: <output>
5. Final summary
6. End with STATUS: RESOLVED or STATUS: NEEDS_HUMAN

── MODE 2: PROVIDE INFORMATION ───────────────────
Trigger: how to, what is, explain, tell me, onboard,
         permission, access, describe, provide info

1. Call search_knowledge_base
2. TYPE: INFORMATION_ONLY:
   → Return FULL content exactly
   → End with: "Say 'ok fix it' to execute these steps."
3. TYPE: NOT_FOUND → say you don't have this information
4. DO NOT call run_command

── MODE 2→1: USER APPROVES EXECUTION ─────────────
Trigger: "ok fix it", "yes do it", "execute",
         "go ahead", "apply it", "run it"

1. Look back in conversation history
2. Find last INFORMATION_ONLY result
3. Extract steps → execute via run_command or ssh_command
4. Print plan then results
5. End with STATUS: RESOLVED or STATUS: NEEDS_HUMAN

── MODE 3: DIRECT QUERY — LOCAL ──────────────────
Trigger: hostname, IP, disk, memory, processes
         (no remote host mentioned)

1. Call run_command directly
2. Return output

── MODE 4: DIRECT QUERY — REMOTE ─────────────────
Trigger: query with IP address or server name

1. Extract host from message
2. Call ssh_command(host=<host>, command=<cmd>)
3. Return output

── MODE 5: SAVE KNOWLEDGE ────────────────────────
Trigger: "save this solution", "add to knowledge base",
         "remember this fix", "save this", "issue fixed save"

1. Extract from conversation:
   - keyword  : short name (e.g. nginx_down)
   - topic    : full description
   - type     : COMMAND_TASK or INFORMATION_ONLY
   - solution : commands or info that worked
2. Call save_knowledge tool
3. Confirm: "Saved! Will auto-resolve next time."

── MODE 6: SESSION RESUME ────────────────────────
Trigger: user pastes a UUID like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
         or says "session", "thread id", "incident id", "resume"

Steps:
1. Acknowledge the session ID if available
2. Show the entire incident history to user
3. Tell user you can see the full incident history
4. Ask: "What would you like me to try next to fix this issue?"
5. Wait for their instructions
6. Execute their instructions using run_command or ssh_command
7. After fix: ask "Should I save this solution to the knowledge base?"
8. If yes: call save_knowledge tool with the solution

RULES:
- INFORMATION_ONLY → show info, say 'ok fix it' to execute
- COMMAND_TASK → execute immediately
- Remote machine → ssh_command
- Local machine  → run_command
- Never answer GCP/Colt/Striim from own knowledge
- Autonomous incidents → end with STATUS: RESOLVED or NEEDS_HUMAN
""")


# ── LLM Node ──────────────────────────────────────
def llm_node(state: AgentState, model_with_tools):
    messages = [SYSTEM_PROMPT] + state["messages"]
    response = model_with_tools.invoke(messages)

    print("\n" + "="*50)
    print("LLM NODE:")
    if response.tool_calls:
        for tc in response.tool_calls:
            print(f"  Tool : {tc['name']}")
            print(f"  Args : {tc['args']}")
    else:
        print(f"  Answer: {response.content[:120]}")
    print("="*50)

    return {"messages": [response]}


# ── Should Continue ────────────────────────────────
def should_continue(state: AgentState):
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


# ── Core Graph Builder ────────────────────────────
async def build_graph():
    mcp_client = MultiServerMCPClient({
        "windows": {
            "url": "http://localhost:8000/sse",
            "transport": "sse",
        }
    })

    tools = await mcp_client.get_tools()
    print(f"Tools loaded: {[t.name for t in tools]}")

    model_with_tools = model.bind_tools(tools)
    tool_node        = ToolNode(tools)

    graph = StateGraph(AgentState)
    graph.add_node("llm",   lambda state: llm_node(state, model_with_tools))
    graph.add_node("tools", tool_node)
    graph.add_edge(START,   "llm")
    graph.add_conditional_edges(
        "llm",
        should_continue,
        {"tools": "tools", END: END}
    )
    graph.add_edge("tools", "llm")

    return graph.compile()  # LangGraph API handles persistence


# ════════════════════════════════════════════════
# EMAIL
# ════════════════════════════════════════════════
def send_email(to: str, subject: str, body: str):
    try:
        msg = MIMEMultipart()
        msg["From"]    = os.getenv("SMTP_USER", "")
        msg["To"]      = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(
            os.getenv("SMTP_HOST", "smtp.gmail.com"),
            int(os.getenv("SMTP_PORT", "587"))
        ) as server:
            server.starttls()
            server.login(
                os.getenv("SMTP_USER", ""),
                os.getenv("SMTP_PASSWORD", "")
            )
            server.send_message(msg)
        print(f"  ✅ Email sent to {to}")
    except Exception as e:
        print(f"  ❌ Email failed: {e}")


# ════════════════════════════════════════════════
# INCIDENT PARSER
# ════════════════════════════════════════════════
def parse_incident(file_path: Path) -> dict:
    data = {
        "host":        "localhost",
        "issue":       "",
        "priority":    "MEDIUM",
        "reported_by": os.getenv("ALERT_EMAIL", "admin@company.com"),
    }
    for line in file_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("HOST:"):
            data["host"] = line.split(":", 1)[1].strip()
        elif line.startswith("ISSUE:"):
            data["issue"] = line.split(":", 1)[1].strip()
        elif line.startswith("PRIORITY:"):
            data["priority"] = line.split(":", 1)[1].strip()
        elif line.startswith("REPORTED_BY:"):
            data["reported_by"] = line.split(":", 1)[1].strip()
    return data


# ════════════════════════════════════════════════
# PROCESS ONE INCIDENT
# ════════════════════════════════════════════════
async def process_incident(client, incident_file: Path):
    session_id = str(uuid.uuid4())
    incident   = parse_incident(incident_file)
    timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*60}")
    print(f"AUTO INCIDENT : {incident_file.name}")
    print(f"  Host        : {incident['host']}")
    print(f"  Issue       : {incident['issue']}")
    print(f"  Priority    : {incident['priority']}")
    print(f"  Session ID  : {session_id}")
    print(f"{'='*60}")

    prompt = f"""
Autonomous incident at {timestamp}:

HOST     : {incident['host']}
ISSUE    : {incident['issue']}
PRIORITY : {incident['priority']}

Investigate and resolve.
Use ssh_command for remote host, run_command for localhost.
End with STATUS: RESOLVED or STATUS: NEEDS_HUMAN.
"""

    try:
        # Create thread in LangGraph internal DB using session_id as thread_id
        # Create thread in LangGraph internal DB using session_id as thread_id
        await client.threads.create(thread_id=session_id)

        # Stream and collect final message
        final = ""
        async for chunk in client.runs.stream(
            thread_id=session_id,
            assistant_id="windows_agent",
            input={"messages": [{"role": "user", "content": prompt}]},
            stream_mode="values"
        ):
            if chunk.data and "messages" in chunk.data:
                final = chunk.data["messages"][-1]["content"]

        print(f"\nAgent:\n{final}")
        if "STATUS: RESOLVED" in final:
            shutil.move(str(incident_file), PROCESSED_DIR / incident_file.name)
            send_email(
                to=incident["reported_by"],
                subject=f"✅ RESOLVED: {incident['issue']} on {incident['host']}",
                body=f"""Incident Auto-Resolved

File        : {incident_file.name}
Host        : {incident['host']}
Issue       : {incident['issue']}
Priority    : {incident['priority']}
Resolved At : {timestamp}
Session ID  : {session_id}

Resolution Summary:
{final}
"""
            )
            print(f"  ✅ Resolved → processed/")

        else:
            shutil.move(str(incident_file), FAILED_DIR / incident_file.name)
            send_email(
                to=incident["reported_by"],
                subject=f"⚠️ NEEDS ATTENTION: {incident['issue']} on {incident['host']}",
                body=f"""Incident Needs Human Intervention

File        : {incident_file.name}
Host        : {incident['host']}
Issue       : {incident['issue']}
Priority    : {incident['priority']}
Attempted   : {timestamp}
Session ID  : {session_id}

What Was Tried:
{final}

──────────────────────────────────────────
TO CONTINUE WITH THE AGENT:

Click the link below to open full session history:

http://localhost:3000/?apiUrl=http://localhost:2024&assistantId=windows_agent&threadId={session_id}

Give manual instructions to fix.
After fixing say: "issue fixed, save this solution"
──────────────────────────────────────────
"""
            )
            print(f"  ⚠️  Needs human → failed/")
            print(f"  Session: {session_id}")

    except Exception as e:
        print(f"  ❌ Error: {e}")
        shutil.move(str(incident_file), FAILED_DIR / incident_file.name)


# ════════════════════════════════════════════════
# WATCHER LOOP — uses LangGraph API
# ════════════════════════════════════════════════
async def watcher_main():
    print(f"\n🔍 Watcher started — every {WATCH_INTERVAL}s")
    print(f"   Watching : {INCIDENTS_DIR.absolute()}\n")

    client = get_client(url="http://localhost:2024")
    while True:
        files = [f for f in INCIDENTS_DIR.glob("*.txt") if f.is_file()]
        if files:
            print(f"\n📂 {len(files)} incident(s) at {datetime.now().strftime('%H:%M:%S')}")
            for f in files:
                await process_incident(client, f)
        else:
            print(f"  ⏳ {datetime.now().strftime('%H:%M:%S')} — watching...")
        await asyncio.sleep(WATCH_INTERVAL)


# ════════════════════════════════════════════════
# FOR langgraph dev — no checkpointer needed
# LangGraph API handles persistence automatically
# ════════════════════════════════════════════════
app = asyncio.run(build_graph())


if __name__ == "__main__":
    asyncio.run(watcher_main())

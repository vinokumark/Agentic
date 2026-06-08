from mcp.server.fastmcp import FastMCP
import subprocess
import paramiko
import os
import json
from pathlib import Path

mcp = FastMCP("Windows MCP Server", port=8000)

# ── Knowledge Base File ──────────────────────────
KB_FILE = Path("knowledge_base.json")

# ── Default built-in knowledge ───────────────────
DEFAULT_KB = {
    "striim_stop": {
        "type": "COMMAND_TASK",
        "topic": "Stop Striim Service",
        "actions": [
            "systemctl stop striim.node",
            "systemctl status striim.node"
        ]
    },
    "striim_start": {
        "type": "COMMAND_TASK",
        "topic": "Start Striim Service",
        "actions": [
            "systemctl start striim.node",
            "systemctl status striim.node"
        ]
    },
    "striim_info": {
        "type": "INFORMATION_ONLY",
        "topic": "Striim ETL Knowledge",
        "info": """
            Striim is a real-time data integration platform.
            - Check license: Open Striim console → DESCRIBE CLUSTER
            - Default port: 9080
            - Config: /opt/striim/conf
            - Logs: /opt/striim/logs
        """
    },
    "user_add_gcp": {
        "type": "INFORMATION_ONLY",
        "topic": "How to onboard user in GCP Colt environment",
        "info": """
        TOPIC: GCP Colt Datalake User Onboarding

        STEP 1 — Add user in Azure first:
        The user MUST be added in Azure Active Directory before anything else.
        After adding in Azure, wait exactly 60 minutes for replication.

        STEP 2 — User appears in GCP:
        After 60 minutes, the user will automatically replicate
        into the GCP user directory. No manual action needed.

        STEP 3 — Provide GCP permissions:
        Add the user to the respective dataset group.
        Example group: seiebel_readonly@colt.net

        STEP 4 — If user still has issues:
        Contact: GCP_SUPPORT@colt.net

        IMPORTANT: Do not skip Step 1.
        """
    },
    "disk": {
        "type": "COMMAND_TASK",
        "topic": "Fix Low Disk Space",
        "actions": [
            "wmic logicaldisk get caption,freespace,size",
            "del /q/f/s %TEMP%\\*",
        ]
    },
    "memory": {
        "type": "COMMAND_TASK",
        "topic": "Fix High Memory Usage",
        "actions": [
            "wmic OS get FreePhysicalMemory,TotalVisibleMemorySize",
            "tasklist /fo table"
        ]
    },
    "network": {
        "type": "COMMAND_TASK",
        "topic": "Fix Network Issue",
        "actions": [
            "ipconfig /all",
            "ping 8.8.8.8",
            "ipconfig /flushdns"
        ]
    },
    "cpu": {
        "type": "COMMAND_TASK",
        "topic": "Fix High CPU",
        "actions": [
            "wmic cpu get loadpercentage",
            "tasklist /fo table"
        ]
    },
    "folder_file": {
        "type": "COMMAND_TASK",
        "topic": "Create Folder and File",
        "actions": [
            "mkdir <folder_name>",
            "echo. > <folder_name>\\<file_name>",
            "dir <folder_name>"
        ]
    },
}

# ── Load KB from JSON or use default ─────────────
def load_knowledge_base() -> dict:
    if KB_FILE.exists():
        try:
            with open(KB_FILE, "r") as f:
                saved = json.load(f)
            # merge default + saved (saved overrides default)
            merged = {**DEFAULT_KB, **saved}
            print(f"KB loaded: {len(merged)} entries ({len(saved)} from file)")
            return merged
        except Exception as e:
            print(f"KB load error: {e} — using default")
    return dict(DEFAULT_KB)

# ── Save KB to JSON ───────────────────────────────
def save_knowledge_base(kb: dict):
    # only save non-default entries to file
    default_keys = set(DEFAULT_KB.keys())
    custom_entries = {k: v for k, v in kb.items() if k not in default_keys}
    with open(KB_FILE, "w") as f:
        json.dump(custom_entries, f, indent=2)
    print(f"KB saved: {len(custom_entries)} custom entries → {KB_FILE}")

# ── Load on startup ───────────────────────────────
KNOWLEDGE_BASE = load_knowledge_base()

# ── Keyword Map ───────────────────────────────────
KEYWORD_MAP = {
    # GCP
    "onboard the user in gcp":  "user_add_gcp",
    "onboard user in gcp":      "user_add_gcp",
    "onboard the user":         "user_add_gcp",
    "add user in gcp":          "user_add_gcp",
    "add user to gcp":          "user_add_gcp",
    "user in gcp":              "user_add_gcp",
    "gcp user":                 "user_add_gcp",
    "gcp access":               "user_add_gcp",
    "gcp permission":           "user_add_gcp",
    "gcp onboard":              "user_add_gcp",
    "colt environment":         "user_add_gcp",
    "colt gcp":                 "user_add_gcp",
    "gcp colt":                 "user_add_gcp",
    "gcp":                      "user_add_gcp",
    # Striim
    "stop striim":              "striim_stop",
    "start striim":             "striim_start",
    "restart striim":           "striim_stop",
    "striim":                   "striim_info",
    # Windows
    "disk":                     "disk",
    "memory":                   "memory",
    "network":                  "network",
    "cpu":                      "cpu",
    "folder":                   "folder_file",
    "file":                     "folder_file",
}


# ════════════════════════════════════════════════
# TOOL 1 — Search Knowledge Base
# ════════════════════════════════════════════════
@mcp.tool()
def search_knowledge_base(issue: str) -> str:
    """
    Search knowledge base for IT systems and processes.
    Use for: GCP, Colt, Striim, disk, memory, network, CPU.
    DO NOT use for person queries.
    """
    issue_lower = issue.lower()
    print(f"DEBUG KB search: '{issue_lower}'")

    # search keyword map
    for keyword, kb_key in KEYWORD_MAP.items():
        if keyword in issue_lower:
            entry = KNOWLEDGE_BASE.get(kb_key)
            if entry:
                return _format_entry(entry)

    # search custom entries by topic keyword
    for kb_key, entry in KNOWLEDGE_BASE.items():
        topic = entry.get("topic", "").lower()
        if any(word in topic for word in issue_lower.split()):
            print(f"DEBUG KB fuzzy match: '{kb_key}'")
            return _format_entry(entry)

    return f"""TYPE: NOT_FOUND
TOPIC: {issue}
INSTRUCTION: Tell user you don't have information. Ask them to provide solution so you can save it."""


def _format_entry(entry: dict) -> str:
    entry_type = entry.get("type", "UNKNOWN")
    topic      = entry.get("topic", "")

    if entry_type == "INFORMATION_ONLY":
        info = entry.get("info", "")
        return f"""TYPE: INFORMATION_ONLY
TOPIC: {topic}

{info}

INSTRUCTION: Return this exactly. Tell user say 'ok fix it' to execute."""

    elif entry_type == "COMMAND_TASK":
        actions = entry.get("actions", [])
        steps   = "\n".join([f"Step {i+1}: {a}" for i, a in enumerate(actions)])
        return f"""TYPE: COMMAND_TASK
TOPIC: {topic}

ACTION PLAN:
{steps}

INSTRUCTION: Print plan first, then execute each step with run_command."""

    return f"TYPE: UNKNOWN\nTOPIC: {topic}"


# ════════════════════════════════════════════════
# TOOL 2 — Save New Knowledge (RAG Update)
# ════════════════════════════════════════════════
@mcp.tool()
def save_knowledge(
    keyword: str,
    topic: str,
    entry_type: str,
    actions_or_info: str
) -> str:
    """
    Save new knowledge to the knowledge base after human fixes an issue.

    Use this tool when:
    - User says 'save this solution'
    - User says 'add this to knowledge base'
    - User says 'remember this fix'
    - Issue was fixed manually and should be remembered

    Parameters:
    - keyword       : short keyword to trigger this entry (e.g. 'nginx_down')
    - topic         : human readable topic (e.g. 'Fix Nginx Service Down')
    - entry_type    : 'COMMAND_TASK' or 'INFORMATION_ONLY'
    - actions_or_info: for COMMAND_TASK: commands separated by newline
                       for INFORMATION_ONLY: the knowledge text
    """
    global KNOWLEDGE_BASE

    keyword_clean = keyword.lower().replace(" ", "_")

    if entry_type == "COMMAND_TASK":
        actions = [
            a.strip()
            for a in actions_or_info.strip().splitlines()
            if a.strip()
        ]
        new_entry = {
            "type":    "COMMAND_TASK",
            "topic":   topic,
            "actions": actions
        }
        # add to keyword map
        KEYWORD_MAP[keyword.lower()] = keyword_clean

    else:
        new_entry = {
            "type": "INFORMATION_ONLY",
            "topic": topic,
            "info":  actions_or_info
        }
        KEYWORD_MAP[keyword.lower()] = keyword_clean

    # update in-memory KB
    KNOWLEDGE_BASE[keyword_clean] = new_entry

    # persist to disk
    save_knowledge_base(KNOWLEDGE_BASE)

    print(f"KB updated: '{keyword_clean}' → {topic}")
    return f"""Knowledge saved successfully!

Keyword : {keyword_clean}
Topic   : {topic}
Type    : {entry_type}

This will be used automatically next time a similar issue occurs."""


# ════════════════════════════════════════════════
# TOOL 3 — Run Local Command
# ════════════════════════════════════════════════
@mcp.tool()
def run_command(command: str) -> str:
    """
    Execute a shell command on THIS local Windows machine.
    Use for local queries and fix steps.
    For remote machines use ssh_command.
    """
    print(f"DEBUG run_command: '{command}'")
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True
    )
    return result.stdout if result.stdout else result.stderr


# ════════════════════════════════════════════════
# TOOL 4 — SSH Remote Command
# ════════════════════════════════════════════════
@mcp.tool()
def ssh_command(host: str, command: str) -> str:
    """
    Execute a command on a REMOTE machine via SSH.
    Use when user mentions a specific IP or server name.

    Examples:
    - 'check disk on 192.168.1.100'
    - 'restart nginx on prod-server'
    """
    print(f"DEBUG ssh_command {host}: '{command}'")
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            username=os.getenv("SSH_USER", "admin"),
            password=os.getenv("SSH_PASSWORD", ""),
            key_filename=os.getenv("SSH_KEY_PATH", None),
            timeout=10
        )
        stdin, stdout, stderr = client.exec_command(command)
        output = stdout.read().decode()
        error  = stderr.read().decode()
        client.close()
        return output if output else error
    except Exception as e:
        return f"SSH failed to {host}: {str(e)}"


if __name__ == "__main__":
    mcp.run(transport="sse")
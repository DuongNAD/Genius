import os
import sys
import socket
import sqlite3
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core.utils.db import get_db_connection, init_db

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Genius Agent Dashboard", lifespan=lifespan)

def check_port(host: str, port: int) -> bool:
    try:
        # Check connection to localhost on the given port
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False

def check_agent_busy(agent_name: str) -> str:
    names_to_check = [agent_name]
    if agent_name == "grok":
        names_to_check.append("grok_researcher")
    elif agent_name == "claude":
        names_to_check.append("claude_architect")
    elif agent_name == "codex":
        names_to_check.append("codex_reviewer")
    elif agent_name == "tester":
        names_to_check.append("tester_agent")

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in names_to_check)
            cursor.execute(
                f"SELECT 1 FROM agent_logs WHERE agent_name IN ({placeholders}) AND status IN ('processing', 'started') LIMIT 1",
                names_to_check
            )
            if cursor.fetchone():
                return "busy"
    except Exception:
        pass
    return "idle"

@app.get("/api/status")
def get_status():
    agents = {
        "grok": {"port": 8001, "db_name": "grok"},
        "claude": {"port": 8002, "db_name": "claude"},
        "codex": {"port": 8003, "db_name": "codex"},
        "tester": {"port": 8004, "db_name": "tester"}
    }
    
    result = {}
    for name, info in agents.items():
        online = check_port("127.0.0.1", info["port"])
        status = check_agent_busy(info["db_name"])
        result[name] = {
            "port": info["port"],
            "online": online,
            "status": status
        }
    return result

@app.get("/api/conversations")
def get_conversations():
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, prompt, result FROM conversations ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception:
        return []

@app.get("/api/logs")
def get_logs():
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, task_id, agent_name, prompt, result, status, error FROM agent_logs ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception:
        return []

@app.get("/", response_class=HTMLResponse)
def get_index():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Genius Web Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .custom-scrollbar::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
            background: #1f2937;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
            background: #4b5563;
            border-radius: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover {
            background: #6b7280;
        }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen flex flex-col font-sans">
    <header class="bg-gray-800 border-b border-gray-700 py-4 px-6 flex items-center justify-between shadow-md">
        <div class="flex items-center space-x-3">
            <span class="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-teal-400 to-blue-500">
                Genius Administrative Dashboard
            </span>
            <span class="text-xs bg-gray-750 text-gray-400 px-2.5 py-1 rounded border border-gray-700 font-mono">v2.0 Enterprise</span>
        </div>
        <div class="flex items-center space-x-4">
            <button onclick="refreshAll()" class="px-4 py-2 bg-teal-600 hover:bg-teal-500 text-white font-semibold rounded shadow transition duration-150 ease-in-out text-sm">
                Refresh Now
            </button>
            <span id="last-updated" class="text-xs text-gray-500 font-mono">Last updated: Never</span>
        </div>
    </header>

    <main class="flex-grow p-6 space-y-8 max-w-7xl mx-auto w-full">
        <section>
            <h2 class="text-xl font-semibold mb-4 text-gray-300">Agent Server Statuses</h2>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
                <!-- Grok -->
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow flex flex-col justify-between h-40">
                    <div>
                        <div class="flex items-center justify-between">
                            <h3 class="text-lg font-bold text-teal-400">Grok Researcher</h3>
                            <span class="text-xs text-gray-500 font-mono">8001</span>
                        </div>
                        <p class="text-xs text-gray-400 mt-1">Deep search & evidence lookup</p>
                    </div>
                    <div class="space-y-2 mt-4">
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Connection:</span>
                            <span id="conn-grok" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Activity:</span>
                            <span id="act-grok" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                    </div>
                </div>

                <!-- Claude -->
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow flex flex-col justify-between h-40">
                    <div>
                        <div class="flex items-center justify-between">
                            <h3 class="text-lg font-bold text-teal-400">Claude Architect</h3>
                            <span class="text-xs text-gray-500 font-mono">8002</span>
                        </div>
                        <p class="text-xs text-gray-400 mt-1">Architecture & structural layout</p>
                    </div>
                    <div class="space-y-2 mt-4">
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Connection:</span>
                            <span id="conn-claude" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Activity:</span>
                            <span id="act-claude" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                    </div>
                </div>

                <!-- Codex -->
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow flex flex-col justify-between h-40">
                    <div>
                        <div class="flex items-center justify-between">
                            <h3 class="text-lg font-bold text-teal-400">Codex Reviewer</h3>
                            <span class="text-xs text-gray-500 font-mono">8003</span>
                        </div>
                        <p class="text-xs text-gray-400 mt-1">Code review & safety scanner</p>
                    </div>
                    <div class="space-y-2 mt-4">
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Connection:</span>
                            <span id="conn-codex" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Activity:</span>
                            <span id="act-codex" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                    </div>
                </div>

                <!-- Tester -->
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow flex flex-col justify-between h-40">
                    <div>
                        <div class="flex items-center justify-between">
                            <h3 class="text-lg font-bold text-teal-400">Tester Agent</h3>
                            <span class="text-xs text-gray-500 font-mono">8004</span>
                        </div>
                        <p class="text-xs text-gray-400 mt-1">Unit/Integration test runner</p>
                    </div>
                    <div class="space-y-2 mt-4">
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Connection:</span>
                            <span id="conn-tester" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                        <div class="flex justify-between items-center text-sm">
                            <span class="text-gray-400">Activity:</span>
                            <span id="act-tester" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700 text-gray-400">Checking...</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <div class="grid grid-cols-1 gap-8">
            <!-- Conversations History Table -->
            <section class="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden shadow">
                <div class="p-5 border-b border-gray-700 flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
                    <div>
                        <h2 class="text-lg font-semibold text-gray-200">Conversation History</h2>
                        <p class="text-xs text-gray-400 mt-0.5">Global input queries and multi-agent pipeline responses</p>
                    </div>
                    <div class="w-full sm:w-64">
                        <input id="search-conv" oninput="renderConversations()" type="text" placeholder="Search conversations..." class="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded text-sm text-gray-200 focus:outline-none focus:border-teal-500 font-sans" />
                    </div>
                </div>
                <div class="overflow-x-auto custom-scrollbar max-h-96">
                    <table class="min-w-full divide-y divide-gray-750">
                        <thead class="bg-gray-850">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-16">ID</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-40">Timestamp</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">Prompt</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">Result</th>
                            </tr>
                        </thead>
                        <tbody id="conv-tbody" class="divide-y divide-gray-750 bg-gray-800">
                            <tr>
                                <td colspan="4" class="px-6 py-4 text-center text-gray-500 text-sm">Loading conversations...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>

            <!-- Agent Execution Logs Table -->
            <section class="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden shadow">
                <div class="p-5 border-b border-gray-700 flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
                    <div>
                        <h2 class="text-lg font-semibold text-gray-200">Agent Execution Logs</h2>
                        <p class="text-xs text-gray-400 mt-0.5">Logs generated during agent task executions</p>
                    </div>
                    <div class="w-full sm:w-64">
                        <input id="search-logs" oninput="renderLogs()" type="text" placeholder="Search logs..." class="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded text-sm text-gray-200 focus:outline-none focus:border-teal-500 font-sans" />
                    </div>
                </div>
                <div class="overflow-x-auto custom-scrollbar max-h-[32rem]">
                    <table class="min-w-full divide-y divide-gray-750">
                        <thead class="bg-gray-850">
                            <tr>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-16">ID</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-40">Timestamp</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-32">Task ID</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-32">Agent</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">Prompt</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">Result / Error</th>
                                <th scope="col" class="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider w-24">Status</th>
                            </tr>
                        </thead>
                        <tbody id="logs-tbody" class="divide-y divide-gray-750 bg-gray-800">
                            <tr>
                                <td colspan="7" class="px-6 py-4 text-center text-gray-500 text-sm">Loading logs...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>
        </div>
    </main>

    <footer class="bg-gray-800 border-t border-gray-750 py-4 px-6 text-center text-xs text-gray-500 mt-8">
        &copy; 2026 Antigravity 2.0. Administrative Web Dashboard. Code-only execution mode.
    </footer>

    <script>
        function escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        async function updateStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();
                const agents = ['grok', 'claude', 'codex', 'tester'];
                agents.forEach(agent => {
                    const info = data[agent];
                    const connSpan = document.getElementById(`conn-${agent}`);
                    const actSpan = document.getElementById(`act-${agent}`);
                    if (info) {
                        if (info.online) {
                            connSpan.textContent = 'Online';
                            connSpan.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold bg-green-500/20 text-green-400 border border-green-500/30';
                        } else {
                            connSpan.textContent = 'Offline';
                            connSpan.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30';
                        }
                        
                        if (info.status === 'busy') {
                            actSpan.textContent = 'Busy';
                            actSpan.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold bg-yellow-500/20 text-yellow-400 border border-yellow-500/30';
                        } else {
                            actSpan.textContent = 'Idle';
                            actSpan.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-700/50 text-gray-400 border border-gray-600';
                        }
                    }
                });
            } catch (e) {
                console.error("Error updating status:", e);
            }
        }

        let allConversations = [];
        async function updateConversations() {
            try {
                const response = await fetch('/api/conversations');
                allConversations = await response.json();
                renderConversations();
            } catch (e) {
                console.error("Error updating conversations:", e);
            }
        }

        function renderConversations() {
            const searchTerm = document.getElementById('search-conv').value.toLowerCase();
            const filtered = allConversations.filter(c => 
                (c.prompt && c.prompt.toLowerCase().includes(searchTerm)) ||
                (c.result && c.result.toLowerCase().includes(searchTerm)) ||
                (c.timestamp && c.timestamp.toLowerCase().includes(searchTerm)) ||
                (c.id && String(c.id).includes(searchTerm))
            );
            
            const tbody = document.getElementById('conv-tbody');
            tbody.innerHTML = '';
            if (filtered.length === 0) {
                tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-4 text-center text-gray-500 text-sm">No conversations found</td></tr>`;
                return;
            }
            
            filtered.forEach(c => {
                const tr = document.createElement('tr');
                tr.className = 'border-b border-gray-750 hover:bg-gray-750/30 transition-colors';
                tr.innerHTML = `
                    <td class="px-6 py-4 whitespace-nowrap text-sm font-semibold text-gray-400">${c.id}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-400 font-mono">${escapeHtml(c.timestamp || '')}</td>
                    <td class="px-6 py-4 text-sm text-gray-200 font-mono whitespace-pre-wrap break-all max-w-xs md:max-w-md">${escapeHtml(c.prompt || '')}</td>
                    <td class="px-6 py-4 text-sm text-gray-300 font-mono whitespace-pre-wrap break-all max-w-xs md:max-w-md">${escapeHtml(c.result || '')}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        let allLogs = [];
        async function updateLogs() {
            try {
                const response = await fetch('/api/logs');
                allLogs = await response.json();
                renderLogs();
            } catch (e) {
                console.error("Error updating logs:", e);
            }
        }

        function renderLogs() {
            const searchTerm = document.getElementById('search-logs').value.toLowerCase();
            const filtered = allLogs.filter(l => 
                (l.agent_name && l.agent_name.toLowerCase().includes(searchTerm)) ||
                (l.task_id && l.task_id.toLowerCase().includes(searchTerm)) ||
                (l.prompt && l.prompt.toLowerCase().includes(searchTerm)) ||
                (l.result && l.result.toLowerCase().includes(searchTerm)) ||
                (l.error && l.error.toLowerCase().includes(searchTerm)) ||
                (l.status && l.status.toLowerCase().includes(searchTerm))
            );
            
            const tbody = document.getElementById('logs-tbody');
            tbody.innerHTML = '';
            if (filtered.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="px-6 py-4 text-center text-gray-500 text-sm">No logs found</td></tr>`;
                return;
            }
            
            filtered.forEach(l => {
                let statusBadge = '';
                const status = (l.status || '').toLowerCase();
                if (status === 'success') {
                    statusBadge = '<span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-green-500/20 text-green-400 border border-green-500/30">Success</span>';
                } else if (status === 'failure' || status === 'failed') {
                    statusBadge = '<span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30">Failure</span>';
                } else if (status === 'processing') {
                    statusBadge = '<span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-yellow-500/20 text-yellow-400 border border-yellow-500/30">Processing</span>';
                } else {
                    statusBadge = `<span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-blue-500/20 text-blue-400 border border-blue-500/30">${escapeHtml(l.status)}</span>`;
                }
                
                const displayResult = l.error ? `Error: ${l.error}` : (l.result || '');
                
                const tr = document.createElement('tr');
                tr.className = 'border-b border-gray-750 hover:bg-gray-750/30 transition-colors';
                tr.innerHTML = `
                    <td class="px-6 py-4 whitespace-nowrap text-sm font-semibold text-gray-400">${l.id}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-400 font-mono">${escapeHtml(l.timestamp || '')}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-400 font-mono break-all max-w-[8rem]">${escapeHtml(l.task_id || '')}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-teal-400 font-semibold">${escapeHtml(l.agent_name || '')}</td>
                    <td class="px-6 py-4 text-sm text-gray-200 font-mono whitespace-pre-wrap break-all max-w-[12rem]">${escapeHtml(l.prompt || '')}</td>
                    <td class="px-6 py-4 text-sm text-gray-300 font-mono whitespace-pre-wrap break-all max-w-[12rem]">${escapeHtml(displayResult || '')}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm">${statusBadge}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function refreshAll() {
            document.getElementById('last-updated').textContent = "Updating...";
            await Promise.all([updateStatus(), updateConversations(), updateLogs()]);
            const now = new Date();
            document.getElementById('last-updated').textContent = "Last updated: " + now.toLocaleTimeString();
        }

        refreshAll();
        setInterval(refreshAll, 5000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8080)

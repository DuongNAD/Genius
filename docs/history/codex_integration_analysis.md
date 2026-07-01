# Codex Desktop Integration Analysis Report

This report presents a technical analysis of the Codex Desktop application installed on the system and proposes feasible methods to integrate it with the Genius system for automated prompt execution and results extraction.

---

## 1. Application Inventory & Locations

*   **Main Application Launcher (UWP/MSIX Package)**:
    `C:\Program Files\WindowsApps\OpenAI.Codex_26.623.5546.0_x64__2p2nqsd0c76g0\app\Codex.exe`
*   **Daemon/CLI Executables**:
    *   System Path (packaged inside UWP app):
        `C:\Program Files\WindowsApps\OpenAI.Codex_26.623.5546.0_x64__2p2nqsd0c76g0\app\resources\codex.exe`
    *   User Path (automatically deployed user copy):
        `C:\Users\Admin\AppData\Local\OpenAI\Codex\bin\aec6b7c6fcdfb66a\codex.exe`
*   **App Configuration Directory**:
    `C:\Users\Admin\.codex`
    *   `config.toml`: Contains Named Pipe configurations, skill definitions, and local CLI binary paths.
    *   `auth.json`: Houses active ChatGPT session tokens, refresh tokens, and account information.
    *   `models_cache.json`: Stores system instruction templates and model metadata (including GPT-5.5 specs).
*   **App Logs Directory**:
    `C:\Users\Admin\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Codex\Logs\`

---

## 2. Integration Method 1: CLI Non-Interactive Execution (Highly Recommended)

The user copy of `codex.exe` supports a dedicated subcommand `exec` specifically designed to run prompts and agent instructions non-interactively. This is the simplest and most reliable integration path.

### Execution Blueprint
Genius can run `codex.exe` as a subprocess with the following arguments:

```powershell
$null | & "C:\Users\Admin\AppData\Local\OpenAI\Codex\bin\aec6b7c6fcdfb66a\codex.exe" exec "Your programming prompt here" --dangerously-bypass-approvals-and-sandbox --json
```

### Critical Implementation Details
1.  **Redirection of Stdin ($null | ...)**:
    By default, `codex.exe exec` checks if its standard input is piped. If it is, the CLI awaits an EOF signal to append standard input contents (like code or contextual texts) to the prompt. If stdin is left open or un-redirected, the process will hang. Redirecting stdin from `$null` (or closing the stdin pipe) prevents this hang and forces immediate execution.
2.  **Bypassing Sandboxing and Confirmations**:
    The `--dangerously-bypass-approvals-and-sandbox` flag is mandatory under automated environments to suppress user-facing confirmation dialogs (e.g., executing shell commands, modifying filesystem, etc.).
3.  **JSONL Event Stream Parsing (`--json`)**:
    Enabling the `--json` flag formats standard output into JSON Lines (JSONL). The event stream follows a clear lifecycle:
    *   `thread.started`: Emits a unique `thread_id` that can be reused for subsequent prompts.
    *   `turn.started`: Indicates processing of the prompt has started.
    *   `item.completed`: Sent when a message block finishes. The payload contains `item.type = "agent_message"`, where `item.text` holds the final answer/code block.
    *   `turn.completed`: Signals completion of the request and returns token usage statistics (`input_tokens`, `output_tokens`).

### Session Persistence (Resuming Threads)
If Genius needs to maintain a multi-turn conversation (e.g., debugging or building upon previous steps), the CLI supports:
```powershell
$null | & "codex.exe" exec resume --last "Follow-up instructions" --dangerously-bypass-approvals-and-sandbox --json
```
Alternatively, a specific thread ID can be targeted:
```powershell
$null | & "codex.exe" exec resume <thread_id> "Follow-up instructions" --dangerously-bypass-approvals-and-sandbox --json
```

---

## 3. Integration Method 2: Local WebSocket Server (JSON-RPC)

Codex Desktop contains an experimental background server daemon (`app-server`) that communicates over JSON-RPC. When activated, it listens on local port `12491`.

### Listening Daemon Startup
By default, the Electron application starts the daemon via stdio. To expose a loopback network port, Genius can start it manually:
```powershell
& "codex.exe" app-server --listen ws://127.0.0.1:12491
```

Once running, the daemon exposes the following endpoints:
*   `http://127.0.0.1:12491/healthz`: Returns `200 OK` (HTTP health check).
*   `http://127.0.0.1:12491/readyz`: Returns `200 OK` (HTTP readiness check).
*   `ws://127.0.0.1:12491/`: WebSocket upgrade endpoint for JSON-RPC communications.

### Protocol Details
TypeScript bindings generated from the CLI (`app-server generate-ts`) reveal the payload structures:

1.  **Initiating a Turn (`ClientRequest`)**:
    The client starts a turn by sending a JSON-RPC request message with method `"turn/start"` and a payload containing `TurnStartParams`:
    ```json
    {
      "jsonrpc": "2.0",
      "method": "turn/start",
      "id": 1,
      "params": {
        "threadId": "unique-thread-uuid",
        "input": [
          {
            "type": "text",
            "text": "Your programming prompt here",
            "text_elements": []
          }
        ],
        "cwd": "C:\\Workspace",
        "sandboxPolicy": "danger-full-access"
      }
    }
    ```
2.  **Handling Server Streaming & Responses (`ServerNotification`)**:
    The server streams updates using the following notification methods:
    *   `"item/agentMessage/delta"`: Contains chunks of the text/code response.
        ```json
        {
          "method": "item/agentMessage/delta",
          "params": {
            "threadId": "...",
            "turnId": "...",
            "itemId": "...",
            "delta": "def "
          }
        }
        ```
    *   `"turn/completed"`: Signals the end of the turn and contains the compiled final output.

---

## 4. Synthesis & Comparison

| Aspect | Method 1: CLI (`codex exec`) | Method 2: WebSocket (`app-server`) |
| :--- | :--- | :--- |
| **Complexity** | Very Low (single subprocess command) | High (requires WS client, event loop, handshake) |
| **Authentication** | Automatic (reads `auth.json` credentials) | Requires active loopback or manual tokens |
| **Streaming** | Supported via standard out JSONL lines | Supported via WebSocket frame messages |
| **Process Lifecycle** | Transient (processes exit when done) | Persistent (daemon runs continuously) |
| **Robustness** | High (handles credentials, startup, teardown) | Medium (requires daemon monitoring and restart) |
| **Recommendation** | **Primary / Production Choice** | **Secondary / Experimental Choice** |

## 5. Summary Recommendation

For the Genius integration:
1.  **Use Method 1 (CLI execution)** as the primary transport mechanism. It utilizes the logged-in credentials in `C:\Users\Admin\.codex\auth.json` flawlessly, works in non-interactive environments, and outputs structured JSONL events containing the token usage and code outputs directly.
2.  **Ensure stdin redirection** is configured (piping `$null` or closing standard input stream) to guarantee commands never hang.
3.  **Parse the stdout lines** for `item.completed` messages where `item.type` is `agent_message` to extract the generated solution.

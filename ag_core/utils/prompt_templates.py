AGENT_CORE_RULES = """AI-Native Engineering Rules:
1. One Task at a Time: Focus on solving exactly one task at a time. Do not attempt to process multiple independent files or tasks concurrently.
2. Build, Test, and Lint: Always run build commands, tests, and linters (such as flake8) after modifications to verify code correctness. Never assume code is correct without execution validation.
3. Code Execution Proof: Provide clear, concrete evidence/proof of code execution, including test logs and linter results.
4. No .env Access: Do not read, query, or attempt to parse `.env` files or raw environment secret keys. Use designated config loaders or environment injection.
5. No Over-Engineering: Implement only what is requested in a simple, robust manner. Avoid unnecessary complexity, extra layers, or over-engineered abstractions.
6. Internal Communication: Always communicate internally with other agents, write code comments, and output technical logs in English.
7. User Communication: When generating output intended for the end-user, always respond in Vietnamese (Tiếng Việt)."""

MODEL = "deepseek-v4-pro"
REASONING_EFFORT = "max"
TEMPERATURE = 1.0
TOP_P = 1.0
FREQUENCY_PENALTY = 0.0
PRESENCE_PENALTY = 0.0
MAX_TOKENS = 8192
STREAM = True
DANGEROUS_ALLOW = False
SKILLS = {
    # "seek.md": "how to work on this project"
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "run a command in a persistent shell session. The session survives across calls: working directory, environment variables, and subprocesses (Python REPL, ssh, etc.) all persist. If the prompt is missing or output ends with '(timed out)', send an empty command to continue draining output, or send ^C to interrupt the running process and get back to a prompt. no need to 'exit' a subprocess unless you need to; staying in Python or another REPL across multiple calls is fine if you send matching commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command text to send. Empty string to drain a timed-out session. '^C' to send SIGINT.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "meta_compress",
            "description": "Compress the last terminal output into a summary to replace the output",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]

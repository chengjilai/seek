MODEL = "deepseek-v4-flash"          #  "deepseek-v4-pro" "deepseek-v4-flash"
REASONING_EFFORT = None # None / "high" / "max"
TEMPERATURE = 1.0
TOP_P = 1.0
FREQUENCY_PENALTY = 0.0
PRESENCE_PENALTY = 0.0
MAX_TOKENS = 8192
STREAM = True
DANGEROUS_ALLOW = False
SKILLS = {
    # "/home/user/notes/troubleshooting.md": "排查常见服务器问题的注意事项",
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "向一个持久的 Bash 会话发送命令，并等待输出直到出现提示符或交互式提示。可用于 Python REPL等。超时后模型可发送空命令续读，或发送 ^C 中断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "发送到终端会话的文本。特殊值：空字符串继续等待；'^C' 发送 Ctrl+C 中断。"
                    }
                },
                "required": ["command"]
            }
        }
    },

{
        "type": "function",

        "function": {
            "name": "meta_compress",
            "description": "Compress the last terminal output into a summary to replace the output",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"]
            }
        }
    }
]

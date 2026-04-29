MODEL = "deepseek-v4-pro" #  "deepseek-v4-pro" or "deepseek-v4-flash"
REASONING_EFFORT = 'max' # None or "high" or "max"
TEMPERATURE = 1.0
TOP_P = 1.0
FREQUENCY_PENALTY = 0.0
PRESENCE_PENALTY = 0.0
MAX_TOKENS = 8192
STREAM = True
DANGEROUS_ALLOW = False
SKILLS = {
    "seek.md": "how to work on this project",
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "send command to a statefull shell session, get the output untill a new prompt. when timed out, send empty command to continue reading, or send ^C to interrupt",
           "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "text send to session. empty string to continue reading, '^C' to interrupt"
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
